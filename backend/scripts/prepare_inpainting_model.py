from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.providers.inpainting.local_runtime import lama_manifest_path, lama_model_path

MODEL_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
EXPECTED_MD5 = "e3aa4aaa15225a33ec84f9f4bc47e500"


def _digest(path: Path, algorithm: str) -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="显式准备 Big-LaMa TorchScript 模型")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    settings.ensure_directories()
    root = settings.inpainting_models_dir
    root.mkdir(parents=True, exist_ok=True)
    target = lama_model_path(root)
    manifest = lama_manifest_path(root)
    if target.is_file() and manifest.is_file() and not args.force:
        print(f"模型已存在：{target}")
        return
    with tempfile.NamedTemporaryFile(dir=root, prefix="big-lama-", suffix=".part", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        print(f"下载 Big-LaMa（仅由用户显式启动）：{MODEL_URL}")
        urllib.request.urlretrieve(MODEL_URL, temporary)
        md5 = _digest(temporary, "md5")
        if md5.casefold() != EXPECTED_MD5:
            raise RuntimeError(f"上游 MD5 校验失败：{md5}")
        sha256 = _digest(temporary, "sha256")
        os.replace(temporary, target)
        ready = {
            "provider": "lama",
            "model": "big-lama",
            "url": MODEL_URL,
            "md5": md5,
            "sha256": sha256,
            "prepared_at": datetime.now(UTC).isoformat(),
        }
        part_manifest = manifest.with_suffix(".json.part")
        part_manifest.write_text(json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(part_manifest, manifest)
        print(f"Big-LaMa 已就绪：{target}\nSHA-256: {sha256}")
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
