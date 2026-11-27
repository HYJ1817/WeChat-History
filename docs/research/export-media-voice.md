# PDF/JSON/PNG 导出、图片恢复与语音转写接入评估

> 核验日期：2026-11-27  
> 范围：`reference/wechat-ai-memory`（只读 MIT 快照，commit `14d94db8b1affa12a2d7d88f0f830c1e0b8d752a`，tag `v0.3.8-alpha.2`）、本项目 `src/NativeApp`、`src/PythonWorker`、`scripts/`、`docs/` 与 `.venv` 中已安装包的元数据。  
> 方法：只读取上述文件与包元数据；未读取真实微信数据库、媒体、聊天内容或密钥，未联网，未执行 git 操作，未修改源码/配置/脚本/参考仓库。本文是可行性评估，不是实现记录，也不构成法律意见。

## 0. 结论摘要

### 0.1 可行性分层

| 能力 | 结论 | 主要原因 | 建议优先级 |
|---|---|---|---|
| JSON 导出 | **现在可行，风险低** | 上游 `export_json` 已在 vendored 代码中，当前 worker 只差协议命令、路径校验和 UI 入口；无新增依赖 | P1，先做 |
| PDF 导出 | **可行，中等工作量** | 上游渲染链完整，但需引入 Pillow/ReportLab、vendored 渲染/PDF/服务代码、系统字体和 PyInstaller 资源处理 | P2，第二步 |
| PNG 分页 | **可行，作为 PDF 的副产物** | 上游渲染时本就生成 PNG；`pages_dir` 可持久化，不需要独立渲染器 | P2，随 PDF |
| 已知文件图片渲染 | **可行，低到中等风险** | Pillow 的 EXIF 方向修正、缩略图和独立图片页均已有实现；输入必须是明确路径 | P2/P3，可选 |
| V2/WXGF 真实图片恢复 | **技术上可行，交付风险高** | 需要微信 4.x 附件目录、16 字节图片 AES 密钥、XOR 尾部密钥和 FFmpeg；密钥提取依赖运行中的微信进程/内存 | P4，后置 |
| 语音 SILK 解码 + 本地转写 | **技术上可行，交付风险高** | 需要 SILK 解码、faster-whisper、约 500 MB 模型、首次联网下载、长时间任务与缓存治理 | P4/P5，可选、显式同意 |
| 真实微信数据库读取 | **不建议放入本阶段** | 需要 Frida/进程内存/数据库密钥/版本适配，隐私与安全边界完全不同 | 最后，独立评审 |
| 可搜索 PDF / OCR | **当前不可行** | 上游 PDF 是纯图片，无文本层；没有 OCR 代码 | 不做，除非单独立项 |
| 全依赖 onefile 单文件 | **不建议** | FFmpeg、模型、ctranslate2/onnxruntime 等会使体积、启动和杀软误报显著恶化 | 用 onedir 或按功能拆分 |

### 0.2 推荐顺序

1. **JSON 导出**：最低成本，先固定格式、路径、错误码和测试。
2. **PDF + 可选 PNG**：只处理演示数据/文本，先把渲染、字体、打包和长任务边界做通。
3. **已知文件图片渲染**：在 PDF 链上用合成图片验证 EXIF、缩略图、图片页和 `_assets`。
4. **协议长任务能力**：进度事件、按命令超时、取消；没有这些，不建议把语音或大 PDF 放进现有 UI。
5. **语音模型与 SILK 解码**：先做显式模型准备/独立转写，再考虑与真实微信语音关联。
6. **真实微信数据库/图片/语音**：最后做，作为独立安全评审和独立开关，不默认启用。

### 0.3 工作量粗估

以熟悉 Python/C#/WinUI、但不含真实微信数据验证和法律审查为前提：

| 阶段 | 估算 | 说明 |
|---|---:|---|
| JSON 命令 + UI + 测试 | 0.5–1 人日 | 主要是适配器、路径校验、原子写入、协议和 UI |
| PDF/PNG（文本/系统消息） | 3–5 人日 | vendored 代码、依赖、字体、PyInstaller、测试；大历史性能另计 |
| 已知文件图片渲染 | +1–2 人日 | 合成图片资源、`_assets`、图片页、EXIF 测试 |
| V2/WXGF 图片恢复 | +5–10 人日，另加真实数据验证 | 解密、FFmpeg、密钥获取、临时文件生命周期、异常兼容 |
| 语音 SILK + faster-whisper | +4–7 人日，另加模型/长任务工作 | 依赖、模型缓存、进度/取消、下载同意、测试 |
| 真实微信 DB/密钥/语音关联 | +5–10 人日，风险不封顶 | 版本、schema、Frida、管理员权限、隐私评审 |
| 协议 v2 进度/取消（跨 C#/Python） | 2–4 人日 | 若不做，长任务只能靠加大超时，不建议 |

这些估算**不包含**真实微信数据的兼容性验证、法律审查、代码签名、安装器和干净机器验收。

---

## 1. 现有基线：为什么当前只能做 Markdown

### 1.1 Python worker

- `src/PythonWorker/worker.py:2-6` 明确 worker 只用标准库，唯一数据源是虚构演示 JSON，不接 Qt、微信数据库或网络。
- 协议版本为 1，请求上限 1 MiB；`worker.py:22-27`。
- vendored 路径只加入 `models.py` 和 `text_exporters.py`；`worker.py:32-43`。当前只导入 `export_markdown`。
- 命令表只有 `hello`、`list_conversations`、`get_messages`、`export_markdown`、`shutdown`；`worker.py:359-365`。
- `hello` 能力数组为 `demo`、`markdown_export`、`message_pagination`；`worker.py:238-244`。
- `_output_path` 只接受已存在的父目录和 `.md` 后缀；`worker.py:274-285`。
- 非文本消息在交给 vendored 导出前被转换成 `SYSTEM` 占位；`worker.py:288-301`。这是非常重要的现状：当前导出**不保留** image/file/voice 的类型语义，也不产生媒体链接。
- Markdown 先写同目录临时文件再 `os.replace`；`worker.py:304-338`。这是本项目已有、应推广到 PDF/JSON 的原子写入模式。
- worker 只在 stdout 写协议 JSON；`sys.stdout` 被重定向到 stderr，避免第三方库 `print()` 污染协议；`worker.py:447-450`。

### 1.2 协议与 C# 客户端

- `docs/protocol.md:19-25` 列出当前五个命令；`docs/protocol.md:13` 明确当前实现不发进度事件，UI 用不确定进度。
- 错误码仅覆盖当前 Markdown 场景；`docs/protocol.md:35`。
- 请求超时默认 30 秒、导出 120 秒；`docs/protocol.md:37`。
- `WorkerClient.RequestAsync` 一次只允许一个 outstanding 请求，超时会杀死自己启动的 worker；`src/NativeApp/Services/WorkerClient.cs:62-104`，默认 30 秒在 `:78`。
- 客户端已经能识别并忽略 `type:"progress"`，但不会把它交给 UI；`src/NativeApp/Services/Protocol.cs:27-28`，`WorkerClient.cs:117-123`。
- 当前 UI 只调用 `export_markdown`，超时硬编码 120 秒；`src/NativeApp/ViewModels/MainViewModel.cs:131-144`。
- 导出面板标题是“导出 Markdown”，并明确写着非文本只导出占位；`src/NativeApp/MainWindow.xaml:66-80`。

### 1.3 打包与当前发布物

- `scripts/Publish.ps1:22-24` 只安装 `src/PythonWorker/requirements-build.txt`，该文件当前只有 PyInstaller；`src/PythonWorker/requirements-build.txt:1-2`。
- PyInstaller 使用 `--onefile`、`--paths vendor`，未收集 Pillow/ReportLab/FFmpeg/whisper 的数据；`scripts/Publish.ps1:35-51`。
- 发布时复制 `THIRD_PARTY_NOTICES.md`、`README.md`、`LICENSES/` 和 `docs/`；`scripts/Publish.ps1:61-72`。
- smoke test 只验证 `hello → list_conversations → get_messages → shutdown`；`scripts/Publish.ps1:74-133`，没有 PDF/JSON 产物检查。
- 第一阶段报告记录 worker 约 8.0 MB，发布目录约 177.6 MiB；`docs/final-report.md:58-60`。
- 项目文档明确当前没有 PDF、JSON、PNG、图片和语音；`README.md:49-58`，`docs/final-report.md:65-72`，`docs/source-assessment.md:90-101`。

### 1.4 基线的直接含义

