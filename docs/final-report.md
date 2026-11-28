# 最终实现与验证报告

> 日期：2026-11-27
> 范围：把原项目尚未迁移的真实微信读取、导出、媒体、语音、任务/场景/投递与原生界面补齐，并完成本机验证、便携发布与当前用户安装包发布。
> 说明：本报告只记录工程结果，不包含任何真实聊天内容、账户名、绝对数据路径或密钥。
> 命名说明：本报告记录的历史交付物曾使用 `NativeApp.exe`；当前仓库项目、程序集与本地便携版统一使用 `WeChat-History`。历史验证记录保留原名以确保可追溯。

## v0.2.0 增量交付（2026-11-28）

- 新增离线 JSON 导入：支持本应用 JSON v1 导出与兼容 demo 结构，完整验证后原子切换为内存快照；失败保留当前来源，不读取内容指向的外部媒体路径，保留已有语音转写与时长。
- 新增任务历史详情及手动保留期清理：只读预览、10 分钟单次 token、永久删除双确认、全流程操作互斥，以及部分失败的保守提示。
- 清理仅在本地固定 NTFS 卷且建立保护后执行；不进回收站、不保证原子回滚，ADS 不受预览/指纹/防写保护但随宿主文件删除。完整边界见 `docs/protocol.md`。
- 修复多行元数据的 PDF 渲染边界，并补齐 UTF-8、重复字段、时间范围、来源切换和任务维护的合成回归测试。
- 发布授权前的最新全量结果：Python **177/177**、Native **55/55**；Debug 编译 **0 警告 / 0 错误**。本版另做 Release 打包与包内协议冒烟，具体记录随本次发布产物保留。
- 此次增量**没有人工窗口、安装/卸载或真实微信验收**；下文 2026-11-27 的 UI、安装器与真实微信记录属于历史验证，不能视为 v0.2.0 的重新验收。
- 主程序为 `WeChat-History.exe`；便携 ZIP 与当前用户安装版仍沿用 `WeChat-Memory-*-v<版本>` 下载命名，均未签名。

## 1. 交付物与启动方式

### 已发布的可运行版本

```text
微信记忆App/
├─ NativeApp.exe                       WinUI 3 原生应用（自包含 win-x64）
├─ NativeApp.pri
├─ worker/
│  ├─ WechatMemory.Worker.exe          PyInstaller onedir worker
│  └─ _internal/                       Python、frida、ffmpeg、报告/语音依赖
├─ README.md / LICENSE / BUILD-INFO.txt / THIRD_PARTY_NOTICES.md
├─ LICENSES/                           上游 MIT、CPython/PyInstaller、Python 包许可证
├─ docs/                               协议、评估、本报告
└─ logs/                               首次运行后生成
```

- 目录大小：**约 718.5 MiB（883 个文件）**，包含 README、LICENSE、`BUILD-INFO.txt`、许可证与 `docs/` 文档；
  Release 另提供约 193 MiB 的当前用户安装包。`publish-report.json` 保留在开发目录
  `artifacts/publish` 中；以资源管理器显示为准。
- 启动：在项目根目录双击 `微信记忆` 快捷方式，或直接运行

  ```powershell
  .\微信记忆App\NativeApp.exe
  ```

  开发目录中的 `artifacts`、`.tools`、`.venv`、`reference`、`demo` 默认设为隐藏，
  避免和源码混在一起；资源管理器打开“显示隐藏的项目”仍可查看。

- 发布版**不包含演示数据**，启动后只检测本机微信账户；选择账户并确认后才连接。
- 运行时不要求系统安装 .NET、Python 或 PyInstaller；需要 x64 Windows 10 build 19041+ / Windows 11 和交互式桌面。
- 同时提供**便携文件夹**与**当前用户安装包**；便携分发时要复制整个文件夹，不能只发 `NativeApp.exe`。
- 未签名 EXE 可能触发 SmartScreen 或杀毒软件提示；Frida 组件也可能被安全软件拦截。

