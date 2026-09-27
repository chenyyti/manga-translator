# 漫画智能翻译系统 Agent 交接文档

本文面向接手本仓库的 coding agent、自动化修复 agent 和新功能开发 agent。目标是让下一位 agent 在不破坏用户数据、安全边界和既有任务状态的前提下，能够快速定位问题、实现改动并完成验证。

维护细节和数据库迁移说明请同时阅读 [维护交接手册](MAINTAINER_HANDOFF.md)；面向最终用户的操作步骤请阅读 [用户运行说明](USER_GUIDE.md)。如果需要一段可直接复制给新 agent 的完整指令，请使用 [Agent 主提示词](AGENT_HANDOFF_PROMPT.md)。本文是 agent 的工作协议，不把尚未实现的后续阶段当作现有功能。

## 1. 基线事实

- 当前稳定版本：`0.9.0`，覆盖 Phase 1–9。
- 数据库迁移 head：`0015_remove_refined_translation`。应用启动时会自动升级到 head。
- 后端：Python 3.11（3.13 不是项目运行环境）、FastAPI、SQLAlchemy 2、SQLite、Alembic、Pillow。
- 前端：Vue 3、TypeScript、Vite、Vue Router、Pinia、Element Plus、Axios、pnpm lockfile。
- Windows 本地服务：FastAPI `127.0.0.1:8000`，开发前端 `127.0.0.1:5173`。
- 数据目录默认是 `%LOCALAPPDATA%\MangaTranslator`，可用 `MANGA_TRANSLATOR_DATA_DIR` 覆盖。
- 本仓库不是联网服务。应用启动不会下载模型、调用 LLM 或上传图片；这些动作必须由用户显式准备或触发。
- 原图始终只读。当前版本提供导入、检测、OCR、普通翻译、修复排版、批处理、书架阅读器、健康检查和 PNG/JPG/ZIP/EPUB 导出；新项目直接使用普通翻译，前端路由为 `/projects/:id`。OCR 路由按源语言固定为日语 MangaOCR、韩文/英文 PaddleOCR。

未实现或不应假设已实现的能力包括：桌面安装包、自动模型下载、源文件导出、双页/连续阅读、EPUB 编辑器、批量 JPG、全项目以外的新增阅读模式，以及尚未定义的 Phase 10 业务。

## 2. 接手任务的第一步

每个 agent 在修改前按以下顺序做一次短检查：

1. 阅读用户请求，判断是 bug、业务功能、文档还是运行环境问题，并确认是否涉及外部 API、模型或用户数据。
2. 从仓库根目录检查文件结构和现有变更。不要运行 `git reset --hard`、`git checkout --` 或递归删除来“清理环境”；不要覆盖用户已有的未提交修改。
3. 阅读本文件和相关领域的现有测试；优先复现一个最小案例，再改动代码。
4. 确认使用的是 Python 3.11 和仓库锁定的 pnpm 版本。PowerShell 脚本会自动寻找 `manga-translator` 环境，但直接运行 Python 时仍应确认版本。
5. 先做最小、可回滚的实现。后端规则放在共享 service/task 工厂中，路由只做校验和调度；不要在单页和批处理路由各复制一份业务逻辑。
6. 运行目标领域测试，再运行完整检查。若环境问题导致某项无法运行，记录实际命令、错误和未覆盖范围，不要伪造通过结果。
7. 最终交接说明必须写出改动文件、行为变化、测试结果、已知风险和下一步建议。

## 3. Windows 启动与验证

从仓库根目录打开 PowerShell：

```powershell
conda env create -f environment.yml       # 首次使用
conda activate manga-translator
pnpm --dir frontend install --frozen-lockfile
```

如果当前 PowerShell 尚未初始化 Conda，先执行 `conda init powershell`，关闭并重新打开 PowerShell，再激活环境。若脚本执行策略阻止本地脚本，只对当前窗口临时放行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

开发模式：

```powershell
.\scripts\dev.ps1
```

开发页面为 `http://127.0.0.1:5173`，API 为 `http://127.0.0.1:8000`。生产模式：

```powershell
pnpm --dir frontend build
.\scripts\start.ps1
```

