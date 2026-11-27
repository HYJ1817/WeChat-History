# 上游项目结构评估与迁移边界

> **阶段更新（2026-11-27 晚些时候）**：本文件第 1 节的“第一阶段没有迁移真实微信
> 数据源/PDF/语音/任务”等描述是第一阶段的历史记录。第二阶段已经补齐真实微信 4.x
> 只读适配、Markdown/JSON/PDF/PNG、图片恢复、本地语音、场景/任务/投递与新的 WinUI
> 界面；当前范围、验证与限制以 [final-report.md](final-report.md)、
> [protocol.md](protocol.md) 和根目录 `README.md` 为准。本文第 2 节以后的上游结构、
> 许可证与安全评估仍然有效。

> 核验日期：2026-11-27
> 范围：`reference/wechat-ai-memory` 只读快照、`src/NativeApp`、`src/PythonWorker`、
> `scripts/`、`tests/`、`demo/` 与 `reference/DeskBox` 的许可证文件。
> 本文件记录**实际文件与代码**能支持的事实；未验证的内容会明确标注。

## 1. 结论摘要

- 上游 `wechat-ai-memory` 是一个 Python 3.11+、PySide6/Qt 的 Windows 桌面应用，
  直接读取 Windows 微信 4.x 数据库，支持 PDF/Markdown/JSON、图片恢复、语音转写、
  任务记录、场景与投递。许可证为 **MIT**。
- 本仓库第一阶段**没有**迁移 Qt UI、真实微信数据源、PDF/渲染、语音、任务/投递
  框架或 CLI。它实现了一个新的 C#/WinUI 3 外壳、一个 JSON Lines 受控 worker、
  演示数据筛选和 Markdown 导出。
- 只 vendored 上游的两个文件：`models.py` 与 `text_exporters.py`。两文件代码主体
  与上游一致，文件头新增了来源/MIT 归属注释；完整 MIT 文本见
  `LICENSES/wechat-ai-memory-LICENSE.txt`。
- DeskBox 的 `LICENSE` 实际为 **GPLv3** 文本，`LICENSING.md` 声明
  **GPL-3.0-only + 商业许可**双轨。该许可证与 MIT 不兼容；本仓库**未复用其代码**，
  仅参考组织方式，因此没有把 DeskBox 许可证加入 `LICENSES/`。
- 第一阶段的安全边界是设计边界而非正式安全审计：不读真实数据库/密钥/配置、
  应用运行时不联网、不遥测；构建期下载 SDK/NuGet/pip 属于开发准备，不是运行时行为。

## 2. 上游快照与核验方法

| 项目 | 实际值 |
|---|---|
| 上游仓库 | `https://github.com/HYJ1817/wechat-ai-memory` |
| 本地快照 | `reference/wechat-ai-memory`（只读，带 `.git`） |
| HEAD commit | `14d94db8b1affa12a2d7d88f0f830c1e0b8d752a` |
| 最新 tag（指向 HEAD） | `v0.3.8-alpha.2` |
| commit 日期 | 2026-11-16 23:42:23 +0800 |
| 上游版本 | `pyproject.toml` 中 `version = "0.3.8"` |
| 许可证 | MIT，`Copyright (c) 2026 WeChat AI Memory contributors` |
| 工作树状态 | `git status --porcelain` 为空（快照干净） |

用户任务中给出的 commit 与本地 HEAD **一致**，无需改用其他 commit。

核验方法：

1. 读取上游 `LICENSE`、`README.md`、`pyproject.toml` 与模块文件；
2. 用 `grep` 找出导入 `PySide6` 的模块，区分 Qt UI 与非 UI 代码；
3. 读取 `service.py`、`pipeline.py`、`sources/*`、`rendering/*`、`jobs/*`、
   `delivery/*`、`scenes/*`、`voice.py`、`pdf_exporter.py` 的头部与导入关系；
4. 对 vendored 文件与上游同路径文件做规范化逐行比较（见第 3 节）；
5. 读取本项目源码，确认 worker 实际导入、协议命令、日志字段与网络/数据库引用；
6. 读取 NuGet 包、Python `dist-info` 与 lock/metadata，核实第三方版本与许可证；
7. 读取 `artifacts/` 中已有的测试/发布产物；实现阶段随后在本机重跑，结果见第 7 节。

