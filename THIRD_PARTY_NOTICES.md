# 第三方组件与许可证声明

> 核验日期：2026-11-27
> 版本来源：`src/NativeApp/WeChat-History.csproj`、`obj/project.assets.json`、
> `.tools/packages/*.nuspec`、`.venv/.../dist-info/METADATA` 与包内许可证文件。
> 本文件是工程记录，不构成法律意见；分发前请由权利人复核。

本仓库当前没有独立的项目级 `LICENSE` 文件；本文件只列出第三方组件，不能替代
项目自身的整体许可证。

## 1. 汇总

| 组件 | 版本/来源 | 许可证 | 用途/分发状态 |
|---|---|---|---|
| Microsoft Windows App SDK / WinUI | `Microsoft.WindowsAppSDK.WinUI` **2.7.0**，并解析出 Base/Foundation/InteractiveExperiences **2.7.0** | Microsoft Windows App SDK 软件许可条款（包内 `license.txt`，非 MIT/OSI） | 原生 UI 运行时；随自包含发布输出分发 |
| CommunityToolkit.Mvvm | **8.4.2** | MIT | MVVM 源生成/命令；随应用分发 |
| CPython | **3.11.15**（当前 `.venv`；脚本支持 3.11–3.13 x64） | Python Software Foundation License Version 2 | worker 解释器；源码运行时不随仓库分发；PyInstaller 打包时会进入 worker |
| PyInstaller | **6.22.3**（`requirements-build.txt` 固定） | GPLv2-or-later，带 bootloader 特殊例外 | 仅构建期；用于生成 `WechatMemory.Worker.exe` |
| wechat-ai-memory vendored 代码 | commit `14d94db8b1affa12a2d7d88f0f830c1e0b8d752a`，tag `v0.3.8-alpha.2` | MIT | 微信 4.x 发现/解密/密钥/媒体/语音、PDF/渲染、场景/任务/投递、模型与文本导出；随 worker 分发 |
| Python 运行依赖 | frida 17.28.0、pycryptodome 3.24.1、zstandard 0.25.0、pywinauto 0.6.9、Pillow 11.3.0、reportlab 4.5.1、faster-whisper 1.2.1、silk-python 0.2.9、imageio-ffmpeg 0.8.1（及传递依赖） | 见第 11 节 | 真实微信读取、图片/PDF、任务、语音；随 worker 分发 |
| FFmpeg（imageio-ffmpeg 内置） | **9.0.2 essentials build（gyan.dev）** | FFmpeg 自身许可证；该构建自称 essentials，通常为 GPLv3，**分发前必须复核** | WXGF/媒体解码；随 worker 分发，约 101 MiB |
| Microsoft .NET 运行时 | `Microsoft.NETCore.App.Runtime.win-x64` / `Microsoft.WindowsDesktop.App.Runtime.win-x64` **10.0.15** | MIT（NuGet 包元数据） | dotnet publish 自包含输出 |
| Microsoft WebView2 | `Microsoft.Web.WebView2` **1.0.3719.77** | Microsoft 提供的 BSD-3-Clause 风格许可（包内 `LICENSE.txt`） | Windows App SDK 依赖；随自包含发布输出分发 |
| Microsoft.Windows.SDK.BuildTools | **10.0.26100.4654**（另有 MSIX 1.7.251221100） | Microsoft Windows SDK 许可（包元数据 `aka.ms/WinSDKLicenseURL`） | 仅构建期 |
| 测试依赖 | xunit **2.9.3**、xunit.runner.visualstudio **4.0.1**、Microsoft.NET.Test.Sdk **18.12.0** | xUnit 两项 Apache-2.0；Test.Sdk MIT | 仅测试，不随应用发布 |

## 2. Microsoft Windows App SDK / WinUI 2.7.0

