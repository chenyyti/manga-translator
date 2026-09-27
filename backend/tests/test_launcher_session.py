from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.launcher_session import LauncherSession


def test_launcher_session_expires_after_heartbeat_timeout() -> None:
    current = [0.0]
    session = LauncherSession(
        "test-session",
        heartbeat_interval_seconds=10,
        heartbeat_timeout_seconds=30,
        clock=lambda: current[0],
    )

    assert session.status("test-session") == {"managed": True, "active": False}
    session.heartbeat("test-session")
    assert session.status("test-session") == {"managed": True, "active": True}
    current[0] = 31.0
    assert session.status("test-session") == {"managed": True, "active": False}


def test_launcher_session_api_accepts_only_current_session(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        project_root=tmp_path / "project",
        launcher_session_id="test-session",
        launcher_heartbeat_interval_seconds=10,
        launcher_heartbeat_timeout_seconds=30,
    )
    with TestClient(create_app(settings)) as client:
        info = client.get("/api/runtime/session")
        assert info.status_code == 200
        assert info.json()["data"] == {
            "managed": True,
            "session_id": "test-session",
            "heartbeat_interval_seconds": 10,
            "heartbeat_timeout_seconds": 30,
        }

        invalid = client.post(
            "/api/runtime/session/heartbeat", json={"session_id": "wrong-session"}
        )
        assert invalid.status_code == 403
        assert invalid.json()["error"]["code"] == "LAUNCHER_SESSION_INVALID"

        heartbeat = client.post(
            "/api/runtime/session/heartbeat", json={"session_id": "test-session"}
        )
        assert heartbeat.status_code == 200
        status = client.get(
            "/api/runtime/session/status", params={"session_id": "test-session"}
        )
        assert status.status_code == 200
        assert status.json()["data"] == {"managed": True, "active": True}