## 3. 上游结构评估

### 3.1 Qt/PySide6 UI（未迁移）

上游的 Qt 界面位于：

- `src/wechat_context_exporter/ui/`：`main_window.py`、`dashboard_window.py`、
  `fluent.py`、`glass_effects.py`、`glass_widgets.py`、`launch.py`、
  `modern_styles.py`、`native_theme.py`、`scene_delivery.py`；其中至少 8 个文件
  直接导入 `PySide6`。
- 入口：`app.py`、`app_modern.py`、`launch_modern.py`、`start*.cmd/vbs`。
- UI 相关测试：`test_modern_ui.py`、`tests/test_ui.py`、`tests/test_fluent.py`、
  `tests/test_dashboard_interaction.py`、`tests/test_glass_dashboard.py`。
- 资源：`src/wechat_context_exporter/assets/` 与 `docs/` 中的品牌/截图。

**迁移状态：未迁移。** 本仓库用新的 C#/WinUI 3 + XAML + MVVM 实现原生外壳；
没有复制 Qt 文件、样式、图像或测试。上游 Qt 依赖 `PySide6>=6.7,<6.12`
（`pyproject.toml` 的可选依赖 `gui`），本项目不需要 Qt。

### 3.2 数据模型与文本导出（已 vendored）

| 上游文件 | 本仓库位置 | 用途 | 状态 |
|---|---|---|---|
| `src/wechat_context_exporter/models.py` | `src/PythonWorker/vendor/wechat_context_exporter/models.py` | `Conversation`、`Message`、枚举等数据模型 | vendored |
| `src/wechat_context_exporter/text_exporters.py` | `src/PythonWorker/vendor/wechat_context_exporter/text_exporters.py` | `export_markdown`、`export_json` | vendored |
| `src/wechat_context_exporter/__init__.py` | `src/PythonWorker/vendor/wechat_context_exporter/__init__.py` | 上游的 `__init__` 会导入 `service.py`（进而牵出渲染/PDF 依赖）；本项目只保留最小说明，不导入这些模块 | 重写为最小文件 |

**逐行比较结果：**

- 去掉 vendored 文件新增的来源/归属注释与空行后，`models.py` 的 52 行有效代码与
  上游逐行相同；`text_exporters.py` 的 62 行有效代码与上游逐行相同。
- 因此准确说法是“**代码主体逐字一致，文件头新增 MIT 来源注释**”，而不是整个文件
  字节级完全一致。
- `__init__.py` 不是上游副本；它是刻意最小化的空壳，避免把 `ExportService`、
  PDF/渲染/Qt 时代的依赖带进 worker。
- worker 直接 `from wechat_context_exporter.models import ...` 与
  `from wechat_context_exporter.text_exporters import export_markdown`，绕过上游
  包级 `__init__` 的副作用。

### 3.3 导出编排、PDF、渲染、语音（未迁移）

| 上游区域 | 代表文件 | 作用/依赖 | 迁移状态 |
|---|---|---|---|
| 导出服务 | `service.py` | 组合 source、Markdown/JSON/PDF、渲染与进度回调 | 未迁移 |
| PDF | `pdf_exporter.py` | ReportLab | 未迁移 |
| 渲染 | `rendering/chat.py`、`config.py`、`fonts.py`、`image_page.py`、`scaled_draw.py` | Pillow/ReportLab/字体/图片页 | 未迁移 |
| 语音 | `voice.py`、`sources/wechat4_voice.py` | faster-whisper、SILK、缓存 | 未迁移 |
| 路径/CLI | `paths.py`、`cli.py`、`__main__.py` | CLI 与输出根目录 | 未迁移 |

本阶段只调用 vendored 的 `export_markdown`；`export_json` 虽然随文件保留，但协议
没有暴露 JSON 导出。PDF、图片页、语音转写和进度回调都不存在。

### 3.4 真实微信数据源（未迁移）

上游 `sources/` 同时包含两类来源：