- NuGet 包：`Microsoft.WindowsAppSDK.WinUI/2.7.0`；同时解析
  `Microsoft.WindowsAppSDK.Base/2.7.0`、`Microsoft.WindowsAppSDK.Foundation/2.7.0`、
  `Microsoft.WindowsAppSDK.InteractiveExperiences/2.7.0`。
- 项目引用：`src/NativeApp/WeChat-History.csproj`。
- 许可证：包内 `license.txt` 为 **“MICROSOFT SOFTWARE LICENSE TERMS —
  MICROSOFT WINDOWS APP SDK”**，属于 Microsoft 的软件许可条款，不是 MIT 或
  OSI 开源许可证。条款允许按约定再分发随 NuGet 包 binplace 到应用中的可分发代码；
  分发时需遵守其通知、 indemnity 等要求。
- 第三方通知：包内 `NOTICE.txt` 列出随包组件的通知；其中可见
  `Newtonsoft.Json 13.0.1 - MIT`（Copyright James Newton-King）等条目。完整列表
  以包内 `NOTICE.txt` 为准。
- 本地文件：
  - `.tools/packages/microsoft.windowsappsdk.winui/2.7.0/license.txt`
  - `.tools/packages/microsoft.windowsappsdk.winui/2.7.0/NOTICE.txt`
  - Base/Foundation/InteractiveExperiences 各自包目录下的 `license.txt` / 元数据。
- 项目地址：<https://github.com/microsoft/windowsappsdk>。

## 3. CommunityToolkit.Mvvm 8.4.2

- NuGet 包：`CommunityToolkit.Mvvm/8.4.2`。
- 许可证：**MIT**。包内 `License.md` 的版权行为
  “Copyright © .NET Foundation and Contributors”。
- 包内另有 `ThirdPartyNotices.txt`，列出 DeferredEvents、UWP 通知测试、
  MVVM Light 等第三方来源的通知；完整文本以该文件为准。
- 本地文件：`.tools/packages/communitytoolkit.mvvm/8.4.2/License.md`、
  `.tools/packages/communitytoolkit.mvvm/8.4.2/ThirdPartyNotices.txt`。
- 项目地址：<https://github.com/CommunityToolkit/dotnet>。

## 4. CPython 3.11.15

- 当前项目 `.venv` 的解释器为 **CPython 3.11.15 x64**（本机状态；脚本要求
  64 位、3.11 ≤ 版本 ≤ 3.13，自动发现优先 3.11/3.13）。
- 许可证：**Python Software Foundation License Version 2**（解释器安装目录内
  的 `LICENSE.txt`；当前环境对应 CPython 3.11.15 的许可证文本）。
- 分发状态：源码运行时不把 Python 复制进仓库；PyInstaller 构建的
  `WechatMemory.Worker.exe` 会携带 Python 运行时。若要分发该 worker，应把
  Python 的 `LICENSE.txt` 及其历史/第三方通知一并放入发布材料。
- 项目地址：<https://www.python.org/>。

## 5. PyInstaller 6.22.3

- 版本来源：`src/PythonWorker/requirements-build.txt` 固定
  `pyinstaller==6.22.3`；当前 `.venv` 的 `dist-info` 也是 **6.22.3**。
- 许可证：**GNU General Public License v2-or-later，带 bootloader 特殊例外**。
  包元数据原文：
  “GPLv2-or-later with a special exception which allows to use PyInstaller to
  build and distribute non-free programs (including commercial ones)”。
- 例外允许把编译后的 bootloader/相关文件链接或嵌入其他程序并分发，且不因这些
  文件触发 GPL 限制；但 PyInstaller 本身仍是 GPL，修改 PyInstaller 源码或分发
  其非 bootloader 部分仍需遵守 GPL。
- 本地文件：
  `.venv/Lib/site-packages/pyinstaller-6.22.3.dist-info/licenses/COPYING.txt`。