1. JSON 是唯一可以“零新依赖”接入的格式，但仍要处理当前媒体占位语义。
2. PDF/PNG 不能只复制 `pdf_exporter.py`；`service.py` 还依赖渲染、字体、Pillow、ReportLab 和 `ChatSource`。
3. 上游 `service.py:15` 通过 `from .sources import ChatSource` 导入，而 `sources/__init__.py:1-14` 会连带导入真实微信源。若直接 vendored `service.py`，会无意拉入 `frida`、`pycryptodome`、`zstandard` 等重依赖。必须提供最小 `sources` 包或修改该导入。
4. 当前协议没有进度、取消和长任务语义。PDF 大历史或语音转写不能直接套用 120 秒超时。

---

## 2. 上游导出实现的真实入口与链路

### 2.1 入口函数/类

| 入口 | 签名/位置 | 作用 |
|---|---|---|
| Markdown | `export_markdown(conversation, messages, output_path) -> Path`，`reference/wechat-ai-memory/src/wechat_context_exporter/text_exporters.py:11-36` | 生成按日期分节的 Markdown |
| JSON | `export_json(conversation, messages, output_path) -> Path`，同文件 `:39-68` | 生成 Version 1 交换 JSON |
| PDF | `PdfExporter.export(page_paths, output_path, title) -> Path`，`.../pdf_exporter.py:20-44` | 把已渲染 PNG 逐页画到 A4 PDF |
| 编排 | `ExportService.export(source, options, progress=None) -> ExportResult`，`.../service.py:61-144` | 查询、渲染、生成 PNG/PDF、伴随 Markdown/JSON、复制附件 |
| 选项 | `ExportOptions`，`.../service.py:21-31` | 会话、时间范围、PDF 路径、图片页、pages 目录、Markdown/JSON 路径、关键词 |
| 结果 | `ExportResult`，`.../service.py:34-43` | PDF、总页数、聊天页数、图片页数、消息数、各输出路径 |
| 聊天渲染 | `ChatRenderer.render(conversation, messages, start=None, end=None) -> list[RenderedChatPage]`，`.../rendering/chat.py:62-77` | 分页并绘制聊天页 |
| 图片页 | `ImagePageRenderer.render(conversation, message, attachment_number, attachment_count) -> Image.Image`，`.../rendering/image_page.py:28-68` | 为单张图片生成独立页 |
| 字体 | `FontBook.discover() -> FontBook`，`.../rendering/fonts.py:17-27` | 从系统字体中选择常规/粗体/emoji |
| JSON 读回 | `JsonChatSource(path)`，`.../sources/json_source.py:12-19` | 读取 Version 1 JSON，支持相对媒体路径 |

### 2.2 PDF/PNG 渲染链

1. **选择消息**：`ExportService.export` 先 `find_conversation` 和 `select_messages`；`service.py:67-69`。筛选可用 `query` 同时作用于发送者、内容和类型；`service.py:227-248`。
2. **渲染聊天页**：`ChatRenderer.render` 把消息切成日期块/消息块，按 `content_height` 分页；`chat.py:62-77`、`:79-152`。
3. **页面尺寸**：逻辑尺寸 1240×1754、300 DPI、缩放因子 2，因此实际 PNG 为 2480×3508；`rendering/config.py:6-29`，测试也断言了 2480×3508；`tests/test_export_service.py:57-62`。
4. **保存 PNG**：每张聊天页写入 `page_NNNN_chat.png`；`service.py:83-87`。
5. **图片独立页**：仅当 `include_image_pages` 且消息有 `image_path` 时，按聊天页内图片出现顺序逐张渲染；文件名为 `page_NNNN_image.png`；`service.py:88-100`。`RenderedChatPage.image_message_ids` 记录该页图片；`chat.py:17-21`、`:176-177`。
6. **生成 PDF**：`PdfExporter.export` 用 ReportLab A4 画布逐页 `drawImage`，`pageCompression=1`，`preserveAspectRatio=False`，无边距、整页铺满；`pdf_exporter.py:27-43`。
7. **关闭 ASCII85**：`rl_config.useA85=0`，避免图片流膨胀约 25%；`pdf_exporter.py:11-17`。
8. **伴随文件**：`ExportService` 在 PDF 之后写 Markdown/JSON，并把图片/文件/语音复制到 `<pdfstem>_assets`；`service.py:102-123`、`:159-194`。
9. **持久化 PNG**：若给了 `pages_dir`，先建目录，删除匹配 `page_[0-9][0-9][0-9][0-9]_*.png` 的旧文件，再复制本次页面；`service.py:125-132`。
10. **进度**：回调只报 1/5、2/5、3/5、5/5，没有 4/5；`service.py:70`、`:102`、`:104`、`:134`。当前 worker 没有接这个回调。
11. **性能/内存**：每张 2480×3508 RGB 页面仅像素就约 26 MB（2480×3508×3 字节），`ChatRenderer.render` 会先在内存中生成全部聊天页（`chat.py:62-77`），`ImagePageRenderer` 还会逐张生成图片页；页数多时内存会线性上升。上游实测记录为 2,439 条约 4.5 分钟、43,347 条约 43 分钟/1.6 GB，并给出 183 MB PDF 的例子；`reference/wechat-ai-memory/docs/UI_DESIGN_PROPOSAL.md:24-26`。这些数字来自上游环境，不能直接当作本项目的承诺，但足以说明需要页数/消息数上限、进度和流式/分批处理评估。

### 2.3 PDF/PNG 的字体和图片要求

- **字体**：`FontBook.discover` 依次查找 Windows `msyh.ttc`/`msyh.ttf`、Noto CJK、DejaVu、PingFang；没有常规字体就抛 `RuntimeError`；`fonts.py:17-27`、`:104-127`。粗体缺失时回退常规体；`fonts.py:20-27`。emoji 字体是可选的，候选包括 Windows `seguiemj.ttf`；`fonts.py:121-126`。
- **emoji 回退**：`draw_text_with_fallback` 按 emoji 区段切分，优先 `embedded_color=True`，失败后退回普通绘制；`fonts.py:50-80`、`:93-101`。这意味着 PDF 的 emoji 最终是位图，不是可选中文本。
- **字体不分发**：PDF 只嵌入页面位图，不嵌入字体；因此运行时需要系统字体，发布包不需要携带 `msyh`/`seguiemj`。反过来，在缺少这些字体的机器上渲染会失败或 emoji 显示异常。
- **图片读取**：聊天缩略图使用 Pillow 打开，`ImageOps.exif_transpose` 修正方向，`convert("RGB")`，LANCZOS 缩放到逻辑 720×390 上限；`chat.py:351-363`、`:84`、`:94`。图片页在可用框内按原始宽高比缩放；`image_page.py:81-96`。
- **缺失图片**：聊天页显示“Image unavailable”；`chat.py:279-280`、`:337-341`。图片页显示“Original image is unavailable”；`image_page.py:97-105`。注意：只有消息类型仍是 `IMAGE` 且 `image_path` 非空时才会进入图片页；真实微信源在无图时会把类型改成 `SYSTEM`（见 4.4）。
- **安全边界**：上游没有设置 Pillow 的 `Image.MAX_IMAGE_PIXELS`，也没有对恶意超大图片做专门限制；若未来允许用户选择任意图片目录，应自行加上限和异常处理。

### 2.4 输出结构与命名

以 PDF 为锚点，典型输出为：

- `<stem>.pdf`：PDF 本体。
- `<stem>.md` / `<stem>.json`：可选伴随文件；`service.py:106-123`。
- `<stem>_assets/`：可选附件目录；仅当有可复制的 image/file/voice 文件时创建；`service.py:163-193`。文件名为 `NNNN_安全化消息ID.ext`；`service.py:184-186`。
- `<pages_dir>/page_0001_chat.png`、`page_0002_image.png`……：可选持久化 PNG；`service.py:85-100`、`:125-132`。
- PDF 页序与 `page_paths` 一致；`page_count` 包含图片页；`chat_page_count` 只算聊天页；`image_page_count` 只算图片页；`service.py:135-144`。
- 聊天页页脚显示“Chat page X / Y”，其中 Y 是聊天页数，不含图片页；`chat.py:343-349`。图片页页脚固定显示原图比例与本地导出；`image_page.py:60-67`。

### 2.5 JSON 导出格式

`export_json` 的顶层结构是固定的 Version 1；`text_exporters.py:42-67`：

- 顶层：`version`（固定 1）、`conversations`（数组）。
- 会话：`id`、`name`、`kind`（`direct`/`group`）、`messages`。
- 消息：`id`、`sender`、`timestamp`（ISO 8601）、`type`、`content`、`is_outgoing`。
- 可选字段：`reply_to`（非空时）、`audio_path`（非空时）、`duration_ms`（非 `None` 时）、`transcript`（非空时）；`text_exporters.py:57-60`。
- 编码：UTF-8，`ensure_ascii=False`，缩进 2；`text_exporters.py:67`。
- 当前 `MessageType` 包含 `text/image/file/voice/system`；`models.py:14-19`。上游 `docs/json-format.md:56-66` 的字段表只列到 `system`，但代码实际接受 `voice` 及上述可选字段；文档与实现存在这一差异，接入时应以代码和实际测试为准。

