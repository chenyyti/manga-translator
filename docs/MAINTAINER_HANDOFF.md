# 漫画智能翻译系统 0.9.0 维护交接手册

这份手册面向接手代码、排查线上本地实例或继续升级功能的维护者。当前稳定边界是 Phase 1–9；本文件不把未定义的 Phase 10 当作已实现功能。

如果任务交给自动化 coding agent 或需要一份可直接执行的修改流程，请先阅读 [Agent 交接文档](AGENT_HANDOFF.md)。

## 1. 当前状态与不可破坏的约束

### 版本和事实来源

- 当前版本：`0.9.0`。
- 数据库迁移链：`0001_phase1` → `0012_project_yolo_model_store` → `0015_remove_refined_translation`，应用启动时自动 `upgrade head`。
- 后端运行要求：Python `>=3.11,<3.13`；前端使用 Vue 3、TypeScript、Vite、pnpm lockfile。
- 本地生产服务：手动 `start.ps1` 固定监听 `127.0.0.1:8000`；受管一键入口优先使用 8000、被占用时自动选择后续可用端口；开发前端默认 `127.0.0.1:5173`。
 - 现有脚本是运行方式的来源：根目录 `启动项目.bat`，以及 `scripts/resolve-python.ps1`、`launcher.ps1`、`dev.ps1`、`start.ps1`、`check.ps1` 和各模型安装/准备脚本。

### 必须保持的架构不变量

1. 原图只读。所有缩略图、预览、裁剪、掩膜、修复图、成品和导出都写入项目工作区。
2. 数据库只存元数据和工作区相对路径；任何文件访问都先做项目目录 containment 校验。
3. 文件写入使用临时文件加原子重命名；`.part` 文件不得成为当前有效资产。
4. 页面级 geometry/OCR/translation/render revision 必须参与后台写回校验；旧任务结果不能覆盖更新后的人工结果。
5. API Key 只从 Windows 凭据管理器读取；SQLite、Pinia、日志和 API 响应不得出现明文密钥、Authorization 或完整 OCR/译文。
6. 外部模型和 Provider 不在应用启动时联网或自动下载；准备操作必须由用户显式执行。
7. 页面和任务元数据分页返回；前端图片使用 Blob Object URL 和容量为 8 的 LRU，淘汰、切换和卸载都要释放 URL。
8. 项目 ZIP/EPUB 导出不能跳过缺失、损坏或过期页面；历史导出保留，删除只清理文件并标记记录。

## 2. 代码地图

| 位置                                            | 责任                                                                               |
| ----------------------------------------------- | ---------------------------------------------------------------------------------- |
| `backend/app/main.py`                           | 创建 FastAPI、目录初始化、迁移/SQLite 优化、运行时和任务管理器生命周期、路由注册。 |
| `backend/app/api/`                              | REST/WebSocket 路由、统一响应 envelope、参数校验和安全文件响应。                   |
| `backend/app/api/schemas.py`                    | Pydantic 请求/响应模型及枚举边界。                                                 |
| `backend/app/db/models.py`                      | SQLAlchemy 模型和版本字段；图片/字体二进制不进入数据库。                           |
| `backend/app/db/session.py`                     | SQLite 连接 PRAGMA、异步会话、Alembic 自动迁移和数据库优化。                       |
| `backend/alembic/versions/`                     | 0001–0011 的线性迁移；新增迁移必须保持单一 head。                                  |
| `backend/app/providers/`                        | detection、ocr、llm、vlm、inpainting 的协议、注册表、HTTP 或本地运行时。           |
| `backend/app/services/`                         | 导入、检测、OCR、翻译、渲染、批处理、导出和任务广播/恢复。                    |
| `backend/app/core/`                             | Settings、错误类型、日志配置和工作区相关通用逻辑。                                 |
| `frontend/src/api/client.ts`                    | REST 客户端和二进制资产 URL；不得把 Blob/Base64 放入 Pinia。                       |
| `frontend/src/router/`、`views/`、`components/` | 书架、项目、阅读器、任务中心、设置及编辑器。                                       |
| `frontend/src/composables/`、`stores/`          | Blob LRU、WebSocket、撤销历史和轻量项目/任务状态。                                 |
| `scripts/`                                      | Windows 环境解析、启动、检查和显式模型安装/准备。                                  |

路由按领域拆分在 `detection_routes.py`、`ocr_routes.py`、`llm_routes.py`、`translation_routes.py`、`vlm_routes.py`、`render_routes.py`、`batch_routes.py`、`reader_routes.py` 和 `export_routes.py`。通用项目、页面、导入和任务接口在 `routes.py`。

## 3. 启动生命周期和任务恢复

### Windows 一键入口

依赖已准备完成后，可双击项目根目录的 `启动项目.bat`。它从自身所在目录运行，检查 `pnpm` 和前端依赖，重新执行生产构建，然后调用 `scripts/launcher.ps1`。受管启动器优先使用 8000，发现占用时自动选择后续可用端口，并把实际端口同时用于后端健康检查和浏览器地址；它还会创建一次性会话，在系统默认浏览器的普通窗口中打开页面，并通过前端心跳管理页面生命周期。页面失联后只停止本次启动的后端。该入口不创建 Conda 环境、不安装依赖、不下载模型，也不改变手动 `scripts/start.ps1` 的行为。

