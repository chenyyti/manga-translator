from __future__ import annotations

import io
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.core.config import Settings
from app.main import create_app
from app.providers.inpainting.base import InpaintingProviderInfo, InpaintingResult
from tests.helpers import wait_for_task


class FakeInpaintingRuntime:
    async def providers(self):
        return [
            InpaintingProviderInfo("auto", "Auto", True, "test", True, None, ("cpu",), "test"),
            InpaintingProviderInfo("fast", "FAST", True, "test", True, None, ("cpu",), "test"),
            InpaintingProviderInfo("opencv", "OpenCV", True, "test", True, None, ("cpu",), "test"),
            InpaintingProviderInfo("lama", "Big-LaMa", True, "test", True, "big-lama", ("cpu",), "test"),
        ]

    async def inpaint(self, provider, image_path, mask_path, output_path, *, device, max_edge, opencv_radius):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image_path, output_path)
        return InpaintingResult(output_path, provider, "cpu")

    async def close(self):
        return None


def _image_bytes() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (320, 240), "white")
    ImageDraw.Draw(image).rectangle((60, 60, 260, 180), fill="#ddd")
    image.save(output, "PNG")
    return output.getvalue()


def _project(client: TestClient) -> tuple[str, str]:
    session = client.post(
        "/api/import-sessions",
        json={
            "project_name": "渲染测试",
            "source_language": "ja",
            "target_language": "zh-CN",
            "translation_mode": "quick",
            "source_type": "single",
        },
    ).json()["data"]["id"]
    response = client.post(
        f"/api/import-sessions/{session}/files",
        data={"relative_path": "1.png"},
        files={"file": ("1.png", _image_bytes(), "image/png")},
    )
    assert response.status_code == 201, response.text
    identifiers = client.post(f"/api/import-sessions/{session}/commit").json()["data"]
    assert wait_for_task(client, identifiers["task_id"])["status"] == "completed"
    page = client.get(f"/api/projects/{identifiers['project_id']}/pages").json()["data"]["items"][0]
    return identifiers["project_id"], page["id"]


def test_render_provider_and_settings(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project"),
        inpainting_runtime=FakeInpaintingRuntime(),
    )
    with TestClient(app) as client:
        providers = client.get("/api/providers/inpainting").json()["data"]["items"]
        assert {item["id"] for item in providers} == {"auto", "fast", "opencv", "lama"}
        settings = client.get("/api/settings/render").json()["data"]
        assert settings["repair_mode"] == "auto"
        response = client.put("/api/settings/render", json={**settings, "expected_revision": 0, "font_size": 40})
        assert response.status_code == 200
        assert response.json()["data"]["revision"] == 1
        conflict = client.put("/api/settings/render", json={**settings, "expected_revision": 0})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "SETTINGS_REVISION_CONFLICT"


def test_repair_then_render_current_page(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project"),
        inpainting_runtime=FakeInpaintingRuntime(),
    )
    with TestClient(app) as client:
        project_id, page_id = _project(client)
        assert client.put(
            f"/api/pages/{page_id}/regions",
            json={
                "expected_revision": 0,
                "regions": [
                    {
                        "id": "r1",
                        "class_id": 0,
                        "class_name": "text",
                        "confidence": 0.9,
                        "x1": 50,
                        "y1": 50,
                        "x2": 270,
                        "y2": 190,
                        "source": "manual",
                        "is_manual_edited": True,
                    }
                ],
            },
        ).status_code == 200
        assert client.post(f"/api/pages/{page_id}/review").status_code == 200
        # A page with no translation cannot render while it has a real region.
        blocked = client.post(f"/api/pages/{page_id}/render", json={})
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "TRANSLATION_NOT_READY"
        # Empty pages are still allowed to produce an oriented full-resolution PNG.
        client.put(f"/api/pages/{page_id}/regions", json={"expected_revision": 1, "regions": []})
        client.post(f"/api/pages/{page_id}/review")
        created = client.post(f"/api/pages/{page_id}/render", json={})
        assert created.status_code == 202, created.text
        task = wait_for_task(client, created.json()["data"]["task_id"])
        assert task["status"] == "completed", task
        state = client.get(f"/api/pages/{page_id}/render-state").json()["data"]
        assert state["render_status"] == "completed"
        asset = client.get(f"/api/pages/{page_id}/asset/rendered")
        assert asset.status_code == 200
        assert asset.headers["content-type"] == "image/png"
        assert "etag" in asset.headers
        assert client.get("/api/bookshelf").json()["data"]["total"] == 1


