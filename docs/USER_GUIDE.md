# 漫画智能翻译系统 0.9.0 用户运行说明

这份说明面向第一次运行本项目的用户。它只覆盖 Windows 本地版本 0.9.0；如果你需要修改代码、迁移数据库或增加 Provider，请阅读[维护交接手册](MAINTAINER_HANDOFF.md)。

## 1. 你将得到什么

当前版本是一套只监听本机的漫画翻译工作台，功能按阶段逐步完成：

- 导入单张、多张、文件夹或 ZIP 图片，并在独立工作区中保留源文件。
- 使用已准备的 YOLO、MangaOCR、PaddleOCR、LLM 和本地图像修复运行时完成检测、识别、翻译、修复与排版。
- 通过批处理任务中心处理约 200 页项目，支持暂停、继续、取消、失败重试和重启恢复。
- 在书架和单页阅读器中查看最新 PNG 成品，保存阅读进度并选择封面。
- 下载当前页 PNG/JPG，或创建 ZIP/EPUB 3 Fixed Layout 项目导出；健康检查会在归档导出前指出缺页和过期页。

应用不会修改源文件，不会在启动或任务期间静默下载模型，也不会把漫画图片上传给外部服务。执行翻译时只向所选 LLM Provider 发送 OCR 文本。

## 2. 运行前准备

### 必需环境

| 组件       | 要求                                                                |
| ---------- | ------------------------------------------------------------------- |
| 操作系统   | Windows 10/11                                                       |
| Python     | 3.11.x；3.13 不作为本项目运行环境                                   |
| Node.js    | 20 或更高版本                                                       |
| pnpm       | 推荐 11.19.0（以 `frontend/package.json` 的 `packageManager` 为准） |
| PowerShell | Windows PowerShell 5+ 或 PowerShell 7                               |
| 浏览器     | Chrome、Edge 或其他现代浏览器                                       |

GPU 不是必需条件。没有 NVIDIA GPU 时使用 CPU 安装脚本；有兼容 CUDA 的 NVIDIA GPU 时，可按 GPU 脚本安装并在设置中确认实际设备。

### 创建环境

在仓库根目录打开 PowerShell，执行：

```powershell
conda env create -f environment.yml
conda activate manga-translator
python --version       # 应显示 3.11.x
pnpm --version
pnpm --dir frontend install --frozen-lockfile
```

如果 PowerShell 拒绝执行本地脚本，只对当前窗口临时放宽策略，不要为了运行项目永久修改系统策略：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

`scripts/resolve-python.ps1` 会优先使用当前的 Python 3.11，再尝试查找名为 `manga-translator` 的 Conda 环境。若提示找不到 Python，请先激活环境或重新执行 `conda env create -f environment.yml`。

### 配置数据目录（可选）

默认数据目录为：

```text
%LOCALAPPDATA%\MangaTranslator
```

如需把数据放到其他磁盘，在启动前设置环境变量：

```powershell
$env:MANGA_TRANSLATOR_DATA_DIR = 'D:\MangaTranslatorData'
```

也可以复制 `.env.example` 为 `.env`，再填写同名变量。`.env` 只放本地运行参数，绝不要放 API Key。修改环境变量后必须重新启动后端才会生效。

## 3. 启动和停止

### 一键启动（Windows）

完成环境创建和前端依赖安装后，可直接双击项目根目录的 `启动项目.bat`。脚本会：

1. 检查 `pnpm`、前端目录和已安装的依赖。
2. 重新构建前端生产文件。
3. 优先使用 8000 端口启动本地生产服务；若 8000 已被占用，会自动选择后续可用端口，并等待实际端口的 `/api/health` 成功。
4. 使用系统默认浏览器的普通窗口访问启动器实际选择的本机地址。

一键入口不会自动创建 Conda 环境、安装依赖、下载模型或调用第三方 API。缺少依赖时，先在项目根目录执行：

```powershell
pnpm --dir frontend install --frozen-lockfile
```

关闭启动窗口或按 `Ctrl+C` 会停止本地服务。
关闭应用页面后，前端心跳超时会让启动器停止本次启动的后端；关闭浏览器中的其他页面不会影响服务。普通 `start.ps1` 也使用默认浏览器，但不启用这套受管生命周期。

### 开发模式

开发模式启动 Vite 前端和带热重载的 FastAPI 后端：

```powershell
.\scripts\dev.ps1
```

浏览器访问 `http://127.0.0.1:5173`。前端进程占用当前窗口，后端在隐藏窗口中运行；按 `Ctrl+C` 结束前端时，脚本会尝试停止它启动的后端。

### 本地生产模式

