# -*- coding: utf-8 -*-
"""Neutral AI content blocks shared by MCP / OpenAI / Anthropic adapters.

The neutral shape deliberately keeps only what every provider can carry:

* text:  ``{"type": "text", "text": "..."}``
* image: ``{"type": "image", "mime": "image/jpeg", "b64": "...",
            "width": 512|None, "height": 512|None, "bytes": 123|None,
            "label": "#1"}``   (``label`` is optional)

Everything in this module is pure stdlib and never raises for bad input; callers
are adapters that must be able to pass model/user data straight through.
"""
from __future__ import annotations

import base64
import json
import re

DEFAULT_IMAGE_MIME = "image/jpeg"
_TRUNCATED = "...[truncated]"

_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return str(value)
    except Exception:
        return ""


def _as_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _clean_b64(value):
    return "".join(_as_text(value).split())


def _looks_like_b64(value):
    text = _clean_b64(value)
    if not text or len(text) % 4 == 1:
        return ""
    return text if _B64_RE.match(text) else ""


def _is_data_url(value):
    if not isinstance(value, str):
        return False
    head, sep, _payload = value.strip().partition(",")
    if not sep or head[:5].lower() != "data:" or ";base64" not in head.lower():
        return False
    mime = head[5:].split(";", 1)[0].strip().lower()
    return mime.startswith("image/") and len(mime) > len("image/")


def data_url_parts(value):
    """Return ``(mime, b64)`` for an ``data:image/...;base64,`` URL.

    Only image payloads are accepted.  Anything else (plain text, a normal URL,
    malformed data) returns ``("", "")`` instead of raising.
    """
    if not isinstance(value, str):
        return "", ""
    text = value.strip()
    if not text.lower().startswith("data:"):
        return "", ""
    header, sep, payload = text.partition(",")
    if not sep:
        return "", ""
    meta = header[5:]
    if ";base64" not in meta.lower():
        return "", ""
    mime = meta.split(";", 1)[0].strip().lower()
    if not mime.startswith("image/") or len(mime) <= len("image/"):
        return "", ""
    b64 = _clean_b64(payload)
    if not b64:
        return "", ""
    if not _looks_like_b64(b64):
        return "", ""
    return mime, b64


def text_content(text):
    """Build a neutral text block.  ``None`` becomes an empty string."""
    return {"type": "text", "text": _as_text(text)}


def image_content(b64, mime, width=None, height=None, label=""):
    """Build a neutral image block.

    ``b64`` may also be a ``data:image/...;base64,`` URL; its mime wins over the
    supplied ``mime``.  Invalid/empty payloads still produce a block so callers
    can decide whether to drop it (provider adapters drop empty images).
    """
    raw = _as_text(b64)
    url_mime, url_b64 = data_url_parts(raw)
    if url_mime:
        mime = url_mime
        raw = url_b64
    else:
        raw = _clean_b64(raw)
    mime = _as_text(mime).strip().lower()
    if not mime.startswith("image/"):
        mime = DEFAULT_IMAGE_MIME
    size = None
    if raw and _looks_like_b64(raw):
        try:
            size = len(base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False))
        except Exception:
            size = None
    block = {
        "type": "image",
        "mime": mime,
        "b64": raw,
        "width": _as_int(width),
        "height": _as_int(height),
        "bytes": size,
    }
    text = _as_text(label).strip()
    if text:
        block["label"] = text
    return block


def _blocks(content):
    """Normalise a block / list of blocks into a flat list of image/text dicts."""
    if isinstance(content, str):
        return [text_content(content)]
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, (list, tuple)):
        return []
    out = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in ("text", "image"):
            out.append(item)
    return out


def to_openai(content):
    """Project neutral blocks to OpenAI chat content parts."""
    out = []
    for block in _blocks(content):
        if block.get("type") == "text":
            out.append({"type": "text", "text": _as_text(block.get("text"))})
            continue
        mime = _as_text(block.get("mime")).strip().lower() or DEFAULT_IMAGE_MIME
        b64 = _as_text(block.get("b64"))
        url_mime, url_b64 = data_url_parts(b64)
        if url_mime:
            mime, b64 = url_mime, url_b64
        else:
            b64 = _clean_b64(b64)
        if not _looks_like_b64(b64):
            continue
        out.append({
            "type": "image_url",
            "image_url": {
                "url": "data:%s;base64,%s" % (mime, b64),
                "detail": "auto",
            },
        })
    return out


def to_anthropic(content):
    """Project neutral blocks to Anthropic messages content blocks."""
    out = []
    for block in _blocks(content):
        if block.get("type") == "text":
            out.append({"type": "text", "text": _as_text(block.get("text"))})
            continue
        mime = _as_text(block.get("mime")).strip().lower() or DEFAULT_IMAGE_MIME
        b64 = _as_text(block.get("b64"))
        url_mime, url_b64 = data_url_parts(b64)
        if url_mime:
            mime, b64 = url_mime, url_b64
        else:
            b64 = _clean_b64(b64)
        if not _looks_like_b64(b64):
            continue
        out.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
    return out


def to_mcp(content):
    """Project neutral blocks to MCP content blocks (dicts, not SDK objects)."""
    out = []
    for block in _blocks(content):
        if block.get("type") == "text":
            out.append({"type": "text", "text": _as_text(block.get("text"))})
            continue
        mime = _as_text(block.get("mime")).strip().lower() or DEFAULT_IMAGE_MIME
        b64 = _as_text(block.get("b64"))
        url_mime, url_b64 = data_url_parts(b64)
        if url_mime:
            mime, b64 = url_mime, url_b64
        else:
            b64 = _clean_b64(b64)
        if not _looks_like_b64(b64):
            continue
        out.append({"type": "image", "data": b64, "mimeType": mime})
    return out


