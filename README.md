# 漫画智能翻译

这是一个仅监听本机的漫画项目工作台，当前版本为 `0.9.0`。支持安全导入、YOLO 文本区域检测、MangaOCR/PaddleOCR 识别、LLM 文本翻译、本地图像修复与排版、批处理、阅读器和 ZIP/EPUB 导出。源文件始终原样复制到独立工作区。

## 快速开始与文档

新用户请先阅读[用户运行说明](docs/USER_GUIDE.md)，维护和升级请阅读[维护交接手册](docs/MAINTAINER_HANDOFF.md)；交给其他 coding agent 修改 bug 或新增业务时，请先阅读 [Agent 交接文档](docs/AGENT_HANDOFF.md) 和可直接复制的 [Agent 主提示词](docs/AGENT_HANDOFF_PROMPT.md)。下面是最短的本地启动路径：

依赖准备完成后，也可以直接双击项目根目录的 `启动项目.bat`。它会重新构建前端，启动受管的本地生产服务，并使用系统默认浏览器的普通窗口访问页面；默认优先使用 8000 端口，若被占用会自动选择后续可用的本机端口并让浏览器访问实际端口。关闭该页面后，前端心跳超时会停止本次启动的后端。该入口不会自动安装依赖、下载模型或调用第三方 API。

仓库中的模型文件由 Git LFS 管理。克隆项目后，先运行 `git lfs install` 和 `git lfs pull`，再按下文安装依赖。本地 `qa-original/` 包含个人测试文档，不纳入公开仓库；端到端测试使用仓库内的合成图片。

```powershell
conda env create -f environment.yml
conda activate manga-translator
pnpm --dir frontend install --frozen-lockfile
.\scripts\dev.ps1
```

开发地址为 `http://127.0.0.1:5173`，生产启动前先运行 `pnpm --dir frontend build`，再运行 `.\scripts\start.ps1`。应用只监听 `127.0.0.1`，启动时会自动迁移 SQLite；模型和 API Profile 必须由用户显式准备，不会静默联网。

## 书架与阅读器

只有项目全部有效页面拥有最新成品时才会进入书架。书架卡片显示成品封面、最近阅读页和“继续阅读”；阅读器采用独立 `/reader/<project-id>?page=<page-index>` 路由，支持上一页、下一页、页码跳转、键盘导航和全屏。阅读进度保存到 SQLite，成品图片按当前页前后两页预取，并通过容量为 8 的 Blob LRU 及时释放 Object URL。损坏或缺少成品的页会以页码占位显示。

## 批处理与任务中心

新建项目无需选择翻译模式，统一使用 OCR 文本翻译与本地修复/排版。项目页的“批量处理”依次执行 OCR、翻译、修复与成品生成。批量检测仍在检测阶段结束，不会自动串联 OCR。每页每个阶段都会立即落库，关闭应用后重启会从未完成阶段恢复，人工 OCR 与人工译文默认不会被覆盖。

升级时旧精翻项目会转为普通翻译项目：原图、检测框与 OCR 保留，旧译文（含人工修改）、人物和视觉资料、修复文件、成品及导出会清除。相关文件和视觉模型凭据由可重试的启动清理流程处理。

顶部“任务中心”提供轻量分页任务列表、当前页和阶段统计，并支持暂停、继续、取消和重试失败页面。暂停或取消会在当前模型推理/外部请求完成后生效；翻译只发送 OCR 文本。设置页的“任务并发”用于调整批处理窗口及 OCR/LLM 并发，YOLO、Big-LaMa 和渲染始终固定单实例。

## 配置图像修复与成品生成

应用不会在启动或生成期间下载修复模型。CPU 环境运行 `scripts/install-rendering-cpu.ps1`，NVIDIA 环境运行 `scripts/install-rendering-gpu.ps1`。需要 Big-LaMa 时显式运行：

```powershell
.\scripts\prepare-inpainting-model.ps1
```

Big-LaMa 固定保存在项目 `models\inpainting\big-lama.pt`，下载后会校验上游 MD5 并记录 SHA-256。项目页的“修复预览”与“生成当前页”只处理当前页；成品严格使用 EXIF 方向校正后的原图分辨率，写入项目 `masks`、`cache/inpainted` 和 `rendered` 子目录。`sfx_preserve` 区域不会被擦除或重新排版。

## 配置翻译

在“设置 → 翻译模型与 API”中显式添加 Profile 并保存 API Key。密钥只写入 Windows 凭据管理器，SQLite 和前端只保存末四位提示。OpenAI、DeepSeek、Claude、Gemini 会预填官方地址；Qwen 与通用兼容接口需要填写自己的 HTTPS Base URL。测试连接会发送一次最多 64 token 的真实请求。

