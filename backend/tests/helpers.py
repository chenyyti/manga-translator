from __future__ import annotations

import io
import time

from fastapi.testclient import TestClient
from PIL import Image


def image_bytes(size: tuple[int, int] = (640, 960), color: str = "white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def wait_for_task(client: TestClient, task_id: str, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()["data"]
        if task["status"] in {"completed", "failed", "cancelled"}:
            return task
        time.sleep(0.03)
    raise AssertionError("task did not finish")
