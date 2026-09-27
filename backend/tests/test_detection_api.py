from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.detection.base import (
    DetectionModelInfo,
    DetectionPrediction,
    DetectionRuntimeInfo,
)
from app.services.detection_tasks import filter_detection_output
from tests.helpers import image_bytes, wait_for_task


class FakeDetectionRuntime:
    def __init__(self) -> None:
        self.predicted: list[tuple[Path, Path, str]] = []

    async def status(self) -> DetectionRuntimeInfo:
        return DetectionRuntimeInfo("test", "test", False, None, None)

    async def inspect(self, model_path: Path) -> DetectionModelInfo:
        assert model_path.read_bytes() == b"fake model"
        return DetectionModelInfo(
            class_names={0: "text_region"},
            framework_version="test",
            task_name="detect",
        )

    async def predict(
        self,
        model_path: Path,
        image_path: Path,
        *,
        confidence: float,
        image_size: int,
        device: str,
    ) -> DetectionPrediction:
        assert confidence == 0.25
        assert image_size == 1280
        self.predicted.append((model_path, image_path, device))
        return DetectionPrediction(
            regions=[
                {
                    "class_id": 0,
                    "class_name": "text_region",
                    "confidence": 0.91,
                    "x1": 4.0,
                    "y1": 5.0,
                    "x2": 40.0,
                    "y2": 60.0,
                }
            ],
            actual_device="cpu",
        )

    async def close(self) -> None:
        return None


@pytest.fixture
def detection_client(test_settings: Settings) -> Iterator[tuple[TestClient, FakeDetectionRuntime]]:
    runtime = FakeDetectionRuntime()
    with TestClient(create_app(test_settings, detection_runtime=runtime)) as client:
        yield client, runtime


def _create_project(client: TestClient) -> tuple[str, str]:
    created = client.post(
        "/api/import-sessions",
        json={
            "project_name": "检测测试",
            "source_language": "ja",
            "target_language": "zh-CN",
            "translation_mode": "quick",
            "source_type": "single",
        },
    ).json()["data"]
    uploaded = client.post(
        f"/api/import-sessions/{created['id']}/files",
        data={"relative_path": "第1页.png"},
        files={"file": ("第1页.png", image_bytes((80, 100)), "image/png")},
    )
    assert uploaded.status_code == 201
    committed = client.post(f"/api/import-sessions/{created['id']}/commit").json()["data"]
    assert wait_for_task(client, committed["task_id"])["status"] == "completed"
    page = client.get(f"/api/projects/{committed['project_id']}/pages").json()["data"]["items"][0]
    return committed["project_id"], page["id"]


def _settled_models(client: TestClient):
    # Lists are nonblocking now; await the managed preparation explicitly.
    client.portal.call(client.app.state.detection_model_catalog.wait_ready)
    return client.get("/api/detection-models")


def _detection_settings(default_model_id: str | None) -> dict[str, object]:
    return {
        "default_model_id": default_model_id,
        "device": "auto",
        "confidence": 0.25,
        "image_size": 1280,
    }


def test_detection_output_keeps_overlapping_text_boxes_for_two_class_model() -> None:
    regions = [
        {
            "class_id": 0,
            "class_name": "text",
            "x1": 20,
            "y1": 20,
            "x2": 40,
            "y2": 40,
        },
        {
            "class_id": 0,
            "class_name": "text",
            "x1": 80,
            "y1": 80,
            "x2": 100,
            "y2": 100,
        },
        {
            "class_id": 1,
            "class_name": "balloon",
            "x1": 10,
            "y1": 10,
            "x2": 60,
            "y2": 60,
        },
    ]

    filtered = filter_detection_output(regions, '{"0":"text","1":"balloon"}')

    assert filtered == [regions[0]]


def test_detection_output_preserves_models_without_balloon_class() -> None:
    regions = [{"class_id": 0, "class_name": "text_region"}]

    assert filter_detection_output(regions, '{"0":"text_region"}') == regions