需要“关闭应用页面后结束本次后端”时，可直接双击根目录的 `启动项目.bat`。它会构建前端并调用 `scripts/launcher.ps1`：启动器优先使用 8000，若被占用会自动选择后续可用端口；同时使用一次性会话、系统默认浏览器的普通窗口和前端心跳。页面失联后只停止它自己启动的后端。普通 `start.ps1` 仍是固定 8000 的手动生产模式，不启用受管生命周期。

生产脚本会等待 `GET /api/health` 成功后才打开 `http://127.0.0.1:8000`。接口文档为 `http://127.0.0.1:8000/api/docs`。开发脚本、生产脚本和受管启动器只监听环回地址。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

如果提示端口占用，先查看 PID，再确认是本项目的旧 uvicorn/Vite 进程后关闭：

```powershell
Get-NetTCPConnection -LocalPort 8000,5173 -State Listen
Get-Process -Id <PID>
Stop-Process -Id <PID>
```

不要把带前导反斜杠的 `\scripts\dev.ps1` 当作仓库脚本；它表示当前盘符根目录。必须在仓库根目录使用 `.\scripts\dev.ps1`（命令前没有多余空格）。PowerShell 5.1 需要脚本保持 UTF-8 BOM；修改包含中文的 `.ps1` 后要重新做语法检查。

## 4. 代码地图

### 后端

| 路径 | 责任 |
| --- | --- |
| `backend/app/main.py` | FastAPI 创建、目录初始化、迁移、SQLite 优化、runtime/task manager 生命周期和恢复。 |
| `backend/app/core/config.py` | `Settings`、数据目录和并发/大小/重试限制。 |
| `backend/app/core/errors.py` | 统一安全错误类型、错误码和响应。 |
| `backend/app/core/logging.py` | request ID 日志过滤、文件日志和不可写目录时的安全回退。 |
| `backend/app/db/models.py` | SQLAlchemy 模型、状态字段、版本字段和外键。 |
| `backend/app/db/session.py` | SQLite PRAGMA、异步 session、Alembic 自动迁移和恢复入口。 |
| `backend/alembic/versions/` | `0001_phase1` 到 `0015_remove_refined_translation` 的线性迁移。 |
| `backend/app/api/schemas.py` | Pydantic 请求/响应、状态枚举和边界校验。 |
| `backend/app/api/routes.py` | 导入、项目、页面、通用任务和运行信息。 |
| `backend/app/api/detection_routes.py` | YOLO 模型、检测区域、复核和检测任务。 |
| `backend/app/api/ocr_routes.py` | OCR Provider、设置、区域校对和 OCR 任务。 |
| `backend/app/api/llm_routes.py`、`translation_routes.py` | LLM Profile、密钥生命周期和快速翻译。 |
| `backend/app/api/render_routes.py` | 修复、排版、字体和当前页成品。 |
| `backend/app/api/batch_routes.py` | 批处理预检、创建、暂停、继续、取消和失败重试。 |
| `backend/app/api/reader_routes.py` | 书架可读性、阅读分页、阅读进度和封面。 |
| `backend/app/api/export_routes.py` | PNG/JPG、ZIP/EPUB、导出历史和健康检查。 |
| `backend/app/providers/` | detection、ocr、llm、vlm、inpainting 协议、注册表和真实/假 runtime。 |
| `backend/app/services/launcher_session.py` | 受管启动器的一次性会话和内存心跳状态；不写入数据库。 |
| `backend/app/services/` | 导入、检测、OCR、翻译、渲染、批处理、导出、存储和任务队列。 |

### 前端

| 路径 | 责任 |
| --- | --- |
| `frontend/src/views/` | `BookshelfView`、`ProjectsView`、`NewProjectView`、`ProjectView`、`ReaderView`、`TaskCenterView`、`SettingsView`、`CharactersView`。 |
| `frontend/src/api/client.ts` | REST 请求、统一 envelope 处理和二进制 asset URL。 |
| `frontend/src/composables/useLauncherHeartbeat.ts` | 受管启动时发现会话并定时发送心跳；手动启动模式自动停用。 |
| `frontend/src/router/` | 页面路由，阅读器为 `/reader/:projectId?page=<page_index>`。 |
| `frontend/src/stores/` | 项目、任务、导出和 UI 的轻量 Pinia 状态。 |
| `frontend/src/composables/useBlobLru.ts` | 容量为 8 的 Blob Object URL 缓存和释放。 |
| `frontend/src/composables/useTaskSocket.ts` | 单任务/全局任务 WebSocket、重连和终态刷新。 |
| `frontend/src/components/` | 项目卡片、页面编辑器、任务进度、分析 Drawer 和设置控件。 |
| `frontend/src/test/` | Vitest setup 和前端测试。 |