- 使用方式：仅构建期使用；`scripts/Publish.ps1` 以 `--onedir` 生成
  `worker/WechatMemory.Worker.exe` 与 `worker/_internal/`。构建依赖（如 altgraph 0.17.5 MIT、
  pefile 2024.8.26 MIT、pywin32-ctypes 0.2.3 BSD-3-Clause、packaging 26.4
  Apache-2.0 OR BSD-2-Clause、pyinstaller-hooks-contrib 2026.10）也有各自许可证；
  它们只在构建环境使用，正式分发前应按需审查。
- 项目地址：<https://pyinstaller.org/>。

## 6. wechat-ai-memory vendored 代码

- 来源：`https://github.com/HYJ1817/wechat-ai-memory`，commit
  `14d94db8b1affa12a2d7d88f0f830c1e0b8d752a`，tag `v0.3.8-alpha.2`。
- 许可证：**MIT**；版权行为
  “Copyright (c) 2026 WeChat AI Memory contributors”。
- vendored 目录/文件（均保留来源 commit、MIT 归属与许可证路径）：
  - `models.py`、`text_exporters.py`
  - `sources/base.py`、`wechat4_discovery.py`、`wechat4_crypto.py`、
    `wechat4_key_capture.py`、`wechat4_media.py`、`wechat4_voice.py`
  - `pdf_exporter.py`、`rendering/*`
  - `voice.py`
  - `jobs/*`、`scenes/*`、`delivery/*`
- 使用方式：worker 使用其微信 4.x 读取、图片/语音缓存、PDF 渲染、任务/场景/投递
  与数据模型；`worker.py` 与本项目新增的 `wechat_memory/*` 适配层不属于上游。
- 完整 MIT 文本：`LICENSES/wechat-ai-memory-LICENSE.txt`（上游 `LICENSE` 的
  逐字副本，保留版权行）。
- 上游 `THIRD_PARTY_NOTICES.md` 提到的 `tzwkb/wechat-decrypt`（MIT）与
  `fanyuantaier/wechatauto-replica`（Apache-2.0）与其微信 4.x/图片兼容代码相关；
  本仓库 vendored 的是上游仓库自身的文件，未单独复制这两个仓库。若上游文件内
  继承了额外条款，分发前应结合上游通知复核。

## 7. Microsoft .NET 运行时 10.0.15

- `Microsoft.NETCore.App.Runtime.win-x64/10.0.15` 与
  `Microsoft.WindowsDesktop.App.Runtime.win-x64/10.0.15` 的 NuGet 元数据
  均标注 **MIT**。
- `scripts/Publish.ps1` 使用 `--self-contained true`，因此发布目录包含 .NET
  运行时文件。
- 构建工具链本身为 **.NET SDK 10.0.404**（`global.json`）；SDK 包内
  `.tools/dotnet/LICENSE.txt` 是“MICROSOFT .NET LIBRARY”软件许可条款，属于
  构建期工具许可，不等同于运行时包的 MIT 标注。分发时以实际随应用复制的运行时
  文件及其包许可证为准。
- 项目地址：<https://dotnet.microsoft.com/>。

## 8. Microsoft WebView2 1.0.3719.77

- NuGet 包：`Microsoft.Web.WebView2/1.0.3719.77`（Windows App SDK 的传递依赖）。
- 许可证：包内 `LICENSE.txt` 为 Microsoft 提供的 BSD-3-Clause 风格许可
  （保留版权声明、二进制再分发需在文档/材料中重现声明、不得用 Microsoft 名称
  背书）。包内另有 `NOTICE.txt`。
- 本地文件：`.tools/packages/microsoft.web.webview2/1.0.3719.77/LICENSE.txt`
  与 `NOTICE.txt`。
- 发布目录包含 WebView2 相关 DLL；正式分发时应同时提供上述许可证/通知。

## 9. 构建工具与测试依赖

- `Microsoft.Windows.SDK.BuildTools/10.0.26100.4654` 与
  `Microsoft.Windows.SDK.BuildTools.MSIX/1.7.251221100`：仅构建期；许可证由
  NuGet 元数据指向 `https://aka.ms/WinSDKLicenseURL`。
