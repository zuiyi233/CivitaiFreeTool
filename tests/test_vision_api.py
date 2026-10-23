# -*- coding: utf-8 -*-
"""vision_api：离线测试（FakeApi + 临时目录，不发网络请求）。"""
import base64
import io
import json
import os

import pytest
from PIL import Image, PngImagePlugin

import vision_api


# --------------------------------------------------------------------------- helpers


def make_image(path, size=(120, 80), color=(200, 30, 30), alpha=False):
    mode = "RGBA" if alpha else "RGB"
    value = color + (255,) if alpha else color
    image = Image.new(mode, size, value)
    image.save(str(path))
    image.close()
    return str(path)


def b64_file(path):
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def data_url(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = "image/png" if ext == "png" else "image/jpeg"
    return "data:%s;base64,%s" % (mime, b64_file(path))


def open_b64(b64):
    image = Image.open(io.BytesIO(base64.b64decode(b64)))
    image.load()
    return image


class FakeApi:
    def __init__(self, model_info=None, covers=None, info_images=None,
                 remote=None, gallery=None, gallery_json=True, thumbs=None,
                 workflow=None, detail_error=None):
        self.model_info = model_info or {}
        self.covers = covers if covers is not None else []
        self.info_images = info_images if info_images is not None else []
        self.remote = remote or {}
        self.gallery = gallery or {}
        self.gallery_json = gallery_json
        self.thumbs = thumbs or {}
        self.workflow = workflow
        self.detail_error = detail_error
        self.cover_calls = []
        self.thumb_calls = []
        self.model_calls = []
        self.lib_calls = []
        self.workflow_calls = []

    def get_model_detail(self, path):
        self.model_calls.append(path)
        if self.detail_error:
            raise RuntimeError(self.detail_error)
        return json.dumps({
            "ok": True,
            "path": path,
            "name": os.path.basename(path),
            "info": self.model_info,
            "covers": self.covers,
        }, ensure_ascii=False)

    def _info_images(self, path):
        return list(self.info_images)

    def get_cover_b64(self, url, size=512):
        self.cover_calls.append((url, size))
        return self.remote.get(url, "")

    def lib_detail(self, item_id):
        self.lib_calls.append(item_id)
        value = self.gallery.get(item_id)
        if value is None:
            return json.dumps({"ok": False, "msg": "条目不存在"}, ensure_ascii=False)
        if self.gallery_json:
            return json.dumps(value, ensure_ascii=False)
        return value

    def lib_get_thumbs(self, paths_json, size=320):
        paths = json.loads(paths_json)
        self.thumb_calls.append((list(paths), size))
        return json.dumps({path: self.thumbs.get(path, "") for path in paths},
                          ensure_ascii=False)

    def analyze_workflow(self, path):
        self.workflow_calls.append(path)
        if isinstance(self.workflow, Exception):
            raise self.workflow
        if self.workflow is None:
            return json.dumps({"ok": False, "msg": "未找到工作流"}, ensure_ascii=False)
        return json.dumps(self.workflow, ensure_ascii=False)


def model_fixture(tmp_path):
    model = tmp_path / "demo.safetensors"
    model.write_bytes(b"weights")
    cover = make_image(tmp_path / "cover.png", (100, 80), (10, 120, 30))
    nsfw = make_image(tmp_path / "nsfw.png", (60, 60), (250, 200, 10))
    poster = make_image(tmp_path / "poster.png", (64, 48), (30, 30, 200))
    video = tmp_path / "video_01.mp4"
    video.write_bytes(b"not an image")
    local_b64 = b64_file(cover)
    remote_image = make_image(tmp_path / "remote.png", (48, 32), (90, 90, 90))
    info = {
        "name": "Demo LoRA",
        "type": "LORA",
        "baseModel": "PonyXL",
        "trainedWords": ["demo trigger", "another"],
        "images": [
            {"url": "https://example.test/nsfw.png", "nsfwLevel": 4},
            {"url": "https://example.test/remote.png", "nsfwLevel": 1},
        ],
    }
    covers = [
        {"local_path": cover, "local": True, "b64": local_b64,
         "prompt": "best quality", "negative": "lowres",
         "meta": {"steps": "20", "sampler": "Euler a", "seed": 42, "cfgScale": 7},
         "width": 100, "height": 80},
        {"type": "video", "url": video.as_uri(),
         "poster": data_url(poster), "prompt": "video prompt", "negative": "",
         "meta": {"seed": 7}},
        {"url": "https://example.test/remote.png", "prompt": "remote prompt",
         "meta": {"steps": 30, "sampler": "DPM++ 2M"}},
        {"local_path": nsfw, "local": True, "nsfwLevel": 4, "prompt": "nsfw image"},
    ]
    remote = {
        "https://example.test/remote.png": data_url(remote_image),
        "https://example.test/poster.jpg": data_url(poster),
        "https://example.test/fallback.png": data_url(remote_image),
    }
    api = FakeApi(model_info=info, covers=covers, remote=remote)
    return str(model), cover, nsfw, poster, str(video), api


# ----------------------------------------------------------------------- model vision


def test_model_vision_inline_images_meta_and_video_poster(tmp_path):
    model, cover, _nsfw, _poster, video, api = model_fixture(tmp_path)
    result = vision_api.model_vision(api, model, limit_images=4)

    assert result["ok"] is True
    assert result["kind"] == "model"
    assert result["subject"]["path"] == model
    assert result["subject"]["name"] == "Demo LoRA"
    assert result["subject"]["type"] == "LORA"
    assert result["subject"]["base_model"] == "PonyXL"
    assert result["subject"]["trigger_words"] == ["demo trigger", "another"]
    assert result["limits"]["max_images"] == 8
    assert len(result["items"]) == 4
    assert [item["label"] for item in result["items"]] == ["#1", "#2", "#3", "#4"]

    first = result["items"][0]
    assert first["source"] == "local"
    assert first["path"] == cover
    assert first["is_video"] is False
    assert first["image"]["mime"] == "image/jpeg"
    assert first["meta"]["prompt"] == "best quality"
    assert first["meta"]["steps"] == "20"
    assert first["meta"]["model_name"] == "Demo LoRA"
    assert "demo trigger" in first["text"]

    video_item = result["items"][1]
    assert video_item["is_video"] is True
    assert video_item["source"] == "poster"
    assert video_item["image"]["mime"] == "image/jpeg"
    assert open_b64(video_item["image"]["b64"]).size == (64, 48)
    assert all(url != video for url, _size in api.cover_calls)


def test_model_vision_paths_never_reads_images_or_remote(tmp_path):
    model, cover, _nsfw, _poster, video, api = model_fixture(tmp_path)
    result = vision_api.model_vision(api, model, limit_images=4, delivery="paths")

    assert result["ok"] is True
    assert len(result["items"]) == 4
    assert all("image" not in item for item in result["items"])
    assert all("b64" not in json.dumps(item.get("meta", {})) for item in result["items"])
    assert result["items"][0]["path"] == cover
    assert result["items"][1]["is_video"] is True
    assert result["items"][1]["path"] == video
    assert result["items"][2]["url"] == "https://example.test/remote.png"
    assert api.cover_calls == []


def test_model_vision_safe_limit_and_omitted(tmp_path):
    model, _cover, _nsfw, _poster, _video, api = model_fixture(tmp_path)
    safe = vision_api.model_vision(api, model, limit_images=8, safe=True)
    assert safe["ok"] is True
    assert len(safe["items"]) == 3
    assert safe["omitted"] == 1
    assert "NSFW" in safe["note"]

    limited = vision_api.model_vision(api, model, limit_images=2)
    assert len(limited["items"]) == 2
    assert limited["omitted"] == 2


def test_model_vision_falls_back_to_info_images(tmp_path):
    model = tmp_path / "fallback.safetensors"
    model.write_bytes(b"x")
    poster = make_image(tmp_path / "poster.jpg", (40, 30), (5, 5, 5))
    info = {"name": "Fallback", "type": "Checkpoint", "baseModel": "SDXL",
            "trainedWords": ["fw"]}
    images = [
        {"url": "https://example.test/fallback.png", "width": 48, "height": 32,
         "meta": {"prompt": "fallback prompt", "steps": 28}},
        {"type": "video", "url": "https://example.test/v.mp4",
         "imageUrl": "https://example.test/poster.jpg", "meta": {"prompt": "video prompt"}},
    ]
    api = FakeApi(model_info=info, covers=[], info_images=images,
                  remote={"https://example.test/fallback.png": data_url(make_image(
                              tmp_path / "fallback.png", (48, 32), (70, 70, 70))),
                          "https://example.test/poster.jpg": data_url(poster)})
    result = vision_api.model_vision(api, str(model))

    assert result["ok"] is True
    assert len(result["items"]) == 2
    assert result["items"][0]["source"] == "remote"
    assert result["items"][0]["meta"]["prompt"] == "fallback prompt"
    assert result["items"][1]["source"] == "poster"
    assert result["items"][1]["is_video"] is True
    urls = [url for url, _size in api.cover_calls]
    assert "https://example.test/fallback.png" in urls
    assert "https://example.test/poster.jpg" in urls
    assert "https://example.test/v.mp4" not in urls


def test_model_vision_handles_detail_error_and_bad_image(tmp_path):
    model = tmp_path / "broken.safetensors"
    model.write_bytes(b"x")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a real png")
    good = make_image(tmp_path / "good.png", (30, 20), (1, 2, 3))
    api = FakeApi(detail_error="boom", covers=[])
    api.info_images = [{"url": "", "local_path": str(bad)},
                       {"local_path": good, "prompt": "ok"}]
    result = vision_api.model_vision(api, str(model))

    assert result["ok"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["path"] == good
    assert result["omitted"] == 1
    assert any("失败" in note for note in result["notes"])


def test_model_vision_image_budget_stops_with_omitted(tmp_path, monkeypatch):
    model, _cover, _nsfw, _poster, _video, api = model_fixture(tmp_path)
    monkeypatch.setitem(vision_api.LIMITS, "max_image_bytes", 16)
    result = vision_api.model_vision(api, model, limit_images=4)

    assert result["ok"] is True
    assert result["items"] == []
    assert result["omitted"] == 4
    assert "超过上限" in result["note"]


def test_model_vision_bad_paths(tmp_path):
    api = FakeApi()
    missing = tmp_path / "missing.safetensors"
    directory = tmp_path / "dir"
    directory.mkdir()
    assert vision_api.model_vision(api, "")["code"] == "invalid_argument"
    assert vision_api.model_vision(api, str(directory))["code"] == "invalid_argument"
    assert vision_api.model_vision(api, str(missing))["code"] == "not_found"
    result = vision_api.model_vision(api, str(missing))
    assert result["ok"] is False
    assert {"ok", "code", "msg"} <= set(result)


# --------------------------------------------------------------------- gallery vision


def gallery_fixture(tmp_path):
    first = make_image(tmp_path / "one.png", (100, 60), (120, 10, 10))
    second = make_image(tmp_path / "two.jpg", (80, 40), (10, 120, 10))
    poster = make_image(tmp_path / "poster.png", (64, 48), (10, 10, 120))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video bytes")
    ghost = str(tmp_path / "ghost.png")
    gallery = {
        1: {"ok": True, "id": 1, "path": first, "filename": "one.png", "kind": "image",
            "width": 100, "height": 60, "prompt": "p1", "negative": "n1",
            "models": ["modelA"], "sampler": "Euler a", "seed": 1,
            "tags": [{"id": 3, "name": "tag"}], "starred": True,
            "preview": data_url(first)},
        2: {"ok": True, "id": 2, "path": str(video), "filename": "clip.mp4",
            "kind": "video", "width": 640, "height": 480, "prompt": "video",
            "preview": data_url(poster)},
        3: {"ok": True, "id": 3, "path": second, "filename": "two.jpg",
            "kind": "image", "width": 80, "height": 40, "prompt": "p3",
            "preview": ""},
        4: {"ok": True, "id": 4, "path": ghost, "filename": "ghost.png",
            "kind": "image", "width": 32, "height": 32, "preview": ""},
    }
    thumbs = {ghost: data_url(poster)}
    api = FakeApi(gallery=gallery, thumbs=thumbs)
    return first, second, str(video), gallery, api


def test_gallery_vision_inline_preview_path_and_poster(tmp_path):
    _first, _second, video, _gallery, api = gallery_fixture(tmp_path)
    result = vision_api.gallery_vision(api, [1, 2, 3, 4])

    assert result["ok"] is True
    assert result["kind"] == "gallery"
    assert [item["id"] for item in result["items"]] == [1, 2, 3, 4]
    assert [item["label"] for item in result["items"]] == ["#1", "#2", "#3", "#4"]
    assert result["items"][0]["source"] == "local"
    assert result["items"][0]["meta"]["prompt"] == "p1"
    assert result["items"][0]["meta"]["models"] == ["modelA"]
    assert result["items"][0]["meta"]["starred"] is True
    assert result["items"][1]["is_video"] is True
    assert result["items"][1]["source"] == "poster"
    assert result["items"][1]["path"] == video
    assert result["items"][2]["source"] == "local"
    assert result["items"][3]["source"] == "local"
    assert api.thumb_calls


def test_gallery_vision_paths_skips_preview_and_thumbs(tmp_path):
    _first, _second, _video, _gallery, api = gallery_fixture(tmp_path)
    result = vision_api.gallery_vision(api, [1, 2, 3], delivery="paths")

    assert result["ok"] is True
    assert all("image" not in item for item in result["items"])
    assert "b64" not in json.dumps(result)
    assert result["items"][2]["path"].endswith("two.jpg")
    assert api.thumb_calls == []


def test_gallery_vision_missing_id_and_limit(tmp_path):
    first, _second, _video, _gallery, api = gallery_fixture(tmp_path)
    result = vision_api.gallery_vision(api, [1, 99, 3])
    assert result["ok"] is True
    assert [item["id"] for item in result["items"]] == [1, 3]
    assert result["omitted"] == 1
    assert any("99" in note for note in result["notes"])

    for item_id in range(5, 11):
        api.gallery[item_id] = {
            "ok": True, "id": item_id, "path": first,
            "filename": "copy_%d.png" % item_id, "kind": "image",
            "width": 100, "height": 60, "preview": data_url(first),
        }
    limited = vision_api.gallery_vision(api, list(range(1, 11)))
    assert limited["ok"] is True
    assert len(limited["items"]) == 8
    assert limited["omitted"] >= 2


def test_gallery_vision_accepts_detail_dict_and_validates_ids(tmp_path):
    _first, _second, _video, gallery, _api = gallery_fixture(tmp_path)
    api = FakeApi(gallery=gallery, gallery_json=False)
    result = vision_api.gallery_vision(api, ["1", 2])
    assert result["ok"] is True
    assert [item["id"] for item in result["items"]] == [1, 2]

    assert vision_api.gallery_vision(api, None)["code"] == "invalid_argument"
    assert vision_api.gallery_vision(api, [])["code"] == "invalid_argument"
    assert vision_api.gallery_vision(api, ["x"])["code"] == "invalid_argument"


# -------------------------------------------------------------------- workflow vision


def workflow_fixture(tmp_path, absolute_preview=False):
    flow_path = make_image(tmp_path / "flow.png", (200, 100), (40, 50, 60))
    preview = make_image(tmp_path / "preview.png", (90, 60), (60, 50, 40))
    preview_b64 = b64_file(preview)
    if not absolute_preview:
        preview_b64 = "data:image/png;base64," + preview_b64
    data = {
        "ok": True,
        "file": os.path.basename(flow_path),
        "source": "comfyui",
        "preview_b64": preview_b64,
        "pos_prompt": "a cat",
        "neg_prompt": "blurry",
        "models": ["model.safetensors"],
        "node_count": 3,
        "generation_parameters": {"steps": 20, "sampler": "Euler a"},
        "has_workflow": True,
    }
    return flow_path, FakeApi(workflow=data)


def test_workflow_vision_returns_preview_and_parsed_text(tmp_path):
    flow_path, api = workflow_fixture(tmp_path)
    result = vision_api.workflow_vision(api, flow_path)

    assert result["ok"] is True
    assert result["kind"] == "workflow"
    assert result["pos_prompt"] == "a cat"
    assert result["neg_prompt"] == "blurry"
    assert result["models"] == ["model.safetensors"]
    assert result["node_count"] == 3
    assert result["generation_parameters"]["steps"] == 20
    assert result["source"] == "comfyui"
    assert result["subject"]["path"] == flow_path
    assert len(result["items"]) == 1
    assert result["items"][0]["image"]["mime"] == "image/jpeg"
    assert open_b64(result["items"][0]["image"]["b64"]).size == (90, 60)

    paths = vision_api.workflow_vision(api, flow_path, delivery="paths")
    assert paths["ok"] is True
    assert paths["items"][0]["path"] == flow_path
    assert "image" not in paths["items"][0]


def test_workflow_vision_bare_base64_and_no_preview(tmp_path):
    flow_path, api = workflow_fixture(tmp_path, absolute_preview=True)
    result = vision_api.workflow_vision(api, flow_path)
    assert result["ok"] is True
    assert len(result["items"]) == 1

    api.workflow = {"ok": True, "source": "comfyui", "pos_prompt": "only text"}
    empty = vision_api.workflow_vision(api, flow_path)
    assert empty["ok"] is True
    assert empty["items"] == []
    assert empty["note"]
    assert empty["hint"]


def test_workflow_vision_errors(tmp_path):
    flow_path, api = workflow_fixture(tmp_path)
    api.workflow = {"ok": False, "msg": "未找到工作流"}
    result = vision_api.workflow_vision(api, flow_path)
    assert result == {"ok": False, "code": "invalid_media", "msg": "未找到工作流"}

    api.workflow = RuntimeError("boom")
    failed = vision_api.workflow_vision(api, flow_path)
    assert failed["ok"] is False
    assert failed["code"] == "internal_error"

    missing = tmp_path / "missing.json"
    assert vision_api.workflow_vision(api, str(missing))["code"] == "not_found"


# --------------------------------------------------------------------- contact sheet


def test_contact_sheet_gallery_builds_one_labeled_image(tmp_path):
    _first, _second, video, gallery, api = gallery_fixture(tmp_path)
    result = vision_api.contact_sheet(api, "gallery", ids=[1, 2, 3, 4],
                                      limit=4, cols=2, cell=128)

    assert result["ok"] is True
    assert result["kind"] == "contact_sheet"
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["image"]["mime"] == "image/jpeg"
    image = open_b64(item["image"]["b64"])
    assert image.size == (256, 256)
    image.close()
    assert len(result["mapping"]) == 4
    assert [row["label"] for row in result["mapping"]] == ["#1", "#2", "#3", "#4"]
    assert [row["id"] for row in result["mapping"]] == [1, 2, 3, 4]
    assert result["mapping"][0]["prompt"] == "p1"
    assert result["omitted"] == 0
    assert all(row["id"] != 2 or row["filename"] == "clip.mp4"
               for row in result["mapping"])


def test_contact_sheet_model_paths_and_limits(tmp_path):
    model, cover, _nsfw, _poster, _video, api = model_fixture(tmp_path)
    result = vision_api.contact_sheet(api, "model", path=model, limit=2,
                                      cols=2, cell=128)
    assert result["ok"] is True
    assert len(result["mapping"]) == 2
    assert result["mapping"][0]["path"] == cover
    assert result["items"][0]["image"]["mime"] == "image/jpeg"

    _first, _second, _video, _gallery, gallery_api = gallery_fixture(tmp_path)
    limited = vision_api.contact_sheet(gallery_api, "gallery", ids=[1, 2, 3, 4],
                                       limit=2, cols=1, cell=96)
    assert limited["ok"] is True
    assert len(limited["mapping"]) == 2
    assert limited["omitted"] >= 2


def test_contact_sheet_paths_writes_temp_jpeg(tmp_path):
    _first, _second, _video, _gallery, api = gallery_fixture(tmp_path)
    result = vision_api.contact_sheet(api, "gallery", ids=[1, 3], delivery="paths",
                                      cols=2, cell=96)
    assert result["ok"] is True
    item = result["items"][0]
    assert "image" not in item
    assert item["path"]
    assert os.path.isfile(item["path"])
    with Image.open(item["path"]) as image:
        assert image.format == "JPEG"
    os.unlink(item["path"])


def test_contact_sheet_bad_source(tmp_path):
    api = FakeApi()
    assert vision_api.contact_sheet(api, "nope")["code"] == "invalid_argument"
    assert vision_api.contact_sheet(api, "gallery", ids=[])["code"] == "invalid_argument"
    assert vision_api.contact_sheet(api, "model")["code"] == "invalid_argument"


# --------------------------------------------------------------------- inspect image


def metadata_png(tmp_path):
    path = tmp_path / "meta.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters",
                  "a cat\nNegative prompt: blurry\n"
                  "Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 123, Size: 512x512")
    image = Image.new("RGB", (96, 64), (12, 34, 56))
    image.save(str(path), pnginfo=info)
    image.close()
    return str(path)


def test_inspect_image_path_extracts_metadata_and_preview(tmp_path):
    path = metadata_png(tmp_path)
    result = vision_api.inspect_image(None, path=path)

    assert result["ok"] is True
    assert result["kind"] == "image"
    assert result["format"] == "PNG"
    assert result["width"] == 96
    assert result["height"] == 64
    assert result["source"] == "path"
    assert result["filename"] == "meta.png"
    assert result["prompt"] == "a cat"
    assert result["negative"] == "blurry"
    assert result["params"]["steps"] == "20"
    assert result["params"]["sampler"] == "Euler a"
    assert result["params"]["cfg"] == "7"
    assert result["params"]["seed"] == "123"
    assert result["metadata_source"] != "none"
    assert result["image"]["mime"] == "image/jpeg"
    assert open_b64(result["image"]["b64"]).size == (96, 64)


def test_inspect_image_base64_upload_and_data_url(tmp_path):
    path = metadata_png(tmp_path)
    raw_b64 = b64_file(path)
    result = vision_api.inspect_image(None, data_b64="data:image/png;base64," + raw_b64,
                                      filename="upload.png", include_image=False)
    assert result["ok"] is True
    assert result["source"] == "upload"
    assert result["prompt"] == "a cat"
    assert "image" not in result

    plain = vision_api.inspect_image(None, data_b64=raw_b64, max_side=32)
    assert plain["ok"] is True
    assert plain["image"]["width"] <= 64
    assert plain["image"]["height"] <= 64

    paths = vision_api.inspect_image(None, path=path, delivery="paths")
    assert paths["ok"] is True
    assert paths["path"] == path
    assert "image" not in paths


def test_inspect_image_without_metadata_returns_none_hint(tmp_path):
    path = make_image(tmp_path / "plain.jpg", (50, 40), (1, 2, 3))
    result = vision_api.inspect_image(None, path=path)
    assert result["ok"] is True
    assert result["metadata_source"] == "none"
    assert result["hint"]
    assert result["meta"] == {}
    assert result["prompt"] == ""


def test_inspect_image_rejects_non_image_and_bad_args(tmp_path):
    workflow = tmp_path / "flow.json"
    workflow.write_text("{\"nodes\": []}", encoding="utf-8")
    result = vision_api.inspect_image(None, path=str(workflow))
    assert result["ok"] is False
    assert result["code"] == "invalid_media"

    directory = tmp_path / "dir"
    directory.mkdir()
    assert vision_api.inspect_image(None, path=str(directory))["code"] == "invalid_argument"
    assert vision_api.inspect_image(None, path=str(tmp_path / "none.png"))["code"] == "not_found"
    assert vision_api.inspect_image(None)["code"] == "invalid_argument"
    assert vision_api.inspect_image(None, path=str(workflow), data_b64="QUJD")["code"] == "invalid_argument"
    assert vision_api.inspect_image(None, data_b64="***")["code"] == "invalid_media"
    assert vision_api.inspect_image(None, data_b64="data:text/plain;base64,QUJD")["code"] == "invalid_media"


def test_inspect_image_upload_limit(tmp_path, monkeypatch):
    path = make_image(tmp_path / "big.png", (64, 64), (1, 2, 3))
    monkeypatch.setitem(vision_api.LIMITS, "max_upload_bytes", 10)
    result = vision_api.inspect_image(None, path=path)
    assert result["ok"] is False
    assert result["code"] == "invalid_media"
    assert "上限" in result["msg"]