伴随 JSON 的路径行为：

- `ExportService._materialize_attachments` 把可读取的 image/file/voice 文件复制到 `<pdfstem>_assets`，并把消息内容/`audio_path` 改为副本路径；`service.py:159-194`。
- `_relative_attachment_paths` 再把绝对路径改成相对于 Markdown/JSON 所在目录的 POSIX 相对路径；`service.py:197-219`。
- 源文件不存在时，`_materialize_attachments` 直接保留原消息；但 `_relative_attachment_paths` 仍会把绝对路径转成相对路径。因此 JSON/Markdown 可能出现指向不存在文件的相对链接；上游没有 `exists` 标记。接入时应在 UI/导出结果中明确这一点，或自行增加校验，但不要改变 Version 1 的既有字段语义。
- 语音的 `audio_path` 指向被复制的 SILK 文件，不是转写后的 WAV；转写文本在 `content`/`transcript`，`duration_ms` 保留时长；`service.py:170-193`，`voice.py:151-155`。

`JsonChatSource` 的校验边界：

- 必须是对象且 `version == 1`；`json_source.py:29-33`。
- 重复会话 ID/消息 ID 会被拒绝；`json_source.py:35-62`。
- 图片/文件相对路径以 JSON 文件所在目录解析；`json_source.py:72-73`。
- `audio_path` 支持相对/绝对；`duration_ms` 必须是非负整数；`is_outgoing` 必须是布尔；`reply_to` 必须是字符串或 null；`json_source.py:74-107`。
- 时间戳带时区时转换为本机时间并去掉时区；`json_source.py:129-137`。本项目演示加载器固定 UTC+08:00，二者语义不同，不能直接混用。

### 2.6 `paths.py` 与 `pipeline.py` 的边界

- `paths.py:19-32` 定义默认归档根 `C:/微信记忆`（Windows）或 `~/微信记忆`（POSIX），可用 `WECHAT_AI_MEMORY_OUTPUT_DIR` 覆盖；`paths.py:50-53` 生成默认导出路径。
- 本项目已有系统保存对话框和显式 `outputPath`，不应把 `paths.py` 的硬编码根目录带入 worker/UI；否则会绕过用户选择，并造成路径耦合。
- `pipeline.py` 提供 staging、原子 rename、manifest/state、投递等完整任务生命周期；`pipeline.py:74-120`、`:350-370`。它依赖 jobs/delivery 等本项目尚未迁移的模块，第一/第二阶段不需要引入。若需要原子性，建议在 worker 内做一个薄的“临时目录 → 校验 → `os.replace`”层，而不是搬整个 pipeline。

---

## 3. 依赖、体积、Python 3.11 与 PyInstaller

### 3.1 按功能划分的依赖矩阵

| 功能 | 必需依赖 | 不需要的依赖 | 说明 |
|---|---|---|---|
| JSON | 无新增依赖 | Pillow/ReportLab/FFmpeg/faster-whisper | 只用 `text_exporters.py` 和标准库 |
| PDF/PNG | `Pillow`、`reportlab` | FFmpeg/faster-whisper | 系统字体是运行前提 |
| 已知文件图片渲染 | `Pillow` | FFmpeg | 支持 Pillow 能解码的格式；WebP/JPEG/PNG 需实际测试 |
| V2/WXGF | `pycryptodome`、`imageio-ffmpeg`（WXGF） | faster-whisper | XOR 密钥可从样本推导；AES 密钥需用户提供或从进程提取 |
| 真实微信数据库 | `frida`、`pycryptodome`、`zstandard`、SQLite（标准库），上游还可能用 `pywinauto` | Pillow/ReportLab 不是数据库必需 | 高风险区域 |
| SILK 解码 | `silk-python`（导入名 `pysilk`） | — | 原生扩展，PyInstaller 需显式处理 |
| faster-whisper | `faster-whisper`，传递依赖 `ctranslate2`、`tokenizers`、`huggingface_hub`、`onnxruntime`、`numpy`、`av` 等 | — | 模型权重不在包内 |

### 3.2 版本、体积与 Python 3.11 兼容性

上游 `pyproject.toml:10` 要求 Python `>=3.11`；核心依赖范围在 `pyproject.toml:13-23`。本项目 `.venv` 是 CPython 3.11.15，以下包已在该环境安装，可作为 3.11 可用的实际证据（版本/体积来自 `.venv/Lib/site-packages` 与 `dist-info`）：

| 包 | 上游范围 | 已安装版本 | 安装目录约 | 许可证（元数据） |
|---|---|---:|---:|---|
| reportlab | `>=4.2,<5` | 4.5.1 | 9.0 MB | BSD（ReportLab 许可文本） |
| Pillow | `>=10.4,<12` | 11.3.0 | 16 MB | MIT-CMU |
| faster-whisper | `>=1.2,<2` | 1.2.1 | 1.5 MB | MIT |
| ctranslate2 | 传递依赖 | 4.9.1 | 67 MB | MIT |
| tokenizers | 传递依赖 | 0.23.3 | 7.9 MB | Apache-2.0（元数据分类器） |
| huggingface_hub | 传递依赖 | 2.5.0 | 7.7 MB | Apache-2.0 |
| onnxruntime | 传递依赖 | 1.32.0 | 54 MB | MIT |
| numpy | 传递依赖 | 2.4.6 | 37 MB | BSD-3-Clause 等多许可组合 |
| av | 传递依赖 | 18.1.1 | 4.8 MB | BSD-3-Clause |
| silk-python | `>=0.2.8,<0.3` | 0.2.9 | 1.1 MB | BSD（元数据；内含 SILK 解码绑定） |
| pycryptodome | `>=3.21,<4` | 3.24.1 | 8.1 MB | BSD + Public Domain |
| zstandard | `>=0.23,<1` | 0.25.0 | 1.5 MB | BSD-3-Clause |
| imageio-ffmpeg | `>=0.6,<1` | 0.8.1 | 101 MB | BSD-2-Clause；内含 FFmpeg 二进制 |
| frida | `>=17,<18` | 17.28.0 | 163 MB | wxWindows Library Licence 3.1 |

要点：

- **体积不是安装目录的简单相加**。PDF/PNG 只需 Pillow + ReportLab，未压缩约 25 MB；语音链的 ctranslate2 + onnxruntime + numpy + av + tokenizers + huggingface_hub + pysilk 已约 180 MB；WXGF 还要再带约 100 MB 的 FFmpeg 二进制。PyInstaller onefile 会有压缩和重复数据，最终体积必须实测。
- **Python 3.11 兼容性**：已安装的 `ctranslate2`、`pysilk` 等含 `cp311` 原生扩展；`reportlab`、Pillow、faster-whisper 在 3.11 可用。不能据此推断 3.12/3.13 或所有 wheel 组合都可用；本项目要求 3.11–3.13，应在目标 Python 版本上重建并测试。
- **Python 3.11 不是唯一风险**：`frida` 与微信版本、Windows 权限的耦合比 Python 版本更脆弱；上游 README 也明确微信升级后可能需要适配（`README.md:110-118`）。

### 3.3 PyInstaller 影响

- 上游 spec 显式 `collect_data_files("imageio_ffmpeg")` 和 `collect_data_files("faster_whisper")`；`packaging/windows/wechat_ai_memory.spec:12-19`，portable spec 同位置 `:12-19`。它**没有**把模型权重放入包内。
- 上游 spec 的 `hiddenimports=[]`，说明不能只依赖这个 spec 就认为语音/FFmpeg 一定能打包；`pysilk` 是在函数内动态导入（`voice.py:70-74`），PyInstaller 静态分析可能漏掉，需要显式 `--hidden-import pysilk`。
- `reportlab` 的 `_rl_accel`、Pillow 的图片插件、`ctranslate2`/`onnxruntime` 的 DLL 都需要 PyInstaller hook 或手工收集；应在目标构建中检查 `--onefile` 解包后的导入和实际导出，而不是只看构建成功。
- 当前 `Publish.ps1` 的 onefile 命令没有 `--collect-data`/`--hidden-import`；`scripts/Publish.ps1:38-46`。新增依赖后必须补构建参数或改用 spec。
- `--onefile` 每次启动解包到临时目录；上游 README 明确提示单文件首次启动会释放运行库并等待十几秒（`README.md:77-79`）。当前 worker 8 MB，加入 Pillow/ReportLab 后尚可接受；加入 FFmpeg/whisper 后不建议继续 onefile。
- 建议：
  - PDF/JSON 阶段：可继续 onefile，增加依赖和 smoke test。
  - 图片 WXGF 或语音阶段：优先 `--onedir`，或把可选功能拆成独立 worker/exe，让主 worker 保持轻量。
  - 模型权重永远不放进安装包；放在用户数据目录或独立模型包。

