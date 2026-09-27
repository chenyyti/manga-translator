from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.providers.ocr.base import (
    OCRBatchItem,
    OCRBatchResult,
    OCRImageInput,
    OCRProviderInfo,
    OCRRecognition,
)
from tests.helpers import image_bytes, wait_for_task


class FakeOCRRuntime:
    def __init__(self, delay: float = 0) -> None:
        self.delay = delay
        self.calls: list[tuple[str, Path, str, str]] = []

    async def providers(self) -> list[OCRProviderInfo]:
        return [
            OCRProviderInfo(
                "mangaocr", "MangaOCR", ("ja",), True, "test", True, ("test-ja",), True
            ),
            OCRProviderInfo(
                "paddleocr",
                "PaddleOCR",
                ("ko", "en"),
                True,
                "test",
                True,
                ("test-ko", "test-en"),
                True,
            ),
        ]

    async def recognize(
        self, provider: str, image_path: Path, *, language: str, device: str
    ) -> OCRRecognition:
        self.calls.append((provider, image_path, language, device))
        assert image_path.is_file()
        if self.delay:
            await asyncio.sleep(self.delay)
        text = {"ja": "こんにちは", "ko": "안녕하세요", "en": "Hello"}[language]
        confidence = None if provider == "mangaocr" else 0.97
        return OCRRecognition(text, confidence, "cuda:0")

    async def close(self) -> None:
        return None


class BatchFakeOCRRuntime(FakeOCRRuntime):
    def __init__(self, delay: float = 0) -> None:
        super().__init__(delay)
        self.batches: list[tuple[OCRImageInput, ...]] = []

    async def recognize_batch(
        self,
        provider: str,
        images: tuple[OCRImageInput, ...],
        *,
        language: str,
        device: str,
        batch_size: int,
    ) -> OCRBatchResult:
        assert batch_size == 8
        self.batches.append(images)
        if self.delay:
            await asyncio.sleep(self.delay)
        text = {"ja": "こんにちは", "ko": "안녕하세요", "en": "Hello"}[language]
        confidence = None if provider == "mangaocr" else 0.97
        return OCRBatchResult(
            items=tuple(
                OCRBatchItem(
                    image.region_id,
                    OCRRecognition(text, confidence, "cuda:0"),
                )
                for image in images
            ),
            requested_batch_size=8,
            effective_batch_size=len(images),
            inference_ms=5,
        )


@pytest.fixture
def ocr_client(test_settings: Settings) -> Iterator[tuple[TestClient, FakeOCRRuntime]]:
    runtime = FakeOCRRuntime()
    with TestClient(create_app(test_settings, ocr_runtime=runtime)) as client:
        yield client, runtime


def _create_reviewed_page(
    client: TestClient, *, language: str = "ja", region_count: int = 1
) -> tuple[str, str, list[str]]:
    created = client.post(
        "/api/import-sessions",
        json={
            "project_name": "OCR 测试",
            "source_language": language,
            "target_language": "zh-CN",
            "translation_mode": "quick",
            "source_type": "single",
            "ocr_provider": "auto",
        },
    ).json()["data"]
    client.post(
        f"/api/import-sessions/{created['id']}/files",
        data={"relative_path": "第1页.png"},
        files={"file": ("第1页.png", image_bytes((100, 120)), "image/png")},
    )
    committed = client.post(f"/api/import-sessions/{created['id']}/commit").json()["data"]
    assert wait_for_task(client, committed["task_id"])["status"] == "completed"
    page = client.get(f"/api/projects/{committed['project_id']}/pages").json()["data"]["items"][0]
    regions = [
        {
            "id": f"region-{index}",
            "class_id": 0,
            "class_name": "text_region",
            "x1": 5 + index * 20,
            "y1": 5,
            "x2": 20 + index * 20,
            "y2": 30,
            "source": "manual",
        }
        for index in range(region_count)
    ]
    saved = client.put(
        f"/api/pages/{page['id']}/regions",
        json={"expected_revision": 0, "regions": regions},
    )
    assert saved.status_code == 200, saved.text
    assert client.post(f"/api/pages/{page['id']}/review").status_code == 200
    return committed["project_id"], page["id"], [item["id"] for item in regions]