def test_model_detection_edit_review_and_delete(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
) -> None:
    client, runtime = detection_client
    uploaded = client.post(
        "/api/detection-models",
        data={"name": "对话框检测"},
        files={"file": ("comic.pt", b"fake model", "application/octet-stream")},
    )
    assert uploaded.status_code == 201, uploaded.text
    model = uploaded.json()["data"]
    assert model["class_names"] == {"0": "text_region"}
    assert client.get("/api/settings/detection").json()["data"]["default_model_id"] == model["id"]

    project_id, page_id = _create_project(client)
    started = client.post(
        f"/api/projects/{project_id}/detection-tasks",
        json={
            "model_id": model["id"],
            "start_page": 1,
            "end_page": 1,
            "confidence": 0.25,
            "image_size": 1280,
            "device": "auto",
            "overwrite": False,
        },
    )
    assert started.status_code == 202, started.text
    task = wait_for_task(client, started.json()["data"]["task_id"])
    assert (task["status"], task["completed"], task["failed"]) == ("completed", 1, 0)
    assert runtime.predicted

    document = client.get(f"/api/pages/{page_id}/regions").json()["data"]
    assert document["revision"] == 1
    assert document["regions"][0]["class_name"] == "text_region"
    region = document["regions"][0]
    region["x1"] = 8
    region["is_manual_edited"] = True
    saved = client.put(
        f"/api/pages/{page_id}/regions",
        json={"expected_revision": 1, "regions": [region]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["revision"] == 2

    conflict = client.put(
        f"/api/pages/{page_id}/regions",
        json={"expected_revision": 1, "regions": [region]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "REGION_REVISION_CONFLICT"

    reviewed = client.post(f"/api/pages/{page_id}/review")
    assert reviewed.status_code == 200
    assert reviewed.json()["data"]["detection_status"] == "detected"
    assert reviewed.json()["data"]["reviewed_at"] is None

    removed = client.delete(f"/api/detection-models/{model['id']}")
    assert removed.status_code == 200
    assert client.get("/api/detection-models").json()["data"]["items"] == []
    preserved = client.get(f"/api/pages/{page_id}/regions").json()["data"]
    assert len(preserved["regions"]) == 1
    assert preserved["regions"][0]["model_id"] is None


def test_project_yolo_files_are_scanned_and_deletable(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
    test_settings: Settings,
) -> None:
    client, _runtime = detection_client
    model_path = test_settings.yolo_models_dir / "manual-detector.pt"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"fake model")

    first = _settled_models(client)
    assert first.status_code == 200
    models = first.json()["data"]["items"]
    assert len(models) == 1
    assert models[0]["filename"] == "manual-detector.pt"
    assert models[0]["source"] == "project"
    assert models[0]["storage_scope"] == "project"
    assert models[0]["relative_path"] == "yolo/manual-detector.pt"
    assert models[0]["readonly"] is False

    second = client.get("/api/detection-models")
    assert len(second.json()["data"]["items"]) == 1
    removed = client.delete(f"/api/detection-models/{models[0]['id']}")
    assert removed.status_code == 200
    assert not model_path.exists()
    assert _settled_models(client).json()["data"]["items"] == []
    assert client.get("/api/settings/detection").json()["data"]["default_model_id"] is None


def test_project_yolo_scan_selects_first_ready_model_as_default(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
    test_settings: Settings,
) -> None:
    client, _runtime = detection_client
    model_path = test_settings.yolo_models_dir / "manual-detector.pt"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"fake model")

    response = _settled_models(client)
    assert response.status_code == 200
    model = response.json()["data"]["items"][0]

    settings = client.get("/api/settings/detection")
    assert settings.status_code == 200
    assert settings.json()["data"]["default_model_id"] == model["id"]


def test_detection_default_can_be_selected_and_reassigned_after_deletion(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
) -> None:
    client, _runtime = detection_client
    first = client.post(
        "/api/detection-models",
        data={"name": "模型一"},
        files={"file": ("one.pt", b"fake model", "application/octet-stream")},
    ).json()["data"]
    second = client.post(
        "/api/detection-models",
        data={"name": "模型二"},
        files={"file": ("two.pt", b"fake model", "application/octet-stream")},
    ).json()["data"]

    selected = client.put("/api/settings/detection", json=_detection_settings(second["id"]))
    assert selected.status_code == 200, selected.text
    assert selected.json()["data"]["default_model_id"] == second["id"]

    removed_non_default = client.delete(f"/api/detection-models/{first['id']}")
    assert removed_non_default.status_code == 200, removed_non_default.text
    assert client.get("/api/settings/detection").json()["data"]["default_model_id"] == second["id"]

    third = client.post(
        "/api/detection-models",
        data={"name": "模型三"},
        files={"file": ("three.pt", b"fake model", "application/octet-stream")},
    ).json()["data"]
    removed_default = client.delete(f"/api/detection-models/{second['id']}")
    assert removed_default.status_code == 200, removed_default.text
    assert client.get("/api/settings/detection").json()["data"]["default_model_id"] == third["id"]

    assert client.delete(f"/api/detection-models/{third['id']}").status_code == 200
    assert client.get("/api/settings/detection").json()["data"]["default_model_id"] is None


def test_invalid_project_yolo_file_is_reported_without_startup_failure(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
    test_settings: Settings,
) -> None:
    client, _runtime = detection_client
    model_path = test_settings.yolo_models_dir / "invalid.pt"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"not a model")

    response = _settled_models(client)
    assert response.status_code == 200
    model = next(item for item in response.json()["data"]["items"] if item["filename"] == "invalid.pt")
    assert model["status"] == "invalid"
    assert model["readonly"] is False
    assert model["error_message"]
    selected = client.put("/api/settings/detection", json=_detection_settings(model["id"]))
    assert selected.status_code == 409
    assert selected.json()["error"]["code"] == "YOLO_MODEL_UNAVAILABLE"


def test_missing_project_yolo_file_cannot_be_selected(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
    test_settings: Settings,
) -> None:
    client, _runtime = detection_client
    model_path = test_settings.yolo_models_dir / "missing.pt"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"fake model")
    model = _settled_models(client).json()["data"]["items"][0]

    model_path.unlink()
    missing = _settled_models(client).json()["data"]["items"][0]
    assert missing["status"] == "missing"
    selected = client.put("/api/settings/detection", json=_detection_settings(model["id"]))
    assert selected.status_code == 409
    assert selected.json()["error"]["code"] == "YOLO_MODEL_UNAVAILABLE"