### 3.4 模型下载、来源与离线

- 默认模型名为 `small`，可用 `WECHAT_AI_MEMORY_WHISPER_MODEL` 覆盖；`voice.py:18-19`。
- 模型根目录为 `%LOCALAPPDATA%\WeChatAIMemory\models`（`app_data_dir()` + `models`）；`voice.py:22-24`、`:99`、`:130-136`。
- 首次使用会由 faster-whisper 从 Hugging Face 下载约 500 MB 的 `small` 模型；上游 README `README.md:116-120`，CHANGELOG `CHANGELOG.md:85-88`。
- 下载完成后可离线复用；上游没有显式设置“离线模式”或校验模型哈希，是否真正离线取决于本地缓存是否完整。若要保证离线，应预置模型目录并设置 Hugging Face 的离线环境变量或直接传本地模型路径；这需要实现和测试，不能只靠文档声明。
- 模型权重没有随仓库或 PyInstaller spec 分发；模型自身的许可证也必须在分发前单独核对。
- 首次下载是网络行为，与当前“应用与 worker 运行时不联网”（`README.md:147`）的声明冲突。必须做成显式用户同意、可取消、可见进度的独立步骤；不要在导出过程中静默下载。

---

## 4. 图片恢复：从“显示已知图片”到“解密微信原图”

### 4.1 两个层次

| 层次 | 输入 | 依赖 | 风险 | 建议 |
|---|---|---|---|---|
| 图片渲染 | 用户明确提供的 PNG/JPEG/WebP 等 | Pillow、字体 | 低 | 随 PDF 阶段做 |
| V2/WXGF 恢复 | 微信 4.x `msg/attach` 下的 `.dat`、AES 图片密钥、XOR 尾部密钥、FFmpeg | pycryptodome、imageio-ffmpeg、微信进程/附件目录 | 高 | 后置，独立开关 |

### 4.2 V2/WXGF 调用链

1. `WeChat4LocalSource.__init__` 选择账户、捕获数据库原始密钥、创建解密数据库缓存、图片缓存和语音缓存；`wechat4_source.py:36-58`。图片缓存拿到 `attachment_dir` 和可选的 `image_key`；`wechat4_source.py:52`。
2. 处理图片消息（类型 3）时，从消息的 packed info/内容提取图片标识，调用 `DecryptedImageCache.resolve`；成功则设为 `IMAGE` 和本地路径，失败则设为 `SYSTEM` 并写入 `[图片]`；`wechat4_source.py:246-254`。
3. `DecryptedImageCache.resolve` 要求 AES 密钥和 XOR 密钥都存在；首次调用时递归扫描附件目录的 `*.dat`，去掉 `_t`/`_h` 后缀并大小写归一化建索引；`wechat4_media.py:169-204`。
4. 对每个标识，按“无后缀原图 → `_h` → `_t` 缩略图”的顺序尝试，同变体按文件大小降序；`wechat4_media.py:225-236`。成功解密一个就返回。
5. `decrypt_v2_image` 校验 V2 magic、AES 段长度、XOR 段位置，AES-ECB 解密头部，校验 PKCS#7 padding，XOR 尾部；`wechat4_media.py:41-65`。
6. 解密结果若以 `wxgf` 开头，进入 `decode_wxgf_image`：解析 WXGF 分区，取最大 HEVC 分区，调用 FFmpeg 解码为 PNG；`wechat4_media.py:82-126`、`:129-151`。
7. 通过文件头识别 JPEG/PNG/GIF/WebP 扩展名；`wechat4_media.py:68-79`。解出的文件写入临时目录，并缓存路径；`wechat4_media.py:206-222`。`close()` 清理临时目录；`wechat4_media.py:192-195`。
8. 渲染层读取这些临时文件：聊天页生成 EXIF 修正后的缩略图（`chat.py:351-363`），图片页生成保持比例的独立图（`image_page.py:81-96`）。

XOR 密钥不是用户输入的：`derive_image_xor_key` 从最新的 `*_t.dat` 样本尾部两个字节反推，投票取众数；`wechat4_media.py:20-38`。因此它的前提是附件目录里存在可用的 V2 样本。

### 4.3 真实微信数据库/密钥依赖

- **数据库密钥**：`WeChat4LocalSource` 通过 `capture_account_key` 获取原始数据库密钥；`wechat4_source.py:45-50`。这个模块使用 Frida hook 微信进程的 SHA-512 相关代码来截取密钥；`wechat4_key_capture.py:17-100`。这意味着需要运行中的微信、可附加的进程权限，且对微信版本敏感。
- **图片 AES 密钥**：`extract_image_key` 先在附件目录找 V2 `_t.dat` 样本，再枚举 `Weixin.exe` 进程，读取进程内存中的配置/字符串候选，用样本的 16 字节密文验证候选；`wechat4_crypto.py:595-653`。没有运行中的微信会抛错；没有权限打开进程会提示以管理员运行；`wechat4_crypto.py:615-617`、`:651-652`。
- **图片密钥推导**：`derive_image_key(cfg_dword, 账户标识)` 用配置 DWORD 与账户标识做 MD5 并取前 16 字节；`wechat4_crypto.py:503-505`。上游源码中该函数由 `_image_key_from_config` 使用；`wechat4_crypto.py:585-590`。
- **数据库读取**：`WeChat4LocalSource` 用 `DecryptedDatabaseCache` 读取消息/联系人等数据库；`wechat4_crypto.py:172-208`。它复制数据库和 WAL 到临时目录后解密，`close()` 清理；`wechat4_crypto.py:210-234`。
- **账户发现**：`WeChat4Account` 约定 `db_storage` 和 `msg/attach` 路径；`wechat4_discovery.py:10-24`。账户发现扫描候选 `xwechat_files` 根；`wechat4_discovery.py:33-81`。这不是通用文件选择器，仍绑定微信目录结构。
- **图片缓存本身与数据库的关系**：`DecryptedImageCache` 只需要附件目录和密钥；但“消息 → 图片标识”的关联来自数据库。因此完整链路同时依赖数据库和附件目录；单独做“给一个目录解密图片”可以绕开数据库，但仍需 AES/XOR 密钥，且不在上游现成命令中。
- **版本范围**：上游只处理微信 4.x 的 V2/WXGF；没有旧版 3.x `.dat` 的通用支持。README 明确“微信版本升级后可能需要适配”（`README.md:110-118`）。

### 4.4 无媒体时的占位行为

- **真实微信图片无密钥/无文件**：源把消息类型改为 `SYSTEM`，内容为 `[图片]`；`wechat4_source.py:246-254`。这种消息不会触发图片独立页，因为 `image_path` 为 `None`，`service.py:77` 的 `image_messages` 不包含它。
- **已知图片路径但文件缺失/损坏**：若消息类型仍是 `IMAGE`，聊天页显示“Image unavailable”（`chat.py:279-280`、`:337-341`）；图片页显示“Original image is unavailable”（`image_page.py:97-105`）。这是 JSON/演示路径才会出现的状态，真实微信源通常会先降级成 `SYSTEM`。
- **缩略图缺失但原图存在**：`_load_thumbnail` 返回 `None` 时，聊天页用占位框；`image_path` 仍非空，图片页仍会尝试渲染原图。渲染器的缩略图和图片页是两条独立读取路径。
- **无 FFmpeg 的 WXGF**：`decode_wxgf_image` 抛“需要 bundled FFmpeg”；`wechat4_media.py:90-92`。`DecryptedImageCache._decrypt` 捕获异常并返回 `None`（`wechat4_media.py:206-215`），所以最终仍可能退化为 `[图片]`，而不是明确告诉用户“缺少 FFmpeg”。若接入，应把这种原因暴露为可区分的错误或诊断，而不是静默占位。
- **无媒体时不要伪造**：当前项目原则是“非文本只导出占位，不伪造链接”；`README.md:41-42`，`worker.py:288-301`。这一点应继续保持。

### 4.5 图片恢复的可行性与工作量

- 技术上，上游代码完整；合成测试覆盖 V2 解密、对齐 padding、WXGF 分区解析和 mock FFmpeg；`tests/test_wechat4_media.py:21-105`。这些测试不依赖真实微信数据。
- 真实上线的主要难点不是 AES 算法，而是：运行中的微信进程、管理员权限、版本差异、附件是否被清理、FFmpeg 二进制许可与打包、临时文件清理、错误提示。
- 建议：
  - P2/P3 只做“已知文件图片渲染”，不碰微信进程。
  - 真实 V2/WXGF 作为独立可选功能，所有密钥只在内存中使用，不写入磁盘、不经过协议、不写日志；默认关闭。
  - 如果做，先做“用户选择附件目录 + 用户提供密钥”的离线版本（仍需安全评审），再考虑进程内提取；不要默认扫描进程内存。

