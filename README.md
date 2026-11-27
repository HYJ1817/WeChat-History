# 微信记忆 · Windows 原生版

> **直接使用**：双击 `微信记忆` 快捷方式，或打开 `微信记忆App` 文件夹双击 `NativeApp.exe`。完整项目目录中的开发缓存和测试产物默认隐藏；需要查看时在资源管理器打开“显示隐藏的项目”。分发时不要只复制 `NativeApp.exe`，要复制整个 `微信记忆App` 文件夹。

**把本机微信 4.x 的聊天记录，只读地整理成本地 Markdown / JSON / PDF，并支持场景、任务记录与文件夹 / Obsidian 投递。**

桌面外壳是 C# + WinUI 3 + XAML + Windows App SDK + MVVM；Python 只作为受控的本地子进程，负责发现微信账户、读取/解密数据库、导出与本地语音转写。没有 Electron、WebView、Qt 或浏览器 UI，没有遥测、云同步或远程 AI API。

本项目是 MIT 许可的微信记忆上游项目的 Windows 原生迁移。上游的 Qt 界面没有复用；数据模型、微信 4.x 读取、PDF 渲染、语音、任务/场景/投递模块以 vendored 方式复用，并保留上游 MIT 版权与许可证文本（见 `LICENSES/`）。参考仓库 DeskBox 只用于组织方式参考，**未复制其代码**（GPL-3.0-only 与 MIT 不兼容）。

## 1. 现在能做什么

| 区域 | 当前实现 |
|---|---|
| 原生界面 | 三栏 WinUI 3 界面：数据源与账户、会话列表、消息预览、筛选、右侧“导出 / 任务 / 场景 / 语音”页签；窄窗口自动把右侧页签移到底部 |
| 安装 | Release 提供未签名的当前用户安装包（安装到 `%LOCALAPPDATA%\Programs\WeChatMemory`），也可使用便携文件夹 |
| 数据源 | **本机真实微信 4.x 只读数据**；启动后自动检测账户，选择账户并确认后才连接。发布版不包含演示数据 |
| 账户发现 | 在本机用户目录与磁盘根目录的受限范围内发现微信 4.x 数据目录；连接前显示最近使用时间，连接后显示本机微信昵称；不显示账户 ID 或绝对路径 |
| 真实连接 | Frida 在内存中捕获数据库密钥；密钥只在 worker 内存中使用，不写磁盘、日志、协议或命令行。连接会关闭并重启微信一次，并尝试自动点击登录。原始数据库只读，不修改 |
| 会话与消息 | 会话列表、会话名称筛选、消息分页（每页 50，最多 100）、关键词搜索（发送者/内容/类型相关文本）、UTC+08:00 的包含式日期筛选 |
| Markdown | 导出当前会话与已应用筛选；文本、图片占位、文件占位、语音转写/占位；原子写入，覆盖前需确认 |
| JSON | 版本 1 的本地 JSON 结构；不含音频路径或密钥；媒体不可用时给明确占位 |
| PDF / PNG | 使用系统中文字体与上游渲染器生成 PDF；图片可读且启用图片时，每张图片追加一个独立高清页；可选把每页另存为 PNG。空范围也会生成明确说明 |
| 图片恢复 | 可选：捕获图片密钥后，将可读图片解密到私有临时目录并在消息/导出中引用；不可读时明确占位 |
| 本地语音 | 可选：`faster-whisper` 在本机转写语音消息；模型不随包分发，首次需用户在界面中同意后下载到应用数据目录 |
| 场景 | 4 个内置场景 + 自定义场景保存/删除；场景可带天数、查询词、格式与提示词 |
| 任务记录 | 本地任务目录、SHA-256 工件清单、状态、失败恢复、历史列表；支持重新投递未完成/失败任务 |
| 投递 | 普通文件夹与 Obsidian 仓库。剪贴板与 Agent 目标在上游定义中存在，但本版本**不执行**，任务保持待投递 |
| 进度与取消 | 长连接、PDF、任务和转写通过 `progress` 事件显示阶段与数量；界面不会伪造成百分比。关闭窗口会停止自有 worker；请求超时只终止 worker 自身（不会连带终止微信），并立即清理本次私有临时目录 |
| 隐私 | 无遥测、无上传、无第三方 AI API；日志只写固定事件名/错误码，不写路径、账户、聊天正文或密钥 |

### 尚未完成或未验证