- **JSON 来源**：`sources/json_source.py`，读取版本 1 的 JSON 交换格式；
- **真实微信 4.x 来源**：`sources/wechat4_crypto.py`、`wechat4_discovery.py`、
  `wechat4_key_capture.py`、`wechat4_media.py`、`wechat4_source.py`、
  `wechat4_voice.py`，以及 `sources/base.py`。这些模块涉及 `frida`、
  `pycryptodome`、`zstandard`、SQLite、进程/账户发现、密钥捕获、媒体恢复等。

**迁移状态：未迁移。** 本仓库没有这些文件、没有相应依赖、没有微信路径扫描、
没有 SQLite/解密/密钥逻辑。worker 只接受 `source: "demo"`；其他来源报
`SOURCE_NOT_FOUND`。

上游 `sources/json_source.py` **也没有被 vendored**。本项目的 `worker.py` 自行
实现了最小演示加载器，与上游 JSON 来源的差异包括：

- 上游接受无时区时间戳并按本机本地时间处理；本项目要求时间戳带时区，并统一
  归一化为 UTC+08:00；
- 上游支持 `audio_path`、`duration_ms`、`transcript`、`reply_to`、`is_outgoing`
  等字段并将其用于媒体/语音；本项目演示加载器只校验 `id`、`sender`、
  `timestamp`、`type`、`content`，不解析媒体；
- 上游 JSON 来源可配合 service/pipeline 做 PDF/JSON/场景投递；本项目只做
  Markdown 导出；
- 因此不能把本项目描述为“完整支持上游 JSON 格式”，它只支持演示文件所需的子集。

### 3.5 任务、投递、场景与打包（未迁移）

- `pipeline.py`：一次导出的任务生命周期（staging、atomic rename、manifest、
  state、delivery）。
- `jobs/`：任务记录、manifest/state、哈希。
- `delivery/`：文件夹、Obsidian、剪贴板、Agent 投递。
- `scenes/`：群聊总结、项目复盘、待办、需求变更等场景与提示词。
- `packaging/windows/`、`scripts/`、`start*.cmd`：上游 PyInstaller/Windows 打包与
  启动脚本。
- `docs/`、`README.md`、品牌资产：上游文档与视觉资产。

**迁移状态：均未迁移。** 本仓库没有场景、提示词、任务记录、投递或上游打包脚本。

### 3.6 上游测试（未复制）

上游 `tests/` 包含 CLI、交付、导出服务、worker、Fluent/玻璃 UI、任务、JSON、
路径、渲染、场景、语音、微信 4.x crypto/discovery/media/source 等测试。
本仓库**没有复制**这些测试；现有测试是新的：

- `tests/PythonWorker.Tests/test_worker.py`：演示 worker 的行为/协议/导出测试；
- `tests/NativeApp.Tests/`：协议、日期范围、进程管道测试；
- `tests/NativeApp.Tests/fixtures/fake_worker.py`：C# 测试用的假 worker；
- `scripts/Test-NativeFlow.ps1`：真实 WinUI 窗口的 UI 自动化端到端脚本。

## 4. 本项目的替代实现

### 4.1 原生 UI

`src/NativeApp/MainWindow.xaml` 与 `MainViewModel.cs` 实现：

- 三个演示会话列表、日期选择、应用/清除筛选；
- 50 条/页的消息分页与“加载更多”；
- 系统 `FileSavePicker` 导出、打开文件/文件夹；
- 重新连接、错误 InfoBar、不确定进度、连接状态；
- 窄窗口下把导出面板移到下方。

这些是新的 WinUI 3 代码，不是上游 Qt UI 的逐控件翻译。

### 4.2 Worker 协议与演示适配

`src/PythonWorker/worker.py` 是新的受控子进程：

- 标准库 + vendored 模型/导出函数；
- 只接受版本 1 请求；stdout 只写协议 JSON，诊断走 stderr；
- `hello`、`list_conversations`、`get_messages`、`export_markdown`、`shutdown`；
- 演示加载、UTC+08:00 包含式日期筛选、稳定排序、分页、原子 Markdown 写入；
- 非文本演示消息在交给 vendored `export_markdown` 前转换为显式 system 占位，
  不生成假的媒体链接或假转写；