---

## 5. 语音：SILK 读取、faster-whisper 与缓存

### 5.1 SILK 读取链

1. 真实微信语音消息类型为 34；`wechat4_source.py:258-272`。源解析时长，并调用 `WeChatVoiceCache.resolve`。
2. `WeChatVoiceCache` 根据账户标识的 SHA-256 前 16 位建立 `%LOCALAPPDATA%\WeChatAIMemory\voice-audio\<account-hash>` 目录；`wechat4_voice.py:12-21`。
3. `resolve` 把消息数据库路径 `message_N.db` 映射为 `media_N.db`；`wechat4_voice.py:75-81`。然后通过解密数据库缓存读取 `Name2Id` 找到会话 ID，再按 `svr_id` 或 `local_id` 查询 `VoiceInfo.voice_data`；`wechat4_voice.py:30-58`。
4. 取到 SILK 字节后，用 SHA-256 命名 `.silk` 文件，原子替换写入缓存；`wechat4_voice.py:62-72`。找不到数据库、找不到行、空数据都返回 `None`。
5. 源在无音频时使用 `voice_placeholder(duration_ms, available=False)`，内容是“语音消息 · 音频暂不可读”一类占位；`voice.py:27-30`，`wechat4_source.py:258-272`。
6. 解码由 `decode_silk_to_wav(source, destination, sample_rate=24_000)` 完成：动态导入 `pysilk`，解码到内存临时文件，再写单声道 16-bit 24 kHz WAV；`voice.py:70-87`。
7. 转写由 `VoiceTranscriber.transcribe_messages(messages, progress=None, cancelled=None)` 完成；`voice.py:101-158`。它只处理 `VOICE` 且 `voice_path` 存在的消息；`voice.py:108-112`。

### 5.2 faster-whisper 配置

- 模型：默认 `small`，可用环境变量覆盖；`voice.py:18-19`。
- 设备/精度：`device="cpu"`、`compute_type="int8"`；`voice.py:131-136`。
- 转写参数：`language="zh"`、`beam_size=5`、`vad_filter=True`；`voice.py:145-150`。
- 结果：拼接各 segment 的文本，空结果替换为“语音内容未识别”；写入缓存并把消息的 `content`/`transcript` 替换为文本；`voice.py:151-155`。
- 模型加载点在“有未缓存语音”之后；如果全部命中缓存，不会导入 faster-whisper，也不会下载模型；`voice.py:113-128`。这对测试和离线缓存有利。

### 5.3 缓存位置与首次下载

| 数据 | 位置 | 生命周期 | 说明 |
|---|---|---|---|
| SILK 音频 | `%LOCALAPPDATA%\WeChatAIMemory\voice-audio\<account-hash>` | 持久，无 TTL/清理 | 按音频 SHA-256 命名；`wechat4_voice.py:20-21`、`:62-72` |
| 转写文本 | `%LOCALAPPDATA%\WeChatAIMemory\voice-transcripts\<audio-sha256>.json` | 持久，无 TTL/清理 | 内容含 transcript 与 model；`voice.py:41-67` |
| 模型 | `%LOCALAPPDATA%\WeChatAIMemory\models` | 持久 | faster-whisper 的 `download_root`；`voice.py:99`、`:130-136` |
| 临时 WAV | 系统临时目录下的 `wechat-memory-voice-*` | 本次调用结束清理 | `voice.py:137-157` |
| 临时数据库/图片 | 系统临时目录下的 `wce-*` | 数据源关闭时清理 | `wechat4_crypto.py:184`、`:232-234`；`wechat4_media.py:174`、`:192-195` |

- 首次使用 `small` 模型约需 500 MB 下载；来源为 Hugging Face；`README.md:116-120`，`CHANGELOG.md:85-88`。下载完成后可离线复用。
- 没有显式缓存清理、容量上限或过期策略；音频和转写文本会持续累积。对于大量语音，磁盘影响可能超过模型本身。
- 转写缓存键是“音频 SHA-256 + 模型名”；`voice.py:33-38`、`:45-54`、`:66-67`。换模型不会复用旧文本；同一音频重复转写会命中缓存。

### 5.4 隐私与磁盘影响

- **本地处理**：SILK 解码和 faster-whisper 推理都在本机；上游 README 明确聊天内容和语音不上传（`README.md:94-104`、`:120`）。但**模型下载是网络请求**，与“运行时不联网”的当前项目声明冲突。
- **持久化明文**：SILK 音频和转写 JSON 都是明文存放在 `%LOCALAPPDATA%`，没有加密；Windows 备份、同步盘、其他同用户进程都可能读取。转写文本还会进入 PDF/Markdown/JSON，成为新的敏感副本。
- **临时文件**：数据库、图片、WAV 在正常关闭时会清理，但进程被强杀时不保证清理；上游 README 说数据源关闭时删除（`README.md:104`），这是正常路径承诺，不是异常路径保证。
- **日志**：当前 worker 的 stdout 只走协议，stderr 由 C# 客户端排空但不显示/不记录（`docs/protocol.md:37`）。新增库可能向 stderr 打印警告；必须保持 stderr 不进入 UI/日志，且不要把转写文本、路径、密钥放进错误消息。
- **同意**：模型下载、真实微信进程读取、图片密钥提取都应显式同意；不要第一次导出就自动触发。

### 5.5 语音的可行性与工作量

- 纯 SILK → WAV：`pysilk` 自身很小（约 1.1 MB），合成测试可直接编码再解码；上游 `tests/test_voice.py:48-60`。
- 纯缓存转写：不加载模型也能测试；`tests/test_voice.py:19-41`。
- 真正转写：需要 faster-whisper 全套原生依赖和模型；首次约 500 MB、CPU 推理可能较慢，长音频/多消息会超过当前 30/120 秒超时。
- 真实微信语音：还依赖数据库密钥、`media_N.db`、`VoiceInfo`、微信版本和 SILK 数据完整性；上游 CHANGELOG 声称在真实微信 4.x 中验证过 126 条语音（`CHANGELOG.md:90-94`），但本次评估没有复验，也没有读取真实数据。
- 建议：先做“显式模型准备 + 独立 SILK 转写”的可选功能，配进度/取消和缓存管理；真实微信语音关联最后做。

---

## 6. 许可证与 MIT 复用边界

### 6.1 上游 MIT 代码的复用边界

- 上游 `LICENSE` 是 MIT，版权行为“Copyright (c) 2026 WeChat AI Memory contributors”；本项目已逐字保存在 `LICENSES/wechat-ai-memory-LICENSE.txt`，当前 vendored 文件头也保留了来源 commit 和 MIT 指引（`src/PythonWorker/vendor/wechat_context_exporter/text_exporters.py:1-5`）。
- MIT 允许复制、修改、合并、发布、再许可和销售，但要求在软件的所有副本或实质部分中保留版权声明和许可声明。若继续 vendored `pdf_exporter.py`、`rendering/*`、`service.py`、`voice.py`、`sources/wechat4_media.py` 等文件，必须：
  - 在文件头保留上游来源、commit、MIT 归属；
  - 在 `THIRD_PARTY_NOTICES.md` 增加文件清单；
  - 保持 `LICENSES/wechat-ai-memory-LICENSE.txt` 完整；
  - 不要把这些文件改成“无来源的自有代码”。
- 上游 `THIRD_PARTY_NOTICES.md:1-44` 还声明了两类参考代码：`tzwkb/wechat-decrypt`（MIT）和 `fanyuantaier/wechatauto-replica`（Apache License 2.0）。它们对应微信 4.x 数据库/图片兼容代码。当前本项目声明这些代码**没有** vendored（`THIRD_PARTY_NOTICES.md:99-101`）；一旦复制 `wechat4_crypto.py`/`wechat4_media.py`/`wechat4_source.py`，这个声明就不再成立，必须补上相应 MIT/Apache 通知和版权行。
- MIT 不提供任何担保；它不是安全审计，也不授权读取他人数据。许可证合规与隐私/权限合规是两件事。

### 6.2 新依赖的许可证（已核验）

以下来自 `.venv/Lib/site-packages/*.dist-info` 的元数据/许可证文件，核验日期为 2026-11-27：

