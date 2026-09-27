from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.batch_routes import router as batch_router
from app.api.detection_routes import router as detection_router
from app.api.export_routes import router as export_router
from app.api.llm_routes import router as llm_router
from app.api.ocr_routes import router as ocr_router
from app.api.reader_routes import router as reader_router
from app.api.render_routes import router as render_router
from app.api.routes import router
from app.api.translation_routes import router as translation_router
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.db.session import Database, optimize_database, run_migrations
from app.providers.detection.base import DetectionRuntime
from app.providers.detection.ultralytics_provider import UltralyticsDetectionRuntime
from app.providers.inpainting.base import InpaintingRuntime
from app.providers.inpainting.local_runtime import LocalInpaintingRuntime
from app.providers.llm.base import LLMRuntime
from app.providers.llm.http_runtime import HttpLLMRuntime
from app.providers.llm.secrets import SecretStore, WindowsCredentialStore
from app.providers.ocr.base import OCRRuntime
from app.providers.ocr.local_runtime import LocalOCRRuntime
from app.services.batch_tasks import BatchTaskCoordinator, get_performance_settings
from app.services.detection_models import DetectionModelCatalog
from app.services.detection_tasks import DetectionTaskManager
from app.services.export_tasks import ExportTaskManager
from app.services.feature_cleanup import finish_feature_cleanup
from app.services.launcher_session import LauncherSession
from app.services.ocr_tasks import OCRTaskManager
from app.services.render_tasks import RenderTaskManager
from app.services.storage import resolve_within
from app.services.tasks import ImportTaskManager, TaskBroadcaster
from app.services.translation_tasks import TranslationTaskManager

logger = logging.getLogger(__name__)