- 错误码、1 MiB 请求上限、重复 requestId 拒绝等见 `docs/protocol.md`。

### 4.3 C# 进程边界

`WorkerClient.cs` 与 `WorkerLocator.cs` 是新的 C# 实现：

- 一次只允许一个 outstanding 请求；
- stdout/stderr 异步排空；
- 只接受匹配 requestId 与协议 v1 的最终响应，忽略 `progress`；
- 超时/协议错误/进程退出使连接失效；
- 关闭时只回收自己启动的 worker 进程本身，不按进程名清理其他 Python；worker 启动的微信不在终止范围内；
- worker 查找仅限发布目录 `worker\` 或项目标记，不搜索用户/微信目录。

## 5. 许可证边界

### 5.1 wechat-ai-memory（MIT）

上游 `LICENSE` 为 MIT，版权行：

```text
Copyright (c) 2026 WeChat AI Memory contributors
```

MIT 要求在任何副本或实质部分中保留版权声明与许可声明。本仓库通过以下方式满足：

- vendored 文件头保留来源 commit、MIT 归属与 `LICENSES/` 指引；
- `LICENSES/wechat-ai-memory-LICENSE.txt` 为上游 `LICENSE` 的逐字副本
  （使用 `cp` 复制，未改写或删除版权行）；
- `THIRD_PARTY_NOTICES.md` 再次列出来源、commit、文件与许可证。

上游 `THIRD_PARTY_NOTICES.md` 提到 `tzwkb/wechat-decrypt`（MIT）与
`fanyuantaier/wechatauto-replica`（Apache-2.0），但它们对应的是上游的微信 4.x
数据库/图片兼容代码；这些代码**没有**被 vendored。本仓库 vendored 的两个文件只
导入 Python 标准库与彼此，不包含上述第三方代码，因此当前不需要额外复制这两份
通知。

### 5.2 DeskBox（GPL-3.0-only + 商业许可；未复用代码）

- `reference/DeskBox/LICENSE` 实际内容是 **GNU General Public License v3** 全文。
- `reference/DeskBox/LICENSING.md` 明确：从 DeskBox 1.6.0 起采用
  **`GPL-3.0-only` + Commercial License** 双轨；开放源码轨道要求衍生作品按
  GPL-3.0-only 发布完整对应源码并保留声明。
- GPL-3.0-only 的 copyleft 义务与 MIT 项目的宽松再分发不兼容；若把 DeskBox
  代码并入本项目，不能继续按 MIT 方式分发而不履行 GPL 义务。
- 本仓库**未复用其代码**：没有 DeskBox 源码文件、资源、catalog 或模板副本；
  在 `src/`、`tests/`、`scripts/`、`docs/` 中未发现 DeskBox 命名空间/标识引用。
  DeskBox 仅用于项目组织方式的参考。
- 因此 `LICENSES/` **不加入** DeskBox 许可证。若未来要复用其代码/资产，必须先
  重新做许可证评估：要么整体满足 GPL-3.0-only，要么取得其商业许可。

### 5.3 本项目自身许可证

截至核验日期，仓库根目录**没有**发现独立的项目级 `LICENSE` 文件。上述第三方
许可证不能替代项目自身的整体许可证；正式分发前需要由权利人明确项目许可证。

## 6. 安全与隐私边界

### 6.1 已实施的设计边界

- **不读真实数据库/密钥/配置**：`src/` 中没有 `sqlite`、`frida`、微信路径、
  `db_storage`、密钥捕获或数据库解密代码；worker 只打开显式传入的演示 JSON。
- **不扫描用户目录**：`WorkerLocator` 只查看应用目录和项目标记；不搜索用户配置、
  微信安装目录或聊天记录。
- **运行时不联网**：应用与 worker 没有 HTTP 客户端、socket、远程 API、遥测或
  上传代码；通信是本机 stdin/stdout 的 JSON Lines。构建脚本会访问 SDK/NuGet/pip
  官方源，这是构建期行为，不是应用遥测。
- **日志最小化**：`AppLog` 只接受固定事件名与安全错误码；不写路径、发送者、
  消息正文、stderr、异常消息或 payload。日志按 UTC 日期分文件，超过 1 MiB 时
  清空重写。
- **stderr 不转发**：`WorkerClient` 排空 stderr 但不显示、不记录文本，只触发
  一个诊断计数事件。
- **导出原子性**：worker 先在同目录写临时 `.md.tmp`，成功后 `os.replace`；失败
  不破坏已有文件；`overwrite` 必须显式布尔值，默认不覆盖。
- **日期确定性**：演示数据固定 UTC+08:00，不随机器时区变化；起止日期都包含。
- **演示数据虚构**：`demo/conversations.json` 由 `scripts/generate_demo.py`
  生成，所有内容为虚构；不要用真实聊天替换。

### 6.2 未覆盖/未验证

- 这不是第三方安全审计、渗透测试或形式化验证。
- 未对恶意/超大/畸形演示 JSON 做系统 fuzz；现有测试覆盖部分错误路径。
- 未验证真实微信环境（本阶段**不接入**）、所有 Windows 版本、干净机器、
  安装器、代码签名、自动更新、长时间运行或并发多实例。
- 未验证 PyInstaller onefile worker 在所有杀毒软件/企业策略下的行为。

## 7. 验证记录与未验证事项

### 已核实

- 上游 HEAD 为 `14d94db8b1affa12a2d7d88f0f830c1e0b8d752a`，tag
  `v0.3.8-alpha.2`，工作树干净。
- 上游 `LICENSE` 为 MIT，版权行保留；已逐字复制到
  `LICENSES/wechat-ai-memory-LICENSE.txt`。
- `models.py` 与 `text_exporters.py` 的有效代码与上游 HEAD 逐行一致（去除新增
  文件头注释/空行后比较）。
- DeskBox `LICENSE` 为 GPLv3 全文；`LICENSING.md` 为 GPL-3.0-only + 商业双轨。
- 本项目 `src/` 中没有网络客户端、SQLite、frida、微信路径或数据库/密钥代码；
  worker 只导入标准库与 vendored 模块。
- NuGet/Python 包版本与许可证来自 `WeChat-History.csproj`、`project.assets.json`、
  `.tools/packages/*.nuspec`、`.venv/.../dist-info/METADATA` 与包内许可证文件。
- `artifacts/test-results/native-tests.trx` 记录第一阶段当时的 22/22 通过（本轮已扩充到 42/42）；
  `artifacts/native-flow/report.json`（Debug 版）、
  `artifacts/native-flow-publish/report.json`（发布版）与
  `artifacts/publish/publish-report.json` 均记录成功。

### 未验证

- 本评估编写时没有重跑测试；实现阶段随后在本机重跑了 Python/C# 测试、Debug 与
  发布版 UI 自动化以及发布 smoke test（结果见 `docs/final-report.md`）。
- 未在干净 Windows 机器上复验环境准备/构建/发布；未验证 Windows 10 所有版本
  与 Windows 11 的所有配置。
- 未验证真实微信数据（本阶段有意不接入）、安装器、代码签名、自动更新、
  长时间稳定性、并发多实例、无障碍/高 DPI 全矩阵、非中文系统。
- 未做许可证法律审查；`THIRD_PARTY_NOTICES.md` 是工程记录，不构成法律意见。

## 8. 第一阶段迁移结论

**已迁移**：

- 上游数据模型（vendored）；
- 上游 Markdown 导出函数（vendored，`export_json` 保留但未暴露）；
- 以新的 C#/WinUI 3 外壳、JSON Lines worker、演示数据、日期筛选、分页和
  原子 Markdown 导出验证端到端链路。

**明确未迁移**：

- Qt/PySide6 UI 与全部 UI 资产/测试；
- 微信 4.x 数据发现、密钥、解密、媒体、语音；
- PDF、渲染、图片页、JSON/PNG 导出；
- 任务/投递/场景/CLI；
- 上游真实数据源与业务编排。

因此，本仓库应被描述为“**第一阶段原生演示版**”，而不是“wechat-ai-memory 的
完整 Windows 重写”。任何超出演示数据的隐私、兼容性或功能声明都缺少本阶段
代码与测试的支持。