### 源码运行

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Prepare-Python.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\Run.ps1
```

## 2. 实际改动

### 2.1 C# / WinUI 3 原生界面

- 重写 `src/NativeApp/MainWindow.xaml` 与代码后置：
  - 三栏布局：左侧数据源/账户/会话筛选；中间日期、关键词、分页消息；右侧“导出 / 任务 / 场景 / 语音”页签。
  - 窄窗口把右侧页签移到底部；保留原生日历、文件/文件夹选择器、InfoBar、进度条。
  - 标题栏跟随应用主题：设置 `AppWindowTitleBar` 的背景/前景/按钮各状态颜色，并用 DWM immersive dark mode 兼容 Windows 10；主题切换时重新应用，避免深色界面顶部出现白色系统标题栏。
  - 隐藏系统标题栏左上角的图标与文字：`ExtendsContentIntoTitleBar` + 32px 自定义拖动区，保留右侧最小化/最大化/关闭按钮；应用名称改在内容区顶部显示。
  - 真实微信优先：启动只检测账户，不读取聊天、不重启微信；点击“连接微信”并确认后才连接。
  - 明确提示“会重启微信一次”，并提示手机确认与登录超时后的处理方式。
  - 图片恢复按钮、JSON/PDF/PNG 导出、任务目标（文件夹/Obsidian）、场景保存/删除、语音模型同意对话框。
- 重写 `MainViewModel`：
  - 真实微信优先的启动/账户发现/连接流程（6 分钟命令级超时）、进度事件消费。
  - 连接成功后从只读联系人表取本机微信昵称并更新账户下拉框；连接前因数据库加密只显示最近使用时间，不伪造昵称。联系人表读取失败只影响昵称，不阻断连接。
  - `--demo` 仅作为自动化测试入口，读取仓库外部的虚构 fixture；发布版没有演示入口或演示数据。
  - 会话/消息筛选、搜索、分页；消息预览默认最新在前（`order:"desc"`），导出仍按时间正序；导出 Markdown/JSON/PDF；图片开关。
  - 场景应用/保存/删除；任务创建/历史/重新投递；语音状态、模型准备（用户同意后）、转写。
  - 所有错误以固定中文消息显示；日志只写事件名/错误码。
- 扩展 `ChatModels.cs`：账户、场景、任务、语音、导出详情 DTO。
- 扩展 `Protocol.cs` / `WorkerClient.cs`：识别并转发 `type:"progress"`，不把它当最终响应。
- `WorkerLocator` 同时支持发布版 onedir worker 与源码 venv；只启动本应用自己的子进程。

### 2.2 Python worker 与真实微信适配

- `src/PythonWorker/wechat_memory/wechat4_source.py`（新增，核心适配层）：
  - 账户发现（受限路径，token 为哈希）；微信主程序定位（Program Files、磁盘根目录及一层子目录）。
  - Frida 内存密钥捕获 + pywinauto 登录点击线程（微信 4.2 的 UIA invoke 不可用）。
  - 只读解密到 `%LOCALAPPDATA%\WechatMemoryNative\worker-temp` 下每个 worker 独立的私有根；正常关闭/超时/强杀由桌面端删除，崩溃遗留由下一个 worker 按锁文件清理。
  - 会话/联系人/消息分表查询、UTC+08 包含式日期、关键词、分页、最多 20 万条内存保护。
  - 图片密钥按需捕获、图片解密缓存；语音缓存与本地转写入口。
  - 固定安全错误码；不返回账户目录、数据库路径、密钥或正文到错误中。
- `src/PythonWorker/worker.py` 重写：
  - 保留演示命令用于自动化测试；发布版不提供演示入口，也不分发 demo JSON。
  - 新命令：`list_accounts`、`connect`、`disconnect`、真实/演示 `list_conversations`、`get_messages`（搜索/分页）、`export_json`、`export_pdf`、场景、任务、投递、语音。
  - 进度事件：`{"type":"progress","current":...,"total":...,"label":...}`。
  - 演示图片仅当 `content` 指向可读本地文件时才按图片导出；占位文本仍转为显式系统占位，不伪造链接。
  - 结构化异常映射；只有未知异常写 stderr（客户端不显示/不记录正文）。
- vendored 上游 MIT 模块（commit `14d94db8b1affa12a2d7d88f0f830c1e0b8d752a`）：
  - 微信 4.x 发现/加解密/密钥捕获/媒体/语音、PDF/渲染、场景/任务/投递。
  - 修改 `DecryptedDatabaseCache` / `DecryptedImageCache` 支持私有 `temp_root`。
- `wechat_memory/exporters.py`（新增）：
  - Markdown、JSON v1、PDF + PNG 页；图片可读且在界面启用图片时，每张图片追加一个独立高清页；原子写入；图片资产复制；空范围明确说明。普通导出读取上限为 200,000 条消息；PDF 另限 20,000 条消息和 500 页；固定中文错误。
- `wechat_memory/tasks.py`（新增）：
  - 场景内置/自定义存储；任务 staging → SHA-256 清单 → 原子提交；失败恢复；任务历史不带消息预览。
  - 文件夹与 Obsidian 投递；Obsidian 不覆盖已有笔记；剪贴板/Agent 不执行。

### 2.3 打包、依赖与文档

- 新增 `src/PythonWorker/requirements.txt`（固定 frida/pycryptodome/zstandard/pywinauto/Pillow/reportlab/faster-whisper/silk-python/imageio-ffmpeg）。
- `requirements-build.txt` 改为包含运行依赖 + PyInstaller + hooks。
- `scripts/Publish.ps1`：
  - 从 onefile 改为 **onedir**，避免每次启动解压数百 MB、降低杀软误报。
  - 增加 frida、pysilk、faster-whisper、imageio-ffmpeg、pywinauto/comtypes、reportlab、vendored 包的 hidden imports/submodules/binaries。
  - 复制 CPython、PyInstaller 与 Python 包许可证到发布目录。
- 重写 `README.md`、`docs/protocol.md`、本报告；更新第三方组件说明。
- 测试脚本保留在 `artifacts/`（不提交），包括演示协议、真实协议、图片恢复、打包 worker、UI 自动化。

### 2.4 本轮缺陷修复（2026-11-27）

本轮针对代码审查确认的 11 个缺陷和 10 条未验证线索逐项处理；所有数据库、目录、会话和图片均为合成数据，未操作真实微信。

1. **SQLite URI 编码**：新增 `sqlite_read_only_uri()`（`Path.resolve().as_uri() + "?mode=ro"`），替换真实来源、解密、语音中所有字符串拼接；含 `#`、`%` 的目录有回归测试。
2. **解密明文与 PDF 中间页残留**：桌面端为每个 worker 创建独立私有根并设置 `WECHAT_MEMORY_TEMP_ROOT`；正常关闭、请求超时、读取失败、异常退出和释放时删除；worker 启动时按锁文件清理崩溃遗留根和旧 `%TEMP%\wechat-memory-pdf-*`；PDF 中间页写入同一私有根。
3. **多实例误删缓存**：worker 根与会话目录使用跨进程锁（Windows `msvcrt.locking` / POSIX `flock`），清理只删除无活动锁的目录；解密缓存命中前校验明文文件仍存在，缺失则重新解密。
4. **误杀微信**：不再使用 `Kill(entireProcessTree: true)`，只终止持有的 worker 进程；新增集成测试用 worker 启动的普通子进程代表微信，确认超时后子进程仍存活。
5. **PDF 先渲染后判限**：改为先布局并统计页数，超过 500 页直接返回 `PAGE_LIMIT_EXCEEDED`；渲染逐页保存并立即释放，避免 501 条消息先产出约 12 GiB 位图。
6. **覆盖导出残留**：资产目录和图片页改为暂存后替换；覆盖时清理旧 `<stem>_assets` 与多余的 `page_*_image.png`，失败回滚。
7. **同日同会话任务混写**：文件夹投递目录名加入任务 ID；同一日期、同一会话的多次导出各自独立。清单中的工件缺失时投递失败，不再静默跳过。
8. **Markdown/HTML 注入**：聊天正文、发送者、标题统一转义；代码围栏按内容动态加长；图片只引用本地文件；JSON 保留原文；空范围 Markdown 也走同一安全导出器。
9. **重新投递假成功**：解析响应中的 `delivery.status`，只有 delivered/copied 才显示成功；failed 更新任务状态并显示固定错误。
10. **场景丢日期**：保存场景写入当前显式 `start`/`end`、关键词和输出选项；应用支持显式日期、`days` 和无界窗口，显式日期优先；无效日期报错，无窗口场景清空旧筛选。
11. **UI 测试假通过**：`Test-NativeFlow.ps1` 整段粘贴完整路径并比较规范化后的实际路径；不一致立即抛出，不再出现 `customPathUsed=false` 但 `ok=true`；测试后恢复剪贴板。

