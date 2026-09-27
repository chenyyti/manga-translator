from __future__ import annotations

import io
import sqlite3
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.core.config import Settings
from app.main import create_app
from tests.test_phase8_reader import FakeInpaintingRuntime, _rendered_project


def _wait_export(client: TestClient, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/tasks/{task_id}").json()["data"]
        if snapshot["status"] in {"completed", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.02)
    raise AssertionError("export task did not finish")


def test_page_downloads_and_archive_exports(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            project_root=tmp_path / "project",
            thumbnail_size=100,
        ),
        inpainting_runtime=FakeInpaintingRuntime(),
    )
    with TestClient(app) as client:
        project_id, page_id, config = _rendered_project(client)
        with sqlite3.connect(client.app.state.database.path) as connection:
            rendered_relative = connection.execute(
                "SELECT rendered_path FROM pages WHERE id = ?", (page_id,)
            ).fetchone()[0]
        source = config.data_dir / rendered_relative
        original_bytes = source.read_bytes()
        png = client.get(f"/api/pages/{page_id}/export/png")
        jpg = client.get(f"/api/pages/{page_id}/export/jpg")
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"
        assert jpg.status_code == 200 and jpg.headers["content-type"] == "image/jpeg"
        assert Image.open(io.BytesIO(png.content)).size == (320, 240)
        assert Image.open(io.BytesIO(jpg.content)).size == (320, 240)
        assert source.read_bytes() == original_bytes
        assert client.get(
            f"/api/pages/{page_id}/export/png",
            headers={"If-None-Match": png.headers["etag"]},
        ).status_code == 304

        created = client.post(f"/api/projects/{project_id}/exports", json={"format": "zip"})
        assert created.status_code == 202, created.text
        data = created.json()["data"]
        assert _wait_export(client, data["task_id"])["status"] == "completed"
        downloaded = client.get(f"/api/exports/{data['export_id']}/download")
        archive = zipfile.ZipFile(io.BytesIO(downloaded.content))
        assert archive.namelist() == ["manifest.json", "page-0001.png"]
        assert archive.getinfo("page-0001.png").compress_type == zipfile.ZIP_STORED
        assert client.get(
            f"/api/exports/{data['export_id']}/download",
            headers={"If-None-Match": downloaded.headers["etag"]},
        ).status_code == 304

        epub = client.post(f"/api/projects/{project_id}/exports", json={"format": "epub"})
        assert epub.status_code == 202
        epub_data = epub.json()["data"]
        assert _wait_export(client, epub_data["task_id"])["status"] == "completed"
        epub_download = client.get(f"/api/exports/{epub_data['export_id']}/download")
        epub_zip = zipfile.ZipFile(io.BytesIO(epub_download.content))
        assert epub_zip.namelist()[0] == "mimetype"
        assert epub_zip.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert epub_zip.read("mimetype") == b"application/epub+zip"

        assert client.delete(f"/api/exports/{data['export_id']}").status_code == 200
        assert client.get(f"/api/exports/{data['export_id']}").json()["data"]["status"] == "deleted"


def test_health_report_blocks_missing_page(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", project_root=tmp_path / "project"),
        inpainting_runtime=FakeInpaintingRuntime(),
    )
    with TestClient(app) as client:
        project_id, page_id, config = _rendered_project(client)
        with sqlite3.connect(client.app.state.database.path) as connection:
            rendered_relative = connection.execute(
                "SELECT rendered_path FROM pages WHERE id = ?", (page_id,)
            ).fetchone()[0]
        (config.data_dir / rendered_relative).unlink()
        report = client.get(f"/api/projects/{project_id}/health-check").json()["data"]
        assert report["status"] == "error"
        assert report["exportable"]["zip"] is False
        assert report["checks"][0]["page_index"] == 1
        response = client.post(f"/api/projects/{project_id}/exports", json={"format": "zip"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EXPORT_PAGES_NOT_READY"
