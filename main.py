"""astrbot_plugin_babeldoc —— 基于 BabelDOC 的保留排版 PDF 论文翻译插件。

工作流：
    用户发送英文 PDF（私聊直接发送 / 群聊 @机器人 发送，或用 /pdf翻译 指令）
    → 插件下载文件并入队
    → 后台 worker 调用 BabelDOC 引擎翻译（布局解析 + LLM 翻译 + 排版重建）
    → 将生成的中英双语 PDF 以文件消息回传到原会话
"""

import asyncio
import dataclasses
import shutil
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.engine import BabeldocEngine, TranslateOptions

PLUGIN_NAME = "astrbot_plugin_babeldoc"
COMMAND_NAMES = ("pdf翻译", "babeldoc")

USAGE = (
    "📖 BabelDOC 论文翻译\n"
    "• 直接发送 PDF 文件即可自动翻译（群聊请 @我 并附上文件）\n"
    "• /pdf翻译 warmup —— 预先下载翻译所需模型资源（首次约数百 MB）\n"
    "• /pdf翻译 status —— 查看队列与引擎状态\n"
    "翻译完成后会自动回传保留原始排版的中英双语 PDF。"
)

FILE_SIZE_LIMIT_HINT = "文件过大，已取消翻译"


@dataclasses.dataclass
class TranslationJob:
    """一条待翻译任务。入队时快照全部所需信息，与后续配置变更解耦。"""

    umo: str  # unified_msg_origin，用于在 worker 中主动回传结果
    sender_name: str
    source_file_name: str
    input_path: Path
    job_dir: Path
    options: TranslateOptions
    report_progress: bool
    progress_step: int