- 测试依赖（不随应用分发）：
  - `xunit/2.9.3`：Apache-2.0；
  - `xunit.runner.visualstudio/4.0.1`：Apache-2.0；
  - `Microsoft.NET.Test.Sdk/18.12.0`：MIT。
  版本来自 `.tools/packages/*/*.nuspec`。

## 10. 分发注意事项

- `scripts/Publish.ps1` 复制本文件、`README.md`、`LICENSES/` 与 `docs/`，并收集
  CPython、PyInstaller 与已安装 Python 包的 `dist-info/licenses` 文本到
  `LICENSES/python-runtime/`；仍**不会**自动复制 NuGet 包、WebView2、FFmpeg 的
  全部许可证/通知文件。正式分发前，请按本文件列出的路径补充对应文本并保留版权行。
- 当前 `微信记忆App` 快照已补齐本文件、`README.md`、
  `LICENSES/` 与 `docs/`；以后重新运行 `Publish.ps1` 时会自动复制当时存在的文件。
- 本仓库根目录当前没有项目级 `LICENSE`；在确定项目自身许可证之前，不应把本
  仓库作为可再分发的完整产品发布。
- 若未来复用 DeskBox 代码，请先阅读 `reference/DeskBox/LICENSING.md`：其为
  GPL-3.0-only + 商业双许可，与 MIT 不兼容；当前仓库未复用其代码，故未附带其
  许可证。

## 11. Python 运行依赖与 FFmpeg

版本来自 `.venv` 的 `dist-info`（2026-11-27）：

| 包 | 版本 | 许可证 |
|---|---|---|
| frida | 17.28.0 | wxWindows Library Licence 3.1（LGPL 风格，带例外） |
| pycryptodome | 3.24.1 | BSD / Public Domain 组合 |
| zstandard | 0.25.0 | BSD-3-Clause |
| pywinauto | 0.6.9 | BSD-3-Clause |
| Pillow | 11.3.0 | MIT-CMU（HPND 风格） |
| reportlab | 4.5.1 | BSD-3-Clause 风格 |
| faster-whisper | 1.2.1 | MIT |
| silk-python / pysilk | 0.2.9 | BSD |
| imageio-ffmpeg | 0.8.1 | BSD-2-Clause |
| ctranslate2 | 4.9.1 | MIT |
| av | 18.1.1 | BSD-3-Clause |
| onnxruntime | 1.32.0 | MIT |
| numpy | 2.4.6 | BSD-3-Clause 及组合许可 |
| tokenizers / huggingface_hub | 0.23.3 / 2.5.0 | 见各自 `dist-info` 元数据 |
| comtypes | 1.5.0 | MIT |
| pywin32 | 312 | PSF |
| cffi | 2.1.1 | MIT-0 |

- 完整的包许可证文本由 `Publish.ps1` 收集到发布目录
  `LICENSES/python-runtime/<dist-info>/`。
- **FFmpeg**：`imageio-ffmpeg` 内置 `ffmpeg-win-x86_64-v9.0.2.exe`，版本字符串为
  `9.0.2-essentials_build-www.gyan.dev`。FFmpeg 是独立程序，按其自身许可证分发；
  gyan.dev 的 essentials 构建通常启用 GPL 组件（常见为 GPLv3），但必须以该二进制
  随附/发布页的许可证为准。**当前发布目录未附带 FFmpeg 的完整许可证文本，不能视为
  已完成再分发合规审查。** 若不能接受该许可证，可移除 `imageio_ffmpeg` 包并改用
  系统 FFmpeg，或只在内部使用、不再分发。
- 语音模型权重不随包分发；用户同意后从模型仓库下载，适用其各自许可证（当前默认
  faster-whisper `small` / Whisper 权重，需在分发前复核）。