`create_app()` 的 lifespan 顺序是：

1. `Settings.ensure_directories()` 创建数据库、工作区、暂存、日志、模型、字体目录。
2. 在后台线程执行 `run_migrations()`，随后执行 `optimize_database()`。
3. 创建 SQLite `Database`、TaskBroadcaster、各 Provider runtime 和各领域 TaskManager。
4. 从持久化任务恢复导入、检测、OCR、翻译、渲染、批处理和导出；活动阶段按版本锁重新判断。
5. 启动 FastAPI 服务；退出时按相反方向停止管理器、关闭运行时和释放数据库连接。

`tasks`/`task_items` 保存历史和阶段计数，单页任务与批处理共享服务工厂。恢复时：运行中的任务回到待调度，处理中阶段重新排队，已完成阶段不重复执行；暂停/取消在当前模型推理或外部请求返回后生效。页面每完成一个独立阶段即提交，单页失败不应阻断其他页面。

导出任务使用 `export_artifacts` 和独立暂存目录。启动时会清理不再属于活动任务的导出暂存和残留 `.part`；同一项目同一格式只能有一个活动导出。项目删除前必须先取消活动任务，再验证路径并清理工作区。

## 4. 数据和安全边界

### 工作区布局

默认根目录是 `%LOCALAPPDATA%\MangaTranslator`，可由 `MANGA_TRANSLATOR_DATA_DIR` 覆盖：

```text
data\app.db
workspace\projects\<project-id>\
  original/ thumbnails/ preview/ masks/ crops/
  rendered/ export/ cache/ metadata/
staging/ logs/ models/ fonts/
```

数据库中保存项目相对路径和状态；服务层通过 `resolve_within()` 等 helper 将其解析到确切根目录，不能接受绝对路径、盘符、`..` 或跨项目路径。新增文件资产必须经过 containment、可读性和原子写入检查。

### API、日志和凭据

- REST 成功响应为 `{ "success": true, "data": ... }`；失败响应为 `success:false`、安全错误码、中文提示、可选安全 details 和 `request_id`。
- 中间件只接受 `127.0.0.1`、`localhost`、`testserver` Host，并限制开发 CORS 来源。
- 图片/导出响应提供私有缓存头、ETag 和条件请求；JSON 不嵌入 Base64。
- LLM 凭据目标名为 `MangaTranslator/llm/{profile_uuid}`；API 请求前实时从 SecretStore 读取。升级时旧视觉模型凭据由可重试的启动清理流程删除。
- 生产日志只记录请求 ID、任务 ID、阶段和安全错误码。禁止记录 API Key、Authorization、厂商错误正文、完整 OCR/译文或图片数据。

## 5. 数据库迁移和升级流程

应用启动会自动迁移，但维护者应在改动前后显式检查：

```powershell
Set-Location backend
python -m alembic current
python -m alembic heads
python -m alembic upgrade head
```

新增迁移时：

1. 先修改 `backend/app/db/models.py` 和对应 Pydantic schema，确认旧数据的默认值和可空策略。
2. 使用 `python -m alembic revision -m "short_description"` 生成新版本，保持唯一线性 `down_revision`。
3. 同时覆盖 `upgrade()` 和可验证的 `downgrade()`；涉及 SQLite 表重建时使用 Alembic batch 模式。
4. 在空数据库和至少一份旧版本 fixture 上分别运行升级测试；检查索引、外键、默认值和迁移后的状态聚合。
5. 发布前备份整个数据目录，停止应用后再复制数据库；不要在运行时复制 WAL 数据，也不要手工编辑 `alembic_version`。

版本变更至少同步 `backend/pyproject.toml`、`backend/app/core/config.py`、`frontend/package.json`、README 和用户/维护文档。前端 lockfile、CI 运行时和构建产物也要按依赖变化重新验证。

## 6. Provider 与任务扩展方式

新增 Provider 或流水线阶段时遵循以下顺序，避免把业务规则复制到路由：

1. 定义协议、Provider ID、能力描述和安全配置。
2. 在 `providers/<domain>/` 添加真实运行时；网络调用使用受控超时、TLS 校验、重试和响应 schema 校验。
3. 在数据库模型和 Alembic 中加入状态、版本和外键；在 `schemas.py` 加入请求/响应类型。
4. 把单页逻辑抽到共享 TaskManager/工厂，再由单页和批处理路由调用。
5. 写回时同时匹配区域/页面 ID、geometry、OCR、translation 或 render revision。
6. 增加假运行时测试、API 测试、恢复/取消/并发测试、前端按钮门控和 E2E 冒烟。
7. 最后更新 README、用户指南和本手册的能力边界。