def create_app(
    config: Settings | None = None,
    detection_runtime: DetectionRuntime | None = None,
    ocr_runtime: OCRRuntime | None = None,
    llm_runtime: LLMRuntime | None = None,
    inpainting_runtime: InpaintingRuntime | None = None,
    secret_store: SecretStore | None = None,
) -> FastAPI:
    app_settings = config or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        startup_started = time.perf_counter()
        timings: dict[str, float] = {}
        app.state.startup_timings = timings
        app_settings.ensure_directories()
        configure_logging(app_settings)
        phase_started = time.perf_counter()
        await asyncio.to_thread(run_migrations, app_settings.database_path)
        # Alembic's fileConfig replaces root handlers and levels while a
        # migration runs. Restore the application file/progress configuration
        # before any recovered or newly queued task can emit lifecycle logs.
        configure_logging(app_settings)
        timings["migrations_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)
        db = Database(app_settings.database_path)
        performance = await get_performance_settings(db)
        # Persisted performance settings are the source of truth after a
        # restart.  Keep the existing manager constructors unchanged and
        # apply the loaded limits before any recovery worker is started.
        app_settings.ocr_concurrency = performance.ocr_concurrency
        app_settings.llm_concurrency = performance.llm_concurrency
        launcher = LauncherSession(
            app_settings.launcher_session_id,
            heartbeat_interval_seconds=app_settings.launcher_heartbeat_interval_seconds,
            heartbeat_timeout_seconds=app_settings.launcher_heartbeat_timeout_seconds,
        )
        broadcaster = TaskBroadcaster()
        manager = ImportTaskManager(db, app_settings, broadcaster)
        runtime = detection_runtime or UltralyticsDetectionRuntime()
        detection_catalog = DetectionModelCatalog(db, app_settings, runtime)
        detection_manager = DetectionTaskManager(db, app_settings, runtime, broadcaster)
        detection_manager.catalog = detection_catalog
        local_ocr_runtime = ocr_runtime or LocalOCRRuntime(app_settings.ocr_models_dir)
        ocr_manager = OCRTaskManager(db, app_settings, local_ocr_runtime, broadcaster)
        local_llm_runtime = llm_runtime or HttpLLMRuntime(
            max_retries=app_settings.llm_max_retries,
            retry_base_seconds=app_settings.llm_retry_base_seconds,
        )
        local_inpainting_runtime = inpainting_runtime or LocalInpaintingRuntime(
            app_settings.inpainting_models_dir
        )
        local_secret_store = (
            secret_store if secret_store is not None else WindowsCredentialStore.from_optional()
        )
        await finish_feature_cleanup(db, app_settings.data_dir, local_secret_store)
        translation_manager = TranslationTaskManager(
            db, app_settings, local_llm_runtime, local_secret_store, broadcaster
        )
        render_manager = RenderTaskManager(
            db, app_settings, local_inpainting_runtime, broadcaster
        )
        export_manager = ExportTaskManager(db, app_settings, broadcaster)
        batch_manager = BatchTaskCoordinator(
            db,
            app_settings,
            broadcaster,
            ocr_manager=ocr_manager,
            translation_manager=translation_manager,
            render_manager=render_manager,
        )
        batch_manager._semaphore = asyncio.Semaphore(performance.batch_concurrency)  # noqa: SLF001
        batch_manager.pipeline_window = performance.pipeline_window
        app.state.settings = app_settings
        app.state.launcher_session = launcher
        app.state.database = db
        app.state.task_manager = manager
        app.state.detection_task_manager = detection_manager
        app.state.detection_model_catalog = detection_catalog
        app.state.ocr_task_manager = ocr_manager
        app.state.llm_runtime = local_llm_runtime
        app.state.secret_store = local_secret_store
        app.state.translation_task_manager = translation_manager
        app.state.inpainting_runtime = local_inpainting_runtime
        app.state.render_task_manager = render_manager
        app.state.export_task_manager = export_manager
        app.state.batch_task_manager = batch_manager
        phase_started = time.perf_counter()
        await manager.start()
        await detection_manager.start()
        await ocr_manager.start()
        await translation_manager.start()
        await render_manager.start()
        await export_manager.start()
        await batch_manager.start()
        timings["recovery_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

        async def prepare_models() -> None:
            try:
                await detection_catalog.sync()
            except Exception:
                logger.exception("Background model inventory failed")
                detection_catalog.status = "failed"
                detection_catalog.error = "检测模型目录读取失败，请刷新设置页重试"

        async def maintain_database() -> None:
            try:
                await asyncio.to_thread(optimize_database, app_settings.database_path)
            except Exception:
                logger.warning("Deferred database optimization failed")

        preparation = asyncio.create_task(prepare_models(), name="startup-yolo")
        maintenance = asyncio.create_task(maintain_database(), name="database-optimize")
        timings["core_ms"] = round((time.perf_counter() - startup_started) * 1000, 1)
        logger.info("Startup timings: %s", timings)
        try:
            yield
        finally:
            preparation.cancel()
            await asyncio.gather(preparation, return_exceptions=True)
            await detection_catalog.close()
            await batch_manager.stop()
            await ocr_manager.stop()
            await detection_manager.stop()
            await manager.stop()
            await translation_manager.stop()
            await render_manager.stop()
            await export_manager.stop()
            await local_llm_runtime.close()
            await local_inpainting_runtime.close()
            await local_ocr_runtime.close()
            await runtime.close()
            await maintenance
            await db.dispose()

    app = FastAPI(
        title=app_settings.app_name,
        version=app_settings.version,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        error = {"code": exc.code, "message": exc.message}
        if exc.details:
            error["details"] = exc.details
        error["request_id"] = request.state.request_id
        return JSONResponse(status_code=exc.status_code, content={"success": False, "error": error})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "提交的数据不符合要求",
                    "details": {"fields": fields},
                    "request_id": request.state.request_id,
                },
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Provider responses, authorization headers and OCR text are not
        # safe to put into production logs.  Keep the diagnostic record
        # intentionally generic; the client still receives the request ID for
        # local correlation without exposing a traceback or exception body.
        logger.error(
            "Unhandled request error",
            extra={"request_id": getattr(request.state, "request_id", "-")},
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务暂时无法完成请求",
                    "request_id": getattr(request.state, "request_id", "-"),
                },
            },
        )

    app.include_router(router)
    app.include_router(detection_router)
    app.include_router(ocr_router)
    app.include_router(llm_router)
    app.include_router(translation_router)
    app.include_router(render_router)
    app.include_router(export_router)
    app.include_router(reader_router)
    app.include_router(batch_router)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str) -> FileResponse:
        dist = app_settings.frontend_dist
        if not (dist / "index.html").is_file():
            raise AppError("FRONTEND_NOT_BUILT", "前端尚未构建", status_code=404)
        requested = resolve_within(dist, full_path or "index.html")
        if full_path and requested.is_file():
            return FileResponse(requested)
        return FileResponse(dist / "index.html")

    return app


app = create_app()