未验证线索中确认并修复的：

- 图片索引不失效：解析失败后按 30 秒节流重建索引。
- 毫秒时间戳：按表内 `MAX(create_time)` 识别秒/毫秒，筛选边界先换算再查询。
- 分页内存：查询改为游标流式读取；完整命中范围仍需在 20 万条上限内物化（见第 5 节）。
- 连接后切换图片/语音：记录连接时能力，切换后要求重新连接，不自动重启微信。
- `prune(keep_unfinished=True)` 误删 FAILED：保留除 DELIVERED/COPIED/EXPIRED 外的所有任务。
- 单日显示“前一天”：新增 UTC+8 的 `display_date()`，任务摘要、Obsidian frontmatter 和提示词统一使用。
- 多格式导出快照不一致：任务导出复用同一消息快照，不再按格式重新查询。
- Obsidian 覆盖竞态：改为排他创建（`open("x")`），不会覆盖已有笔记。

未修复或仅部分改善：

- 分页查询仍会把时间范围内的匹配行读入内存后再切片；已用游标避免 `fetchall()` 的整表副本，但极端范围仍受 `MAX_MESSAGES`（20 万）限制。
- 损坏任务 JSON：列出任务时已跳过并记录 `last_warnings`，不会遮蔽其他任务；本轮未新增 UI 展示该警告。
- 未在真实微信/真实 Obsidian 仓库上复跑本轮新增的界面流程。