def test_project_yolo_scan_ignores_subdirectories_and_updates_replaced_file(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
    test_settings: Settings,
) -> None:
    client, _runtime = detection_client
    model_path = test_settings.yolo_models_dir / "replace.pt"
    model_path.write_bytes(b"fake model")
    nested_path = test_settings.yolo_models_dir / "nested" / "ignored.pt"
    nested_path.parent.mkdir(parents=True, exist_ok=True)
    nested_path.write_bytes(b"fake model")
    temporary_path = test_settings.yolo_models_dir / "partial.pt.part"
    temporary_path.write_bytes(b"fake model")

    first = _settled_models(client).json()["data"]["items"]
    assert [item["filename"] for item in first] == ["replace.pt"]
    first_sha256 = first[0]["sha256"]

    model_path.write_bytes(b"new model!")
    second = _settled_models(client).json()["data"]["items"]
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["sha256"] != first_sha256


def test_runtime_settings_expose_project_model_directories(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
    test_settings: Settings,
) -> None:
    client, _runtime = detection_client
    runtime = client.get("/api/settings/runtime").json()["data"]
    assert runtime["model_source"] == "project"
    assert runtime["models_dir"] == str(test_settings.models_dir)
    assert runtime["ocr_models_dir"] == str(test_settings.ocr_models_dir)
    assert runtime["inpainting_models_dir"] == str(test_settings.inpainting_models_dir)
    assert runtime["yolo_models_dir"] == str(test_settings.yolo_models_dir)


def test_detection_task_explains_where_to_put_a_missing_project_model(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
) -> None:
    client, _runtime = detection_client
    project_id, _page_id = _create_project(client)
    response = client.post(
        f"/api/projects/{project_id}/detection-tasks",
        json={
            "model_id": "missing-model",
            "start_page": 1,
            "end_page": 1,
            "confidence": 0.25,
            "image_size": 1280,
            "device": "auto",
            "overwrite": False,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "YOLO_MODEL_REQUIRED"
    assert "models/yolo" in response.json()["error"]["message"]


def test_manual_region_bounds_are_validated(
    detection_client: tuple[TestClient, FakeDetectionRuntime],
) -> None:
    client, _runtime = detection_client
    _project_id, page_id = _create_project(client)
    response = client.put(
        f"/api/pages/{page_id}/regions",
        json={
            "expected_revision": 0,
            "regions": [
                {
                    "id": "manual-1",
                    "class_id": 0,
                    "class_name": "text_region",
                    "x1": 4,
                    "y1": 5,
                    "x2": 1000,
                    "y2": 60,
                    "source": "manual",
                }
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REGION"
