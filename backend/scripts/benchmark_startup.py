"""Measure isolated application startup without opening the user's database."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    started = time.perf_counter()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    import_ms = (time.perf_counter() - started) * 1000
    results = []
    with tempfile.TemporaryDirectory(prefix="manga-startup-") as temporary:
        settings = Settings(data_dir=Path(temporary))
        for index in range(args.runs):
            start = time.perf_counter()
            with TestClient(create_app(settings)) as client:
                assert client.get("/api/health").status_code == 200
                ready_ms = (time.perf_counter() - start) * 1000
                response = client.get("/api/runtime/startup")
                if response.status_code == 200 and "application/json" in response.headers.get(
                    "content-type", ""
                ):
                    while response.json()["data"]["yolo"]["status"] == "preparing":
                        time.sleep(0.05)
                        response = client.get("/api/runtime/startup")
                yolo_ms = (time.perf_counter() - start) * 1000
            results.append(
                {"run": index + 1, "core_ms": round(ready_ms), "yolo_ms": round(yolo_ms)}
            )
    print(
        json.dumps(
            {
                "import_ms": round(import_ms),
                "runs": results,
                "core_median_ms": round(statistics.median(r["core_ms"] for r in results)),
            }
        )
    )


if __name__ == "__main__":
    main()
