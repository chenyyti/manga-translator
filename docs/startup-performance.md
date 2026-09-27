# 启动提速与验证

## 使用

继续双击 `启动项目.bat`，或运行 `scripts/launcher.ps1` / `scripts/start.ps1`。
三个入口共用前端输入指纹判断。需要强制重建时使用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/launcher.ps1 -ForceRebuild
```

首次构建成功后保存 `frontend/dist/build-inputs.json`。源码、公共资源、HTML、依赖清单、锁文件、TypeScript/Vite 配置、`.env*` 或 `VITE_*` 环境变量变化（包括文件删除）会使缓存失效；产物缺失或缓存损坏也会重建。缓存有效时不检查 pnpm 和 node_modules。构建失败会中止本次启动。

Python 优先级为显式 `MANGA_TRANSLATOR_PYTHON`、已验证路径缓存、PATH、Conda 环境。缓存保存在当前用户 LocalAppData/MangaTranslator/launcher；缓存不可写不会阻止已验证解释器启动。

## 核心服务与模型分离

- `/api/health` 继续兼容，成功代表核心服务和数据库就绪。
- `/api/runtime/startup` 只读取内存中的核心状态、迁移/恢复耗时和 YOLO 准备状态，不加载模型、不探测 CUDA。
- YOLO 校验由受管理的后台任务串行执行；模型列表请求只做轻量目录登记，返回 `preparation` 和各模型状态。
- `0014_detection_validation_cache` 保留原模型 ID，新增纳秒修改时间、环境指纹和校验版本。路径、大小、mtime、环境和版本一致时复用成功结果，不重新计算 SHA-256 或加载模型。
- 检测模型准备中时，相关检测操作不可用；已有检测框的 OCR、翻译和成图照常工作。恢复的检测任务等待对应模型；无效模型有独立错误状态。
- 上传先写入不参与扫描的暂存目录，校验后原子发布并登记，防止列表扫描重复登记。
- 校验不持有长期数据库事务；写入前再次核对文件。关闭时取消后台任务并终止所属模型工作进程；启动器退出时清理自身后端进程树。
- OCR/修复模型不预加载，不联网下载，不改变独立批量检测与 OCR 起始的批量处理流程。

## 本机对比（2026-09-18）

使用真实项目 YOLO 模型、临时数据库，同一 Python 进程内连续创建/关闭应用 5 次。首轮创建迁移，后续复用同一临时数据库。**下表不是双击到窗口可操作的总时间**；Python 模块导入、浏览器启动和前端加载单独计量。

| 核心健康接口就绪耗时（毫秒） | 第1次 | 第2次 | 第3次 | 第4次 | 第5次 | 中位数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 优化前 | 5175 | 3948 | 3853 | 3901 | 4409 | 3948 |
| 优化后 | 1480 | 161 | 136 | 141 | 135 | 141 |

优化后 YOLO 就绪耗时分别为 5410、216、190、197、190 毫秒（从对应轮应用创建起算）。首次模型校验仍需时间，但不再挡住书架。

前端实际构建约 22.6 秒；随后 5 次有效缓存检查为 96、77、76、67、72 毫秒，中位数 76 毫秒。首页主 JS 从约 1.327 MB 降至 1.128 MB，约 198 KB 的 VueKonva/Konva 分块延后至编辑路由加载。

## 复测

```powershell
conda run -n manga-translator python backend/scripts/benchmark_startup.py --runs 5
powershell -ExecutionPolicy Bypass -File scripts/test-frontend-cache.ps1
```

启动器输出构建、Python 定位、核心 HTTP 就绪和前端心跳连接耗时。基准脚本单独输出后端模块导入耗时。书架数据加载并完成 Vue 更新后记录浏览器 Performance 标记 `bookshelf-ready`，可在开发者工具中读取 `performance.getEntriesByName('bookshelf-ready')`；心跳连接不等同于书架可操作。

测试覆盖慢校验不阻塞核心服务、准备中拒绝检测、缓存跨重启复用、文件变化/删除、损坏缓存、环境变化、任务恢复及关闭取消。前端测试验证准备期间轮询、完成/离开后停止；E2E 验证书架不请求 Konva，并覆盖现有检测、翻译与成图流程。

Windows 下 Playwright 需要权限清理其创建的进程树。受限沙箱曾出现全部用例通过但清理不退出；使用允许清理本次测试子进程的权限重跑后正常退出，无需修改产品或跳过测试。