_DROP = object()


def strip_inline_media(obj):
    """Recursively drop image bytes while keeping every other field.

    * dict keys ``b64`` / ``preview_b64`` are removed;
    * ``data:image/...;base64,...`` string values become ``""``;
    * the same strings inside lists are removed entirely.

    Returns ``(clean_obj, omitted_count)``.  The input object is never mutated.
    """
    omitted = 0

    def walk(value):
        nonlocal omitted
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key in ("b64", "preview_b64"):
                    if item not in (None, "", b"", bytearray(), [], {}):
                        omitted += 1
                    continue
                cleaned = walk(item)
                out[key] = "" if cleaned is _DROP else cleaned
            return out
        if isinstance(value, list):
            out = []
            for item in value:
                cleaned = walk(item)
                if cleaned is _DROP:
                    continue
                out.append(cleaned)
            return out
        if isinstance(value, tuple):
            out = []
            for item in value:
                cleaned = walk(item)
                if cleaned is _DROP:
                    continue
                out.append(cleaned)
            return tuple(out)
        if isinstance(value, str) and _is_data_url(value):
            omitted += 1
            return _DROP
        return value

    try:
        cleaned = walk(obj)
    except Exception:
        return obj, 0
    if cleaned is _DROP:
        return "", omitted
    return cleaned, omitted


_CLAMP_NOTE = "...(截断)"


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=str, allow_nan=False)


def _clamp_strings(value, budget):
    """夹逼超长字符串（URL 签名、描述、base64 残片），保留可辨识的开头。"""
    if isinstance(value, str):
        if len(value) <= budget:
            return value
        return value[:max(0, budget - len(_CLAMP_NOTE))] + _CLAMP_NOTE
    if isinstance(value, dict):
        return dict((k, _clamp_strings(v, budget)) for k, v in value.items())
    if isinstance(value, list):
        return [_clamp_strings(v, budget) for v in value]
    return value


def _row_lists(obj, path="$"):
    """列出所有还能整行丢弃的数组（连父容器与键的路径），顶层数组由调用方补。"""
    out = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            here = "%s.%s" % (path, key)
            if isinstance(val, list) and val:
                out.append((obj, key, here))
            out.extend(_row_lists(val, here))
    elif isinstance(obj, list):
        for idx, val in enumerate(obj):
            out.extend(_row_lists(val, "%s[%d]" % (path, idx)))
    return out


def _annotate(work, dropped, limit):
    if not dropped:
        return work
    total = sum(dropped.values())
    note = {"truncated": True, "dropped_rows": dropped,
            "hint": "结果超过 %d 字符，已整行丢弃 %d 行（保持合法 JSON）；"
                    "需要更全请用 fields/limit 缩小或分页再取" % (limit, total)}
    if isinstance(work, dict):
        out = dict(work)
        out.update(note)
        return out
    wrapped = {"items": work}
    wrapped.update(note)
    return wrapped


def _shrink_to_fit(text, limit):
    """在 limit 内尽力保持合法 JSON；放不进就返回 None，由调用方如实切片标记。"""
    reserve = len(_dumps({"truncated": True, "dropped_rows": {"$": 1},
                          "hint": "x" * 96})) + 8
    if limit < 64 + reserve:
        return None
    try:
        work = json.loads(text)
    except Exception:
        return None
    if not isinstance(work, (dict, list)):
        return None
    work = _clamp_strings(work, max(32, min(320, limit // 8)))
    dropped = {}
    for _ in range(2000):
        try:
            cur = _dumps(_annotate(work, dropped, limit))
        except Exception:
            return None
        if len(cur) <= limit:
            return cur
        cands = _row_lists(work)
        if isinstance(work, list) and work:
            cands.append((None, None, "$"))
        if not cands:
            return None
        def _size(c):
            return len(work) if c[0] is None else len(c[0][c[1]])
        cont, key, where = max(cands, key=_size)
        if cont is None:
            work.pop()
        else:
            cont[key] = cont[key][:-1]
        dropped[where] = dropped.get(where, 0) + 1
    return None


def compact_json(data, max_chars=4000):
    """把结果压进 max_chars。

    旧实现直接切字符串再补标记，超限的工具结果就成了**解析不了的残串**——Agent 拿到
    `Unterminated string`，只会当成接口坏了。现在按结构收缩：先夹逼超长字符串，
    再整行丢弃有序列表的尾部，并如实标 `truncated/dropped_rows/hint`。
    只有连外壳都放不下的极端小 limit 才退回切片。
    """
    try:
        limit = int(max_chars)
    except (TypeError, ValueError, OverflowError):
        limit = 4000
    if limit < 1:
        limit = 1
    try:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                          default=str, allow_nan=False)
    except Exception:
        try:
            text = json.dumps(_as_text(data), ensure_ascii=False)
        except Exception:
            text = '""'
    if len(text) <= limit:
        return text
    shrunk = _shrink_to_fit(text, limit)
    if shrunk is not None:
        return shrunk
    if limit <= len(_TRUNCATED):
        return _TRUNCATED[:limit]
    return text[:limit - len(_TRUNCATED)] + _TRUNCATED
