from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.services import tasks as import_tasks
from tests.helpers import image_bytes, wait_for_task


def create_session(
    client: TestClient, source_type: str = "multiple", translation_mode: str = "quick"
) -> str:
    response = client.post(
        "/api/import-sessions",
        json={
            "project_name": "测试漫画",
            "source_language": "ja",
            "target_language": "zh-CN",
            "translation_mode": translation_mode,
            "source_type": source_type,
        },
    )
    assert response.status_code == 201
    return response.json()["data"]["id"]


def upload(client: TestClient, session_id: str, name: str, body: bytes) -> None:
    response = client.post(
        f"/api/import-sessions/{session_id}/files",
        data={"relative_path": name},
        files={"file": (name, body, "image/png")},
    )
    assert response.status_code == 201, response.text


def test_translation_mode_defaults_to_quick_and_rejects_refined(client: TestClient) -> None:
    missing = client.post(
        "/api/import-sessions",
        json={
            "project_name": "缺少模式",
            "source_language": "ja",
            "target_language": "zh-CN",
            "source_type": "single",
        },
    )
    assert missing.status_code == 201
    assert missing.json()["data"]["translation_mode"] == "quick"

    invalid = client.post(
        "/api/import-sessions",
        json={
            "project_name": "错误模式",
            "source_language": "ja",
            "target_language": "zh-CN",
            "translation_mode": "unsupported",
            "source_type": "single",
        },
    )
    assert invalid.status_code == 422

    refined = client.post(
        "/api/import-sessions",
        json={
            "project_name": "不支持精翻",
            "source_language": "ja",
            "source_type": "single",
            "translation_mode": "refined",
        },
    )
    assert refined.status_code == 422

    quick = client.post(
        "/api/import-sessions",
        json={
            "project_name": "旧客户端普通翻译",
            "source_language": "ja",
            "source_type": "single",
            "translation_mode": "quick",
        },
    )
    assert quick.status_code == 201
    assert quick.json()["data"]["translation_mode"] == "quick"

    visual_profile = client.post(
        "/api/import-sessions",
        json={
            "project_name": "旧视觉模型配置",
            "source_language": "ja",
            "source_type": "single",
            "vlm_profile_id": "old-profile",
        },
    )
    assert visual_profile.status_code == 422