生产模式由 FastAPI 托管已经构建的前端：

```powershell
pnpm --dir frontend build
.\scripts\start.ps1
```

脚本会启动 `http://127.0.0.1:8000` 并打开默认浏览器。若提示 `frontend/dist 不存在`，先完成前端构建。关闭运行窗口或按 `Ctrl+C` 会停止本地服务。

两个模式都只监听环回地址，不接受局域网连接。接口文档在 `http://127.0.0.1:8000/api/docs`；开发模式下 API 请求由 Vite 代理到同一台机器的 8000 端口。

应用首次启动会自动执行 Alembic 迁移并优化 SQLite，不需要手工创建数据库。请不要在服务运行时复制或删除 `data/app.db`、`data/app.db-wal` 和 `data/app.db-shm`。

## 4. 第一次使用的推荐顺序

### 4.1 准备检测模型

在项目根目录按硬件选择一个脚本：

```powershell
# CPU
.\scripts\install-detection-cpu.ps1

# NVIDIA GPU（二选一，不要同时执行）
.\scripts\install-detection-gpu.ps1
```

安装后将 `.pt` 检测模型直接放入项目 `models\yolo\` 目录，或在“设置 → 运行、检测与 OCR”使用导入按钮。应用会自动扫描并真实加载验证模型，不会修改源模型。没有可用检测模型时，检测按钮会保持禁用。

### 4.2 准备 OCR（需要 OCR 时）

```powershell
# CPU
.\scripts\install-ocr-cpu.ps1
.\scripts\prepare-ocr-models.ps1 -Provider all -Device cpu

# NVIDIA GPU
.\scripts\install-ocr-gpu.ps1
.\scripts\prepare-ocr-models.ps1 -Provider all -Device auto
```

脚本会准备全部三个固定模型：日语 MangaOCR、韩文 PaddleOCR、英文 PaddleOCR。模型位于项目目录的 `models\ocr`；脚本由用户显式执行，应用不会自动联网下载。项目会根据源语言自动选择对应 Provider，设置页只允许调整运行设备。

### 4.3 准备图像修复（需要生成成品时）

```powershell
# 安装 OpenCV/渲染依赖
.\scripts\install-rendering-cpu.ps1
# 或：.\scripts\install-rendering-gpu.ps1

# 可选：显式准备 Big-LaMa 模型
.\scripts\prepare-inpainting-model.ps1
```

FAST 和 OpenCV 可在没有 Big-LaMa 的情况下工作。AUTO 或 LaMa 运行时缺失时会按界面提示回退到 OpenCV；Big-LaMa 固定保存在项目目录的 `models\inpainting`，不会写回源图。

### 4.4 配置翻译 Profile（可选）

在“设置”中：

1. 在“翻译模型与 API”添加 LLM Profile。
2. 输入 API Key 后保存；密钥只存入 Windows 凭据管理器，SQLite 和界面只显示末四位遮罩。
3. 点击“测试连接”前确认费用和网络策略。测试会发送一次很小的真实请求，不会把密钥写入日志。

Qwen 与通用兼容 Provider 需要填写自己的 HTTPS Base URL。凭据存储不可用时，应用仍可启动，但禁止保存、测试或使用对应 Profile。

### 4.5 创建和处理项目

1. 在“我的书架”点击“开始翻译”，填写项目名称、源语言（日文/韩文/英文）和目标语言（简体中文）。无需选择翻译模式。
2. 项目提供 YOLO 检测、OCR、文本翻译、目标文本回填和本地修复/排版。
3. 选择单张、多张、文件夹或 ZIP 图片后创建项目。上传期间可以取消；原文件会复制到项目工作区，源目录不会被修改。
   后台默认同时处理 2 页，以兼顾导入速度和电脑响应；需要调整时可在启动前设置 `MANGA_TRANSLATOR_IMPORT_PAGE_CONCURRENCY` 为 1–4。
4. 等待导入任务完成，再进入项目页执行检测。检测完成后保存检测框，系统不会自动串联 OCR。
5. 对已复核页面执行当前页 OCR；必要时在右侧面板人工校对原文。移动或缩放检测框后，旧 OCR 会显示为过期，保存并重新复核后再识别。
6. 翻译只发送 OCR 文本；批量处理依次执行 OCR、文本翻译和成图。
7. 在“修复预览”确认擦除效果，再点击“生成当前页”。非 `sfx_preserve` 区域必须拥有有效中文译文；成品严格使用 EXIF 校正后的原始分辨率 PNG。
8. 所有有效页面拥有最新成品后，项目进入书架。可在阅读器翻页、恢复最近阅读位置、选择封面，并在项目页执行健康检查和导出。

## 5. 数据、备份与清理

升级到移除精翻的版本时，旧精翻项目会自动改为普通翻译。原图、检测框和 OCR 保留；旧译文（包括人工译文）、人物与视觉分析资料、修复图、成品、封面和导出会清除，需要重新翻译和成图。普通翻译项目的现有成果不受影响。

### 目录说明

项目模型目录与默认数据目录分别包含：

```text
data\app.db                    SQLite 元数据（运行时还可能有 -wal/-shm）
workspace\projects\<project>\  每个项目的 original、thumbnails、preview、masks、
                               crops、rendered、export、cache、metadata