def test_provider_settings_and_language_validation(
    ocr_client: tuple[TestClient, FakeOCRRuntime],
) -> None:
    client, _runtime = ocr_client
    providers = client.get("/api/providers/ocr").json()["data"]
    assert providers["automatic"] == {"ja": "mangaocr", "ko": "paddleocr", "en": "paddleocr"}
    assert all(item["model_ready"] for item in providers["items"])
    assert client.get("/api/settings/ocr").json()["data"] == {
        "japanese_provider": "mangaocr",
        "korean_provider": "paddleocr",
        "english_provider": "paddleocr",
        "device": "auto",
    }
    invalid = client.post(
        "/api/import-sessions",
        json={
            "project_name": "错误 Provider",
            "source_language": "ko",
            "target_language": "zh-CN",
            "translation_mode": "quick",
            "source_type": "single",
            "ocr_provider": "mangaocr",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "OCR_PROVIDER_FIXED"
    updated = client.put(
        "/api/settings/ocr",
        json={
            "japanese_provider": "mangaocr",
            "korean_provider": "paddleocr",
            "english_provider": "auto",
            "device": "cpu",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"] == {
        "japanese_provider": "mangaocr",
        "korean_provider": "paddleocr",
        "english_provider": "paddleocr",
        "device": "cpu",
    }
    settings_mismatch = client.put(
        "/api/settings/ocr",
        json={
            "japanese_provider": "paddleocr",
            "korean_provider": "paddleocr",
            "english_provider": "paddleocr",
            "device": "cpu",
        },
    )
    assert settings_mismatch.status_code == 422
    assert settings_mismatch.json()["error"]["code"] == "OCR_PROVIDER_FIXED"
    project_id, _page_id, _region_ids = _create_reviewed_page(client)
    rejected = client.put(
        f"/api/projects/{project_id}/ocr-provider",
        json={"provider": "paddleocr"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "OCR_PROVIDER_FIXED"
    canonical = client.put(
        f"/api/projects/{project_id}/ocr-provider",
        json={"provider": "auto"},
    )
    assert canonical.status_code == 200
    assert canonical.json()["data"]["ocr_provider"] == "mangaocr"


@pytest.mark.parametrize(
    ("language", "expected_provider"),
    [("ja", "mangaocr"), ("ko", "paddleocr"), ("en", "paddleocr")],
)
def test_project_creation_uses_fixed_provider_for_language(
    ocr_client: tuple[TestClient, FakeOCRRuntime], language: str, expected_provider: str
) -> None:
    client, _runtime = ocr_client
    created = client.post(
        "/api/import-sessions",
        json={
            "project_name": "固定 OCR 路由",
            "source_language": language,
            "target_language": "zh-CN",
            "translation_mode": "quick",
            "source_type": "single",
            "ocr_provider": "auto",
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["ocr_provider"] == expected_provider
    upload = client.post(
        f"/api/import-sessions/{data['id']}/files",
        data={"relative_path": "第1页.png"},
        files={"file": ("第1页.png", image_bytes(), "image/png")},
    )
    assert upload.status_code == 201, upload.text
    committed = client.post(f"/api/import-sessions/{data['id']}/commit")
    identifiers = committed.json()["data"]
    assert wait_for_task(client, identifiers["task_id"])["status"] == "completed"
    project = client.get(f"/api/projects/{identifiers['project_id']}").json()["data"]
    assert project["ocr_provider"] == expected_provider


@pytest.mark.parametrize(
    ("language", "expected_provider"),
    [("ja", "mangaocr"), ("ko", "paddleocr"), ("en", "paddleocr")],
)
def test_batch_ocr_uses_fixed_provider_for_language(
    ocr_client: tuple[TestClient, FakeOCRRuntime], language: str, expected_provider: str
) -> None:
    client, runtime = ocr_client
    project_id, _page_id, _region_ids = _create_reviewed_page(client, language=language)
    payload = {"run_ocr": True, "run_translation": False, "run_render": False}
    preview = client.post(f"/api/projects/{project_id}/batch-tasks/preview", json=payload)
    assert preview.status_code == 200, preview.text

    created = client.post(f"/api/projects/{project_id}/batch-tasks", json=payload)
    assert created.status_code == 202, created.text
    task = wait_for_task(client, created.json()["data"]["task_id"])
    assert task["status"] == "completed", task
    assert runtime.calls[-1][0] == expected_provider
    assert runtime.calls[-1][2] == language


def test_page_ocr_manual_edit_and_bbox_outdated(
    ocr_client: tuple[TestClient, FakeOCRRuntime],
) -> None:
    client, runtime = ocr_client
    project_id, page_id, region_ids = _create_reviewed_page(client)
    rejected = client.post(f"/api/pages/{page_id}/ocr", json={"provider": "paddleocr"})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "OCR_PROVIDER_FIXED"
    started = client.post(f"/api/pages/{page_id}/ocr", json={})
    assert started.status_code == 202, started.text
    task = wait_for_task(client, started.json()["data"]["task_id"])
    assert (task["completed"], task["failed"]) == (1, 0)
    assert runtime.calls[0][0] == "mangaocr"

    document = client.get(f"/api/pages/{page_id}/regions").json()["data"]
    region = document["regions"][0]
    assert region["source_text"] == "こんにちは"
    assert region["ocr_status"] == "completed"
    assert region["ocr_confidence"] is None

    manual = client.put(
        f"/api/regions/{region_ids[0]}/ocr-text",
        json={"text": "  手動\r\n修正  ", "expected_ocr_revision": region["ocr_revision"]},
    )
    assert manual.status_code == 200
    assert manual.json()["data"]["source_text"] == "手動\n修正"
    assert manual.json()["data"]["ocr_status"] == "manual"

    current = client.get(f"/api/pages/{page_id}/regions").json()["data"]
    unchanged = client.put(
        f"/api/pages/{page_id}/regions",
        json={"expected_revision": current["revision"], "regions": current["regions"]},
    )
    assert unchanged.json()["data"]["regions"][0]["ocr_status"] == "manual"
    moved_regions = unchanged.json()["data"]["regions"]
    moved_regions[0]["x1"] += 1
    moved = client.put(
        f"/api/pages/{page_id}/regions",
        json={"expected_revision": unchanged.json()["data"]["revision"], "regions": moved_regions},
    )
    moved_region = moved.json()["data"]["regions"][0]
    assert moved_region["source_text"] == "手動\n修正"
    assert moved_region["ocr_status"] == "outdated"
    # Editing a detected box invalidates OCR but no longer introduces a
    # separate manual-review gate.
    restarted = client.post(f"/api/pages/{page_id}/ocr", json={})
    assert restarted.status_code == 202
    wait_for_task(client, restarted.json()["data"]["task_id"])
    assert client.get(f"/api/projects/{project_id}").json()["data"]["ocr_provider"] == "mangaocr"


def test_region_ocr_requires_manual_overwrite_confirmation(
    ocr_client: tuple[TestClient, FakeOCRRuntime],
) -> None:
    client, _runtime = ocr_client
    _project_id, page_id, region_ids = _create_reviewed_page(client, language="en")
    region = client.get(f"/api/pages/{page_id}/regions").json()["data"]["regions"][0]
    manual = client.put(
        f"/api/regions/{region_ids[0]}/ocr-text",
        json={"text": "Manual", "expected_ocr_revision": region["ocr_revision"]},
    ).json()["data"]
    rejected = client.post(
        f"/api/regions/{region_ids[0]}/ocr",
        json={"expected_ocr_revision": manual["ocr_revision"]},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "OCR_MANUAL_CONFIRM_REQUIRED"
    started = client.post(
        f"/api/regions/{region_ids[0]}/ocr",
        json={
            "expected_ocr_revision": manual["ocr_revision"],
            "overwrite_manual": True,
        },
    )
    assert started.status_code == 202
    assert wait_for_task(client, started.json()["data"]["task_id"])["status"] == "completed"
    saved = client.get(f"/api/pages/{page_id}/regions").json()["data"]["regions"][0]
    assert saved["source_text"] == "Hello"
    assert saved["ocr_provider"] == "paddleocr"
    assert saved["ocr_confidence"] == 0.97


def test_manual_edit_wins_over_inflight_ocr(test_settings: Settings) -> None:
    runtime = FakeOCRRuntime(delay=0.2)
    with TestClient(create_app(test_settings, ocr_runtime=runtime)) as client:
        _project_id, page_id, region_ids = _create_reviewed_page(client)
        original = client.get(f"/api/pages/{page_id}/regions").json()["data"]["regions"][0]
        started = client.post(f"/api/pages/{page_id}/ocr", json={}).json()["data"]
        edited = client.put(
            f"/api/regions/{region_ids[0]}/ocr-text",
            json={"text": "人工优先", "expected_ocr_revision": original["ocr_revision"]},
        )
        assert edited.status_code == 200
        task = wait_for_task(client, started["task_id"])
        assert task["skipped"] == 1
        final = client.get(f"/api/pages/{page_id}/regions").json()["data"]["regions"][0]
        assert final["source_text"] == "人工优先"
        assert final["ocr_status"] == "manual"


def test_page_ocr_uses_in_memory_batch_and_records_metrics(test_settings: Settings) -> None:
    runtime = BatchFakeOCRRuntime()
    with TestClient(create_app(test_settings, ocr_runtime=runtime)) as client:
        _project_id, page_id, _region_ids = _create_reviewed_page(client, region_count=3)
        started = client.post(f"/api/pages/{page_id}/ocr", json={}).json()["data"]
        task = wait_for_task(client, started["task_id"])
        assert task["status"] == "completed"
        assert task["completed"] == 3
        assert len(runtime.batches) == 1
        assert [item.region_id for item in runtime.batches[0]] == [
            "region-0",
            "region-1",
            "region-2",
        ]
        assert not list(test_settings.projects_dir.rglob("crops/ocr/*.png"))

    with sqlite3.connect(test_settings.database_path) as connection:
        raw = connection.execute(
            "SELECT result_json FROM tasks WHERE id = ?", (started["task_id"],)
        ).fetchone()[0]
    metrics = json.loads(raw)
    assert metrics["actual_device"] == "cuda:0"
    assert metrics["requested_batch_size"] == 8
    assert metrics["effective_batch_size"] == 3
    assert metrics["inference_ms"] == 5


def test_manual_edit_wins_over_inflight_batch_ocr(test_settings: Settings) -> None:
    runtime = BatchFakeOCRRuntime(delay=0.2)
    with TestClient(create_app(test_settings, ocr_runtime=runtime)) as client:
        _project_id, page_id, region_ids = _create_reviewed_page(client)
        original = client.get(f"/api/pages/{page_id}/regions").json()["data"]["regions"][0]
        started = client.post(f"/api/pages/{page_id}/ocr", json={}).json()["data"]
        for _ in range(100):
            if runtime.batches:
                break
            import time

            time.sleep(0.01)
        edited = client.put(
            f"/api/regions/{region_ids[0]}/ocr-text",
            json={"text": "人工批量优先", "expected_ocr_revision": original["ocr_revision"]},
        )
        assert edited.status_code == 200
        task = wait_for_task(client, started["task_id"])
        assert task["skipped"] == 1
        final = client.get(f"/api/pages/{page_id}/regions").json()["data"]["regions"][0]
        assert final["source_text"] == "人工批量优先"
        assert final["ocr_status"] == "manual"