## 5. 不可破坏的架构与安全约束

这些约束优先级高于方便实现。新代码违反任一项都应视为阻塞问题：

1. **源文件只读**：导入后原图写入项目 `original/`，预览、缩略图、裁剪、掩膜、修复图、成品和导出写入工作区，绝不改用户源目录。
2. **数据库不存图片**：SQLite 只保存元数据、状态、版本和工作区相对路径。接口不返回绝对路径、Base64 或完整图片内容。
3. **路径 containment**：所有来自数据库、请求或模型的路径先经过 `resolve_within()`；拒绝绝对路径、盘符、`..`、跨项目路径和符号链接逃逸。文件写入前后都保持项目目录边界。
4. **原子文件**：写入使用 `.part` 临时文件、flush/fsync 和原子 rename。当前有效路径只能指向完整文件；任务取消或失败后不留下可被误读的 `.part`。
5. **版本过期保护**：后台写回必须匹配页面/区域 ID 以及对应 geometry、OCR、translation、render revision 和输入哈希。用户的人工编辑、bbox 变化和较新任务始终优先，旧结果只能计为 skipped/outdated。
6. **统一响应**：成功返回 `{success:true,data:...}`；失败返回 `{success:false,error:{code,message,details?,request_id}}`。生产响应不返回堆栈。
7. **本机边界**：FastAPI 只监听 `127.0.0.1`；Host、CORS、静态资源和下载接口都不能放开局域网访问。
8. **密钥安全**：LLM API Key 只进 Windows Credential Manager/SecretStore，SQLite 仅保存末四位提示；禁止进入 Pinia、LocalStorage、日志、异常、测试快照或响应。
9. **内容脱敏**：日志只记录 request ID、task ID、阶段和安全错误码；不得记录 Authorization、厂商错误正文、完整 OCR/译文或图片数据。
10. **显式联网**：模型准备脚本和真实 Provider 调用必须由用户明确执行。默认测试使用假 runtime，不在启动、迁移、普通页面浏览时联网。
11. **前端轻量状态**：Pinia 只放项目摘要、分页缓存、当前页、任务/导出元数据和 UI 状态；Blob、高清图片、Base64、字体二进制和 API Key 不持久化。
12. **URL 生命周期**：预览、修复图、成品和缩略图通过 Blob Object URL 加载；切换页/项目、路由离开、组件卸载和 LRU 淘汰时调用 `URL.revokeObjectURL()`。

## 6. 领域状态与任务规则

检测完成后页面可以进入 OCR、文本翻译、修复和排版流程。项目创建时直接使用普通翻译，服务端拒绝旧精翻模式请求。修复、排版和生成只写工作区。

任务状态为 `pending | running | pausing | paused | completed | failed | cancelled`；部分页面失败时应保留成功页并用安全错误码标记失败项。暂停/取消在当前模型推理或外部请求返回后生效，不能粗暴终止进程或回滚已完成阶段。进程重启时：运行任务回待调度，处理中阶段重新排队，已完成阶段复用，且同一页面同一阶段最多一个有效子任务。

现有任务类型包括：

- 导入、检测、OCR、`quick_translation`。
- `page_repair`、`page_render`、`batch_pipeline`。
- `export_zip`、`export_epub`。

任务事件先发完整轻量快照，再发增量事件；前端断线后重连并刷新终态。200 页项目的列表、任务项、阅读页和导出历史必须分页，默认批次不超过 50 条。

## 7. 工作区和数据操作

逻辑工作区布局：