staging\                       上传和导出暂存区
logs\app.log                   应用日志（不记录密钥和正文）
  fonts\                         已验证的字体文件

项目根目录下还包含：

```text
models\ocr\                   OCR 模型
models\inpainting\            Big-LaMa 模型
models\yolo\                  用户提供的 YOLO .pt 模型
models\MODEL_MANIFEST.json    内置模型文件清单
```
```

数据库只保存工作区相对路径，不保存图片二进制。项目页、阅读器和导出均执行路径 containment 检查。

### 备份和恢复

1. 停止开发或生产服务，确认没有任务正在写入。
2. 复制整个 `MANGA_TRANSLATOR_DATA_DIR` 目录到备份介质；不要只复制运行中的 `app.db`。
3. 迁移到新机器时恢复目录，并在启动前设置相同的 `MANGA_TRANSLATOR_DATA_DIR`。
4. Windows 凭据管理器中的 API Key 不在数据库备份内，换机后需要重新输入 Profile 密钥。

应用启动会自动向前迁移数据库。升级前应先备份；不要直接降级程序或手工删除迁移记录。

### 安全清理

优先在界面删除项目或导出记录。不要用通配符删除整个数据根目录；如果必须清理残留 `.part` 文件，先停止应用并只处理明确属于本项目的暂存目录。删除项目不可恢复，备份请在删除前完成。

## 6. 常见问题

| 现象                           | 处理方式                                                                                        |
| ------------------------------ | ----------------------------------------------------------------------------------------------- |
| 提示找不到 Python 3.11         | 激活 `manga-translator` 环境；确认 `python --version` 为 3.11.x。                               |
| 脚本被 PowerShell 拒绝         | 在当前窗口执行 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`，无需修改系统策略。 |
| 8000 或 5173 端口被占用        | 一键入口会从 8000 开始自动选择后续可用端口；手动 `scripts/start.ps1` 仍使用固定的 8000，开发脚本仍使用 5173。 |
| 生产启动提示前端未构建         | 执行 `pnpm --dir frontend build`，确认 `frontend/dist/index.html` 存在。                        |
| 检测/OCR/修复按钮不可用        | 在设置中导入模型并确认“已就绪”；依赖安装和模型准备都不会由应用自动完成。                        |
| OCR 或 LaMa 模型准备失败       | 检查磁盘空间、网络和 Python 环境；重新显式运行对应准备脚本。已有项目不会因此修改源图。          |
| 无法保存或测试 LLM Profile | 检查 Windows 凭据管理器/keyring 服务和当前用户权限；凭据库不可用时应用会拒绝涉及密钥的操作。    |
| 任务显示暂停或重启后继续       | 任务中心会从最后一个已完成阶段恢复；暂停/取消要等当前模型推理或外部请求返回后才生效。           |
| 项目无法 ZIP/EPUB 导出         | 打开“项目健康检查”，修复缺失、损坏或过期成品；归档导出不会跳过问题页。                          |
| 阅读器显示缺页占位             | 该页没有最新 rendered PNG。页码仍保留；回到项目页重新生成后即可恢复。                           |
| SQLite locked 或启动迁移失败   | 先停止重复运行的后端进程，等待文件锁释放；不要删除 `-wal/-shm`，保留日志后再重试。              |

日志默认位于数据目录的 `logs\app.log`。提交问题时只提供时间、请求 ID、任务 ID 和安全错误码，不要上传数据库、源图或包含密钥的配置。

## 7. 当前边界

- 只支持本机 Windows 运行，不监听局域网，也没有桌面安装壳。
- 模型和第三方 API 都必须由用户显式准备或配置；应用启动不联网。
- 源文件只读；当前页成品为无损 PNG，另提供当前页 JPG 和项目 ZIP/EPUB 导出。
- 阅读器是单页模式，不提供原图切换、连续滚动、双页模式或 EPUB 编辑器。
- 当前版本没有定义新的 Phase 10 业务功能；后续改造请先阅读维护交接手册并形成独立提案。
