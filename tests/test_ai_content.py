# -*- coding: utf-8 -*-
"""ai_content：中性内容块与 provider 适配的离线测试。"""
import json

import ai_content


def test_text_content_tolerates_bad_input():
    assert ai_content.text_content("hello") == {"type": "text", "text": "hello"}
    assert ai_content.text_content(None) == {"type": "text", "text": ""}
    assert ai_content.text_content(123) == {"type": "text", "text": "123"}


def test_image_content_normalizes_data_url_and_size():
    block = ai_content.image_content(
        "data:image/webp;base64,QUJD", "image/png", width="64", height=None, label="#1")
    assert block["type"] == "image"
    assert block["mime"] == "image/webp"
    assert block["b64"] == "QUJD"
    assert block["width"] == 64
    assert block["height"] is None
    assert block["bytes"] == 3
    assert block["label"] == "#1"

    plain = ai_content.image_content("QUJD", "", width="bad", label="")
    assert plain["mime"] == "image/jpeg"
    assert plain["width"] is None
    assert "label" not in plain

    empty = ai_content.image_content(None, None)
    assert empty["b64"] == ""
    assert empty["bytes"] is None


def test_data_url_parts():
    assert ai_content.data_url_parts("data:image/png;base64,QUJD") == ("image/png", "QUJD")
    assert ai_content.data_url_parts("DATA:IMAGE/JPEG;BASE64,QUJD") == ("image/jpeg", "QUJD")
    assert ai_content.data_url_parts("data:image/png;base64,QUJD\n") == ("image/png", "QUJD")
    assert ai_content.data_url_parts("data:text/plain;base64,QUJD") == ("", "")
    assert ai_content.data_url_parts("data:image/png,QUJD") == ("", "")
    assert ai_content.data_url_parts("https://example.test/a.png") == ("", "")
    assert ai_content.data_url_parts(None) == ("", "")


def test_provider_projection():
    blocks = [
        ai_content.text_content("look"),
        ai_content.image_content("QUJD", "image/png", label="#1"),
        {"type": "unknown"},
        ai_content.image_content("", "image/png"),
    ]
    openai = ai_content.to_openai(blocks)
    assert openai == [
        {"type": "text", "text": "look"},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,QUJD", "detail": "auto"}},
    ]

    anthropic = ai_content.to_anthropic(blocks)
    assert anthropic == [
        {"type": "text", "text": "look"},
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
    ]

    mcp = ai_content.to_mcp(blocks)
    assert mcp == [
        {"type": "text", "text": "look"},
        {"type": "image", "data": "QUJD", "mimeType": "image/png"},
    ]

    assert ai_content.to_openai(None) == []
    assert ai_content.to_openai("plain") == [{"type": "text", "text": "plain"}]
    assert ai_content.to_anthropic({"type": "text", "text": None}) == [
        {"type": "text", "text": ""}]


def test_strip_inline_media_is_recursive_and_non_mutating():
    original = {
        "name": "demo",
        "b64": "AAAA",
        "preview_b64": "BBBB",
        "note": "data:image/png;base64,CCCC",
        "covers": [
            {"url": "https://example.test/a.png", "b64": "DDDD"},
            {"poster": "data:image/jpeg;base64,EEEE", "label": "#2"},
            "data:image/png;base64,FFFF",
            "keep",
        ],
        "meta": {"steps": 20, "b64": ""},
        "text_data": "data:text/plain;base64,AAAA",
    }
    clean, omitted = ai_content.strip_inline_media(original)

    assert omitted == 6
    assert "b64" not in clean
    assert "preview_b64" not in clean
    assert clean["note"] == ""
    assert clean["text_data"] == "data:text/plain;base64,AAAA"
    assert clean["covers"][0] == {"url": "https://example.test/a.png"}
    assert clean["covers"][1] == {"poster": "", "label": "#2"}
    assert clean["covers"][2] == "keep"
    assert clean["meta"] == {"steps": 20}
    assert original["b64"] == "AAAA"
    assert original["covers"][0]["b64"] == "DDDD"


def test_strip_inline_media_bad_input():
    assert ai_content.strip_inline_media(None) == (None, 0)
    value, omitted = ai_content.strip_inline_media("data:image/png;base64,QUJD")
    assert value == "" and omitted == 1
    value, omitted = ai_content.strip_inline_media(42)
    assert value == 42 and omitted == 0


def test_compact_json_truncates_and_serializes():
    assert ai_content.compact_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'
    assert ai_content.compact_json(None) == "null"
    long = ai_content.compact_json({"text": "x" * 200}, max_chars=40)
    assert len(long) <= 40
    assert long.endswith("...[truncated]")
    assert ai_content.compact_json({"bad": object()}).startswith("{")
    assert "nan" in ai_content.compact_json({"x": float("nan")})


def test_compact_json_stays_parsable_when_over_limit():
    """真机暴露的缺陷：超限直接切字符串，MCP 工具结果变成解析不了的残串。"""
    rows = [{"id": "t%d" % i, "filename": "m%d.safetensors" % i, "status": "done",
             "expected_sha256": "AB" * 32, "actual_sha256": "ab" * 32,
             "verification_status": "verified",
             "route_reason": "按 C 站模型类型 LORA 落 loras" * 2}
            for i in range(12)]
    out = ai_content.compact_json({"ok": True, "tasks": rows, "count": len(rows)}, max_chars=1200)
    assert len(out) <= 1200
    data = json.loads(out)
    assert data["ok"] is True
    assert data["truncated"] is True
    dropped = data["dropped_rows"]["$.tasks"]
    assert dropped > 0
    assert len(data["tasks"]) == 12 - dropped
    assert "fields" in data["hint"]


def test_compact_json_wraps_top_level_list():
    out = ai_content.compact_json([[str(i) * 60] for i in range(20)], max_chars=800)
    assert len(out) <= 800
    data = json.loads(out)
    assert data["truncated"] is True
    assert isinstance(data["items"], list) and data["items"]


def test_compact_json_clamps_long_strings_before_dropping_rows():
    payload = {"ok": True, "keep": 1,
               "rows": [{"note": "n" * 5000} for _ in range(3)]}
    out = ai_content.compact_json(payload, max_chars=600)
    assert len(out) <= 600
    data = json.loads(out)
    assert data["keep"] == 1
    assert len(data["rows"]) == 3, "先夹逼长字符串，不该急着丢整行"
    assert data["rows"][0]["note"].endswith("...(截断)")


def test_compact_json_keeps_small_limit_marker_fallback():
    """连外壳都放不下时仍按老口径给标记残串，但绝不抛异常。"""
    out = ai_content.compact_json({"a": {"b": {"c": "x" * 500}}}, max_chars=12)
    assert len(out) <= 12
