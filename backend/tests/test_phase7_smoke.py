from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.llm.base import LLMProviderInfo, TranslationResult
from app.providers.llm.secrets import MemorySecretStore
from tests.helpers import image_bytes, wait_for_task


class FakeLLMRuntime:
    async def providers(self) -> list[LLMProviderInfo]:
        return [LLMProviderInfo("openai", "Fake LLM", ("ja",), "https://example.test", True, "test", "chat")]

    async def translate_page(self, profile, payload):
        return TranslationResult(
            {region.region_id: f"译文-{region.region_id}" for region in payload.regions},
            profile.provider,
            profile.model,
        )

    async def test_connection(self, profile):
        return {"provider": profile.provider, "model": profile.model}

    async def close(self):
        return None


@pytest.fixture
def llm_client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project"),
            llm_runtime=FakeLLMRuntime(),
            secret_store=MemorySecretStore(),
        )
    ) as client:
        yield client


def _create_project(
    client: TestClient,
    *,
    llm_id: str | None = None,
    vlm_id: str | None = None,
    translation_mode: str = "quick",
) -> tuple[str, str]:
    created = client.post(
        "/api/import-sessions",
        json={
            "project_name": "Phase 7",
            "source_language": "ja",
            "target_language": "zh-CN",
            "translation_mode": translation_mode,
            "source_type": "single",
            "ocr_provider": "auto",
            "llm_profile_id": llm_id,
            "vlm_profile_id": vlm_id,
        },
    ).json()["data"]
    client.post(
        f"/api/import-sessions/{created['id']}/files",
        data={"relative_path": "1.png"},
        files={"file": ("1.png", image_bytes((100, 120)), "image/png")},
    )
    committed = client.post(f"/api/import-sessions/{created['id']}/commit").json()["data"]
    assert wait_for_task(client, committed["task_id"])["status"] == "completed"
    page = client.get(f"/api/projects/{committed['project_id']}/pages").json()["data"]["items"][0]
    assert client.put(
        f"/api/pages/{page['id']}/regions",
        json={
            "expected_revision": 0,
            "regions": [
                {
                    "id": "r1",
                    "class_id": 0,
                    "class_name": "text",
                    "x1": 5,
                    "y1": 5,
                    "x2": 80,
                    "y2": 30,
                    "source": "manual",
                }
            ],
        },
    ).status_code == 200
    assert client.post(f"/api/pages/{page['id']}/review").status_code == 200
    assert client.put(
        "/api/regions/r1/ocr-text", json={"text": "こんにちは", "expected_ocr_revision": 0}
    ).status_code == 200
    return committed["project_id"], page["id"]


def _profile(client: TestClient, path: str) -> str:
    return client.post(
        path,
        json={
            "name": "p",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "model": "m",
            "api_key": "key",
        },
    ).json()["data"]["id"]


def _create_raw_project(client: TestClient, page_count: int = 1) -> tuple[str, list[dict[str, object]]]:
    created = client.post(
        "/api/import-sessions",
        json={
            "project_name": "Batch split",
            "source_language": "ja",
            "target_language": "zh-CN",
            "translation_mode": "quick",
            "source_type": "multiple" if page_count > 1 else "single",
            "ocr_provider": "auto",
        },
    ).json()["data"]
    for index in range(1, page_count + 1):
        filename = f"{index}.png"
        response = client.post(
            f"/api/import-sessions/{created['id']}/files",
            data={"relative_path": filename},
            files={"file": (filename, image_bytes((100, 120)), "image/png")},
        )
        assert response.status_code == 201, response.text
    committed = client.post(f"/api/import-sessions/{created['id']}/commit").json()["data"]
    assert wait_for_task(client, committed["task_id"])["status"] == "completed"
    pages = client.get(f"/api/projects/{committed['project_id']}/pages").json()["data"]["items"]
    return committed["project_id"], pages


def test_batch_quick_has_persisted_parent_and_stages(llm_client: TestClient) -> None:
    llm_id = _profile(llm_client, "/api/llm-profiles")
    project_id, page_id = _create_project(llm_client, llm_id=llm_id)
    mismatch = llm_client.post(
        f"/api/projects/{project_id}/batch-tasks/preview",
        json={"translation_mode": "refined", "run_ocr": False, "run_render": False},
    )
    assert mismatch.status_code == 422
    visual = llm_client.post(f"/api/pages/{page_id}/vlm")
    assert visual.status_code in {404, 405}
    preview = llm_client.post(
        f"/api/projects/{project_id}/batch-tasks/preview",
        json={"run_ocr": False, "run_render": False, "llm_profile_id": llm_id},
    )
    assert preview.status_code == 200, preview.text
    created = llm_client.post(
        f"/api/projects/{project_id}/batch-tasks",
        json={"run_ocr": False, "run_render": False, "llm_profile_id": llm_id},
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["data"]["task_id"]
    task = wait_for_task(llm_client, task_id)
    assert task["status"] == "completed", task
    assert task["task_type"] == "batch_pipeline"
    items = llm_client.get(f"/api/tasks/{task_id}/items").json()["data"]["items"]
    assert items[0]["page_id"] == page_id
    assert [stage["stage"] for stage in items[0]["stages"]] == ["translation"]
    child_id = items[0]["stages"][0]["child_task_id"]
    assert child_id
    assert llm_client.get(f"/api/tasks/{child_id}").json()["data"]["parent_task_id"] == task_id


def test_batch_rejects_legacy_integrated_detection(llm_client: TestClient) -> None:
    project_id, _page_id = _create_project(llm_client)
    response = llm_client.post(
        f"/api/projects/{project_id}/batch-tasks/preview",
        json={
            "run_ocr": False,
            "run_translation": False,
            "run_render": False,
            "detection": {
                "model_id": "legacy-model",
                "confidence": 0.25,
                "image_size": 1280,
                "device": "auto",
                "overwrite": False,
            },
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BATCH_DETECTION_SEPARATE"


def test_batch_skips_undetected_pages_and_requires_one_detected_page(
    llm_client: TestClient,
) -> None:
    project_id, pages = _create_raw_project(llm_client, page_count=2)
    first_page_id = str(pages[0]["id"])
    saved = llm_client.put(
        f"/api/pages/{first_page_id}/regions",
        json={
            "expected_revision": 0,
            "regions": [
                {
                    "id": "split-r1",
                    "class_id": 0,
                    "class_name": "text",
                    "x1": 5,
                    "y1": 5,
                    "x2": 80,
                    "y2": 30,
                    "source": "manual",
                }
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    payload = {"run_ocr": False, "run_translation": False, "run_render": False}
    preview = llm_client.post(
        f"/api/projects/{project_id}/batch-tasks/preview", json=payload
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()["data"]
    assert data["eligible_pages"] == 1
    assert data["skipped_pages"] == 1
    assert data["skipped"][0]["reason"] == "尚未完成 YOLO 检测"
    assert "estimated_detection_pages" not in data

    undetected_project_id, _ = _create_raw_project(llm_client)
    blocked_preview = llm_client.post(
        f"/api/projects/{undetected_project_id}/batch-tasks/preview", json=payload
    )
    assert blocked_preview.status_code == 409
    assert blocked_preview.json()["error"]["code"] == "NO_ELIGIBLE_PAGES"
    blocked = llm_client.post(
        f"/api/projects/{undetected_project_id}/batch-tasks", json=payload
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "NO_ELIGIBLE_PAGES"
    assert "批量检测" in blocked.json()["error"]["message"]


