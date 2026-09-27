from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.detection.ultralytics_provider import UltralyticsDetectionRuntime
from tests.helpers import wait_for_task
from tests.test_detection_api import FakeDetectionRuntime, _create_project, _settled_models


class CountingRuntime(FakeDetectionRuntime):
    def __init__(self):
        super().__init__()
        self.inspections = 0
        self.release = threading.Event()
        self.release.set()
        self.cancelled = False

    async def inspect(self, model_path: Path):
        self.inspections += 1
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            return await super().inspect(model_path)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_core_and_catalog_do_not_wait_for_model_validation(test_settings: Settings):
    test_settings.ensure_directories()
    (test_settings.yolo_models_dir / "slow.pt").write_bytes(b"fake model")
    runtime = CountingRuntime()
    runtime.release.clear()
    with TestClient(create_app(test_settings, detection_runtime=runtime)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/projects").status_code == 200
        state = client.get("/api/runtime/startup").json()["data"]
        assert state["core_status"] == "ready"
        assert state["yolo"]["status"] == "preparing"
        models = client.get("/api/detection-models").json()["data"]["items"]
        assert models[0]["status"] == "preparing"
        project_id, _ = _create_project(client)
        response = client.post(
            f"/api/projects/{project_id}/detection-tasks",
            json={
                "model_id": models[0]["id"],
                "start_page": 1,
                "end_page": 1,
                "confidence": 0.25,
                "image_size": 1280,
                "device": "auto",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "YOLO_MODEL_PREPARING"
        runtime.release.set()
        assert _settled_models(client).json()["data"]["items"][0]["status"] == "ready"
        assert runtime.inspections == 1


def test_validation_cache_survives_restart_and_invalidates_changes(test_settings: Settings):
    test_settings.ensure_directories()
    path = test_settings.yolo_models_dir / "cached.pt"
    path.write_bytes(b"fake model")
    first_id = None
    for expected in (1, 0, 1):
        runtime = CountingRuntime()
        with TestClient(create_app(test_settings, detection_runtime=runtime)) as client:
            model = _settled_models(client).json()["data"]["items"][0]
            assert model["status"] == "ready"
            assert runtime.inspections == expected
            if first_id is not None:
                assert model["id"] == first_id
            first_id = model["id"]
        if expected == 0:
            path.write_bytes(b"fake model")  # New mtime, same bytes still requires validation.


def test_shutdown_cancels_background_preparation(test_settings: Settings):
    test_settings.ensure_directories()
    (test_settings.yolo_models_dir / "slow.pt").write_bytes(b"fake model")
    runtime = CountingRuntime()
    runtime.release.clear()
    with TestClient(create_app(test_settings, detection_runtime=runtime)) as client:

        async def started():
            for _ in range(100):
                if runtime.inspections:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("validation never started")

        client.portal.call(started)
    assert runtime.cancelled


@pytest.mark.parametrize(
    "field,value",
    [
        ("validation_environment", "changed"),
        ("validation_version", 0),
        ("sha256", "x" * 64),
        ("class_names_json", "broken json"),
    ],
)
def test_invalid_cache_is_revalidated(test_settings: Settings, field: str, value):
    test_settings.ensure_directories()
    (test_settings.yolo_models_dir / "cached.pt").write_bytes(b"fake model")
    with TestClient(create_app(test_settings, detection_runtime=CountingRuntime())) as client:
        _settled_models(client)
    with closing(sqlite3.connect(test_settings.database_path)) as connection:
        connection.execute(f"UPDATE detection_models SET {field} = ?", (value,))
        connection.commit()
    runtime = CountingRuntime()
    with TestClient(create_app(test_settings, detection_runtime=runtime)) as client:
        assert _settled_models(client).json()["data"]["items"][0]["status"] == "ready"
        assert runtime.inspections == 1


def test_recovered_detection_waits_for_its_model(test_settings: Settings):
    test_settings.ensure_directories()
    path = test_settings.yolo_models_dir / "recovered.pt"
    path.write_bytes(b"fake model")
    with TestClient(create_app(test_settings, detection_runtime=CountingRuntime())) as client:
        model = _settled_models(client).json()["data"]["items"][0]
        project_id, _ = _create_project(client)
        # Persist a pending task without executing it in the first process.
        client.app.state.detection_task_manager.enqueue = lambda _: None
        response = client.post(
            f"/api/projects/{project_id}/detection-tasks",
            json={
                "model_id": model["id"],
                "start_page": 1,
                "end_page": 1,
                "confidence": 0.25,
                "image_size": 1280,
                "device": "auto",
            },
        )
        assert response.status_code == 202
        task_id = response.json()["data"]["task_id"]
    path.write_bytes(b"fake model")
    runtime = CountingRuntime()
    runtime.release.clear()
    with TestClient(create_app(test_settings, detection_runtime=runtime)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get(f"/api/tasks/{task_id}").json()["data"]["status"] == "pending"
        assert runtime.predicted == []
        runtime.release.set()
        assert wait_for_task(client, task_id)["status"] == "completed"
        assert len(runtime.predicted) == 1


def test_deleted_model_is_not_reloaded(test_settings: Settings):
    test_settings.ensure_directories()
    path = test_settings.yolo_models_dir / "removed.pt"
    path.write_bytes(b"fake model")
    runtime = CountingRuntime()
    with TestClient(create_app(test_settings, detection_runtime=runtime)) as client:
        model = _settled_models(client).json()["data"]["items"][0]
        path.unlink()
        missing = _settled_models(client).json()["data"]["items"][0]
        assert missing["id"] == model["id"]
        assert missing["status"] == "missing"
        assert runtime.inspections == 1


@pytest.mark.asyncio
async def test_closing_runtime_terminates_owned_worker():
    runtime = UltralyticsDetectionRuntime()
    runtime._executor.submit(time.sleep, 30)
    workers = list(runtime._executor._processes.values())
    assert workers
    await asyncio.wait_for(runtime.close(), timeout=10)
    assert all(not worker.is_alive() for worker in workers)
