from __future__ import annotations

import json
import re
from collections.abc import Iterator

_REASONING_BLOCK = re.compile(
    r"<(?P<tag>think|analysis|reasoning)\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _decoded_candidates(text: str) -> Iterator[object]:
    """Yield JSON values from plain output or output wrapped in prose/markup."""
    decoder = json.JSONDecoder()
    try:
        yield json.loads(text)
    except json.JSONDecodeError:
        pass

    # Providers sometimes add a sentence, a markdown fence, or special model
    # tokens around the JSON.  Decode from every possible JSON start while
    # respecting quoted braces through JSONDecoder.raw_decode().
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        yield value


def parse_json_response(raw: object) -> object:
    """Parse a provider response that may contain harmless output wrappers.

    Structured provider responses can already contain a decoded object/list;
    those values are returned unchanged.  Text responses are allowed to have
    a complete reasoning block, markdown fences, special tokens, or short
    explanatory text around the JSON payload.  The caller remains responsible
    for validating the decoded value against its provider-specific schema.
    """
    if isinstance(raw, dict | list):
        return raw
    if not isinstance(raw, str):
        raise ValueError("response content is not text or JSON")

    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("response content is empty")

    cleaned = _REASONING_BLOCK.sub("", text).strip()
    candidates = [cleaned]
    if cleaned != text:
        candidates.append(text)
    for candidate in candidates:
        for value in _decoded_candidates(candidate):
            # A few gateways JSON-encode the provider's text one extra time.
            # Decode that harmless wrapper too, while leaving normal strings
            # to the schema validator in the caller.
            if isinstance(value, str) and value.strip() != candidate.strip():
                for nested in _decoded_candidates(value.strip()):
                    return nested
            return value
    raise ValueError("response content does not contain a JSON value")