当前固定阶段包括：导入、检测、OCR、普通翻译、修复、排版渲染、批处理、阅读和 ZIP/EPUB 导出。旧精翻项目在 `0015_remove_refined_translation` 迁移中转为普通翻译，清除旧译文、人物及视觉分析、成品和导出；原图、检测框与 OCR 保留。Phase 9 后没有既定 Phase 10；任何新功能先形成提案，明确数据、权限、任务、过期保护和测试范围后再实现。

## 7. 前端维护约定

- Pinia 只保存项目摘要、当前页、分页缓存、任务/导出轻量状态和 UI 状态；禁止保存图片 Blob、Base64、API Key 或完整项目高清数据。
- 页面、书架、任务中心和导出历史使用分页/虚拟列表；默认页面元数据批次不超过 50 条。
- 预览、成品和缩略图通过 Blob Object URL 加载。`useBlobLru` 容量固定为 8；切换页面/项目、路由离开、组件卸载和 LRU 淘汰都调用 `URL.revokeObjectURL`。
- WebSocket 连接先接收完整轻量快照，再应用增量事件；断线后重连并刷新终态数据，避免持续轮询。
- 后台任务进入完成、失败、取消或过期后，刷新当前页、缩略图、任务和项目摘要，但不能覆盖用户当前选择造成的竞态。

## 8. 测试、验收和本地数据

从仓库根目录运行统一检查：

```powershell
.\scripts\check.ps1
```

它会依次运行后端 Ruff/pytest、前端 Prettier、TypeScript、Vitest、生产构建和 Playwright。针对单个领域可使用：

```powershell
Set-Location backend
python -m pytest tests/test_phase9_export.py -q
python -m pytest tests/test_phase8_reader.py -q
Set-Location ..\frontend
pnpm format:check
pnpm typecheck
pnpm test
pnpm build
pnpm e2e
```

Provider 真实验收脚本（`validate_llm.py`、`validate_vlm.py`、`validate_ocr.py`、`validate_inpainting.py`、`validate_detection.py`）只有在用户显式提供模型或密钥时运行；CI 和普通回归使用假运行时，不能把收费 API 或大模型下载纳入默认检查。

测试会使用临时数据目录和 E2E 数据目录。清理前确认没有 pytest、Playwright 或 uvicorn 进程；不要删除用户默认数据目录来“修复”测试失败。迁移测试必须覆盖空库、旧库升级、约束、分页、ETag/304、任务恢复和 `.part` 清理。

## 9. 故障排查手册

| 问题           | 首先检查                                                                                             |
| -------------- | ---------------------------------------------------------------------------------------------------- |
| 应用启动失败   | `logs/app.log`、Python 版本、数据目录权限和 Alembic 当前版本；不要先删数据库。                       |
| 端口占用       | 一键入口会从 8000 开始自动选择后续可用端口；手动 `start.ps1` 固定使用 8000，开发前端使用 5173。       |
| 前端空白       | `frontend/dist/index.html`、构建输出和浏览器控制台；生产服务必须在仓库现状下读取 dist。              |
| 模型未就绪     | Provider 依赖版本、模型缓存路径、设置页 readiness；应用不负责自动下载。                              |
| 凭据失败       | Windows Credential Manager/keyring 是否可用；`SECRET_STORE_UNAVAILABLE` 时禁止绕过安全层写入数据库。 |
| 任务卡在处理中 | 先等待当前外部请求返回；再读取任务状态和日志，确认恢复逻辑没有重复阶段。                             |
| 成品/导出过期  | 检查页面 revision、文件 SHA-256、健康检查和是否存在 `.part`；旧成品应保留且不能被旧任务覆盖。        |
| SQLite locked  | 停止重复服务，等待 WAL 锁释放；保留 `app.db-wal`/`app.db-shm`，不要强删。                            |

对外报告问题时只提供版本、迁移 head、任务 ID、请求 ID、阶段和错误码。源图、数据库、日志原文和 Profile 密钥需要先脱敏。

## 10. 发布和交接清单

### 发布前

- [ ] 版本号在后端、前端、README 和文档中一致。
- [ ] 新迁移从当前 head 线性升级，空库和旧库测试通过。
- [ ] `.env.example` 只包含安全默认值，没有任何密钥。
- [ ] 运行 `.\scripts\check.ps1`，并记录测试时间、Python/Node/pnpm 版本。
- [ ] 生产前端重新 `pnpm --dir frontend build`，确认 dist 与 API 路由一致。
- [ ] 检查导入、任务恢复、阅读器、健康检查、PNG/JPG、ZIP/EPUB 和删除清理。
- [ ] 明确模型缓存是否已准备；不要在发布脚本中加入静默联网。

### 交接时记录

```text
应用版本：
Alembic head：
Python / Node / pnpm：
数据目录（只记录逻辑位置，不提交用户绝对路径）：
已准备的模型 Provider：
活动任务和导出：
最近一次完整检查时间及结果：
已知问题 / 临时规避：
下一阶段提案：
```

交接文档更新应与行为变化同一变更提交；如果命令、默认值、状态码、页面名称或安全边界改变，先更新本手册和用户指南，再更新 README 的快速开始。