- **语音端到端未验证**：代码路径、依赖与界面已完成；本次交付**没有下载 Whisper 模型**，因此没有实际转写结果。首次使用需在“语音”页确认下载（约 460 MB，联网）后在本机运行。
- **真实连接已通过协议层验证，UI 点击流程未再次复验**：最终打包 worker 的真实连接在 2026-11-27 通过（167 个会话、167 张消息表，返回非通用账户昵称，断开与退出正常）；但发布版 UI 的“连接微信”按钮本轮没有再次自动点击截图。更早一次 UI 复跑曾返回 `WECHAT_CONNECT_FAILED`，可能是微信未及时关闭、手机/扫码确认未完成或密钥捕获失败；首次使用时请在微信窗口按提示完成确认。
- **代码签名、自动更新**没有实现，也无法仅靠代码完成：需要签名证书和更新服务器。Release 提供未签名的当前用户安装包，也可使用便携文件夹；未签名 EXE 可能触发 SmartScreen/杀毒提示。
- **未在干净机器、Windows 10、其他微信 4.x 小版本上验收**；真实微信验证只覆盖当前开发机。
- **本轮界面修复的端到端范围**：场景保存/应用、投递失败提示、连接后切换图片/语音的重新连接提示已通过单元测试与合成协议验证；真实微信连接后的切换流程和完整场景 UI 流程未实机执行。
- **PDF 500 页上限**：页数在渲染前检查、逐页释放已有单元测试；未在 501 条消息的真实规模下测量峰值内存。
- 不迁移上游的 AI 摘要/问答/RAG/OCR；不调用任何远程模型。场景里的“提示词”只是写进交付文件的文本，不会自动执行。
- 不支持 ARM64；不提供英文界面；不支持 MSIX。

## 2. 架构

```text
WinUI 3 (C#, MVVM)
   │  stdin/stdout UTF-8 JSON Lines, protocol v1
   ▼
WechatMemory.Worker.exe  (PyInstaller onedir, Python 3.11)
   ├─ demo source                 仅自动化测试使用，不随发布包分发
   ├─ wechat_memory.wechat4_source
   │    ├─ 账户发现（受限路径）
   │    ├─ Frida 内存密钥捕获 + 登录点击
   │    ├─ DecryptedDatabaseCache（只读原始库 → 私有临时明文副本）
   │    ├─ 消息查询、搜索、分页、图片缓存、语音缓存
   │    └─ 固定安全错误码，不返回路径/正文/密钥
   ├─ wechat_memory.exporters     Markdown / JSON / PDF / PNG
   └─ wechat_memory.tasks         场景、任务记录、文件夹/Obsidian 投递
```

- UI 不直接打开任何微信数据库，也不接触密钥。
- 一次只有一个请求在 worker 中执行；`hello` 做协议/时区校验；每个请求恰好一个最终响应，中间可有 `progress` 事件。
- 默认超时 30 秒；连接、PDF、任务、转写使用更长的命令级超时。
- 只管理本应用启动的 worker 进程；关闭时先 `shutdown`，超时才终止 worker 进程本身。worker 启动的微信不在终止范围内，避免误杀用户微信及其未发送内容；其余辅助子进程（登录点击、图片解码等）都是短命进程，会自行退出。

### 应用数据位置

