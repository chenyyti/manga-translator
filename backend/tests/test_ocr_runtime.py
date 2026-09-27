from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.providers.ocr.base import OCRImageInput
from app.providers.ocr.local_runtime import (
    LocalOCRRuntime,
    _paddle_batch_once,
    _result_payload,
    paddle_model_dir,
    recognize_batch_local,
)
from app.providers.ocr.registry import fixed_provider_for_language, resolve_provider


def test_fixed_provider_routing_and_explicit_language_guard() -> None:
    settings = SimpleNamespace(
        japanese_provider="paddleocr",
        korean_provider="mangaocr",
        english_provider="mangaocr",
    )
    assert fixed_provider_for_language("ja") == "mangaocr"
    assert fixed_provider_for_language("ko") == "paddleocr"
    assert fixed_provider_for_language("en") == "paddleocr"
    assert resolve_provider("ja", "auto", settings) == "mangaocr"
    assert resolve_provider("ko", "auto", settings) == "paddleocr"
    assert resolve_provider("en", "auto", settings) == "paddleocr"
    assert resolve_provider("ja", "auto", settings, requested="mangaocr") == "mangaocr"
    assert resolve_provider("ko", "paddleocr", settings, requested="auto") == "paddleocr"
    with pytest.raises(ValueError):
        resolve_provider("ja", "paddleocr", settings)
    with pytest.raises(ValueError):
        resolve_provider("en", "auto", settings, requested="mangaocr")
    with pytest.raises(ValueError):
        fixed_provider_for_language("fr")


def test_paddle_result_and_onnx_cache_path(tmp_path) -> None:
    payload = _result_payload({"res": {"rec_text": "Hello", "rec_score": 0.98}})
    assert payload == {"rec_text": "Hello", "rec_score": 0.98}
    assert paddle_model_dir(tmp_path, "ko").name == "korean_PP-OCRv5_mobile_rec_onnx"
    with pytest.raises(RuntimeError):
        _result_payload([])


@pytest.mark.asyncio
async def test_installed_packages_are_not_reported_ready_without_markers(tmp_path) -> None:
    runtime = LocalOCRRuntime(tmp_path)
    try:
        providers = await runtime.providers()
        assert all(not provider.model_ready for provider in providers)
    finally:
        await runtime.close()


def _image(region_id: str, color: tuple[int, int, int] = (10, 20, 30)) -> OCRImageInput:
    return OCRImageInput(region_id, 2, 1, bytes(color * 2))


def test_batch_runtime_preserves_order_and_reduces_cuda_batch_on_oom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []

    def fake_batch(inputs, _target, _models_root):
        seen.append(tuple(item.region_id for item in inputs))
        if len(inputs) > 2:
            raise RuntimeError("CUDA out of memory")
        return [(f"text-{item.region_id}", None) for item in inputs]

    monkeypatch.setattr("app.providers.ocr.local_runtime._cuda_available", lambda _provider: True)
    monkeypatch.setattr("app.providers.ocr.local_runtime._manga_batch_once", fake_batch)
    inputs = tuple(_image(f"r{index}") for index in range(5))

    result = recognize_batch_local("mangaocr", inputs, "ja", "auto", "models", 8)

    assert [item["region_id"] for item in result["items"]] == [
        "r0",
        "r1",
        "r2",
        "r3",
        "r4",
    ]
    assert result["requested_batch_size"] == 8
    assert result["effective_batch_size"] <= 2
    assert seen[0] == ("r0", "r1", "r2", "r3", "r4")


def test_batch_runtime_auto_falls_back_to_cpu_for_single_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_batch(inputs, target, _models_root):
        if target.startswith("cuda"):
            raise RuntimeError("provider failure")
        return [(f"cpu-{item.region_id}", None) for item in inputs]

    monkeypatch.setattr("app.providers.ocr.local_runtime._cuda_available", lambda _provider: True)
    monkeypatch.setattr("app.providers.ocr.local_runtime._manga_batch_once", fake_batch)

    result = recognize_batch_local("mangaocr", (_image("r1"),), "ja", "auto", "models", 8)

    assert result["items"][0]["text"] == "cpu-r1"
    assert result["items"][0]["actual_device"] == "cpu"
    assert result["effective_batch_size"] == 1


def test_batch_runtime_isolates_one_bad_region_without_losing_neighbors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_batch(inputs, _target, _models_root):
        if any(item.region_id == "bad" for item in inputs):
            raise ValueError("bad crop")
        return [(f"text-{item.region_id}", None) for item in inputs]

    monkeypatch.setattr("app.providers.ocr.local_runtime._cuda_available", lambda _provider: True)
    monkeypatch.setattr("app.providers.ocr.local_runtime._manga_batch_once", fake_batch)
    inputs = (_image("left"), _image("bad"), _image("right"))

    result = recognize_batch_local("mangaocr", inputs, "ja", "cuda", "models", 8)

    assert [item["text"] for item in result["items"]] == ["text-left", "", "text-right"]
    assert result["items"][1]["error"] == "本区域 OCR 失败"


def test_batch_runtime_forces_single_item_batches_on_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []

    def fake_batch(inputs, target, _models_root):
        seen.append(len(inputs))
        assert target == "cpu"
        return [(item.region_id, None) for item in inputs]

    monkeypatch.setattr("app.providers.ocr.local_runtime._cuda_available", lambda _provider: False)
    monkeypatch.setattr("app.providers.ocr.local_runtime._manga_batch_once", fake_batch)

    result = recognize_batch_local(
        "mangaocr",
        (_image("r1"), _image("r2"), _image("r3")),
        "ja",
        "auto",
        "models",
        8,
    )

    assert seen == [1, 1, 1]
    assert result["requested_batch_size"] == 8
    assert result["effective_batch_size"] == 1


def test_paddle_in_memory_images_are_passed_as_bgr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePaddle:
        def predict(self, *, input, batch_size):
            assert batch_size == 1
            assert input[0][0, 0].tolist() == [30, 20, 10]
            return [{"res": {"rec_text": "Hello", "rec_score": 0.9}}]

    monkeypatch.setattr(
        "app.providers.ocr.local_runtime._paddle_model",
        lambda _language, _models_root, _target: FakePaddle(),
    )

    assert _paddle_batch_once((_image("r1"),), "en", "cuda:0", "models") == [
        ("Hello", 0.9)
    ]