| 组件 | 版本 | 许可证 | 分发提示 |
|---|---:|---|---|
| reportlab | 4.5.1 | BSD（ReportLab 许可文本） | 保留许可证和版权行；开源版即可满足当前 PDF 用法 |
| Pillow | 11.3.0 | MIT-CMU | 保留 LICENSE；注意 CMU 措辞 |
| faster-whisper | 1.2.1 | MIT | 保留 LICENSE |
| ctranslate2 | 4.9.1 | MIT | 含原生库；保留许可证 |
| tokenizers | 0.23.3 | Apache-2.0（元数据分类器） | 保留 NOTICE/LICENSE（若 wheel 未带，需从上游补） |
| huggingface_hub | 2.5.0 | Apache-2.0 | 保留 LICENSE |
| onnxruntime | 1.32.0 | MIT | 含原生库；保留许可证 |
| numpy | 2.4.6 | BSD-3-Clause 等多许可组合 | 保留 `LICENSE.txt` 及其子许可 |
| av | 18.1.1 | BSD-3-Clause | 含 FFmpeg 绑定；保留 LICENSE/AUTHORS |
| silk-python | 0.2.9 | BSD（元数据） | 保留版权/许可；其绑定上游 SILK 解码器的声明也需核对 |
| pycryptodome | 3.24.1 | BSD + Public Domain | 保留 LICENSE/AUTHORS |
| zstandard | 0.25.0 | BSD-3-Clause | 保留 LICENSE |
| imageio-ffmpeg | 0.8.1 | BSD-2-Clause | **还包含 FFmpeg 二进制，见下** |
| frida | 17.28.0 | wxWindows Library Licence 3.1 | 仅在真实微信源需要；保留 COPYING，分发前做法律复核 |
| PyInstaller | 6.22.3 | GPLv2-or-later + bootloader 例外 | 仅构建期；当前通知已覆盖 |
| CPython | 3.11.15 | PSF License 2 | PyInstaller 打包后随 worker 分发；保留 LICENSE |

### 6.3 FFmpeg、字体、模型的额外注意

- **FFmpeg 二进制**：`imageio_ffmpeg` 包内实际有 `ffmpeg-win-x86_64-v9.0.2.exe`，大小 105,423,872 字节（约 100.5 MiB）。包元数据只声明 `imageio-ffmpeg` 自身是 BSD-2-Clause；**没有在 dist-info 中看到该 FFmpeg 二进制的完整许可证/构建配置说明**。FFmpeg 可能是 LGPL-2.1+ 或 GPL（取决于编译选项），分发前必须核对二进制来源、configure 参数、许可证文本和源码提供义务。不能因为 Python 包是 BSD 就认为 FFmpeg 二进制也是 BSD。这是当前最明确的许可证阻断项之一。
- **字体**：上游 PDF 只使用系统字体做位图渲染，不分发 `msyh`/`seguiemj`。不要为了“跨机器一致”而把 Windows 字体复制进发布包；若确实要内置 CJK 字体，选择 OFL 的 Noto Sans CJK 并附许可证，同时接受体积增加。
- **模型权重**：`small` 模型不在仓库/安装包内；其许可证随模型卡/下载源而定，必须单独核对。转写文本的版权/隐私归属也应在产品条款中说明。
- **`silk-python`**：元数据为 BSD，但它绑定/包含 SILK 解码代码；如果发布包内含其原生扩展，应保留上游版权/许可声明。不要只复制 `pysilk` 的 Python 包装而丢掉底层声明。

### 6.4 当前通知/发布脚本的缺口

- 当前 `THIRD_PARTY_NOTICES.md` 只覆盖 Windows App SDK、CommunityToolkit、CPython、PyInstaller、上游 vendored 两个文件、.NET、WebView2、测试依赖；没有 reportlab/Pillow/faster-whisper/FFmpeg/SILK 等条目。
- `scripts/Publish.ps1:61-72` 只复制项目根的通知、README、`LICENSES/` 和 `docs/`；不会自动收集 `site-packages` 中各包的 LICENSE。新增依赖后，必须显式复制或生成第三方通知。
- 当前仓库没有项目级 `LICENSE`；`THIRD_PARTY_NOTICES.md:8-9` 和 `docs/source-assessment.md:229-230` 都指出这一点。正式分发前需要权利人确定项目整体许可证。
- `docs/source-assessment.md:207-211` 的“未 vendored 微信兼容代码”结论，在接入 `wechat4_*` 后会失效；必须同步更新，而不是只改代码。

---

## 7. 推荐接入方案（不改代码，仅协议/UI 草案）

### 7.1 分阶段方案

#### P1：JSON 导出（立即）

- 复用 vendored `export_json`（`text_exporters.py:44-73`）。
- 用与 Markdown 相同的“演示媒体 → `SYSTEM` 占位”适配器；这样 JSON 中非文本消息是 `type:"system"`，不是伪造的 image/file/voice 路径。必须在 UI/文档中说明。
- 新增 worker 命令 `export_json`，输出绝对 `.json` 路径；父目录必须存在；`overwrite` 必须显式布尔；先临时写入再 `os.replace`。
- 验证：`version:1`、字段齐全、可选字段按规则省略、UTF-8、空范围、路径冲突、重复 ID 拒绝、客户端超时/错误映射。
- UI：在现有导出面板增加“JSON”选项；保存对话框选 `.json`；结果显示消息数/字节数/路径。

#### P2：PDF + 可选 PNG（下一步）

- vendored `pdf_exporter.py`、`rendering/`、`service.py`，以及最小 `sources` 基础模块；不要复制完整 `sources/__init__.py`。
- 需要一个轻量 `DemoChatSource`（实现 `list_conversations`/`get_messages`）或直接构造 `ExportOptions` 所需的数据；不要为了 PDF 引入真实微信源。
- 给 `ChatRenderer` 提供明确的 `FontBook`；启动时探测字体，缺字体返回 `FONT_UNAVAILABLE`，不要等到 PDF 中途异常。
- 先只支持文本/系统消息；图片消息仍按现有占位处理，或使用合成图片验证。
- `includeImagePages` 默认 true；`pagesDir` 可选；默认临时目录，导出结束后清理；不要自动删除用户指定的旧 `page_*.png`，除非有清单/用户确认。
- UI：PDF 复选框、PNG 复选框（“保留分页 PNG”）、结果页数/聊天页/图片页/字节数；打开 PDF/文件夹。

#### P3：已知文件图片渲染（可选）

- 仅接受明确的图片路径（演示资源或用户选择），不扫描微信目录。
- 用 Pillow 处理 EXIF、缩略图、图片页；在 `_assets` 中复制图片。
- 增加合成 JPEG/PNG 资源作为测试夹具；不要使用真实聊天图片。
- UI 可显示“包含图片页”说明；无图片时明确“没有可渲染图片”。

#### P4/P5：真实微信媒体/语音（后置）

- 先完成协议长任务能力（进度、按命令超时、取消）。
- 真实微信源作为独立可选组件/独立 worker，默认关闭；需要管理员权限/WeChat 运行时，必须在 UI 前置说明。
- 图片：优先“用户选择附件目录 + 显式密钥”的离线模式；进程内存扫描只作为最后手段，不默认开启。
- 语音：先做模型准备和独立 SILK 转写；模型下载单独同意，不混在导出中；真实微信语音最后接。
- 任何密钥/账户标识都不进入协议、日志或导出文件。

### 7.2 协议草案（增量，保持 v1 兼容）

建议先保持 `protocolVersion: 1`，只新增命令、能力和进度事件；`Protocol.cs:27-28` 已能忽略进度事件，因此对旧客户端是兼容的。若将来要实现“处理中途取消”，需要 worker 并发读取控制命令，属于语义变化，再评估升到 v2。

#### hello 能力

在现有 `capabilities` 数组中增加字符串（旧客户端忽略未知项）：

- `json_export`
- `pdf_export`
- `png_pages`
- `image_render`（仅已知文件）
- `voice_transcribe`（仅当依赖和模型状态满足）
- `progress_events`

也可以增加一个结构化对象（旧客户端忽略），例如模型状态/字体状态；不要把真实路径或密钥放进去。

#### 新命令