```text
%LOCALAPPDATA%\MangaTranslator\
  data\app.db (+ app.db-wal / app.db-shm)
  workspace\projects\<project-id>\
    original\ thumbnails\ preview\ masks\ crops\ rendered\ export\ cache\ metadata\
  staging\ logs\ fonts\

项目根目录的 `models\` 保存随项目分发的 OCR、Big-LaMa 与 YOLO 模型：

```text
models\ocr\ models\inpainting\ models\yolo\ MODEL_MANIFEST.json
```
```

`MANGA_TRANSLATOR_DATA_DIR` 覆盖整个根目录。数据库中的路径必须是相对于该根目录或项目工作区的安全路径；不要把用户机器的绝对路径写入 API、日志、测试快照或文档。

需要备份或迁移时先停止所有 uvicorn、Vite、pytest、Playwright 和模型进程，再复制整个数据目录。不要在 SQLite 运行时只复制 `app.db`，也不要手工删除 `-wal`、`-shm` 或编辑 `alembic_version`。项目/导出删除优先使用 API；确需清理残留文件时只操作已确认属于目标项目的目录。

## 8. Bug 修复流程

### 8.1 后端/API bug

1. 用临时 `MANGA_TRANSLATOR_DATA_DIR` 或 pytest fixture 复现，避免直接破坏用户默认数据。
2. 从响应的 `request_id`、错误码、任务 ID 和阶段定位；不要把生产堆栈返回给前端。
3. 沿着 schema → router → service/task → storage/provider 的调用链确认边界，修复共享层而不是给一个页面打补丁。
4. 涉及状态变化时增加并发/恢复测试：旧任务结果不能覆盖人工编辑或新版本。
5. 涉及文件时验证 containment、原子 rename、源图字节不变、失败后的旧成品保留和 `.part` 清理。
6. 检查异常是否被统一错误处理转换，日志是否脱敏。

上传导入 500 的特殊检查项：`relative_to_root()` 和 `resolve_within()` 必须同时保持安全 containment 与 Windows 重解析/沙箱路径兼容；修改后要重启旧 uvicorn 进程，否则浏览器仍会请求旧代码。

### 8.2 前端 bug

1. 先确认 API envelope、字段命名、分页和状态枚举与 `schemas.py`/路由一致。
2. 图片只通过 Blob LRU 读取；检查 abort、URL revoke、缓存淘汰和组件卸载。
3. 按钮门控必须反映真实状态（已复核、模型就绪、版本未过期、Profile 可用等），不能仅隐藏错误。
4. WebSocket 终态刷新不能覆盖用户当前选择；翻页使用 `router.replace`，阅读进度保存失败不应阻止本地翻页。
5. 敏感表单（尤其 API Key）提交后立即清空，不进入 Pinia 或 LocalStorage。

### 8.3 运行环境 bug

- `conda activate` 报错：执行 `conda init powershell` 后重开 PowerShell；确认 `python --version` 是 3.11.x。
- `pnpm` 找不到：确认 Node.js 已安装，然后执行 `npm install --global pnpm@11.19.0` 并重新打开终端。
 - `scripts\dev.ps1` 找不到：先 `Set-Location` 到仓库根目录，再用 `.\scripts\dev.ps1`（实际命令不要有前导空格或前导反斜杠）。
- PowerShell 中文解析错误：脚本必须是 UTF-8 BOM；用 Windows PowerShell 5.1 的 parser 检查全部 `scripts/*.ps1`。
- 浏览器无法连接：先请求 `/api/health`，再检查 8000/5173 是否有旧进程或脚本是否正在等待迁移。
- 导入接口 500：确认后端已重启到最新代码；检查日志中的 request ID/错误码，不要只重复上传。
- SQLite 只读或 locked：停止重复服务并检查数据目录权限，保留 WAL 文件；不要删除数据库作为第一反应。

## 9. 新业务扩展顺序

任何新功能都按以下顺序实现，避免路由和单页/批处理逻辑分叉：

