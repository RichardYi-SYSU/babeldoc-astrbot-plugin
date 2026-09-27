# astrbot_plugin_babeldoc

基于 [BabelDOC](https://github.com/funstory-ai/BabelDOC) 的 AstrBot 插件，实现保留格式的 PDF 论文翻译：

> 在 QQ 机器人中发送一份英文 PDF 论文，机器人自动返回精美排版的中英双语 PDF。

BabelDOC 负责全部底层工作（版面解析 → 公式/图表保护 → LLM 翻译 → 中英排版重建 → 字体内嵌），本插件将它嵌入 AstrBot 的异步消息流中。

## ✨ 功能

- **自动翻译**：私聊直接发送 PDF；群聊 @机器人 发送 PDF；或使用 `/pdf翻译` 指令
- **双语/单语输出**：可配置回传中英双语对照 PDF、纯中文译文 PDF 或两者
- **进度汇报**：翻译过程中按步长汇报进度，不刷屏
- **翻译队列**：多篇论文串行处理，自动排队
- **资源预热**：`/pdf翻译 warmup` 提前下载布局模型与字体（仅首次）
- **OpenAI 兼容**：任何 OpenAI 兼容 API（OpenAI / DeepSeek / 通义千问 / 本地 Ollama 等）均可作为翻译后端

## 📦 安装

1. 通过 AstrBot WebUI（插件市场 → 从 GitHub 安装）安装本插件，或手动克隆到 `data/plugins/astrbot_plugin_babeldoc/`
2. AstrBot 会自动安装 `requirements.txt` 中的依赖（核心依赖为 `babeldoc`，会连带安装 `pymupdf`、`onnxruntime` 等）
3. 在 WebUI 插件配置中填写 OpenAI 兼容 API 的 `base_url`、`api_key`、`model`
4. （可选）发送 `/pdf翻译 warmup` 预热资源，首次翻译会自动触发

## 🚀 使用

| 操作 | 说明 |
| --- | --- |
| 直接发送 PDF 文件 | 私聊自动翻译；群聊需同时 @机器人（可配置为全部翻译） |
| `/pdf翻译` | 查看使用帮助；若消息附带 PDF 则直接翻译 |
| `/pdf翻译 warmup` | 预热 BabelDOC 离线资源 |
| `/pdf翻译 status` | 查看引擎与队列状态 |

翻译完成后机器人会以文件消息回传 PDF（如 `论文标题_双语对照.pdf`）。

## ⚙️ 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `openai_base_url` | `https://api.openai.com/v1` | OpenAI 兼容 API 地址 |
| `openai_api_key` | 空 | API Key（必填） |
| `openai_model` | `gpt-4o-mini` | 翻译模型 |
| `lang_in` / `lang_out` | `en` / `zh` | 原文/译文语言 |
| `output_mode` | `bilingual` | 回传类型：`bilingual`/`mono`/`both` |
| `watermark_output_mode` | `no_watermark` | 是否保留 BabelDOC 水印 |
| `auto_translate` | `true` | 检测到 PDF 自动翻译 |
| `group_trigger_mode` | `at` | 群聊触发方式：`at`/`all`/`off` |
| `report_progress` / `progress_step` | `true` / `25` | 进度汇报开关与步长 |
| `qps` | `4` | 翻译 API 并发请求数 |
| `auto_extract_glossary` | `false` | 自动术语表（质量更高，token 消耗更大） |
| `max_file_size_mb` | `100` | 接受的最大 PDF 大小 |
| `keep_output_files` | `false` | 发送后是否保留生成的 PDF |

## 📌 注意事项

- **协议端要求**：QQ 文件消息依赖 NapCat / Lagrange 等协议端提供文件下载 URL（AstrBot 的 aiocqhttp 适配器会自动调用 `get_private_file_url` / `get_group_file_url` 解析）。群文件"上传到群相册"类型的通知事件不会触发翻译，请以文件消息形式发送。
- **文件回传**：机器人与协议端（NapCat/Lagrange）部署在同一台机器时，本地文件可直接投递；跨机器部署请在 AstrBot 配置中设置 `callback_api_base`（文件服务回调地址），AstrBot 会自动改用下载链接投递。
- **首次运行**：需要下载 DocLayout 版面模型与中日韩字体（数百 MB，仅一次），网络不畅时可先用 BabelDOC 的 `--generate-offline-assets` / `--restore-offline-assets` 在离线机器上恢复资源。
- **扫描版 PDF**：BabelDOC 会自动检测扫描件并报错（纯图 PDF 无法按文本翻译）。
- **磁盘占用**：默认发送成功后即删除临时文件；开启 `keep_output_files` 后产物保留在 `data/plugin_data/astrbot_plugin_babeldoc/jobs/`。

## 🧩 目录结构

```
astrbot_plugin_babeldoc/
├── main.py               # 插件入口：指令、文件监听、翻译队列 worker
├── core/
│   └── engine.py         # BabelDOC async_translate 异步封装
├── metadata.yaml
├── requirements.txt
├── _conf_schema.json
└── README.md
```

## 📄 许可证

本项目采用 **AGPL-3.0** 许可证（与 BabelDOC 一致）。BabelDOC 版权归 [funstory-ai](https://github.com/funstory-ai) 所有，感谢其开源的高质量 PDF 翻译引擎。