| command | payload | result | 说明 |
|---|---|---|---|
| `export_json` | `{source:"demo", conversationId, startDate?, endDate?, outputPath, overwrite:false}` | `{path, messageCount, bytes, format:"json", version:1}` | 独立 JSON；与 Markdown 相同的占位语义 |
| `export_pdf` | `{source:"demo", conversationId, startDate?, endDate?, outputPath, overwrite:false, includeImagePages:true, pagesDir?, markdownPath?, jsonPath?}` | `{pdfPath, pageCount, chatPageCount, imagePageCount, messageCount, bytes, pagesDir?, markdownPath?, jsonPath?, assetsDir?}` | PDF 为主产物；Markdown/JSON/PNG 为可选伴随 |
| `export_pages`（可选） | `{source:"demo", conversationId, startDate?, endDate?, outputDir, overwrite:false}` | `{pagesDir, pageCount, chatPageCount, imagePageCount}` | 仅当确实需要“只导出 PNG”；否则 P2 不做 |
| `voice_model_status`（未来） | `{}` | `{installed, model, modelRoot, requiresDownload, sizeBytes?}` | 不联网、不加载模型，只查缓存 |
| `prepare_voice_model`（未来） | `{consent:true}` | `{ready, model, modelRoot, bytes}` | 显式同意后才联网下载；必须可取消/有进度 |
| `transcribe_messages`（未来） | `{source, conversationId, startDate?, endDate?, model?}` | `{transcribed, cached, total, model}` | 长任务；先只对演示/显式 SILK 文件 |
| `cancel_request`（未来） | `{targetRequestId}` | `{cancelled:true}` | 需要 worker 并发控制；不要在 v1 单线程 handler 中假装支持 |

约束：

- `outputPath` 必须是绝对路径；扩展名分别强制 `.json`、`.pdf`；父目录必须存在。上游 `ExportService` 自己会创建父目录（`service.py:25-26`、`:40-41`），但本项目现有规则是“父目录已存在”（`worker.py:274-285`），应保持后者，避免意外建目录。
- PDF、Markdown、JSON、pages 目录不能指向同一路径；上游已有校验（`service.py:151-156`），worker 还应校验扩展名和父目录。
- `overwrite` 必须显式布尔；默认不覆盖；客户端在系统对话框确认后才传 true。
- PDF 的伴随文件建议统一以 PDF 的 stem 命名：`<stem>.md`、`<stem>.json`、`<stem>_assets/`、`<stem>_pages/`。若用户显式给了不同路径，必须在结果中返回真实路径，并说明相对链接基准。
- 不要允许 `pagesDir` 指向任意已有目录后静默删除 `page_*.png`；上游 `service.py:129-130` 的无条件 glob 删除不适合作为产品行为。
- 真实微信源未来使用 `source:"wechat4"` 时，协议只传“选择哪个账户/会话”的抽象标识，不传数据库密钥、图片密钥或原始 SQL；这些由 worker 在受控边界内处理。

#### 进度事件

建议格式：

`{"protocolVersion":1,"requestId":"...","type":"progress","current":2,"total":5,"label":"Rendering chat pages"}`

- `label` 只能是固定英文/中文阶段名，不含发送者、聊天正文、文件路径或密钥。
- 进度必须单调、最终有终态；在最终 response 之前停止发送。
- 当前 C# 客户端会忽略 progress（`Protocol.cs:27-28`），因此可以先用“只发、不显示”的方式验证兼容性，再改 UI。
- 若暂时不做进度，必须为 PDF/语音设置足够长的按命令超时，并在 UI 明确“可能耗时”；但这不是好的长期方案。

#### 错误码建议

保留既有错误码（`docs/protocol.md:35`），新增：

| 错误码 | 含义 | retryable |
|---|---|---|
| `CAPABILITY_UNAVAILABLE` | 依赖/功能未打包或未启用 | false |
| `FONT_UNAVAILABLE` | 找不到可用常规字体 | false |
| `PAGE_LIMIT_EXCEEDED` | 页面/消息数超过安全上限（若设上限） | false |
| `IMAGE_KEY_UNAVAILABLE` | 图片密钥缺失/无法提取 | false |
| `WECHAT_NOT_RUNNING` | 需要运行中的微信 | true（用户启动后可重试） |
| `ACCESS_DENIED` | 无法读取进程/目录 | false/视情况 |
| `FFMPEG_UNAVAILABLE` | WXGF 解码器缺失 | false |
| `IMAGE_DECODE_FAILED` | V2/WXGF/图片解码失败 | false |
| `VOICE_AUDIO_NOT_FOUND` | 无 SILK 数据 | false |
| `VOICE_DECODE_FAILED` | SILK 解码失败 | false |
| `MODEL_NOT_READY` | 模型未下载/未就绪 | true |
| `MODEL_DOWNLOAD_FAILED` | 模型下载失败 | true |
| `TRANSCRIBE_FAILED` | 推理失败 | false |
| `CANCELLED` | 用户取消 | false |

错误消息不得包含真实聊天内容、发送者、密钥或完整路径；路径可以由结果字段返回，不要塞进异常文本。

### 7.3 UI 暴露建议

- 把现有“导出 Markdown”面板改为“导出”，保留 Markdown，增加 PDF/JSON/PNG 复选框；`MainWindow.xaml:66-80` 的说明文字需要同步更新，不能继续写“非文本只导出占位”而 UI 又提供 PDF/图片页。
- 主按钮按选择调用：
  - 仅 Markdown：现有 `export_markdown`。
  - 仅 JSON：`export_json`。
  - PDF（可带 Markdown/JSON/PNG）：`export_pdf`。
- 保存位置：
  - 主产物由系统保存对话框选择；伴随文件默认同目录同 stem，避免相对链接跨目录。
  - 结果区显示 PDF、Markdown、JSON、assets、pages 的真实路径；提供“打开文件/打开文件夹”。
- 结果摘要：消息数、总页数、聊天页、图片页、字节数；这些字段与 `ExportResult` 对齐（`service.py:34-43`）。
- 长任务：显示确定进度（页数/条数）和取消；如果暂时做不到，PDF 大范围导出应禁用或给出明确警告，不能只靠 120 秒超时。
- 图片/语音：
  - “包含图片页”是普通 PDF 选项。
  - “读取微信图片”必须是独立的高级操作，显示需要微信运行/管理员权限/版本风险；默认关闭。
  - “转写语音”独立按钮，显示模型是否已准备、模型路径/占用量；首次下载必须明确同意；不要让导出按钮隐式触发 500 MB 下载。
- 网络声明：只要保留模型下载，UI 的“无网络请求”文案必须改为“除首次模型下载外不联网”或提供预置模型后保持离线；当前 `README.md:147` 和 `MainWindow.xaml:80` 的绝对措辞需要调整。

### 7.4 超时、进度与取消

- JSON：可沿用 30 秒默认超时。
- PDF/PNG：按页数/消息数设置更长超时（例如 10–30 分钟），并配进度；不要在 120 秒时直接杀 worker，否则用户只看到“失败”，临时文件/进程状态也不可控。
- 语音模型下载：可能 30–60 分钟；必须独立命令、显式同意、进度和取消。
- 语音转写：按条数/音频时长设置超时；逐条上报进度；支持取消。
- 当前 `WorkerClient` 超时会杀进程（`WorkerClient.cs:84-90`），所以“更长的超时”只是临时缓解；真正需要的是长任务协议和可控取消。
- 取消的实现不能依赖 Python 单线程 `for` 循环里轮询 stdin；需要 handler 在可取消线程中运行，主循环同时读取控制命令，或使用独立控制通道。这部分工作量必须计入。

---

## 8. 测试方案

### 8.1 合成/极小样例（不需要真实微信，应进入 CI）

| 测试 | 样例 | 依赖 | 断言 |
|---|---|---|---|
| JSON 结构 | 2–3 条虚构文本/系统消息，空范围，Unicode | 标准库 | `version:1`、字段、可选字段省略、UTF-8、消息顺序 |
| JSON 路径/覆盖 | 临时目录、已存在文件、相对/绝对路径 | 标准库 | 父目录校验、`OUTPUT_EXISTS`、原子替换、失败不破坏旧文件 |
| PDF 文本页 | 1 条虚构文本消息 | Pillow/ReportLab、系统字体 | PDF 可打开；页数 ≥1；A4；无异常 |
| PDF 图片页 | Pillow 生成 64×64/900×500 纯色 PNG | Pillow/ReportLab | 聊天页 + 图片页页数；PNG 2480×3508；图片页存在 |
| 缺失图片 | `IMAGE` 消息指向不存在文件 | Pillow/ReportLab | 聊天占位 + 图片页占位；不崩溃 |
| EXIF 方向 | Pillow 生成带 orientation 的 JPEG（合成） | Pillow | 输出缩略图/图片页方向正确 |
| PNG 命名/页数 | 合成聊天 + 图片 | Pillow/ReportLab | `page_NNNN_chat.png`/`page_NNNN_image.png`；结果计数一致 |
| PDF 字体缺失 | mock `FontBook.discover` 抛错 | 无字体 | `FONT_UNAVAILABLE`，不生成半成品 |
| V2 解密 | 按上游测试构造合成 V2 容器 | pycryptodome | 解密字节、padding、XOR、扩展名 |
| WXGF 分区 | 合成 `wxgf` 头/分区 | 标准库 | 最大分区选择正确；mock FFmpeg 调用参数 |
| 无 FFmpeg | 不提供 ffmpeg、mock 查找失败 | 标准库 | 明确错误/降级，不崩溃 |
| SILK 解码 | `pysilk.encode` 合成 PCM | pysilk | 24 kHz、单声道、16-bit WAV；帧数正确 |
| 转写缓存 | 合成 SILK + 手写缓存 JSON | 无模型 | 不加载模型也能替换文本；缓存键/model 正确 |
| 语音占位 | 无音频/有时长 | 标准库 | 明确的“待转写/音频暂不可读”文案 |
| 协议 | 假 worker + 真实 stdio | C#/Python | 进度事件被忽略；新命令的 requestId/错误码/超时正确 |
| 打包 | onefile/onedir 构建 | PyInstaller | `hello`、JSON、PDF、字体/资源；stderr 无协议污染；体积/启动记录 |

