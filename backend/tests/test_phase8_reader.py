from __future__ import annotations

import io
import shutil
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

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
            InpaintingProviderInfo("lama", "Big-LaMa", True, "test", True, None, ("cpu",), "test"),
        ]

    async def inpaint(
        self,
        provider,
        image_path,
        mask_path,
        output_path,
        *,
        device,
        max_edge,
        opencv_radius,
    ):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image_path, output_path)
        return InpaintingResult(output_path, provider, "cpu")

    async def close(self):
        return None


def _image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (320, 240), "white").save(output, "PNG")
    return output.getvalue()


def _rendered_project(client: TestClient) -> tuple[str, str, Settings]:
    session_id = client.post(
        "/api/import-sessions",
        json={
            "project_name": "阅读器测试",
            "source_language": "ja",
            "target_language": "zh-CN",
            "translation_mode": "quick",
            "source_type": "single",
        },
    ).json()["data"]["id"]
    uploaded = client.post(
        f"/api/import-sessions/{session_id}/files",
        data={"relative_path": "1.png"},
        files={"file": ("1.png", _image_bytes(), "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    identifiers = client.post(f"/api/import-sessions/{session_id}/commit").json()["data"]
    assert wait_for_task(client, identifiers["task_id"])["status"] == "completed"
    page = client.get(
        f"/api/projects/{identifiers['project_id']}/pages"
    ).json()["data"]["items"][0]
    assert client.put(
        f"/api/pages/{page['id']}/regions",
        json={"expected_revision": 0, "regions": []},
    ).status_code == 200
    assert client.post(f"/api/pages/{page['id']}/review").status_code == 200
    task = client.post(f"/api/pages/{page['id']}/render", json={}).json()["data"]["task_id"]
    assert wait_for_task(client, task)["status"] == "completed"
    return identifiers["project_id"], page["id"], client.app.state.settings


def test_reader_cover_progress_and_rendered_thumbnail(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            project_root=tmp_path / "project",
            thumbnail_size=100,
        ),
        inpainting_runtime=FakeInpaintingRuntime(),
    )
    with TestClient(app) as client:
        project_id, page_id, _settings = _rendered_project(client)

        bookshelf = client.get("/api/bookshelf").json()["data"]
        assert bookshelf["total"] == 1
        item = bookshelf["items"][0]
        assert item["cover_thumbnail_url"].endswith("rendered-thumbnail")
        cover = client.get(item["cover_thumbnail_url"])
        assert cover.status_code == 200
        assert cover.headers["content-type"] == "image/webp"

        manifest = client.get(f"/api/projects/{project_id}/reader")
        assert manifest.status_code == 200
        assert manifest.json()["data"]["readable_pages"] == 1
        pages = client.get(f"/api/projects/{project_id}/reader/pages").json()["data"]
        assert pages["items"][0]["readable"] is True
        assert pages["items"][0]["rendered_url"].endswith("/rendered")

        progress = client.put(
            f"/api/projects/{project_id}/reading-progress", json={"page_id": page_id}
        )
        assert progress.status_code == 200
        assert progress.json()["data"]["page_index"] == 1
        assert client.get("/api/bookshelf").json()["data"]["items"][0]["last_read_page_index"] == 1

        selected = client.put(
            f"/api/projects/{project_id}/cover", json={"page_id": page_id}
        )
        assert selected.status_code == 200
        assert selected.json()["data"]["cover_page_id"] == page_id


def test_reader_retains_placeholder_for_missing_render(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project"),
        inpainting_runtime=FakeInpaintingRuntime(),
    )
    with TestClient(app) as client:
        project_id, page_id, settings = _rendered_project(client)
        with sqlite3.connect(client.app.state.database.path) as connection:
            relative_path = connection.execute(
                "SELECT rendered_path FROM pages WHERE id = ?", (page_id,)
            ).fetchone()[0]
        rendered = Path(settings.data_dir) / relative_path
        rendered.write_bytes(b"not a rendered PNG")
        pages = client.get(f"/api/projects/{project_id}/reader/pages").json()["data"]
        assert pages["items"][0]["readable"] is False
        assert pages["items"][0]["missing_reason"] == "render_missing"
        assert client.get("/api/bookshelf").json()["data"]["total"] == 0
        rendered.unlink()
        pages = client.get(f"/api/projects/{project_id}/reader/pages").json()["data"]
        assert pages["items"][0]["readable"] is False
        assert pages["items"][0]["missing_reason"] == "render_missing"


def test_reader_rejects_project_without_completed_render(client: TestClient) -> None:
    session_id = client.post(
        "/api/import-sessions",
        json={
            "project_name": "未生成",
            "source_language": "ja",
            "target_language": "zh-CN",
            "translation_mode": "quick",
            "source_type": "single",
        },
    ).json()["data"]["id"]
    client.post(
        f"/api/import-sessions/{session_id}/files",
        data={"relative_path": "1.png"},
        files={"file": ("1.png", _image_bytes(), "image/png")},
    )
    identifiers = client.post(f"/api/import-sessions/{session_id}/commit").json()["data"]
    assert wait_for_task(client, identifiers["task_id"])["status"] == "completed"
    response = client.get(f"/api/projects/{identifiers['project_id']}/reader")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "READER_NOT_READY"