翻译请求仅包含 OCR 文本和少量历史文本，不上传漫画图片。OCR 按源语言固定路由：日语使用 MangaOCR，韩文和英文使用 PaddleOCR。应用启动不会联网，也不会自动下载模型或调用 Provider。

翻译按页合并区域请求，并按 LLM Profile 的“最大并发”限制实际 HTTP 请求；不同 Profile 可以并行，同一 Profile 的正常请求、重试和异常拆分共享同一额度。LLM Runtime 会复用 HTTP 连接。翻译任务的 `result_json` 记录上下文准备、Profile 排队、Provider、写入、总耗时，以及请求次数、重试、协议修复、拆分次数和提示词字符数，便于定位 Provider 限流或响应缓慢。

显式配置密钥后，可按 Profile UUID 执行真实 Provider 验收：

```powershell
Push-Location backend
python scripts\validate_llm.py <profile-uuid>
Pop-Location
```

## 安装检测环境

激活项目的 Python 3.11 环境后，NVIDIA 显卡运行：

```powershell
.\scripts\install-detection-gpu.ps1
```

没有 NVIDIA 显卡时运行 `scripts\install-detection-cpu.ps1`。安装完成后，将 YOLO `.pt` 模型直接放入项目 `models\yolo\`；应用会自动扫描并真实加载验证，不修改源模型。设置页的导入按钮也会把模型保存到该目录。

## 安装 OCR 环境与模型

应用启动不会联网或自动下载 OCR 模型。NVIDIA 显卡运行：

```powershell
.\scripts\install-ocr-gpu.ps1
.\scripts\prepare-ocr-models.ps1 -Device auto
```

没有 NVIDIA 显卡时，将第一条替换为 `scripts\install-ocr-cpu.ps1`。所有 OCR 模型都会准备到项目 `models\ocr`。脚本会准备日语 MangaOCR、韩文 PaddleOCR 和英文 PaddleOCR 三个固定模型。

如果已有旧版本模型缓存，可显式执行 `scripts\migrate-models-to-project.ps1` 复制到项目目录；应用不会在运行时回退读取旧 AppData 缓存。

```powershell
.\scripts\prepare-ocr-models.ps1 -Provider all -Device auto
```

真实样本可使用 `backend\scripts\validate_ocr.py` 验收；例如在 backend 目录执行 `python scripts/validate_ocr.py mangaocr ja sample.png --device auto`。

页面级批量 OCR 可用 `backend\scripts\benchmark_ocr_page.py` 与旧逐框流程对比。区域文件是包含 `id`、`x1`、`y1`、`x2`、`y2` 的 JSON 数组；例如在 backend 目录执行 `python scripts/benchmark_ocr_page.py mangaocr ja sample.png regions.json --device auto --iterations 3`。脚本会校验识别文本一致性并输出中位耗时和加速倍数。

译文排版可用 `backend\scripts\benchmark_render_page.py` 对比旧逐区域整页合成与共享 RGBA 画布。区域文件是包含 `text`（或 `target_text`）及 `x1`、`y1`、`x2`、`y2` 的 JSON 数组；例如在 backend 目录执行 `python scripts/benchmark_render_page.py sample.png regions.json --iterations 5`。也可用 `--grid 20` 自动生成 20 个测试框。脚本会逐像素校验两条路径的输出并报告中位耗时和加速倍数。

## 环境

- Windows 10/11
- Python 3.11
- Node.js 20+
- pnpm 11.19.0（以 `frontend/package.json` 的 `packageManager` 为准）
- Google Chrome（用于 Playwright 本地验收）

## 开发启动

```powershell
conda env create -f environment.yml
conda activate manga-translator
pnpm --dir frontend install --frozen-lockfile
.\scripts\dev.ps1
```

前端开发地址为 `http://127.0.0.1:5173`，API 为 `http://127.0.0.1:8000`。

## 本地生产启动

```powershell
pnpm --dir frontend build
.\scripts\start.ps1
```

Windows 用户也可以双击项目根目录的 `启动项目.bat` 一键完成前端构建和本地生产启动。脚本默认优先使用 8000 端口，若端口已被占用会自动选择后续可用端口；系统默认浏览器会以普通窗口访问启动器实际选择的端口。关闭该页面后，前端心跳超时会停止本次启动的后端。若依赖尚未安装，先执行 `pnpm --dir frontend install --frozen-lockfile`；开发热更新仍使用 `.\scripts\dev.ps1`（命令前不要有多余空格）。

数据默认保存在 `%LOCALAPPDATA%\MangaTranslator`。如需调整，请在启动前设置 `MANGA_TRANSLATOR_DATA_DIR`。

## 验证

```powershell
.\scripts\check.ps1
```

该命令依次运行 Ruff、后端测试、前端单元测试、TypeScript 类型检查、生产构建和 Playwright 端到端冒烟测试；GitHub Actions 使用同一入口。
