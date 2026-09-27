from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def wait_for_task(client: TestClient, task_id: str) -> dict[str, object]:
    for _attempt in range(600):
        task = client.get(f"/api/tasks/{task_id}").json()["data"]
        if task["status"] in {"completed", "failed", "cancelled"}:
            return task
        time.sleep(0.1)
    raise TimeoutError("任务未在 60 秒内结束")


def run(model_path: Path, image_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="manga-phase2-") as temporary:
        config = Settings(data_dir=Path(temporary) / "data")
        with TestClient(create_app(config)) as client:
            with model_path.open("rb") as model_file:
                model_response = client.post(
                    "/api/detection-models",
                    data={"name": "真实模型验收"},
                    files={"file": (model_path.name, model_file, "application/octet-stream")},
                )
            model_response.raise_for_status()
            model = model_response.json()["data"]

            session = client.post(
                "/api/import-sessions",
                json={
                    "project_name": "真实检测验收",
                    "source_language": "ja",
                    "target_language": "zh-CN",
                    "translation_mode": "quick",
                    "source_type": "single",
                },
            ).json()["data"]
            with image_path.open("rb") as image_file:
                uploaded = client.post(
                    f"/api/import-sessions/{session['id']}/files",
                    data={"relative_path": image_path.name},
                    files={"file": (image_path.name, image_file, "image/jpeg")},
                )
            uploaded.raise_for_status()
            committed = client.post(f"/api/import-sessions/{session['id']}/commit").json()["data"]
            import_task = wait_for_task(client, committed["task_id"])
            if import_task["status"] != "completed":
                raise RuntimeError(str(import_task))
            page = client.get(f"/api/projects/{committed['project_id']}/pages").json()["data"][
                "items"
            ][0]
            detection = client.post(
                f"/api/projects/{committed['project_id']}/detection-tasks",
                json={
                    "model_id": model["id"],
                    "start_page": 1,
                    "end_page": 1,
                    "confidence": 0.25,
                    "image_size": 1280,
                    "device": "auto",
                    "overwrite": False,
                },
            ).json()["data"]
            task = wait_for_task(client, detection["task_id"])
            regions = client.get(f"/api/pages/{page['id']}/regions").json()["data"]
            print(
                json.dumps(
                    {
                        "model": model["name"],
                        "classes": model["class_names"],
                        "task": task,
                        "region_count": len(regions["regions"]),
                        "revision": regions["revision"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    run(args.model.resolve(), args.image.resolve())


if __name__ == "__main__":
    main()