class BabeldocPlugin(Star):
    """插件主体：命令/监听器 + 单并发翻译队列。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.engine = BabeldocEngine()
        # 插件数据统一存放在 data/plugin_data 下，插件更新不会丢失
        self.jobs_dir = StarTools.get_data_dir(PLUGIN_NAME) / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[TranslationJob] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._busy = False
        # 命令与监听器可能同时命中同一条消息，用 message_id 去重
        self._handled_message_ids: set[str] = set()

    async def initialize(self):
        self._worker_task = asyncio.create_task(
            self._worker(), name="babeldoc-translation-worker"
        )

    async def terminate(self):
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        # 让 BabelDOC 释放其内部线程/进程池
        try:
            from babeldoc.const import close_process_pool

            await asyncio.to_thread(close_process_pool)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 指令：/pdf翻译 [warmup|status]
    # ------------------------------------------------------------------

    @filter.command("pdf翻译", alias={"babeldoc"})
    async def pdf_translate(self, event: AstrMessageEvent, subcommand: str = ""):
        """BabelDOC 论文翻译：发送 PDF 文件即可自动翻译。"""
        self._mark_handled(event)
        sub = (subcommand or "").strip().lower()

        if sub == "warmup":
            yield event.plain_result(
                "🔥 正在预热 BabelDOC 资源（模型与字体，首次需下载数百 MB）……"
            )
            try:
                await self.engine.warmup()
                yield event.plain_result("✅ BabelDOC 资源已就绪，可以直接发送 PDF 了。")
            except Exception as e:
                logger.error(f"[babeldoc] 预热失败: {e}")
                yield event.plain_result(f"❌ 预热失败：{e}")
            return

        if sub == "status":
            yield event.plain_result(self._status_text())
            return

        pdf_segs = self._find_pdf_segments(event)
        if pdf_segs:
            for seg in pdf_segs:
                async for msg in self._enqueue_pdf(event, seg):
                    yield msg
            return

        yield event.plain_result(USAGE)

    # ------------------------------------------------------------------
    # 监听器：自动识别消息中的 PDF 文件
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """自动翻译：检测到 PDF 文件消息即入队。"""
        if not bool(self.config.get("auto_translate", True)):
            return
        if event.message_obj.message_id in self._handled_message_ids:
            return
        # 带指令名的消息由上面的指令处理器负责，避免重复入队
        text = (event.message_str or "").strip()
        if any(name in text for name in COMMAND_NAMES):
            return

        group_id = event.get_group_id()
        if group_id:
            mode = str(self.config.get("group_trigger_mode", "at"))
            if mode == "off":
                return
            if mode == "at" and not self._is_at_bot(event):
                return

        pdf_segs = self._find_pdf_segments(event)
        if not pdf_segs:
            return

        for seg in pdf_segs:
            async for msg in self._enqueue_pdf(event, seg):
                yield msg
        # PDF 已被本插件接管，不再交给后续 LLM 等处理链
        event.stop_event()

    # ------------------------------------------------------------------
    # 入队与队列消费
    # ------------------------------------------------------------------

    async def _enqueue_pdf(self, event: AstrMessageEvent, seg: Comp.File):
        """下载文件、校验并放入翻译队列。以 async generator 形式逐条产出提示消息。"""
        source_name = (
            seg.name
            or Path(seg.file_ or "").name
            or Path((seg.url or "").split("?")[0]).name
            or "paper.pdf"
        )
        try:
            src = await seg.get_file()
        except Exception as e:
            logger.error(f"[babeldoc] 文件下载失败: {e}")
            yield event.plain_result(f"❌ 文件《{source_name}》下载失败：{e}")
            return
        if not src:
            yield event.plain_result(
                f"❌ 无法获取《{source_name}》的文件内容，"
                "请确认协议端（NapCat/Lagrange 等）支持文件消息下载。"
            )
            return

        src_path = Path(src)
        try:
            size_mb = src_path.stat().st_size / 1024 / 1024
        except OSError as e:
            yield event.plain_result(f"❌ 读取《{source_name}》失败：{e}")
            return
        max_mb = int(self.config.get("max_file_size_mb", 100))
        if size_mb > max_mb:
            yield event.plain_result(
                f"❌ {FILE_SIZE_LIMIT_HINT}：《{source_name}》为 {size_mb:.1f} MB，"
                f"超过限制 {max_mb} MB。"
            )
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        job_dir = self.jobs_dir / f"{stamp}_{Path(source_name).stem}"
        job_dir.mkdir(parents=True, exist_ok=True)
        input_path = job_dir / "input.pdf"
        shutil.move(src, input_path)

        job = TranslationJob(
            umo=event.unified_msg_origin,
            sender_name=event.get_sender_name(),
            source_file_name=source_name,
            input_path=input_path,
            job_dir=job_dir,
            options=TranslateOptions(
                lang_in=str(self.config.get("lang_in", "en")),
                lang_out=str(self.config.get("lang_out", "zh")),
                model=str(self.config.get("openai_model", "gpt-4o-mini")),
                base_url=str(self.config.get("openai_base_url", "")),
                api_key=str(self.config.get("openai_api_key", "")),
                qps=int(self.config.get("qps", 4)),
                output_mode=str(self.config.get("output_mode", "bilingual")),
                watermark=str(
                    self.config.get("watermark_output_mode", "no_watermark")
                ),
                auto_extract_glossary=bool(
                    self.config.get("auto_extract_glossary", False)
                ),
            ),
            report_progress=bool(self.config.get("report_progress", True)),
            progress_step=int(self.config.get("progress_step", 25)),
        )
        await self._queue.put(job)

        position = self._queue.qsize() + (1 if self._busy else 0)
        logger.info(
            f"[babeldoc] 已入队: {source_name} ({size_mb:.1f} MB) 来自 "
            f"{event.get_sender_name()}，队列长度 {position}"
        )
        yield event.plain_result(
            f"📥 已接收《{source_name}》({size_mb:.1f} MB)，"
            f"当前排队第 {position} 位，翻译完成后自动回传 PDF。"
        )

    async def _worker(self):
        """串行消费翻译队列。BabelDOC 单篇翻译资源占用高，这里保持单并发。"""
        while True:
            job = await self._queue.get()
            self._busy = True
            try:
                await self._process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"[babeldoc] 翻译任务失败: {e}")
                detail = str(e)[:300] or e.__class__.__name__
                await self._send_text(
                    job.umo, f"❌ 翻译《{job.source_file_name}》失败：{detail}"
                )
            finally:
                self._busy = False
                self._cleanup_job(job)
                self._queue.task_done()

    async def _process_job(self, job: TranslationJob):
        if not job.options.api_key:
            await self._send_text(
                job.umo,
                "❌ 尚未配置翻译 API Key，请在 WebUI 的插件配置中填写 openai_api_key 后重试。",
            )
            return

        if not self.engine.warmed:
            await self._send_text(
                job.umo,
                "🔥 首次运行，正在下载 BabelDOC 模型与字体资源（数百 MB，仅此一次），请稍候……",
            )

        output_dir = job.job_dir / "output"
        last_report = {"step": -1, "t": time.monotonic()}

        async def on_progress(event: dict):
            if not job.report_progress or job.progress_step <= 0:
                return
            overall = float(event.get("overall_progress") or 0.0)
            step = int(overall // job.progress_step)
            now = time.monotonic()
            # 按步长汇报，且两次汇报间隔至少 10 秒，避免刷屏
            if step > last_report["step"] and now - last_report["t"] >= 10:
                last_report.update(step=step, t=now)
                stage = str(event.get("stage") or "").strip()
                suffix = f"（{stage}）" if stage else ""
                await self._send_text(
                    job.umo, f"⏳ 《{job.source_file_name}》翻译进度 {overall:.0f}%{suffix}"
                )

        outcome = await self.engine.translate(
            job.input_path, output_dir, job.options, progress_cb=on_progress
        )

        chain = [
            Comp.Plain(
                f"✅ 《{job.source_file_name}》翻译完成，"
                f"用时 {outcome.total_seconds:.0f} 秒。"
            )
        ]
        targets: list[Path] = []

        if outcome.dual_pdf_path and job.options.output_mode in ("bilingual", "both"):
            targets.append(
                self._prepare_send_file(
                    outcome.dual_pdf_path, job.source_file_name, "双语对照"
                )
            )
        if outcome.mono_pdf_path and job.options.output_mode in ("mono", "both"):
            targets.append(
                self._prepare_send_file(
                    outcome.mono_pdf_path, job.source_file_name, "中文译文"
                )
            )

        if not targets:
            await self._send_text(job.umo, "❌ 翻译完成但没有生成任何 PDF，请查看机器人日志。")
            return

        for target in targets:
            chain.append(Comp.File(name=target.name, file=str(target)))
        try:
            await self.context.send_message(job.umo, MessageChain(chain=chain))
        except Exception as e:
            logger.exception(f"[babeldoc] 发送翻译结果失败: {e}")
            await self._send_text(job.umo, f"❌ 翻译完成但回传失败：{e}")

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------

    def _mark_handled(self, event: AstrMessageEvent):
        if len(self._handled_message_ids) > 500:
            self._handled_message_ids.clear()
        self._handled_message_ids.add(event.message_obj.message_id)

    @staticmethod
    def _is_at_bot(event: AstrMessageEvent) -> bool:
        self_id = str(event.message_obj.self_id)
        return any(
            isinstance(seg, Comp.At) and str(seg.qq) == self_id
            for seg in event.message_obj.message
        )

    @staticmethod
    def _find_pdf_segments(event: AstrMessageEvent) -> list[Comp.File]:
        segs: list[Comp.File] = []
        for seg in event.message_obj.message:
            if not isinstance(seg, Comp.File):
                continue
            candidates = (
                seg.name or "",
                Path(seg.file_ or "").name,
                (seg.url or "").split("?")[0],
            )
            if any(str(c).lower().endswith(".pdf") for c in candidates if c):
                segs.append(seg)
        return segs

    @staticmethod
    def _prepare_send_file(result_path: Path, source_name: str, label: str) -> Path:
        """把产物复制成对用户友好的文件名，返回待发送路径。"""
        stem = Path(source_name).stem
        stem = "".join(c for c in stem if c not in '\\/:*?"<>|').strip() or "paper"
        target = result_path.with_name(f"{stem}_{label}.pdf")
        if target != result_path:
            shutil.copy2(result_path, target)
        return target

    def _cleanup_job(self, job: TranslationJob):
        """任务结束（无论成败）后清理其工作目录；另清扫历史残留。"""
        if bool(self.config.get("keep_output_files", False)):
            return
        shutil.rmtree(job.job_dir, ignore_errors=True)
        # 顺带清理 24 小时前因异常退出等原因遗留的任务目录
        now = time.time()
        for d in self.jobs_dir.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > 86400:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

    def _status_text(self) -> str:
        try:
            from babeldoc import __version__ as babeldoc_version
        except Exception:
            babeldoc_version = "未安装"
        waiting = self._queue.qsize() + (1 if self._busy else 0)
        warmed = "已就绪" if self.engine.warmed else "未预热（首次翻译时自动下载资源）"
        return (
            "📊 BabelDOC 翻译状态\n"
            f"• BabelDOC 版本：{babeldoc_version}\n"
            f"• 引擎资源：{warmed}\n"
            f"• 待处理任务：{waiting}（含进行中 1 个）\n"
            f"• 当前模型：{self.config.get('openai_model', 'gpt-4o-mini')}"
        )

    async def _send_text(self, umo: str, text: str):
        try:
            await self.context.send_message(umo, MessageChain(chain=[Comp.Plain(text)]))
        except Exception as e:
            logger.error(f"[babeldoc] 向会话发送消息失败: {e}")