def test_single_project_import_and_asset_delivery(client: TestClient) -> None:
    session_id = create_session(client, "single")
    upload(client, session_id, "第1页.png", image_bytes())
    committed = client.post(f"/api/import-sessions/{session_id}/commit")
    assert committed.status_code == 202
    identifiers = committed.json()["data"]
    task = wait_for_task(client, identifiers["task_id"])
    assert task["status"] == "completed"
    assert task["completed"] == 1

    project = client.get(f"/api/projects/{identifiers['project_id']}").json()["data"]
    assert project["status"] == "ready"
    assert project["translation_mode"] == "quick"
    pages = client.get(f"/api/projects/{project['id']}/pages").json()["data"]
    assert pages["total"] == 1
    page = pages["items"][0]
    preview = client.get(page["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/webp"
    assert "etag" in preview.headers

    bookshelf = client.get("/api/bookshelf").json()["data"]
    assert bookshelf["total"] == 0


def test_corrupt_page_does_not_abort_valid_pages(client: TestClient) -> None:
    session_id = create_session(client)
    upload(client, session_id, "1.png", image_bytes())
    upload(client, session_id, "2.png", b"broken")
    identifiers = client.post(f"/api/import-sessions/{session_id}/commit").json()["data"]
    task = wait_for_task(client, identifiers["task_id"])
    assert task["status"] == "completed"
    assert (task["completed"], task["failed"]) == (1, 1)
    project = client.get(f"/api/projects/{identifiers['project_id']}").json()["data"]
    assert project["status"] == "ready_with_warnings"
    assert project["warning_pages"] == 1


def test_delete_project_removes_record(client: TestClient) -> None:
    session_id = create_session(client, "single")
    upload(client, session_id, "1.png", image_bytes())
    identifiers = client.post(f"/api/import-sessions/{session_id}/commit").json()["data"]
    wait_for_task(client, identifiers["task_id"])
    response = client.delete(f"/api/projects/{identifiers['project_id']}")
    assert response.status_code == 200
    assert client.get(f"/api/projects/{identifiers['project_id']}").status_code == 404


def test_empty_import_has_uniform_error(client: TestClient) -> None:
    session_id = create_session(client)
    response = client.post(f"/api/import-sessions/{session_id}/commit")
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "EMPTY_IMPORT"
    assert body["error"]["request_id"]


def test_duplicate_relative_path_has_conflict_error(client: TestClient) -> None:
    session_id = create_session(client)
    body = image_bytes((64, 96))
    upload(client, session_id, "章节/1.png", body)

    response = client.post(
        f"/api/import-sessions/{session_id}/files",
        data={"relative_path": "章节/1.png"},
        files={"file": ("1.png", body, "image/png")},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DUPLICATE_UPLOAD"


def test_two_hundred_page_import_is_chunked_and_complete(client: TestClient) -> None:
    session_id = create_session(client)
    body = image_bytes((64, 96))
    for page_number in range(200, 0, -1):
        upload(client, session_id, f"第{page_number}页.png", body)

    identifiers = client.post(f"/api/import-sessions/{session_id}/commit").json()["data"]
    task = wait_for_task(client, identifiers["task_id"], timeout=30)
    assert task["status"] == "completed"
    assert (task["completed"], task["failed"]) == (200, 0)

    first_chunk = client.get(
        f"/api/projects/{identifiers['project_id']}/pages", params={"offset": 0, "limit": 50}
    ).json()["data"]
    second_chunk = client.get(
        f"/api/projects/{identifiers['project_id']}/pages", params={"offset": 50, "limit": 50}
    ).json()["data"]
    assert first_chunk["total"] == 200
    assert len(first_chunk["items"]) == len(second_chunk["items"]) == 50
    assert first_chunk["items"][0]["source_filename"] == "第1页.png"
    assert first_chunk["items"][9]["source_filename"] == "第10页.png"


def test_cancel_waits_for_two_active_pages_and_keeps_pending_pages(
    client: TestClient, monkeypatch
) -> None:
    session_id = create_session(client, "folder")
    body = image_bytes((256, 384))
    for page_number in range(1, 5):
        upload(client, session_id, f"chapter/{page_number}.png", body)

    real_generate = import_tasks.generate_derivatives
    release = threading.Event()
    both_started = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def gated_generate(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
        try:
            if not release.wait(timeout=10):
                raise TimeoutError("import workers did not resume")
            return real_generate(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(import_tasks, "generate_derivatives", gated_generate)
    identifiers = client.post(f"/api/import-sessions/{session_id}/commit").json()["data"]
    task_id = identifiers["task_id"]
    try:
        assert both_started.wait(timeout=10)
        cancelled = client.post(f"/api/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
    finally:
        release.set()

    task = wait_for_task(client, task_id, timeout=10)
    assert task["status"] == "cancelled"
    assert (task["completed"], task["failed"], peak) == (2, 0, 2)
    pages = client.get(f"/api/projects/{identifiers['project_id']}/pages").json()["data"]
    assert [page["import_status"] for page in pages["items"]] == [
        "ready",
        "ready",
        "pending",
        "pending",
    ]


def test_delete_waits_for_inflight_import_pages(
    client: TestClient, monkeypatch, test_settings
) -> None:
    session_id = create_session(client, "folder")
    body = image_bytes((256, 384))
    for page_number in range(1, 4):
        upload(client, session_id, f"chapter/{page_number}.png", body)

    real_generate = import_tasks.generate_derivatives
    release = threading.Event()
    both_started = threading.Event()
    lock = threading.Lock()
    active = 0

    def gated_generate(*args, **kwargs):
        nonlocal active
        with lock:
            active += 1
            if active == 2:
                both_started.set()
        try:
            if not release.wait(timeout=10):
                raise TimeoutError("import workers did not resume")
            return real_generate(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(import_tasks, "generate_derivatives", gated_generate)
    identifiers = client.post(f"/api/import-sessions/{session_id}/commit").json()["data"]
    assert both_started.wait(timeout=10)
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        deleted = client.delete(f"/api/projects/{identifiers['project_id']}")
    finally:
        release.set()
        timer.join()
    assert deleted.status_code == 200
    assert not (test_settings.projects_dir / identifiers["project_id"]).exists()
