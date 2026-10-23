# -*- coding: utf-8 -*-
"""Vision content layer for the AI tools.

Every public function returns a neutral ``dict`` (never MCP SDK objects) and
never raises.  Image bytes are only produced when ``delivery="inline"``; the
``paths`` mode only inspects local state and returns paths/URLs plus metadata.

The module only depends on Pillow, the stdlib and the existing local modules
(``ai_content`` / ``ai_contract`` / ``image_gallery``).  It intentionally does
not import ``automation_api`` or ``webui``.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import tempfile
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageDraw, ImageFont

import ai_content
import ai_contract
import image_gallery

LIMITS = ai_contract.LIMITS

VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv", ".avi")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


class _ImageTooLarge(Exception):
    pass


class _TotalTooLarge(Exception):
    pass


# --------------------------------------------------------------------------- utils


def _jsonish(value):
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def _error(code, msg, **extra):
    out = {"ok": False, "code": str(code or "internal_error"), "msg": str(msg or "unknown error")[:800]}
    out.update(extra)
    return out


def _clamp_int(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = int(default)
    return max(int(low), min(int(high), number))


def _norm_delivery(value):
    return "paths" if str(value or "").strip().lower() == "paths" else "inline"


def _max_side(value, default):
    return _clamp_int(value, default, 64, int(LIMITS.get("max_image_side") or 1536))


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _short(text, limit=360):
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _result(kind, items=None, subject=None, omitted=0, notes=None, **extra):
    out = {
        "ok": True,
        "kind": kind,
        "items": list(items or []),
        "omitted": int(omitted or 0),
        "limits": dict(LIMITS),
    }
    if subject is not None:
        out["subject"] = subject
    clean_notes = [str(n) for n in (notes or []) if str(n or "").strip()]
    out["notes"] = clean_notes
    if clean_notes:
        out["note"] = "; ".join(clean_notes)
    out.update(extra)
    return out


def _suffix(value):
    try:
        path = urlsplit(str(value or "")).path
    except ValueError:
        path = str(value or "")
    return path.lower()


def _is_video_source(value):
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if text.startswith("data:video/") or text.startswith("data:application/octet-stream"):
        return True
    return _suffix(text).endswith(VIDEO_EXTS)


def _is_image_path(value):
    try:
        return (isinstance(value, str) and os.path.isfile(value)
                and os.path.splitext(value)[1].lower() in IMAGE_EXTS)
    except OSError:
        return False


def _file_uri_to_path(value):
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    if parsed.scheme.lower() != "file":
        return ""
    path = unquote(parsed.path or "")
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path) if path else ""


def _decode_b64_text(value):
    if not isinstance(value, str):
        return None
    text = "".join(value.split())
    if not text or len(text) % 4 == 1:
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except Exception:
        try:
            return base64.b64decode(padded, validate=False)
        except Exception:
            return None


def _bytes_from_value(value):
    """Decode bytes / data URL / local path / bare base64.  Returns None on failure."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    mime, b64 = ai_content.data_url_parts(text)
    if mime:
        return _decode_b64_text(b64)
    if text.lower().startswith("data:"):
        return None
    try:
        if os.path.isfile(text):
            with open(text, "rb") as handle:
                return handle.read()
    except (OSError, ValueError):
        pass
    return _decode_b64_text(text)