def test_region_typesetting_and_stale_render(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project"),
        inpainting_runtime=FakeInpaintingRuntime(),
    )
    with TestClient(app) as client:
        _project_id, page_id = _project(client)
        response = client.put(
            f"/api/pages/{page_id}/regions",
            json={
                "expected_revision": 0,
                "regions": [
                    {
                        "id": "r1",
                        "class_id": 0,
                        "class_name": "dialogue",
                        "x1": 20,
                        "y1": 20,
                        "x2": 300,
                        "y2": 220,
                        "source": "manual",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        assert client.post(f"/api/pages/{page_id}/review").status_code == 200
        assert client.put(
            "/api/regions/r1/ocr-text",
            json={"text": "原文", "expected_ocr_revision": 0},
        ).status_code == 200
        assert client.put(
            "/api/regions/r1/translation-text",
            json={"text": "你好", "expected_translation_revision": 1},
        ).status_code == 200
        created = client.post(f"/api/pages/{page_id}/render", json={})
        assert created.status_code == 202, created.text
        task_id = created.json()["data"]["task_id"]
        assert wait_for_task(client, task_id)["status"] == "completed"
        with sqlite3.connect(tmp_path / "data" / "data" / "app.db") as connection:
            raw_result = connection.execute(
                "SELECT result_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()[0]
        timings = json.loads(raw_result)["timings_ms"]
        assert set(timings) == {
            "repair", "decode", "mask", "layout", "composite", "save", "thumbnail", "total"
        }
        assert all(value >= 0 for value in timings.values())
        rendered = client.get(f"/api/pages/{page_id}/asset/rendered")
        assert rendered.status_code == 200
        assert Image.open(io.BytesIO(rendered.content)).size == (320, 240)
        # Changing a box marks both the repair and final asset stale.
        document = client.get(f"/api/pages/{page_id}/regions").json()["data"]
        document["regions"][0]["x1"] = 25
        assert client.put(
            f"/api/pages/{page_id}/regions",
            json={"expected_revision": document["revision"], "regions": document["regions"]},
        ).status_code == 200
        state = client.get(f"/api/pages/{page_id}/render-state").json()["data"]
        assert state["render_status"] == "outdated"


@pytest.mark.parametrize("mode", ["mixed", "overlap", "all_failed"])
def test_overflow_skips_region_and_restores_original(tmp_path: Path, mode: str) -> None:
    class PaintedRuntime(FakeInpaintingRuntime):
        async def inpaint(self, provider, image_path, mask_path, output_path, **kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(image_path) as source:
                Image.new("RGB", source.size, "#aabbcc").save(output_path)
            return InpaintingResult(output_path, provider, "cpu")

    app = create_app(Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project"),
                     inpainting_runtime=PaintedRuntime())
    with TestClient(app) as client:
        _, page_id = _project(client)
        # The failing box is processed first; later success must not overwrite it.
        bad = (80, 80, 86, 86) if mode == "overlap" else (25, 25, 31, 31)
        entries = [("bad", bad, "无法容纳" * 100)]
        if mode != "all_failed":
            entries.append(("good", (60, 60, 260, 180), "你好"))
        regions = [{"id": key, "class_id": 0, "class_name": "dialogue", "source": "manual",
                    "x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}
                   for key, box, _ in entries]
        response = client.put(f"/api/pages/{page_id}/regions", json={"expected_revision": 0, "regions": regions})
        assert response.status_code == 200, response.text
        assert client.post(f"/api/pages/{page_id}/review").status_code == 200
        for key, _, text in entries:
            assert client.put(f"/api/regions/{key}/ocr-text", json={"text": "原文", "expected_ocr_revision": 0}).status_code == 200
            assert client.put(f"/api/regions/{key}/translation-text", json={"text": text, "expected_translation_revision": 1}).status_code == 200
        created = client.post(f"/api/pages/{page_id}/render", json={"force_repair": True})
        assert created.status_code == 202, created.text
        task = wait_for_task(client, created.json()["data"]["task_id"])
        assert task["status"] == "completed", task
        rendered = Image.open(io.BytesIO(client.get(f"/api/pages/{page_id}/asset/rendered").content)).convert("RGB")
        original = Image.open(io.BytesIO(_image_bytes())).convert("RGB")
        assert rendered.crop(bad).tobytes() == original.crop(bad).tobytes()
        assert rendered.getpixel((0, 0)) == original.getpixel((0, 0))
        if mode == "all_failed":
            assert rendered.tobytes() == original.tobytes()
        else:
            assert rendered.crop((100, 100, 220, 150)).tobytes() != original.crop((100, 100, 220, 150)).tobytes()