| 数据 | 位置 |
|---|---|
| 私有的解密临时目录 | `%LOCALAPPDATA%\WechatMemoryNative\worker-temp` |
| 任务记录与工件 | `%LOCALAPPDATA%\WechatMemoryNative\jobs` |
| 自定义场景 | `%LOCALAPPDATA%\WechatMemoryNative\scenes.json` |
| 语音模型 | `%LOCALAPPDATA%\WechatMemoryNative\models` |
| 安全事件日志 | 可执行文件目录下的 `logs\`（固定事件名/错误码） |

删除 `%LOCALAPPDATA%\WechatMemoryNative` 会清理任务、场景、模型与临时文件；正在连接时不要强行删除临时目录。图片/数据库/PDF 中间页的明文临时副本都放在每次 worker 独立的私有 `worker-temp\worker-*` 根目录中：正常关闭、请求超时或强制终止 worker 后由桌面端删除整个根目录；崩溃遗留的根目录由下一个 worker 在启动时按锁文件清理，不再只依据 6 小时年龄。锁文件同时保证多实例运行时不会误删仍在使用中的缓存。

## 3. 安全与隐私边界

- **真实微信只读**：读取本机微信 4.x 数据库；不修改原始数据库、不发送消息、不导出联系人列表、不上传任何内容。
- **密钥只在内存**：Frida 捕获的数据库密钥保存在 worker 内存，不写日志、协议、命令行、环境变量或磁盘；关闭/断开时释放。
- **会重启微信一次**：微信 4.x 的密钥捕获需要关闭并重新启动微信。界面在切换真实模式时会显示确认；需要先在微信里保存未发送内容。该行为只影响微信，不修改其他系统设置。
- **临时明文**：为了用 SQLite 只读查询，加密数据库会解密到上述私有临时目录；PDF 中间页也写入同一目录。该目录位于当前用户 `%LOCALAPPDATA%`，正常关闭、超时或强制结束 worker 后由桌面端整体删除，异常崩溃则在下一个 worker 启动时按锁文件清理；这是使用本工具必须接受的本地明文风险。
- **不联网**：应用与 worker 运行时不发起 HTTP 请求。例外是用户主动点“准备语音模型”后，首次从模型仓库下载权重；下载需明确同意，转写仍在本机完成。
- **不执行剪贴板/Agent**：任务目标里的剪贴板与 Agent 不会被自动执行；只支持文件夹与 Obsidian 复制。
- **日志最小化**：只写固定事件名与安全错误码；不写消息、发送者、账户、路径、stderr 或异常正文。

## 4. 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 build 19041+ 或 Windows 11，**x64**，交互式桌面 |
| .NET | .NET SDK 10.0.404（`global.json` 固定）；发布版自包含 .NET 运行时 |
| Python | 源码运行/构建需要 64 位 CPython 3.11–3.13；发布版自带 worker，不需要系统 Python |
| 网络 | 首次 SDK/NuGet/pip 安装，以及用户同意的语音模型下载需要；日常运行不需要 |
| 微信 | 本机已安装并至少登录过微信 4.x；连接时会重启微信一次 |
| 权限 | 读取当前用户微信数据、启动/关闭微信、写入所选导出目录；不需要管理员权限 |

## 5. 快速开始

### 安装版 / 便携版

从 [Releases](https://github.com/HYJ1817/WeChat-History/releases) 下载当前用户安装包 `WeChat-Memory-Setup-*.exe`，或便携包 `WeChat-Memory-Windows-*.zip`。安装包不需要管理员权限；便携包解压后运行 `微信记忆App\NativeApp.exe`，不能只复制 exe。两者都未签名，可能触发 SmartScreen/杀毒提示。

### 源码运行

```powershell
cd C:\path\to\wechat-memory

# 只读检查环境
powershell -ExecutionPolicy Bypass -File .\scripts\Check-Environment.ps1

# 下载 .NET 10.0.404 到项目内 .tools（需要时）
powershell -ExecutionPolicy Bypass -File .\scripts\Bootstrap-Dotnet.ps1

# 创建项目 .venv 并安装运行依赖
powershell -ExecutionPolicy Bypass -File .\scripts\Prepare-Python.ps1

# 构建并启动
powershell -ExecutionPolicy Bypass -File .\scripts\Run.ps1
```

启动后应用只做本地检测：自动发现本机微信账户，不读取聊天、不重启微信。使用步骤：

1. 左侧选择微信账户（找不到时点“刷新”，并确认微信已登录过）；
2. 点“连接微信”，阅读并确认微信会重启一次的提示；
3. 等待密钥捕获与登录；若微信要求手机确认，请在手机上完成；
4. 选择会话、设置日期/关键词，点“应用筛选”；
5. 在右侧“导出”选择 Markdown / JSON / PDF；或在“任务”页生成带历史记录的任务。

> 只复制 `NativeApp.exe` 不能运行。源码运行需要保留项目目录与 `.venv`；发布运行需要保留整个发布文件夹（至少 `worker\` 与 `NativeApp.exe` 同目录）。发布版不包含 `demo\` 数据。

## 6. 构建、测试与发布

### 构建

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Build.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\Build.ps1 -Configuration Release
```

输出：`src/NativeApp/bin/x64/<Debug|Release>/net10.0-windows10.0.19041.0/win-x64/NativeApp.exe`。

### 测试

```powershell
# Python：84 个测试（协议、导出安全与内存、任务/场景/投递、账户字段、联系人表、路径编码、临时目录租约、任务历史、真实来源与错误映射）
.\.venv\Scripts\python.exe -I -X utf8 -m unittest discover -s tests/PythonWorker.Tests -v

# C#：42 个协议/进程、场景筛选与投递反馈测试
& .\.tools\dotnet\dotnet.exe test .\tests\NativeApp.Tests\NativeApp.Tests.csproj -c Debug --nologo

# 演示 UI 自动化（需要交互式桌面）
powershell -ExecutionPolicy Bypass -File .\scripts\Test-NativeFlow.ps1
```