## 3. 验证结果

### 3.1 自动化测试

| 测试 | 命令/入口 | 结果 |
|---|---|---|
| Python 测试 | `.venv/Scripts/python.exe -I -X utf8 -m unittest discover -s tests/PythonWorker.Tests -q` | **84/84 通过**，约 57 秒 |
| C# 测试 | `.tools/dotnet/dotnet test tests/NativeApp.Tests/NativeApp.Tests.csproj -c Debug` | **42/42 通过**，约 2 秒；含新增场景/投递/媒体测试 18 个 |
| UI 自动化（`--demo` 测试 fixture） | `scripts/Test-NativeFlow.ps1`（Debug） | 通过：3 个会话、筛选 4/4 条、导出 546 字节、自定义保存路径精确匹配、worker 回收、退出码 0。fixture 来自仓库，不进入发布包 |
| 发布版 UI 端到端（测试 fixture） | 同上，`-Executable 微信记忆App/NativeApp.exe` | 通过：3 个会话、筛选 4/4 条、导出 546 字节、自定义保存路径精确匹配、实际 worker 为 `WechatMemory.Worker.exe`、回收成功、退出码 0（2026-11-27 缺陷修复后发布构建复跑） |
| 当前用户安装包 | `WeChat-Memory-Setup-v0.1.0.exe /S /D=<临时目录>` | 通过：静默安装退出码 0；`NativeApp.exe`、`worker/WechatMemory.Worker.exe`、`Uninstall.exe`、`LICENSE` 存在；桌面/开始菜单快捷方式与卸载注册表创建；静默卸载退出码 0，目录、快捷方式和注册表清除 |
| 发布版真实优先启动 | `artifacts/Probe-RealFirst.ps1 -Executable 微信记忆App/NativeApp.exe` | 通过：0 个会话（未连接）、发现 2 个账户、连接按钮可用、无 demo 开关；未重启微信 |
| 演示任务/场景/语音 UI | `artifacts/Test-TaskFlow.ps1` | 通过：任务列表出现 1 条；场景下拉可展开；语音按钮可用 |
| 打包 worker 协议 | `artifacts/packaged_protocol_smoke.py` | 通过：capabilities 15、JSON 12 条/3,703 字节、PDF 2 页/412,960 字节、场景 4、任务 delivered、账户 2、voice 组件可用、stderr 空、退出码 0 |
| 打包 worker 消息顺序 | 直接 stdio 请求 `order:"desc"` / 默认 `asc` | 通过：`desc` 返回 m012→m001，默认返回 m001→m012，desc 首位时间 2026-11-25 16:20 |
| 发布版 UI 消息顺序 | `artifacts/Check-MessageOrder.ps1` | 通过：12 条消息，首条 m012（2026-11-25 16:20），末条 m001（2026-11-23 09:12），`newestFirst=true` |
| 发布版自定义标题栏 | `artifacts/Capture-Published.ps1` + 截图 | 通过：左上角系统图标/标题已移除，右侧窗口按钮保留，深色标题栏与内容一致 |
| 打包 worker 账户字段 | 直接 stdio 请求 `list_accounts` | 通过：返回 `token/label/lastUsed/recent`，`lastUsed` 为 25 字符 ISO 时间；账户标签不含编号 |
| PDF 独立图片页（打包 worker） | `artifacts/pdf_image_protocol_smoke.py` | 通过：启用图片时 2 页（1 聊天页 + 1 图片页）、`page_0002_image.png` 写入、PDF 魔数有效；禁用图片时 1 页/0 图片页；stderr 空、退出码 0 |
| 真实协议（源码 worker） | `artifacts/real_protocol_smoke.py` | 通过：167 会话、首批 20 条（总计 152）、关键词查询 40、Markdown 152 条/39,868 字节、无密钥泄漏、stderr 空 |
| 真实图片恢复（源码 worker） | `artifacts/image_protocol_smoke.py` | 通过：图片密钥可用、找到图片、9,480 字节、PNG/JPEG 魔数有效、位于私有临时目录、断开后临时文件删除 |
| 真实 UI 端到端（早期版本） | `artifacts/Test-RealFlow.ps1` | 通过：切换真实模式→确认重启→自动发现账户→连接→浏览→Markdown 导出 170,615 字节→worker 回收 |
| 真实 UI 端到端（真实优先重构后） | 同上 | 启动与账户发现通过；点击“连接微信”并确认后，约 5 分钟返回 `WECHAT_CONNECT_FAILED`，未进入会话列表。日志只有错误码，无法区分是微信未及时关闭、登录确认未完成还是密钥捕获失败；需人工在微信窗口完成确认后重试 |
| 打包 worker 真实连接 | `artifacts/packaged_real_smoke.py` | 通过：账户 2、会话 4、9 条消息、Markdown 554 字节、stderr 空、退出码 0 |
| 打包 worker 真实连接 + 昵称（最终构建） | `artifacts/real_nickname_probe.py` | 通过：连接成功、167 会话/167 消息表、`accountLabel` 非通用标签（`nickname_available=true`）、会话列表 167、断开成功、stderr 空、退出码 0；测试不打印昵称 |