1. **边界设计**：写清状态机、权限/本地边界、输入上限、失败行为、取消/恢复规则和版本过期规则。
2. **Schema 与迁移**：更新 `models.py`、`schemas.py` 和 Alembic。迁移必须从当前唯一 head 线性升级，旧数据有明确默认值；SQLite 表结构变化使用 batch 模式。
3. **Provider/runtime**：协议、Provider ID、能力描述和依赖隔离；外部 HTTP 使用 HTTPS、TLS 校验、无跨域重定向、有限超时和退避；本地模型不自动下载。
4. **Service/Task**：把业务规则放入共享 service/task 工厂，持久化每个阶段和版本锁；支持取消、重启恢复、幂等写回和资源限流。
5. **Router**：只负责认证/参数/路径校验、调用共享 service 和 envelope；二进制响应加 ETag、`Cache-Control: private` 和安全文件名。
6. **Frontend**：更新 API 类型、Pinia 轻量状态、页面门控、WebSocket 终态刷新、Blob URL 释放和错误提示。
7. **测试**：先假 runtime 单测，再 API/迁移/并发/恢复测试，最后 Vitest、E2E 和构建检查。真实 Provider/模型验收只能在用户显式配置后运行。
8. **文档**：如果命令、默认值、状态码、路由或安全边界变化，同步 README、用户指南和维护文档。

新增数据库迁移前后可显式检查：

```powershell
Set-Location backend
python -m alembic current
python -m alembic heads
python -m alembic upgrade head
```

不要在运行中的用户数据库上试验 downgrade。发布前在空库和旧库 fixture 上分别验证升级、索引、外键、默认值、分页和恢复。

## 10. 测试命令

完整检查（推荐交接前执行）：

```powershell
.\scripts\check.ps1
```

后端单独执行：

```powershell
Set-Location backend
python -m ruff check --no-cache app tests scripts
python -m pytest
```

常用目标测试：

```powershell
python -m pytest tests/test_storage.py -q
python -m pytest tests/test_api_import.py -q
python -m pytest tests/test_recovery.py -q
python -m pytest tests/test_phase7_smoke.py tests/test_phase8_reader.py tests/test_phase9_export.py -q
```

前端单独执行：

```powershell
Set-Location frontend
pnpm format:check
pnpm typecheck
pnpm test
pnpm build
pnpm e2e
```

Windows PowerShell 5.1 脚本语法检查：

```powershell
$files = Get-ChildItem scripts -Filter *.ps1
foreach ($file in $files) {
  $tokens = $null
  $errors = $null
  [System.Management.Automation.Language.Parser]::ParseFile(
    $file.FullName, [ref]$tokens, [ref]$errors
  ) > $null
  if ($errors.Count -gt 0) { throw "parse failed: $($file.Name)" }
}
```

测试输出不得包含真实 API Key、Authorization、用户源图、数据库或完整 OCR/译文。Provider、OCR、LaMa 和检测的真实验收脚本只有在用户显式准备模型/密钥后运行；默认 CI 使用注入的假 runtime。

## 11. 交接报告模板

完成一个 bug 修复或新业务后，在最终消息中至少给出：

```text
目标：
根因/设计：
修改文件：
数据迁移：无 / <迁移名称>
API 或页面变化：
安全与过期保护：
测试命令及结果：
未覆盖/已知风险：
用户下一步：
```

如果任务未完成，不要写“已修复”；明确阻塞点、已尝试的安全检查和需要用户提供的最小信息。对外只分享版本、迁移 head、任务/请求 ID、阶段和错误码，不分享绝对路径、源图、数据库、日志原文或密钥。

## 12. 交接前检查清单

- [ ] 未修改用户源文件，所有新资产都在项目工作区且使用原子写入。
- [ ] 数据库只保存相对路径；新增路径经过 containment；没有残留 `.part`。
- [ ] 旧任务、人工 OCR/译文、bbox 和较新版本不会被后台结果覆盖。
- [ ] API 继续使用统一 envelope、request ID 和脱敏错误。
- [ ] 8000/5173 仍只监听 `127.0.0.1`，没有静默联网或自动下载。
- [ ] Pinia 没有新增 Blob、Base64、字体二进制或密钥；Object URL 在所有淘汰路径释放。
- [ ] 迁移保持单一 head，空库和旧库升级都通过。
- [ ] 目标测试、完整 `scripts/check.ps1`（或明确记录未通过原因）已执行。
- [ ] README、USER_GUIDE 和 MAINTAINER_HANDOFF 与实际行为一致。
- [ ] 最终报告包含改动、测试、风险和用户下一步。
