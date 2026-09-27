from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def register(model_path: Path, name: str) -> None:
    digest = sha256_file(model_path)
    with TestClient(create_app(Settings())) as client:
        existing = client.get("/api/detection-models").json()["data"]["items"]
        match = next((model for model in existing if model["sha256"] == digest), None)
        if match and match["name"] == name:
            print(json.dumps({"registered": False, "model": match}, ensure_ascii=False, indent=2))
            return
        if match:
            removed = client.delete(f"/api/detection-models/{match['id']}")
            removed.raise_for_status()
        with model_path.open("rb") as model_file:
            response = client.post(
                "/api/detection-models",
                data={"name": name},
                files={"file": (model_path.name, model_file, "application/octet-stream")},
            )
        response.raise_for_status()
        print(
            json.dumps(
                {"registered": True, "model": response.json()["data"]},
                ensure_ascii=False,
                indent=2,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("--name", default="漫画文本区域检测")
    args = parser.parse_args()
    register(args.model.resolve(), args.name)


if __name__ == "__main__":
    main()