`Test-NativeFlow.ps1` 会启动 GUI、选择会话与日期、打开系统保存对话框、导出 Markdown、检查 worker 回收，并把截图/`report.json` 写到 `artifacts/native-flow`。它依赖中文界面和真实窗口焦点，不适合无头 CI。

本机另做过（脚本位于 `artifacts/`，属于本地验证材料，不属于产品）：
- 真实微信协议烟雾测试：167 个会话、20 条首批消息、152 条导出、39,868 字节、无密钥泄漏、stderr 为空；
- 真实 UI 端到端（真实优先重构前的早期版本）：切换真实微信、确认重启、自动发现账户、连接、浏览、导出 Markdown 170,615 字节、worker 回收；
- 最终构建的打包 worker 真实连接与昵称探针：167 个会话、167 张消息表、`accountLabel` 非通用（昵称可用）、断开成功、stderr 为空；探针不打印昵称；发布版 UI 的点击连接流程未再次自动复验；
- 图片恢复：图片密钥可用，解密出 9,480 字节图片，断开后临时图片被删除。

### 发布

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Publish.ps1
```

发布位置：根目录 `微信记忆App`。`Publish.ps1` 会：
1. 安装 `src/PythonWorker/requirements-build.txt`（含运行依赖 + PyInstaller）；
2. 自包含发布 WinUI 应用；
3. 用 PyInstaller **onedir** 构建 worker，避免每次启动解压数百 MB；
4. 复制 README、通知、许可证、文档和 Python 包许可证文本；不复制测试用 demo 数据；
5. 运行打包 worker 的 hello/列表/日期筛选/shutdown 烟雾测试。

运行：

```powershell
.\微信记忆App\NativeApp.exe
```

`artifacts/` 是本地测试与发布产物，不提交；详细说明见 [docs/final-report.md](docs/final-report.md)。

## 7. 目录结构

```text
src/NativeApp/                 C# / WinUI 3 / MVVM 原生应用
src/PythonWorker/
  worker.py                    JSON Lines worker
  wechat_memory/
    wechat4_source.py          真实微信只读适配层
    exporters.py               Markdown / JSON / PDF / PNG
    tasks.py                   场景、任务、投递
  vendor/wechat_context_exporter/  vendored 上游 MIT 模块
  requirements.txt             运行依赖
  requirements-build.txt       构建依赖
tests/                         Python + C# 测试
scripts/                       引导、构建、运行、测试、发布、诊断
demo/conversations.json        自动化测试用虚构数据（不进入发布包）
docs/                          协议、评估、最终报告
LICENSES/                      上游 MIT 许可证
artifacts/                     本地测试证据与发布输出（不提交）
reference/                     只读上游快照（不提交）
```

## 8. 已知限制

- 只支持本机微信 **4.x**；数据库结构或微信版本变化可能导致 `WECHAT_DB_UNSUPPORTED`。
- 连接需要重启微信并可能要求手机确认；Frida 注入可能被杀毒软件拦截，也可能受微信更新影响。
- 同时只连接一个微信账户；昵称保存在加密数据库中，连接前只能显示最近使用时间，连接成功后才显示微信昵称。
- 非文本消息按类型给占位；图片可恢复时显示“打开已恢复图片”，不可读时占位；语音必须显式启用并准备模型。
- PDF 依赖系统中文字体；缺少字体时返回固定错误。
- 输入内容可能包含任意聊天文本；导出文件的安全由用户选择的保存位置与访问权限决定。
- 没有代码签名、自动更新、崩溃上报、ARM64、英文 UI；安装包是当前用户级且未签名。
- 没有在干净机器/Windows 10/其他微信小版本上复验；也没有正式第三方安全审计。
- 当前发布提供**便携文件夹与未签名的当前用户安装包**，仍不是面向普通用户的可分发产品；完整评估见 [docs/final-report.md](docs/final-report.md) 的“对外分发评估”。

## 9. 文档与许可证

- [docs/protocol.md](docs/protocol.md)：worker 协议 v1、全部命令、进度事件、错误码。
- [docs/final-report.md](docs/final-report.md)：本轮实际改动、验证记录、发布位置、未完成事项。
- [docs/source-assessment.md](docs/source-assessment.md)：上游结构、迁移映射与许可证评估（第一阶段记录，仍保留历史背景）。
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：第三方组件、版本与许可证。
- [LICENSES/wechat-ai-memory-LICENSE.txt](LICENSES/wechat-ai-memory-LICENSE.txt)：上游 MIT 许可证逐字副本。

本仓库以 MIT 许可证发布，见 [LICENSE](LICENSE)。第三方组件与 vendored 代码的许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