本机微信版本为 **4.2.0.13**；真实验证在 2026-11-27 完成。最后的源代码修改
（自定义标题栏、账户标签去编号、昵称读取、消息最新在前、发布脚本防旧 worker）
之后重新发布了 881 个文件（含报告）；发布版 UI（外部测试 fixture）、打包 worker
协议、PDF 独立图片页和真实优先启动探针通过。新增验证：打包 worker 的
`order:"desc"` 返回 m012→m001，默认仍为 m001→m012；发布版 UI 的 12 条消息
`newestFirst=true`；自定义标题栏截图确认左上角系统图标/标题已移除。`Publish.ps1`
的烟雾测试现在同时断言消息顺序和 worker SHA-256，避免再次发布旧 worker。

2026-11-27 20:52 又完成一次缺陷修复后的重新发布：881 个文件、约 753.4 MB。
打包 worker 协议冒烟（hello、3 个会话、4 条日期筛选、消息倒序、shutdown）
通过，stderr 为 0；发布版 UI 自动化再次通过（自定义保存路径、筛选 4/4 条、
worker 回收、退出码 0）。本轮没有重新执行真实微信连接。

早期真实优先重构后的 UI 复跑曾返回 `WECHAT_CONNECT_FAILED`，未进入会话列表；
日志未记录具体环节，可能是微信未及时关闭、登录确认未完成或密钥捕获失败。最终
构建的打包 worker 真实连接探针随后通过：167 个会话/167 张消息表，`accountLabel`
为非通用昵称（`nickname_available=true`），说明联系人表昵称读取在真实 4.2.0.13
数据上可用。探针不打印昵称；发布版 UI 下拉框直接绑定同一个 `accountLabel`，但本轮
没有再次自动点击发布版 UI 的“连接微信”按钮做截图确认。

### 3.2 真实数据链路

已验证的链路：

```text
账户发现 → Frida 内存密钥捕获 → 点击登录（重启微信一次）
→ 只读解密到私有临时目录 → 会话/消息查询 → 日期/关键词筛选
→ Markdown/JSON/PDF 导出 → 图片恢复（可选） → 断开清理
```