def _read_file(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def _prepare_pil(im, max_side, quality=82):
    """Resize + re-encode a PIL image.  Alpha images become PNG, others JPEG q82."""
    side = _max_side(max_side, 768)
    source = im.copy()
    work = source
    try:
        mode = str(source.mode or "").upper()
        alpha = mode in ("RGBA", "LA", "PA") or (
            mode == "P" and "transparency" in (getattr(source, "info", {}) or {}))
        if alpha:
            if work.mode != "RGBA":
                work = source.convert("RGBA")
            work.thumbnail((side, side), _LANCZOS)
            buf = io.BytesIO()
            work.save(buf, "PNG", optimize=True)
            mime = "image/png"
        else:
            if work.mode != "RGB":
                work = source.convert("RGB")
            work.thumbnail((side, side), _LANCZOS)
            buf = io.BytesIO()
            work.save(buf, "JPEG", quality=int(quality))
            mime = "image/jpeg"
        raw = buf.getvalue()
        return {
            "mime": mime,
            "b64": base64.b64encode(raw).decode("ascii"),
            "width": int(work.width),
            "height": int(work.height),
            "bytes": len(raw),
        }
    finally:
        for image in (work, source):
            try:
                image.close()
            except Exception:
                pass


def _prepare_bytes(raw, max_side, quality=82):
    if not raw:
        return None
    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        return _prepare_pil(im, max_side, quality)


def _fetch_cover(api, url, size):
    """Fetch a remote cover through webui's cached thumbnail helper."""
    fn = getattr(api, "get_cover_b64", None)
    if not callable(fn):
        return None, "api 不支持 get_cover_b64"
    try:
        value = fn(url, int(size))
    except TypeError:
        try:
            value = fn(url)
        except Exception as exc:
            return None, str(exc)[:160]
    except Exception as exc:
        return None, str(exc)[:160]
    if not value:
        return None, "空响应"
    raw = _bytes_from_value(value)
    if not raw:
        return None, "返回的不是图片数据"
    return raw, ""


def _check_image_budget(prepared, total_bytes):
    limit = int(LIMITS.get("max_image_bytes") or 3 * 1024 * 1024)
    total_limit = int(LIMITS.get("max_total_image_bytes") or 8 * 1024 * 1024)
    size = int(prepared.get("bytes") or 0)
    if size > limit:
        raise _ImageTooLarge("单图 %dB 超过上限 %dB" % (size, limit))
    if total_bytes + size > total_limit:
        raise _TotalTooLarge("图片总字节超过上限 %dB" % total_limit)
    return total_bytes + size


# --------------------------------------------------------------- metadata helpers


def _trigger_words(info):
    raw = _first(info.get("trainedWords"), info.get("trained_words"),
                 info.get("trigger_words"), info.get("triggers"))
    if isinstance(raw, str):
        raw = re.split(r"[,\n]", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def _model_subject(path, info, detail=None):
    detail = detail if isinstance(detail, dict) else {}
    model = info.get("model") if isinstance(info.get("model"), dict) else {}
    name = _first(info.get("name"), info.get("modelName"),
                  detail.get("name"), os.path.basename(path))
    model_type = _first(info.get("type"), model.get("type"),
                        detail.get("type"), "")
    base_model = _first(info.get("baseModel"), info.get("base_model"),
                        model.get("baseModel"), model.get("base_model"),
                        detail.get("base_model"), "")
    subject = {
        "path": path,
        "name": str(name or ""),
        "type": str(model_type or ""),
        "base_model": str(base_model or ""),
        "trigger_words": _trigger_words(info),
    }
    model_id = _first(info.get("modelId"), model.get("id"), info.get("id"))
    version_id = _first(info.get("versionId"), info.get("version_id"))
    if model_id not in (None, ""):
        subject["model_id"] = model_id
    if version_id not in (None, ""):
        subject["version_id"] = version_id
    return subject


def _entry_nsfw(entry):
    if not isinstance(entry, dict):
        return False
    flag = entry.get("nsfw")
    if flag is True:
        return True
    if isinstance(flag, str) and flag.strip().lower() in ("1", "true", "yes", "nsfw"):
        return True
    level = entry.get("nsfwLevel")
    if isinstance(level, bool):
        return level
    if isinstance(level, (int, float)):
        return int(level) > 1
    if isinstance(level, str):
        text = level.strip().lower()
        if text.isdigit():
            return int(text) > 1
        if text in ("mature", "x", "xxx", "nsfw", "explicit"):
            return True
    return False


def _match_info_entry(cover, images):
    """Return (info image entry, index) matching a detail cover."""
    if not isinstance(images, list) or not images:
        return {}, -1
    url = str(cover.get("url") or cover.get("orig_url") or "")
    image_id = cover.get("image_id") or cover.get("id")
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            continue
        if image_id is not None and str(image.get("id")) == str(image_id):
            return image, index
        if url and url in (str(image.get("url") or ""), str(image.get("imageUrl") or "")):
            return image, index
    local = cover.get("local_path")
    if local:
        match = re.search(r"image_(\d+)[.]", os.path.basename(str(local)), re.I)
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(images) and isinstance(images[index], dict):
                return images[index], index
    return {}, -1


def _video_index(cover, fallback=-1):
    for value in (cover.get("url"), cover.get("orig_url"), cover.get("local_path")):
        match = re.search(r"video_(\d+)", str(value or ""), re.I)
        if match:
            return int(match.group(1)) - 1
    return fallback


def _find_local_poster(model_path, index):
    if index is None or index < 0:
        return ""
    base = os.path.splitext(model_path)[0]
    folder = base + ".images"
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = os.path.join(folder, "image_%02d%s" % (int(index) + 1, ext))
        if os.path.isfile(candidate):
            return candidate
    return ""


def _find_local_video(model_path, index):
    if index is None or index < 0:
        return ""
    folder = os.path.splitext(model_path)[0] + ".images"
    for ext in VIDEO_EXTS:
        candidate = os.path.join(folder, "video_%02d%s" % (int(index) + 1, ext))
        if os.path.isfile(candidate):
            return candidate
    return ""


def _params_from_meta(meta):
    if not isinstance(meta, dict):
        return {}
    aliases = {
        "steps": ("steps", "Steps"),
        "sampler": ("sampler", "Sampler", "sampler_name"),
        "scheduler": ("scheduler", "Scheduler"),
        "seed": ("seed", "Seed", "noise_seed"),
        "cfg": ("cfg", "cfgScale", "CFG scale", "CFG"),
        "denoise": ("denoise", "Denoise"),
        "width": ("width", "Width"),
        "height": ("height", "Height"),
        "model": ("Model", "model", "model_name", "checkpoint"),
        "vae": ("VAE", "vae"),
        "clip_skip": ("clip_skip", "Clip skip"),
    }
    params = {}
    for key, names in aliases.items():
        value = None
        for name in names:
            if name in meta and meta.get(name) not in (None, ""):
                value = meta.get(name)
                break
        if value is not None:
            params[key] = value
    size_value = meta.get("Size")
    if isinstance(size_value, str):
        match = re.match(r"\s*(\d+)\s*[xX]\s*(\d+)", size_value)
        if match:
            params.setdefault("width", int(match.group(1)))
            params.setdefault("height", int(match.group(2)))
    return params


def _cover_meta(entry, info, subject, include_prompts):
    source_meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    meta = dict(source_meta)
    for key in ("steps", "sampler", "sampler_name", "scheduler", "seed", "cfg",
                "cfgScale", "denoise", "width", "height"):
        if key in entry and entry.get(key) not in (None, ""):
            meta.setdefault(key, entry[key])
    prompt = _first(entry.get("prompt"), source_meta.get("prompt"), source_meta.get("positive"))
    negative = _first(entry.get("negative"), source_meta.get("negativePrompt"),
                      source_meta.get("negative_prompt"), source_meta.get("negative"))
    if include_prompts:
        if prompt:
            meta["prompt"] = prompt
        if negative:
            meta["negative"] = negative
    else:
        for key in ("prompt", "negative", "negativePrompt", "positive"):
            meta.pop(key, None)
    width = _int_or_none(_first(entry.get("width"), source_meta.get("width"), source_meta.get("Width")))
    height = _int_or_none(_first(entry.get("height"), source_meta.get("height"), source_meta.get("Height")))
    if width is not None:
        meta["width"] = width
    if height is not None:
        meta["height"] = height
    for key, value in (("model_name", subject.get("name")),
                       ("model_type", subject.get("type")),
                       ("base_model", subject.get("base_model")),
                       ("model_path", subject.get("path"))):
        if value not in (None, ""):
            meta.setdefault(key, value)
    triggers = subject.get("trigger_words") or []
    if triggers:
        meta.setdefault("trigger_words", list(triggers))
    meta.update({k: v for k, v in _params_from_meta(source_meta).items() if k not in meta})
    meta, _ = ai_content.strip_inline_media(meta)
    text = _compose_model_text(meta, subject, include_prompts)
    return meta, text, prompt or "", negative or ""


def _compose_model_text(meta, subject, include_prompts):
    parts = []
    kind = str(subject.get("type") or "model").strip()
    name = str(subject.get("name") or "").strip()
    head = kind + (" " + name if name else "")
    if subject.get("base_model"):
        head += " (%s)" % subject["base_model"]
    parts.append(head)
    width, height = meta.get("width"), meta.get("height")
    if width and height:
        parts.append("%sx%s" % (width, height))
    triggers = meta.get("trigger_words") or subject.get("trigger_words") or []
    if triggers:
        parts.append("triggers: " + ", ".join(str(x) for x in triggers[:8]))
    params_meta = dict(meta)
    params_meta.update({k: v for k, v in _params_from_meta(meta).items() if k not in params_meta})
    params = []
    for key, label in (("steps", "steps"), ("sampler", "sampler"), ("seed", "seed"),
                       ("cfg", "cfg"), ("scheduler", "scheduler")):
        if params_meta.get(key) not in (None, ""):
            params.append("%s %s" % (label, params_meta[key]))
    if params:
        parts.append(", ".join(params))
    if include_prompts and meta.get("prompt"):
        parts.append("prompt: " + _short(meta["prompt"], 320))
    if include_prompts and meta.get("negative"):
        parts.append("negative: " + _short(meta["negative"], 160))
    return " | ".join(parts)


# ------------------------------------------------------------------- model vision


def _info_cover(image):
    """Normalise an ``_info_images`` entry into the cover shape used below."""
    out = dict(image)
    is_video = str(image.get("type") or "").lower() == "video" or _is_video_source(image.get("url"))
    out["type"] = "video" if is_video else "image"
    poster = image.get("imageUrl") or image.get("poster")
    if _is_video_source(poster):
        poster = ""
    if is_video:
        out["poster"] = poster or ""
    else:
        out["url"] = image.get("url") or image.get("imageUrl") or ""
    meta = image.get("meta") if isinstance(image.get("meta"), dict) else {}
    out["prompt"] = image.get("prompt") or meta.get("prompt") or ""
    out["negative"] = (image.get("negative") or meta.get("negativePrompt")
                       or meta.get("negative_prompt") or "")
    return out


def model_vision(api, path, limit_images=4, max_side=768, delivery="inline",
                 include_prompts=True, safe=False):
    """Return model cover/sample images plus prompts and generation metadata."""
    try:
        return _model_vision(api, path, limit_images, max_side, delivery,
                             include_prompts, safe)
    except Exception as exc:
        return _error("internal_error", "%s: %s" % (type(exc).__name__, str(exc)[:300]))


def _model_vision(api, path, limit_images, max_side, delivery, include_prompts, safe):
    path = str(path or "").strip()
    if not path:
        return _error("invalid_argument", "path 不能为空")
    if os.path.isdir(path):
        return _error("invalid_argument", "path 必须是文件，不能是目录")
    if not os.path.isfile(path):
        return _error("not_found", "文件不存在: %s" % path)
    if api is None:
        return _error("invalid_argument", "api 不可用")

    mode = _norm_delivery(delivery)
    limit = _clamp_int(limit_images, 4, 1, int(LIMITS.get("max_images") or 8))
    side = _max_side(max_side, 768)
    notes = []

    detail = {}
    try:
        detail = _jsonish(api.get_model_detail(path))
    except Exception as exc:
        notes.append("读取模型详情失败: %s" % str(exc)[:160])
    if not isinstance(detail, dict):
        detail = {}
    info = detail.get("info") if isinstance(detail.get("info"), dict) else {}
    info_images = info.get("images") if isinstance(info.get("images"), list) else []
    covers = detail.get("covers") if isinstance(detail.get("covers"), list) else []

    if not covers:
        try:
            fallback = getattr(api, "_info_images", None)
            if callable(fallback):
                got = fallback(path)
                if isinstance(got, list) and got:
                    info_images = got
                notes.append("模型详情没有 covers，已使用 _info_images 兜底")
            if not isinstance(info_images, list):
                info_images = []
            covers = [_info_cover(item) for item in info_images if isinstance(item, dict)]
        except Exception as exc:
            notes.append("读取 info 示例图失败: %s" % str(exc)[:160])
            covers = []

    subject = _model_subject(path, info, detail)
    model_nsfw = bool(safe and _entry_nsfw(info))
    items = []
    omitted = 0
    total_bytes = 0
    stopped = False

    for index, cover in enumerate(covers):
        if stopped:
            omitted += 1
            continue
        if len(items) >= limit:
            omitted += 1
            continue
        if isinstance(cover, str):
            cover = {"local_path": cover} if _is_image_path(cover) else {"b64": cover}
        if not isinstance(cover, dict):
            omitted += 1
            notes.append("跳过无效封面 #%d" % (index + 1))
            continue

        matched, matched_index = _match_info_entry(cover, info_images)
        if safe and (model_nsfw or _entry_nsfw(cover) or _entry_nsfw(matched)):
            omitted += 1
            notes.append("safe=true，跳过 NSFW 图片 #%d" % (index + 1))
            continue

        is_video = (str(cover.get("type") or "").lower() == "video"
                    or _is_video_source(cover.get("url"))
                    or _is_video_source(cover.get("orig_url"))
                    or _is_video_source(cover.get("local_path")))
        label = "#%d" % (len(items) + 1)
        meta, text, _prompt, _negative = _cover_meta(cover, info, subject, include_prompts)

        try:
            if is_video:
                if mode == "paths":
                    vidx = _video_index(cover, matched_index if matched_index >= 0 else index)
                    media_path = _find_local_video(path, vidx) or _file_uri_to_path(cover.get("url"))
                    url = cover.get("url") if str(cover.get("url") or "").startswith(("http://", "https://")) else ""
                    text = text + " | video poster" if text else "video poster"
                    item = {
                        "id": _first(cover.get("image_id"), cover.get("id"), url or media_path or index + 1),
                        "label": label, "text": text, "meta": meta,
                        "path": media_path or "", "source": "poster", "is_video": True,
                    }
                    if url:
                        item["url"] = url
                    items.append(item)
                    continue

                poster = _first(cover.get("poster"), cover.get("poster_path"),
                                cover.get("preview"), cover.get("imageUrl"))
                raw = None
                poster_path = ""
                if isinstance(poster, str) and poster and not _is_video_source(poster):
                    if os.path.isfile(poster):
                        poster_path = poster
                        raw = _read_file(poster)
                    elif poster.startswith(("http://", "https://")):
                        raw, err = _fetch_cover(api, poster, side)
                        if not raw:
                            notes.append("视频海报下载失败 #%d: %s" % (index + 1, err))
                    else:
                        raw = _bytes_from_value(poster)
                if not raw:
                    vidx = _video_index(cover, matched_index if matched_index >= 0 else index)
                    poster_path = _find_local_poster(path, vidx)
                    if poster_path:
                        raw = _read_file(poster_path)
                if not raw:
                    omitted += 1
                    notes.append("视频 #%d 没有 poster，已跳过（不会把 mp4 当图片）" % (index + 1))
                    continue
                prepared = _prepare_bytes(raw, side)
                if not prepared:
                    omitted += 1
                    notes.append("视频海报解码失败 #%d" % (index + 1))
                    continue
                total_bytes = _check_image_budget(prepared, total_bytes)
                meta["is_video"] = True
                text = text + " | video poster" if text else "video poster"
                item = {
                    "id": _first(cover.get("image_id"), cover.get("id"),
                                 os.path.basename(poster_path) or index + 1),
                    "label": label, "text": text, "meta": meta, "image": prepared,
                    "path": poster_path or "", "source": "poster", "is_video": True,
                }
                items.append(item)
                continue

            local_path = ""
            for key in ("local_path", "path"):
                value = cover.get(key)
                if _is_image_path(value) and not _is_video_source(value):
                    local_path = str(value)
                    break
            url = ""
            for key in ("url", "orig_url"):
                value = cover.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    url = value
                    break

            if mode == "paths":
                media_path = local_path or _file_uri_to_path(cover.get("url")) or ""
                if not media_path and not url:
                    omitted += 1
                    notes.append("封面 #%d 没有本地路径或 URL" % (index + 1))
                    continue
                item = {
                    "id": _first(cover.get("image_id"), cover.get("id"),
                                 os.path.basename(media_path) or url or index + 1),
                    "label": label, "text": text, "meta": meta,
                    "path": media_path, "source": "local" if media_path else "remote",
                    "is_video": False,
                }
                if url:
                    item["url"] = url
                items.append(item)
                continue

            raw = None
            source = "local" if local_path else "remote"
            if local_path:
                raw = _read_file(local_path)
            if not raw:
                raw = _bytes_from_value(cover.get("b64") or cover.get("preview_b64"))
                if raw and not local_path:
                    source = "local" if cover.get("local") else "remote"
            if not raw and url and not _is_video_source(url):
                raw, err = _fetch_cover(api, url, side)
                source = "remote"
                if not raw:
                    notes.append("封面下载失败 #%d: %s" % (index + 1, err))
            if not raw:
                omitted += 1
                notes.append("封面 #%d 没有可用图片数据" % (index + 1))
                continue
            prepared = _prepare_bytes(raw, side)
            if not prepared:
                omitted += 1
                notes.append("封面解码失败 #%d" % (index + 1))
                continue
            total_bytes = _check_image_budget(prepared, total_bytes)
            item = {
                "id": _first(cover.get("image_id"), cover.get("id"),
                             os.path.basename(local_path) or url or index + 1),
                "label": label, "text": text, "meta": meta, "image": prepared,
                "path": local_path, "source": source, "is_video": False,
            }
            if url:
                item["url"] = url
            items.append(item)
        except _ImageTooLarge as exc:
            omitted += 1
            stopped = True
            notes.append(str(exc))
        except _TotalTooLarge as exc:
            omitted += 1
            stopped = True
            notes.append(str(exc))
        except Exception as exc:
            omitted += 1
            notes.append("封面 #%d 处理失败: %s" % (index + 1, str(exc)[:160]))

    extra = {"hint": "检查模型是否已下载封面/示例图；纯文本场景可用 cft_analyze_workflow。"}
    if not items:
        notes.append("没有可用图片")
    return _result("model", items, subject, omitted, notes, **extra)


# ----------------------------------------------------------------- gallery vision


def _normalise_ids(ids):
    if not isinstance(ids, (list, tuple)):
        return None
    out = []
    for value in ids:
        number = _int_or_none(value)
        if number is None or number <= 0:
            continue
        if number not in out:
            out.append(number)
    return out


def _lib_thumb_bytes(api, path, size):
    fn = getattr(api, "lib_get_thumbs", None)
    if not callable(fn) or not path:
        return None
    try:
        data = _jsonish(fn(json.dumps([path], ensure_ascii=False), int(size)))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(path)
    if not isinstance(value, str) or not value:
        return None
    return _bytes_from_value(value)


def _gallery_meta(detail, item_id, include_meta):
    meta = {
        "id": item_id,
        "filename": detail.get("filename") or "",
        "kind": detail.get("kind") or "",
        "width": detail.get("width"),
        "height": detail.get("height"),
    }
    if include_meta:
        for key in ("prompt", "negative", "models", "sampler", "seed", "steps", "cfg",
                    "scheduler", "checkpoint", "loras", "tags", "starred", "path",
                    "size", "mtime", "duration", "exists", "meta_source", "note", "extra"):
            if key in detail:
                meta[key] = detail[key]
        params = _params_from_meta(detail)
        if params:
            meta.setdefault("params", params)
    meta, _ = ai_content.strip_inline_media(meta)
    return meta


def gallery_vision(api, ids, max_side=800, delivery="inline", include_meta=True):
    """Return selected gallery items as images (or paths) plus metadata."""
    try:
        return _gallery_vision(api, ids, max_side, delivery, include_meta)
    except Exception as exc:
        return _error("internal_error", "%s: %s" % (type(exc).__name__, str(exc)[:300]))


def _gallery_vision(api, ids, max_side, delivery, include_meta):
    wanted = _normalise_ids(ids)
    if wanted is None:
        return _error("invalid_argument", "ids 必须是正整数数组")
    if not wanted:
        return _error("invalid_argument", "ids 不能为空（正整数数组）")
    if api is None:
        return _error("invalid_argument", "api 不可用")

    fn = getattr(api, "lib_detail", None)
    if not callable(fn):
        return _error("unsupported", "当前版本不支持 lib_detail")

    mode = _norm_delivery(delivery)
    side = _max_side(max_side, 800)
    limit = int(LIMITS.get("max_images") or 8)
    requested = wanted[:limit]
    overflow = max(0, len(wanted) - len(requested))
    omitted = overflow
    notes = []
    invalid = sum(1 for value in (ids if isinstance(ids, (list, tuple)) else [])
                  if _int_or_none(value) is None or _int_or_none(value) <= 0)
    if invalid:
        omitted += invalid
        notes.append("忽略 %d 个非法 id" % invalid)
    if overflow:
        notes.append("ids 超过 %d 个，只处理前 %d 个" % (limit, limit))

    items = []
    total_bytes = 0
    stopped = False

    for item_id in requested:
        if stopped:
            omitted += 1
            continue
        try:
            detail = _jsonish(fn(item_id))
        except Exception as exc:
            omitted += 1
            notes.append("id %s 读取失败: %s" % (item_id, str(exc)[:160]))
            continue
        if not isinstance(detail, dict) or detail.get("ok") is False:
            omitted += 1
            message = detail.get("msg") if isinstance(detail, dict) else "条目不存在"
            notes.append("id %s 不存在或不可读: %s" % (item_id, message or "条目不存在"))
            continue

        path = str(detail.get("path") or "")
        filename = str(detail.get("filename") or os.path.basename(path) or "")
        kind = str(detail.get("kind") or "").lower()
        is_video = kind == "video" or _is_video_source(path) or _is_video_source(filename)
        if not kind:
            kind = "video" if is_video else "image"
        preview = _first(detail.get("preview"), detail.get("poster"))
        if not isinstance(preview, str):
            preview = ""
        if _is_video_source(preview):
            preview = ""
        meta = _gallery_meta(detail, item_id, include_meta)
        label = "#%d" % (len(items) + 1)

        text_parts = ["%s %s" % (label, filename or ("gallery #%s" % item_id))]
        if detail.get("width") and detail.get("height"):
            text_parts.append("%sx%s" % (detail["width"], detail["height"]))
        if include_meta and detail.get("prompt"):
            text_parts.append("prompt: " + _short(detail["prompt"], 320))
        text = " | ".join(text_parts)

        try:
            if mode == "paths":
                item = {
                    "id": item_id, "label": label, "text": text, "meta": meta,
                    "path": path, "source": "poster" if is_video else "local",
                    "is_video": bool(is_video),
                }
                url = _first(detail.get("url"), detail.get("url_http"),
                             detail.get("url_local"))
                if isinstance(url, str) and url:
                    item["url"] = url
                items.append(item)
                continue

            raw = None
            source = "poster" if is_video else "local"
            if preview:
                if preview.startswith(("http://", "https://")):
                    raw, err = _fetch_cover(api, preview, side)
                    source = "remote"
                    if not raw:
                        notes.append("id %s 预览下载失败: %s" % (item_id, err))
                else:
                    raw = _bytes_from_value(preview)
            if not raw and not is_video and _is_image_path(path):
                raw = _read_file(path)
                source = "local"
            if not raw and path:
                raw = _lib_thumb_bytes(api, path, side)
                if raw:
                    source = "poster" if is_video else "local"
            if not raw:
                omitted += 1
                notes.append("id %s 没有可用图片%s" % (item_id, "（视频无 poster）" if is_video else ""))
                continue
            prepared = _prepare_bytes(raw, side)
            if not prepared:
                omitted += 1
                notes.append("id %s 图片解码失败" % item_id)
                continue
            total_bytes = _check_image_budget(prepared, total_bytes)
            item = {
                "id": item_id, "label": label, "text": text, "meta": meta,
                "image": prepared, "path": path, "source": source,
                "is_video": bool(is_video),
            }
            items.append(item)
        except _ImageTooLarge as exc:
            omitted += 1
            stopped = True
            notes.append(str(exc))
        except _TotalTooLarge as exc:
            omitted += 1
            stopped = True
            notes.append(str(exc))
        except Exception as exc:
            omitted += 1
            notes.append("id %s 处理失败: %s" % (item_id, str(exc)[:160]))

    if not items:
        notes.append("没有可用图片")
    return _result("gallery", items, {"ids": [item["id"] for item in items]},
                   omitted, notes,
                   hint="先用 cft_browse_gallery 获取 id，再确认文件仍在资源目录内。")


# ---------------------------------------------------------------- workflow vision


def workflow_vision(api, path, max_side=1024, delivery="inline"):
    """Return a workflow image preview plus parsed prompts/model references."""
    try:
        return _workflow_vision(api, path, max_side, delivery)
    except Exception as exc:
        return _error("internal_error", "%s: %s" % (type(exc).__name__, str(exc)[:300]))


def _workflow_vision(api, path, max_side, delivery):
    path = str(path or "").strip()
    if not path:
        return _error("invalid_argument", "path 不能为空")
    if os.path.isdir(path):
        return _error("invalid_argument", "path 必须是文件，不能是目录")
    if not os.path.isfile(path):
        return _error("not_found", "文件不存在: %s" % path)
    if api is None:
        return _error("invalid_argument", "api 不可用")

    fn = getattr(api, "analyze_workflow", None)
    if not callable(fn):
        return _error("unsupported", "当前版本不支持 analyze_workflow")
    try:
        data = _jsonish(fn(path))
    except Exception as exc:
        return _error("internal_error", "工作流解析失败: %s" % str(exc)[:300])
    if not isinstance(data, dict):
        return _error("internal_error", "工作流解析结果格式异常")
    if data.get("ok") is False:
        return _error("invalid_media", data.get("msg") or "未找到工作流或生成参数")

    mode = _norm_delivery(delivery)
    side = _max_side(max_side, 1024)
    notes = []
    items = []
    omitted = 0
    preview = data.get("preview_b64")
    raw = _bytes_from_value(preview) if isinstance(preview, str) else None
    if preview and not raw:
        omitted += 1
        notes.append("preview_b64 无法解码")
    pos_prompt = str(data.get("pos_prompt") or "")
    neg_prompt = str(data.get("neg_prompt") or "")
    models = data.get("models") if isinstance(data.get("models"), list) else []
    generation_parameters = data.get("generation_parameters")
    if not isinstance(generation_parameters, dict):
        generation_parameters = {}
    node_count = _int_or_none(data.get("node_count")) or 0
    source = str(data.get("source") or "")
    filename = str(data.get("file") or os.path.basename(path))
    subject = {"path": path, "filename": filename, "source": source}
    extra = {
        "pos_prompt": pos_prompt,
        "neg_prompt": neg_prompt,
        "models": [str(m) for m in models],
        "node_count": node_count,
        "generation_parameters": generation_parameters,
        "source": source,
    }

    text_parts = ["%s %s" % (source or "workflow", filename)]
    if node_count:
        text_parts.append("%d nodes" % node_count)
    if models:
        text_parts.append("models: " + ", ".join(str(m) for m in models[:8]))
    if pos_prompt:
        text_parts.append("prompt: " + _short(pos_prompt, 360))
    if neg_prompt:
        text_parts.append("negative: " + _short(neg_prompt, 160))
    text = " | ".join(text_parts)
    meta = {
        "filename": filename,
        "source": source,
        "node_count": node_count,
        "models": [str(m) for m in models],
        "pos_prompt": pos_prompt,
        "neg_prompt": neg_prompt,
        "generation_parameters": generation_parameters,
    }

    if raw:
        if mode == "paths":
            if os.path.splitext(path)[1].lower() in (".png", ".jpg", ".jpeg", ".webp"):
                items.append({
                    "id": filename, "label": "#1", "text": text, "meta": meta,
                    "path": path, "source": "local", "is_video": False,
                })
            else:
                notes.append("delivery=paths 时工作流 JSON 没有可引用的本地预览图")
        else:
            try:
                prepared = _prepare_bytes(raw, side)
                if prepared:
                    _check_image_budget(prepared, 0)
                    items.append({
                        "id": filename, "label": "#1", "text": text,
                        "meta": meta, "image": prepared, "path": path,
                        "source": "local", "is_video": False,
                    })
                else:
                    omitted += 1
                    notes.append("预览图解码失败")
            except (_ImageTooLarge, _TotalTooLarge) as exc:
                omitted += 1
                notes.append(str(exc))
            except Exception as exc:
                omitted += 1
                notes.append("预览图处理失败: %s" % str(exc)[:160])
    else:
        notes.append("没有可用的预览图")

    hint = "该文件可能是不含预览的 API 格式工作流；提示词与模型引用仍已解析。"
    return _result("workflow", items, subject, omitted, notes, hint=hint, **extra)


# ----------------------------------------------------------------- contact sheet


def _default_font():
    try:
        return ImageFont.load_default(size=18)
    except TypeError:
        return ImageFont.load_default()


def _draw_label(draw, x, y, text, font):
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        width, height = right - left, bottom - top
    except Exception:
        width, height = 6 * len(text), 12
    draw.rectangle([x - 3, y - 2, x + width + 4, y + height + 3], fill=(255, 255, 255))
    draw.text((x, y), text, fill=(0, 0, 0), font=font)


def _temp_jpeg(prepared):
    fd, target = tempfile.mkstemp(prefix="cft_contact_sheet_", suffix=".jpg")
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(base64.b64decode(prepared["b64"]))
        return target
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(target)
        except OSError:
            pass
        return ""


def _chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def contact_sheet(api, source, ids=None, path=None, limit=16, cols=4, cell=256,
                  delivery="inline"):
    """Compose a labeled JPEG grid from gallery ids or model sample images."""
    try:
        return _contact_sheet(api, source, ids, path, limit, cols, cell, delivery)
    except Exception as exc:
        return _error("internal_error", "%s: %s" % (type(exc).__name__, str(exc)[:300]))


def _contact_sheet(api, source, ids, path, limit, cols, cell, delivery):
    source = str(source or "").strip().lower()
    if source not in ("gallery", "model"):
        return _error("invalid_argument", "source 必须是 gallery 或 model")
    mode = _norm_delivery(delivery)
    limit = _clamp_int(limit, 16, 1, int(LIMITS.get("max_contact_items") or 24))
    cols = _clamp_int(cols, 4, 1, 6)
    cell = _clamp_int(cell, 256, 96, 512)
    notes = []
    items = []
    omitted = 0

    if source == "gallery":
        wanted = _normalise_ids(ids)
        if wanted is None:
            return _error("invalid_argument", "source=gallery 时 ids 必须是正整数数组")
        if not wanted:
            return _error("invalid_argument", "source=gallery 时 ids 不能为空")
        invalid = sum(1 for value in (ids if isinstance(ids, (list, tuple)) else [])
                      if _int_or_none(value) is None or _int_or_none(value) <= 0)
        omitted += invalid
        wanted = wanted[:limit]
        omitted += max(0, len(_normalise_ids(ids) or []) - len(wanted))
        chunk_size = int(LIMITS.get("max_images") or 8)
        for chunk in _chunks(wanted, chunk_size):
            part = gallery_vision(api, chunk, max_side=cell, delivery="inline",
                                  include_meta=True)
            if not part.get("ok"):
                notes.append(part.get("msg") or "读取资源库失败")
                continue
            items.extend(part.get("items") or [])
            omitted += int(part.get("omitted") or 0)
            notes.extend(part.get("notes") or [])
    else:
        if not path:
            return _error("invalid_argument", "source=model 时 path 不能为空")
        part = model_vision(api, path, limit_images=min(limit, int(LIMITS.get("max_images") or 8)),
                            max_side=cell, delivery="inline", include_prompts=True)
        if not part.get("ok"):
            return part
        items = part.get("items") or []
        omitted += int(part.get("omitted") or 0)
        notes.extend(part.get("notes") or [])
        subject = part.get("subject") if isinstance(part.get("subject"), dict) else {}
        path = subject.get("path") or path

    usable = [item for item in items
              if isinstance(item, dict) and isinstance(item.get("image"), dict)
              and item["image"].get("b64")][:limit]
    omitted += max(0, len(items) - len(usable))
    if not usable:
        return _error("invalid_media", "没有可用于拼图的图片",
                      notes=list(dict.fromkeys(notes)))

    rows = int(math.ceil(len(usable) / float(cols)))
    canvas = Image.new("RGB", (cols * cell, rows * cell), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = _default_font()
    mapping = []
    failed = 0
    for index, item in enumerate(usable):
        try:
            raw = base64.b64decode(item["image"]["b64"], validate=False)
            with Image.open(io.BytesIO(raw)) as image:
                frame = image.convert("RGBA")
                box = max(8, cell - 12)
                frame.thumbnail((box, box), _LANCZOS)
                tile = Image.new("RGB", (frame.width, frame.height), (255, 255, 255))
                tile.paste(frame, (0, 0), frame)
                frame.close()
            column, row = index % cols, index // cols
            x = column * cell + max(0, (cell - tile.width) // 2)
            y = row * cell + max(0, (cell - tile.height) // 2)
            canvas.paste(tile, (x, y))
            tile.close()
        except Exception as exc:
            failed += 1
            notes.append("拼图跳过第 %d 张: %s" % (index + 1, str(exc)[:120]))
            continue
        label = "#%d" % (len(mapping) + 1)
        _draw_label(draw, column * cell + 8, row * cell + 8, label, font)
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        row_map = {
            "label": label,
            "filename": (os.path.basename(str(item.get("path") or ""))
                         or str(meta.get("filename") or "")
                         or os.path.basename(str(item.get("url") or ""))),
            "prompt": meta.get("prompt") or "",
        }
        if item.get("id") is not None:
            row_map["id"] = item["id"]
        if item.get("path"):
            row_map["path"] = item["path"]
        if item.get("url"):
            row_map["url"] = item["url"]
        mapping.append(row_map)

    if not mapping:
        canvas.close()
        return _error("invalid_media", "所有图片都无法拼入 contact sheet",
                      notes=list(dict.fromkeys(notes)))
    omitted += failed

    prepared = _prepare_pil(canvas, min(max(canvas.width, canvas.height),
                                      int(LIMITS.get("max_image_side") or 1536)))
    canvas.close()
    try:
        _check_image_budget(prepared, 0)
    except (_ImageTooLarge, _TotalTooLarge) as exc:
        return _error("invalid_media", "contact sheet 超出图片上限: %s" % exc)

    sheet_item = {
        "id": "contact_sheet",
        "label": "#1",
        "text": "Contact sheet: %d images, %dx%d cells, %s cell" % (len(mapping), cols, rows, cell),
        "meta": {"source": source, "count": len(mapping), "cols": cols, "rows": rows,
                 "cell": cell},
        "path": "",
        "source": "local",
        "is_video": False,
    }
    if mode == "paths":
        target = _temp_jpeg(prepared)
        if not target:
            return _error("internal_error", "contact sheet 临时文件写入失败")
        sheet_item["path"] = target
    else:
        sheet_item["image"] = prepared

    return _result("contact_sheet", [sheet_item],
                   {"source": source, "ids": [row.get("id") for row in mapping if row.get("id")],
                    "paths": [row.get("path") for row in mapping if row.get("path")]},
                   omitted, notes, mapping=mapping,
                   hint="用 mapping 里的 id 调 cft_view_gallery 看大图。")


# ------------------------------------------------------------------- inspect image


def inspect_image(api, path=None, data_b64=None, filename="", max_side=768,
                  delivery="inline", include_image=True):
    """Inspect a local image path or base64 upload in memory (never writes media)."""
    try:
        return _inspect_image(api, path, data_b64, filename, max_side,
                              delivery, include_image)
    except Exception as exc:
        return _error("internal_error", "%s: %s" % (type(exc).__name__, str(exc)[:300]))


def _inspect_image(api, path, data_b64, filename, max_side, delivery, include_image):
    path = str(path or "").strip()
    upload = data_b64 if isinstance(data_b64, str) else ""
    if not isinstance(data_b64, (str, type(None))) and data_b64 not in (None, ""):
        return _error("invalid_argument", "data_b64 必须是 base64 字符串")
    if bool(path) == bool(upload.strip()):
        return _error("invalid_argument", "path 与 data_b64 必须二选一")

    mode = _norm_delivery(delivery)
    side = _max_side(max_side, 768)
    cap = int(LIMITS.get("max_upload_bytes") or 8 * 1024 * 1024)
    notes = []
    input_source = "path" if path else "upload"

    if path:
        if os.path.isdir(path):
            return _error("invalid_argument", "path 必须是文件，不能是目录")
        if not os.path.isfile(path):
            return _error("not_found", "文件不存在: %s" % path)
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return _error("not_found", "无法读取文件: %s" % str(exc)[:200])
        if size > cap:
            return _error("invalid_media", "图片超过上传上限 %dB" % cap)
        raw = _read_file(path)
        if not raw:
            return _error("invalid_media", "图片读取失败")
        source_name = str(filename or os.path.basename(path))
    else:
        text = upload.strip()
        mime, b64 = ai_content.data_url_parts(text)
        if text.lower().startswith("data:"):
            if not mime:
                return _error("invalid_media", "data URL 不是图片数据")
            text = b64
        else:
            text = "".join(text.split())
        if not text:
            return _error("invalid_argument", "data_b64 不能为空")
        if len(text) * 3 // 4 > cap + 4:
            return _error("invalid_media", "图片超过上传上限 %dB" % cap)
        raw = _decode_b64_text(text)
        if not raw:
            return _error("invalid_media", "data_b64 不是合法 base64 图片")
        if len(raw) > cap:
            return _error("invalid_media", "图片超过上传上限 %dB" % cap)
        source_name = str(filename or "")

    try:
        im = Image.open(io.BytesIO(raw))
    except Exception:
        return _error("invalid_media", "输入不是可解析的图片（工作流 JSON 等非图片请用 cft_analyze_workflow）")
    fmt = ""
    width = height = 0
    info = {}
    prepared = None
    preview_omitted = 0
    try:
        fmt = str(im.format or "")
        width, height = int(im.width), int(im.height)
        info = dict(im.info or {})
        try:
            im.load()
        except Exception:
            return _error("invalid_media", "图片字节损坏，无法解码")
        if include_image:
            try:
                prepared = _prepare_pil(im, side)
                try:
                    _check_image_budget(prepared, 0)
                except (_ImageTooLarge, _TotalTooLarge) as exc:
                    notes.append("预览图超出上限，已省略: %s" % exc)
                    prepared = None
                    preview_omitted = 1
            except Exception as exc:
                notes.append("预览图生成失败: %s" % str(exc)[:160])
                preview_omitted = 1
    finally:
        try:
            im.close()
        except Exception:
            pass

    if not fmt:
        return _error("invalid_media", "无法识别图片格式")

    try:
        generated = image_gallery.generation(image={}, saved=info)
    except Exception as exc:
        notes.append("元数据解析失败: %s" % str(exc)[:160])
        generated = {}
    if not isinstance(generated, dict):
        generated = {}
    meta = generated.get("meta") if isinstance(generated.get("meta"), dict) else {}
    resources = generated.get("resources") if isinstance(generated.get("resources"), list) else []
    if path and not meta and not resources:
        try:
            fallback = image_gallery.generation(local_path=path)
            if isinstance(fallback, dict):
                fallback_meta = fallback.get("meta") if isinstance(fallback.get("meta"), dict) else {}
                fallback_res = fallback.get("resources") if isinstance(fallback.get("resources"), list) else []
                if fallback_meta or fallback_res:
                    generated = fallback
                    meta, resources = fallback_meta, fallback_res
        except Exception:
            pass

    prompt = str(_first(meta.get("prompt"), meta.get("positive")) or "")
    negative = str(_first(meta.get("negativePrompt"), meta.get("negative_prompt"),
                          meta.get("negative")) or "")
    metadata_source = str(generated.get("metadata_source") or "") if (meta or resources) else "none"
    if metadata_source in ("", "未提供生成数据") and not meta and not resources:
        metadata_source = "none"
    params = _params_from_meta(meta)
    if not source_name:
        source_name = "upload." + (fmt.lower() or "img")

    out = {
        "ok": True,
        "kind": "image",
        "items": [],
        "omitted": int(preview_omitted),
        "limits": dict(LIMITS),
        "notes": notes,
        "subject": {
            "path": path,
            "filename": source_name,
            "format": fmt,
            "width": width,
            "height": height,
        },
        "source": input_source,
        "filename": source_name,
        "format": fmt,
        "width": width,
        "height": height,
        "meta": meta,
        "prompt": prompt,
        "negative": negative,
        "params": params,
        "resources": resources,
        "metadata_source": metadata_source,
    }
    if notes:
        out["note"] = "; ".join(notes)
    if metadata_source == "none":
        out["hint"] = "图片没有可读取的内嵌生成参数；可查看 C 站原图页或同步模型信息。"
    if include_image:
        if mode == "paths":
            if path:
                out["path"] = path
            else:
                notes.append("delivery=paths 但上传数据没有本地路径")
                out["note"] = "; ".join(notes)
        elif prepared:
            out["image"] = prepared
    return out