注意：

- 上游的合成测试可作为参考：`tests/test_wechat4_media.py:21-105`、`tests/test_voice.py:19-60`、`tests/test_rendering.py:11-32`、`tests/test_export_service.py:15-73`。不要复制其中的真实/半真实数据；只复用构造方法。
- `tests/test_wechat4_source.py:40-197` 使用临时 SQLite 合成账户/媒体，是“无真实微信”测试的好范式；但其中的具体名称/内容不应进入产品文档或测试夹具。
- PDF 测试建议用 `pypdf` 只作为开发依赖（上游已把它放在 `dev` 可选依赖，`pyproject.toml:31-35`），不要打进发布包。
- 字体测试要区分“Windows 有 msyh”和“没有 msyh”；后者用 mock，不要为了 CI 复制 Windows 字体。

### 8.2 必须真实数据才能验证的项（手工、隔离环境）

- 真实微信 4.x 账户发现、数据库密钥捕获、数据库 schema 兼容。
- 真实图片 AES 密钥提取、V2 `_t.dat`/原图关联、WXGF HEVC 原图解码、附件被清理后的占位。
- 真实 `media_N.db`/`VoiceInfo` 关联、SILK 数据完整性、126 条语音之类的覆盖。
- 真实语音的转写准确率、中文/方言/噪声、模型大小与 CPU 时延。
- 真实大历史的 PDF 页数、耗时、内存、PDF 体积、分页 PNG 体积。
- 不同微信版本、Windows 版本、管理员/非管理员、企业安全软件。
- 干净机器上的字体可用性、PyInstaller 原生库加载、onefile 启动时间。

这些验证必须使用专用测试账户/机器，不把真实数据库、媒体、聊天文本、密钥提交到仓库或 CI；测试记录只保留脱敏指标（页数、耗时、字节数、错误码）。

### 8.3 验收标准（建议）

- JSON：可被 Version 1 解析器读回；字段/顺序/编码符合 `text_exporters.py:42-67`；占位语义在 UI 和文档中一致。
- PDF：在浏览器/Acrobat/pypdf 中可打开；页数与 `ExportResult` 一致；页面尺寸 A4；图片页不变形；缺失图片有占位。
- PNG：每页 2480×3508；命名/顺序与 PDF 一致；不误删用户文件。
- 字体：缺字体时明确失败；有字体时中文/emoji 不出现大面积方框。
- 图片：合成 V2/WXGF 测试通过；真实环境有明确错误/降级路径；临时目录清理。
- 语音：合成 SILK 解码正确；缓存命中不下载模型；首次下载有同意/进度/取消；无模型时明确 `MODEL_NOT_READY`。
- 协议：stdout 只有 JSON；错误消息不含正文/密钥/发送者；超时/断开后 worker 被回收；进度事件不会破坏旧客户端。
- 许可证：发布目录包含所有新增依赖的 LICENSE/NOTICE；FFmpeg 义务已核对；模型/字体未被误打包。

---

## 9. 明确不可行或不建议

1. **不做真实微信数据库/密钥接入，却声称支持图片/语音原图**：没有数据库密钥和附件目录，V2/WXGF 与 `VoiceInfo` 都无从关联；只靠文件后缀猜不出消息语义。
2. **不读取运行中的微信进程，却要求自动提取图片 AES 密钥**：`extract_image_key` 明确依赖 `Weixin.exe` 进程内存（`wechat4_crypto.py:595-653`）；没有密钥就无法解密 V2 头部。不要把它包装成“纯离线一键恢复”。
3. **没有 FFmpeg 就解 WXGF**：`decode_wxgf_image` 需要外部 FFmpeg（`wechat4_media.py:82-126`）；仓库没有内置 HEVC 解码器。
4. **把 PDF 说成可搜索/可复制文本**：`PdfExporter` 只画图片，没有文本层（`pdf_exporter.py:32-42`）；上游实测也明确“PDF 为纯图片、文本算子为 0”（`reference/wechat-ai-memory/docs/UI_DESIGN_PROPOSAL.md:26`）。要搜索必须另做 OCR/文本层，属于新项目。
5. **把 `ExportService` 原样搬进当前 worker**：`service.py:15` 会通过 `sources/__init__.py` 拉入真实微信源和重依赖；必须裁剪/改造。
6. **在现有 120 秒、无进度、无取消的协议里做语音或大 PDF**：会频繁触发 `REQUEST_TIMEOUT`，且客户端会杀进程（`WorkerClient.cs:84-90`）；用户无法知道进度，也无法安全取消。
7. **首次导出时静默下载 500 MB 模型**：与当前“运行时不联网”的声明冲突，且用户无法预期磁盘/时间；必须独立同意/准备步骤。
8. **把模型权重打进 onefile**：体积、启动解包、杀软误报和更新成本都不可接受；上游 spec 也没有打包权重（`packaging/windows/wechat_ai_memory.spec:12-19`）。
9. **在协议/日志/导出 JSON 中传数据库密钥、图片密钥或原始进程数据**：这是不可接受的安全边界；密钥只应在 worker 进程内短生命周期使用。
10. **默认扫描微信进程内存或自动重启微信**：上游 README 已提示 Frida 风险和“连接微信会重启微信并可能要求扫码”（`README.md:82`、`:84-92`）；这不应作为普通导出路径的默认行为。
11. **无条件删除 `pages_dir` 下的 `page_*.png`**：上游 `service.py:129-130` 会删除匹配的旧文件；如果用户把 `pages_dir` 指向已有目录，可能误删。产品实现必须限制目录/清单或要求确认。
12. **把 Windows 系统字体复制进发布包**：`msyh`/`seguiemj` 不是可自由再分发的项目资源；用系统字体或选择 OFL 字体。
13. **把 `imageio-ffmpeg` 的 BSD-2 当成 FFmpeg 二进制的许可证**：包元数据不等于二进制许可证；见 6.3。
14. **用真实聊天数据做 CI 夹具**：隐私和合规风险；只使用合成/极小样例，真实数据只在隔离环境手工验证。

---

## 10. 实现前必须核实的事项

1. 最小 `sources` 包的具体形状：只提供 `ChatSource`/`SourceError` 和演示适配器，避免 `sources/__init__.py` 的重导入。
2. 目标 Python 版本（3.11/3.12/3.13）下 Pillow、ReportLab、faster-whisper、ctranslate2、pysilk 的 wheel 可用性和 PyInstaller hook 行为；本文的 3.11 证据来自当前 `.venv`，不是全版本承诺。
3. `reportlab`/`Pillow`/`faster-whisper`/`silk-python`/`imageio-ffmpeg` 的实际 wheel 许可证文件是否随包带齐；本文表格来自已安装元数据，仍需法律复核。
4. FFmpeg 二进制的来源、版本、configure 参数、LGPL/GPL 归属、源码提供方式。
5. 模型权重的许可证、校验方式、离线预置方式；是否允许随产品分发。
6. Windows 目标机器是否总有 `msyh`/`seguiemj`；没有时的降级策略（失败还是内置 OFL 字体）。
7. PDF 大历史的页数/内存/耗时上限；是否需要硬性页数/消息数限制。
8. 真实微信 4.x 的版本范围、数据库 schema、附件清理策略、管理员权限要求；这些只能通过隔离环境的真实数据验证，不能在本次只读评估中下结论。
9. `JsonChatSource` 对 `voice` 字段的文档/实现差异是否要在本项目文档中补齐；不要擅自扩展 Version 1 字段。
10. 项目整体许可证和分发主体；当前仓库没有项目级 `LICENSE`，第三方通知不能替代。

---

## 11. 一句话建议

**先把 JSON 做成低成本、可测试的闭环；再用演示数据把 PDF/PNG 的渲染、字体、打包和长任务边界做稳；图片恢复和语音转写保留为显式、可选、后置的高风险能力，真实微信数据库/密钥接入不要进入当前阶段。**