- 原始微信数据库未被修改；没有在测试中发送消息、添加联系人或改变微信设置。
- 密钥没有写入磁盘、日志、协议、命令行或环境变量；测试脚本只打印数量/状态。
- 真实测试产生的导出文件在断言后删除；没有把聊天内容复制进仓库、README 或本报告。
- 发布版 worker 也完成了真实连接和导出，说明 onedir 打包包含 frida/pywinauto 等动态组件。

### 3.3 遇到并解决的问题

1. **微信 4.2 登录窗口的 UIA invoke 无效**：改为 pywinauto 真实鼠标点击登录按钮；连接从超时变为成功。
2. **`extract_database_keys()` 在 4.2 找不到密钥**：改用 Frida 内存捕获。
3. **微信安装在 `C:\微信\Weixin`，不在上游候选路径**：增加受限的主程序定位；捕获失败时恢复启动微信。
4. **图片连接测试首次 `WECHAT_LOGIN_TIMEOUT`**：登录窗口残留；重试后成功，说明捕获依赖微信登录状态，属于环境限制，不是导出逻辑错误。为验证真实图片 PDF 页再次复跑时，连续两次登录超时；因此改用确定性 PNG 验证导出器与打包 worker，真实微信图片恢复的既有验证不受影响。
5. **首次发布构建失败**：新增 XAML `x:Name` 后使用了旧的生成代码，报 `ScenesCombo`/`JobsList` 不存在；清理并重新构建/发布后通过。
6. **PyInstaller onefile 体积/启动问题**：改为 onedir；frida `_frida.pyd` 约 162 MiB、ffmpeg 约 101 MiB，最终发布约 718.5 MiB。
7. **PowerShell 5.1 读取无 BOM 的 UTF-8 测试脚本导致中文匹配失败**：测试脚本改用 AutomationId/ASCII 断言；这是测试脚本问题，不是产品问题。
8. **`Publish.ps1` 无 BOM 导致发布到乱码目录**：脚本里的 `微信记忆App` 中文字面量被 Windows PowerShell 5.1 按 ANSI 解码，实际目标变成 `寰俊璁板繂App`；真实 `微信记忆App` 一直是旧文件，这正是“发布后仍是旧 worker/旧消息顺序”的根因。已把 `Publish.ps1` 保存为 UTF-8 with BOM，并加入 worker SHA-256 校验与 `order:"desc"` 烟雾断言；复跑确认哈希一致、目标目录正确。乱码目录已删除。

## 4. 安全与隐私实现

- **原始数据库只读**：不写回微信数据库；不使用微信的发送接口。
- **密钥内存隔离**：只在 worker 进程内使用；不通过协议/argv/env/日志/磁盘传递。
- **私有临时根**：`%LOCALAPPDATA%\WechatMemoryNative\worker-temp`；每个 worker 使用独立根目录，正常关闭/超时/强杀后由桌面端删除；崩溃遗留由下一个 worker 启动时按锁文件清理，锁文件同时防止多实例误删活跃缓存。使用期间存在解密后的本地明文数据库/图片/PDF 中间页，这是功能必需风险。
- **最小化日志**：固定事件名与错误码；不写路径、账户、消息、发送者、stderr 正文。
- **无上传/遥测/远程 AI**：应用运行时不联网。唯一网络行为是用户在“语音”页明确同意后下载 faster-whisper 模型。
- **发布版不含演示数据**：`--demo` 只供自动化测试从仓库外部 fixture 启动，产品界面没有演示入口。
- **投递边界**：只写用户选择的文件夹或 Obsidian 仓库；不自动执行剪贴板/Agent。
- **进程边界**：只启动/终止自己的 worker；超时只终止 worker 本身，不终止其启动的微信。连接微信时按上游实现关闭并重启微信，这是用户确认后的行为。

## 5. 未完成、未验证与外部限制

### 未完成（需要外部资产，不能仅靠代码）

- **安装器**：已提供 NSIS 当前用户安装包（未签名，不需要管理员权限）；MSIX/带签名安装器未实现。
- **代码签名**：没有证书，无法签 EXE/安装器；未签名会触发 SmartScreen/AV。
- **自动更新**：需要更新服务器、签名与发布通道，未实现。
- **崩溃上报/遥测**：按隐私要求默认不实现。

