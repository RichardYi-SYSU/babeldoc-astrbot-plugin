"""BabelDOC 翻译引擎封装。

BabelDOC 官方将自身定位为"供其他程序嵌入的 PDF 翻译引擎"，其稳定入口是
``babeldoc.format.pdf.high_level.async_translate``：一个异步生成器，逐段产出
进度事件（dict），最后以 ``{"type": "finish", "translate_result": TranslateResult}``
结尾。本模块把它包装成对 AstrBot 友好的纯异步接口：

- ``warmup()``：幂等的离线资源预热（布局模型、字体等），首次运行需下载数百 MB
- ``translate()``：翻译一篇 PDF，过程中回调进度事件，返回单语/双语 PDF 路径

BabelDOC 内部把重活放到线程/进程池执行，不会阻塞宿主的事件循环。
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from pathlib import Path
from typing import Awaitable, Callable


class TranslateError(RuntimeError):
    """翻译流程失败（区分于插件自身的参数/配置错误）。"""


@dataclasses.dataclass
class TranslateOptions:
    """一次翻译任务的参数快照，在入队时从插件配置生成。"""

    lang_in: str = "en"
    lang_out: str = "zh"
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    qps: int = 4
    # bilingual=仅双语对照 / mono=仅单语译文 / both=两者都要
    output_mode: str = "bilingual"
    # no_watermark / watermarked
    watermark: str = "no_watermark"
    auto_extract_glossary: bool = False


@dataclasses.dataclass
class TranslateOutcome:
    mono_pdf_path: Path | None = None
    dual_pdf_path: Path | None = None
    total_seconds: float = 0.0


# 进度事件与 async_translate 产出的事件结构一致：
# {"type": "progress_start|progress_update|progress_end", "stage": str,
#  "stage_progress": float, "overall_progress": float, ...}
ProgressCallback = Callable[[dict], Awaitable[None]]


class BabeldocEngine:
    """进程级共享的 BabelDOC 引擎：负责资源预热与逐篇翻译。"""

    def __init__(self) -> None:
        self._warmup_lock = asyncio.Lock()
        self._warmed = False

    @property
    def warmed(self) -> bool:
        return self._warmed

    async def warmup(self) -> None:
        """下载并校验 BabelDOC 所需的离线资源（幂等，成功后跳过）。"""
        async with self._warmup_lock:
            if self._warmed:
                return
            from babeldoc.assets.assets import warmup as babeldoc_warmup

            await asyncio.to_thread(babeldoc_warmup)
            self._warmed = True

    async def translate(
        self,
        input_path: Path,
        output_dir: Path,
        options: TranslateOptions,
        progress_cb: ProgressCallback | None = None,
    ) -> TranslateOutcome:
        """翻译单个 PDF 文件。

        Args:
            input_path: 输入 PDF 路径
            output_dir: 产物输出目录（会自动创建）
            options: 翻译参数
            progress_cb: 可选的进度回调，收到 async_translate 的原始事件

        Returns:
            TranslateOutcome，其中 mono/dual 路径为 None 表示未生成该产物

        Raises:
            TranslateError: BabelDOC 报告翻译失败
            ValueError: 配置缺失（如未填 API Key）
        """
        if not options.api_key:
            raise ValueError("未配置翻译 API Key，请在插件配置中填写 openai_api_key")

        # 延迟导入：AstrBot 启动加载插件时不应承担 BabelDOC 的导入开销
        from babeldoc.docvision.doclayout import DocLayoutModel
        from babeldoc.format.pdf.high_level import async_translate
        from babeldoc.format.pdf.translation_config import (
            TranslationConfig,
            WatermarkOutputMode,
        )
        from babeldoc.translator.translator import OpenAITranslator

        await self.warmup()
        output_dir.mkdir(parents=True, exist_ok=True)

        # 模型加载（onnx 会话初始化）是同步阻塞操作，放到线程里执行
        doc_layout_model = await asyncio.to_thread(DocLayoutModel.load_available)

        translator = OpenAITranslator(
            lang_in=options.lang_in,
            lang_out=options.lang_out,
            model=options.model,
            base_url=options.base_url or None,
            api_key=options.api_key,
        )
        config = TranslationConfig(
            translator=translator,
            input_file=str(input_path),
            lang_in=options.lang_in,
            lang_out=options.lang_out,
            doc_layout_model=doc_layout_model,
            output_dir=str(output_dir),
            no_mono=options.output_mode == "bilingual",
            no_dual=options.output_mode == "mono",
            watermark_output_mode=(
                WatermarkOutputMode.NoWatermark
                if options.watermark == "no_watermark"
                else WatermarkOutputMode.Watermarked
            ),
            qps=max(1, options.qps),
            auto_extract_glossary=options.auto_extract_glossary,
            save_auto_extracted_glossary=False,
            use_rich_pbar=False,
        )

        start = time.monotonic()
        result = None
        error_msg = ""

        # 注意：必须手动消费 async_translate，不能在收到 finish/error 后继续迭代。
        # 上游 AsyncCallback.finished_callback 先入队终止事件、后置 finished 标志
        # （中间有 sleep），消费者若在此窗口内再次 __anext__ 会在空队列上永久挂起，
        # 因此拿到终止事件立即 break，并用带超时的 aclose() 收尾。
        agen = async_translate(config)
        try:
            async for event in agen:
                event_type = event.get("type")
                if event_type == "error":
                    error_msg = str(event.get("error") or "BabelDOC 翻译失败")
                    break
                if event_type == "finish":
                    result = event.get("translate_result")
                    break
                if progress_cb is not None:
                    await progress_cb(event)
        finally:
            try:
                await asyncio.wait_for(agen.aclose(), timeout=10)
            except Exception:
                pass

        if error_msg:
            raise TranslateError(error_msg)
        if result is None:
            raise TranslateError("翻译流程未返回结果")

        return TranslateOutcome(
            mono_pdf_path=(
                Path(result.mono_pdf_path) if result.mono_pdf_path else None
            ),
            dual_pdf_path=(
                Path(result.dual_pdf_path) if result.dual_pdf_path else None
            ),
            total_seconds=time.monotonic() - start,
        )