### 对外分发评估（当前结论）

当前构建适合**本机自用或懂技术的试用者**，不适合直接交给普通用户：

- 未签名；安装器是当前用户级，SmartScreen/杀毒软件可能拦截，Frida 组件尤其容易被拦截。
- 真实连接需要用户在微信窗口完成扫码/手机确认；无人值守测试已复现失败，不能承诺“一键连接”。
- 只在本机微信 4.2.0.13 验证；其他 4.x 版本、数据库结构和多账户组合未验证。
- 项目已采用 MIT LICENSE；FFmpeg 的 GPL 分发风险未完成法律复核。
- 未在干净 Windows VM、Windows 10 全版本、无 .NET 环境和多款杀毒软件上验收。
- 没有自动更新、崩溃上报或远程支持通道。

要把它变成“给别人用”的产品，至少还需要：安装器签名与代码签名、杀软白名单/兼容性测试、
微信版本矩阵、清晰的人工登录引导与失败回退、许可证/隐私说明，以及更新通道。

### 已实现但未端到端验证

- **微信昵称显示**：联系人表读取优先级（备注 > 昵称 > 别名 > wxid）有单元测试；最终打包 worker 真实连接确认 `accountLabel` 为非通用昵称。尚未单独截图确认发布版 UI 下拉框的最终显示，但 UI 直接使用同一返回字段。
- **语音转写**：依赖、worker 命令、UI 和缓存路径已完成，`voice_status` 显示组件可用；但本次**没有下载 Whisper `small` 模型**，因此没有实际转写音频。首次使用需用户同意下载（约 460 MB）并联网。
- **PDF 独立图片页**：已实现，并通过导出器测试与打包 worker 协议测试用生成的 PNG 验证（`page_NNNN_image.png`、`imagePageCount`、页数上限）；真实微信图片恢复此前已验证，但本次追加的真实图片 PDF 复跑连续两次遇到 `WECHAT_LOGIN_TIMEOUT`，未能在本次生成真实图片页。
- **多账户同时连接**：一次只连接一个账户。
- **所有微信 4.x 小版本/数据库结构**：只在本机 4.2.0.13 验证；版本变化可能报 `WECHAT_DB_UNSUPPORTED`。
- **干净机器/Windows 10/无 .NET/无 Python 环境**：发布版自包含，但未在干净 VM 验收。
- **杀毒软件/Frida 兼容性**：未做多款 AV 测试；Frida 可能被拦截或需要管理员权限。
- **场景保存/应用与投递失败提示**：有 helper 单元测试和真实 `TaskService` 合成 payload 验证；完整 UI 流程未自动化。
- **连接后切换图片/语音的重连提示**：有 `ConnectionMedia` 单元测试；未在真实微信上执行。
- **PDF 500 页上限的内存收益**：有导出器测试；未在 501 条真实规模下测量 RSS。
- **长时间运行/大规模导出/fuzz/正式安全审计**：未做。

## 6. 环境限制说明

- 真实连接会关闭并重启微信一次；若微信要求扫码/手机确认，自动点击可能无法完成，界面会提示超时。连接过程中不要在微信里编辑未发送内容。
- 微信登录窗口的按钮布局、进程名 `Weixin.exe`、数据库结构都可能随版本变化；适配层已把失败映射为固定错误码，但无法保证未来版本。
- `taskkill` 只用于微信（用户已授权）；应用自身的 worker 只终止持有的 worker 进程，不使用进程树终止，也不按名称杀其他 Python；worker 启动的微信和短命辅助进程不受影响。
- 发布目录包含 frida 与 ffmpeg，体积大；这是真实功能与本地转写/图片解码的代价。
- UI 自动化依赖中文界面、交互式桌面和真实窗口焦点；无头 CI 不适用。

## 7. 后续建议

1. 在干净 Windows 11/10 VM 上运行发布版，验证自包含运行时、杀软与 Frida。
2. 复核项目 MIT LICENSE 与第三方许可证（尤其 FFmpeg）后，再扩大对外分发。
3. 若要正式分发，补充安装器签名、代码签名、许可证文件收集与更新通道。
4. 若需要完整语音体验，提供模型下载镜像/离线导入，并在目标机器上跑一次真实转写回归。
5. 若要支持更多微信版本，建立数据库 schema 探测与版本矩阵测试，而不是假设结构不变。
