# -*- coding: utf-8 -*-
"""Civitai 官方 API 封装（纯标准库，无需第三方依赖）"""
import html as _html_mod
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://civitai.com/api/v1"
SITE_BASE = "https://civitai.com"
# tRPC = C 站的内部接口，评论/评价**只有**这里拿得到（/api/v1/* 公开接口没有这两个能力，
# 油猴脚本时代是靠 fetch() 跑在页面里才顺带用上的）。带 `Authorization: Bearer <API Key>`
# 可以直连：实测同一接口匿名 401 `Please use the public API instead` → 带 Key 200 且有真实数据，
# 所以桌面端不需要浏览器、不需要扩展、不需要页面注入（契约见 docs/ext-archive-contract.md §2.2）。
TRPC_BASE = SITE_BASE + "/api/trpc"
COMMENTS_PROCEDURE = "commentv2.getInfinite"
REVIEWS_PROCEDURE = "resourceReview.getPaged"
# 官方 NsfwLevel 位掩码：PG=1 PG13=2 R=4 X=8 XXX=16（browsingLevels 数组同序）→ 全放行 31。
# /api/v1/images 的过滤是 `browsingLevel ?? nsfw ?? publicBrowsingLevelsFlag`：
# **即使带了 Key**，不显式传 browsingLevel 也只给 PG，非 PG 样图会连图带提示词一起静默丢失。
BROWSING_LEVEL_ALL = 1 | 2 | 4 | 8 | 16
MODEL_URL_RE = re.compile(
    r"civitai\.(?:com|red)/models/(\d+)", re.IGNORECASE
)
DOWNLOAD_URL_RE = re.compile(
    r"civitai\.(?:com|red)/api/download/models/(\d+)", re.IGNORECASE
)
# 版本号可能出现在查询串里（任意位置/顺序）：modelVersionId=123 / modelVersionId=123 等
_VERSION_QUERY_KEYS = ("modelversionid", "modelversion", "versionid")


class CivitaiError(Exception):
    """status = HTTP 状态码（网络错误/解析失败时为 None）：tRPC 的降级文案要能分清 401 与断网"""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """跟随重定向时剥离 Authorization 头：307 会原样转发请求头到预签名 URL（如 R2/S3），
    携带 Bearer 会触发 AWS 签名校验错误（Missing x-amz-content-sha256）"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.headers = {k: v for k, v in new.header_items()
                           if k.lower() != "authorization"}
        return new


def build_opener(proxy=None, verify=True):
    """构造 opener；verify=False 时跳过 TLS 证书验证（代理 MITM 场景兜底）"""
    import ssl as _ssl
    ctx = None
    if not verify:
        ctx = _ssl._create_unverified_context()
    if proxy:
        return urllib.request.build_opener(
            _NoAuthRedirect(),
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx) if ctx else urllib.request.HTTPSHandler(),
        )
    if ctx:
        return urllib.request.build_opener(_NoAuthRedirect(), urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(_NoAuthRedirect())


def _normalize_site_base(site_base):
    """站点根只取 https + host（设置里的 site_domain 可能是 'civitai.red' 或完整 URL）。
    非 https / 带账号信息 / 带 path 一律当没配，回落到默认 SITE_BASE —— 绝不能把 Key 发给别的地址。"""
    raw = str(site_base or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    try:
        u = urllib.parse.urlsplit(raw)
    except Exception:
        return ""
    if u.scheme != "https" or not u.netloc or u.username or u.password or (u.path and u.path != "/"):
        return ""
    return u.scheme + "://" + u.netloc


def _positive_id(value):
    """数字 ID 校验（bool 不算 ID）：非法返回 None，调用方据此**不发请求**"""
    if isinstance(value, bool) or value is None:
        return None
    s = str(value).strip()
    if not re.fullmatch(r"\d+", s):
        return None
    n = int(s)
    return n if n > 0 else None


def build_trpc_url(procedure, payload, base=TRPC_BASE):
    """tRPC 走 GET，入参整包塞在 `?input=<urlencoded {"json":{...}}>` 里。

    这里**不补任何默认键**：`cursor` 必须能整个省略（第一页带 cursor:null 会被
    Zod 判 `expected number, received null` → 400），补键的动作交给调用方。"""
    blob = json.dumps({"json": payload}, ensure_ascii=False, separators=(",", ":"))
    return "%s/%s?input=%s" % (base.rstrip("/"), str(procedure),
                               urllib.parse.quote(blob, safe=""))


def trpc_envelope_data(body):
    """tRPC 信封 → (status, data)。`result.data.json` 是真正的载荷；
    json=null 是站点明确回答「这个实体没有 Thread 行」（no_thread），与「有线程但 0 条」(ok) 不同。"""
    if not isinstance(body, dict):
        return ("shape", None)
    err = body.get("error")
    if isinstance(err, dict):
        j = err.get("json") if isinstance(err.get("json"), dict) else {}
        msg = str(j.get("message") or err.get("message") or "tRPC 返回 error 信封").strip()
        return ("rejected", {"message": msg[:200]})
    result = body.get("result")
    if not isinstance(result, dict):
        return ("shape", None)
    data = result.get("data")
    if not isinstance(data, dict) or "json" not in data:
        return ("shape", None)
    payload = data.get("json")
    if payload is None:
        return ("no_thread", None)
    return ("ok", payload)


class CivitaiAPI:
    def __init__(self, api_key="", timeout=30, proxy=None, verify=True, site_base=""):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.verify = verify
        self.opener = build_opener(proxy, verify)
        # tRPC 的站点根：默认与公开 API 同一个 host（civitai.com）。
        # 只做 https + 无账号信息的裸 origin，绝不允许把 path/query 拼进来（build_trpc_url 会校验）。
        self.site_base = _normalize_site_base(site_base) or SITE_BASE
        self.trpc_base = self.site_base + "/api/trpc"
        self._lock = threading.Lock()

    def _request(self, url, raw=False, retries=3):
        """发一次 GET（带 Key、走 build_opener：跟随预签名重定向时自动剥掉 Authorization）"""
        headers = {"User-Agent": "Mozilla/5.0 (CivitaiFreeTool; +local)"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(url, headers=headers)
        for attempt in range(retries):
            err = None  # 每次尝试独立，避免上次失败残留导致重试成功仍报错
            with self._lock:
                try:
                    with self.opener.open(req, timeout=self.timeout) as resp:
                        body = resp.read()
                except urllib.error.HTTPError as e:
                    raise CivitaiError("HTTP %d: %s" % (e.code, e.reason), status=e.code) from e
                except Exception as e:
                    err = e
            if err is None:
                if raw:
                    return body
                try:
                    return json.loads(body.decode("utf-8"))
                except Exception as e:
                    raise CivitaiError("响应解析失败: %s" % e) from e
            # 网络错误：锁外退避后重试
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
            else:
                raise CivitaiError(str(err))

    def _get(self, path, params=None, raw=False, retries=3):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._request(url, raw=raw, retries=retries)

    # ---- 业务接口 ----
    def get_model(self, model_id):
        """按模型 ID 获取完整模型信息"""
        return self._get("/models/%s" % model_id)

    def get_model_version(self, version_id):
        """按版本 ID 获取版本信息（含 files/downloadUrl）"""
        return self._get("/model-versions/%s" % version_id)

    def get_image_metadata(self,image_id,browsing_level=BROWSING_LEVEL_ALL):
        if isinstance(image_id,bool) or not str(image_id).isdigit():raise ValueError('图片 ID 无效')
        params={'imageId':str(image_id),'withMeta':'true','limit':1}
        # browsingLevel 必须显式给：官方过滤是 `browsingLevel ?? nsfw ?? publicBrowsingLevelsFlag`，
        # 不传只放行 PG —— 实测带 Key 的 Soft 图 145676405 不带参数返回 {"items":[]}，加 31 才 items=1。
        # 少这一个参数，非 PG 样图就连图带提示词一起静默丢失（区域/账号策略仍可能把结果压回 SFW，
        # 那按接口实际返回说话，不在这里编造）。
        try:
            level = int(browsing_level)
        except (TypeError, ValueError):
            level = 0
        if 1 <= level <= 63:
            params['browsingLevel'] = str(level)
        result=self._get('/images',params=params,retries=1)
        for image in result.get('items',[]) if isinstance(result,dict) else []:
            if isinstance(image,dict) and str(image.get('id'))==str(image_id):return image
        return {}

    # ---------------- tRPC：评论 / 评价（公开 /api/v1 没有这两个能力） ----------------
    def trpc_query(self, procedure, payload):
        """一次 tRPC GET → `{status, data, error}`；**任何情况下都不抛错**（降级由调用方决定文案）。

        status ∈ ok / no_thread（json:null）/ unauthorized(401) / rejected(400) /
        rate_limited(429) / http_error / shape / no_key。
        没 Key 时一个包都不发：与 webui 的 `requires_civitai_key` 同口径 —— 匿名 tRPC 是
        「成功地拿到空结果」，写进产物只会被读成「这个模型没人评论」，比跳过更糟。"""
        out = {"status": "no_key", "data": None, "error": ""}
        if not self.api_key:
            out["error"] = "本机未配置 Civitai API Key，未发起 tRPC 请求"
            return out
        try:
            url = build_trpc_url(procedure, payload, base=self.trpc_base)
        except (TypeError, ValueError) as e:
            out.update(status="shape", error="入参无法序列化: %s" % str(e)[:200])
            return out
        try:
            body = self._request(url, retries=1)
        except CivitaiError as e:
            code = getattr(e, "status", None)
            status = {401: "unauthorized", 403: "unauthorized",
                      400: "rejected", 429: "rate_limited"}.get(code, "http_error")
            out.update(status=status, error=str(e)[:200])
            return out
        except Exception as e:
            out.update(status="http_error", error=str(e)[:200])
            return out
        status, data = trpc_envelope_data(body)
        if status == "ok":
            out.update(status="ok", data=data)
        elif status == "rejected":
            out.update(status="rejected", error=(data or {}).get("message", "")[:200])
        elif status == "no_thread":
            out["status"] = "no_thread"
        else:
            out.update(status="shape", error="响应不是预期的 tRPC 信封")
        return out

    def fetch_comments(self, model_id, max_pages=5, limit=50, sort="Newest",
                       replies_depth=2, replies_limit=20):
        """模型评论（commentv2.getInfinite）。返回
        `{status, items, pages, truncated, reply_unknown, error, procedure, max_pages}`，不抛错。
        items 每项：{id,user,content,created_at,reply,reply_to,nsfw,hidden,reactions}。"""
        out = {"procedure": COMMENTS_PROCEDURE, "status": "bad_id", "items": [], "pages": 0,
               "truncated": False, "reply_unknown": 0, "error": "",
               "max_pages": max_pages, "model_id": _positive_id(model_id)}
        entity_id = out["model_id"]
        if entity_id is None:
            out["error"] = "模型 ID 无效，未发起评论请求"
            return out
        if not self.api_key:
            out["status"] = "no_key"
            out["error"] = "本机未配置 Civitai API Key：桌面端不发起在线请求，本次未保存评论"
            return out
        base_input = {"entityType": "model",      # **必须小写**：'Model' → 400 invalid_value
                      "entityId": entity_id,
                      "sort": str(sort or "Newest"),
                      "limit": int(limit),
                      "repliesDepth": int(replies_depth),   # 不给 → 服务层 depth<1 直接返回空线程
                      "repliesLimit": int(replies_limit)}
        labels = {}
        pages = max(1, int(max_pages))
        cursor = None
        for page in range(pages):
            payload = dict(base_input)
            if cursor is not None:      # 第一页整个省略 cursor 键（cursor:null = 400）
                payload["cursor"] = cursor
            res = self.trpc_query(COMMENTS_PROCEDURE, payload)
            out["status"] = res["status"]
            out["error"] = res.get("error") or ""
            if res["status"] != "ok":
                break
            out["pages"] += 1
            out["reply_unknown"] += collect_comment_page(out["items"], res.get("data"), labels)
            nxt = res["data"].get("nextCursor") if isinstance(res["data"], dict) else None
            if isinstance(nxt, bool) or not isinstance(nxt, (int, float)) or int(nxt) <= 0:
                break                   # nextCursor 为 null = 到底
            cursor = int(nxt)
            if page == pages - 1:
                out["truncated"] = True
        return out

    def fetch_reviews(self, model_id, model_version_id, max_pages=5, limit=50):
        """版本评价（resourceReview.getPaged）。返回
        `{status, items, pages, total_items, total_pages, current_page, truncated, error, ...}`。

        两条实测硬约束：**modelId 与 modelVersionId 必须一起传**（只给 modelId 是「成功的空结果」
        items=0/total=0，只给 modelVersionId 直接 400），分页字段是 **page** 不是 cursor
        （paginationSchema={limit,page}，cursor 会被 zod 静默忽略、永远停在第 1 页）。"""
        out = {"procedure": REVIEWS_PROCEDURE, "status": "bad_id", "items": [], "pages": 0,
               "total_items": None, "total_pages": None, "current_page": None,
               "truncated": False, "error": "", "max_pages": max_pages,
               "model_id": _positive_id(model_id), "model_version_id": _positive_id(model_version_id)}
        mid, vid = out["model_id"], out["model_version_id"]
        if vid is None:
            out["error"] = ("缺少版本 ID：只按 modelId 查 getPaged 会拿到成功的空结果"
                            "（实测 items=0/total=0），不能当成「这个版本没有评价」，故未发起请求")
            return out
        if mid is None:
            out["error"] = "缺少模型 ID：getPaged 的 modelId 是 Zod 必填项，缺它必然 400，故未发起请求"
            return out
        if not self.api_key:
            out["status"] = "no_key"
            out["error"] = "本机未配置 Civitai API Key：桌面端不发起在线请求，本次未保存评价"
            return out
        pages = max(1, int(max_pages))
        page = 1
        while page <= pages:
            res = self.trpc_query(REVIEWS_PROCEDURE,
                                  {"modelId": mid, "modelVersionId": vid,
                                   "limit": int(limit), "page": page})
            out["status"] = res["status"]
            out["error"] = res.get("error") or ""
            if res["status"] != "ok":
                break
            data = res.get("data") if isinstance(res.get("data"), dict) else {}
            out["pages"] += 1
            out["total_items"] = data.get("totalItems")
            out["total_pages"] = data.get("totalPages")
            out["current_page"] = _positive_id(data.get("currentPage")) or page
            rows = collect_review_rows(out["items"], data)
            if not rows:
                break
            if isinstance(out["total_pages"], int) and not isinstance(out["total_pages"], bool) \
                    and out["current_page"] >= out["total_pages"]:
                break                   # 按 currentPage >= totalPages 收（实测 67 条 / 2 页）
            page = out["current_page"] + 1
            if page > pages:
                out["truncated"] = True
        return out

    def get_model_version_by_hash(self, sha256):
        """按文件 SHA256 反查模型版本（反向解析核心）"""
        return self._get("/model-versions/by-hash/%s" % sha256)

    def get_model_by_version_id(self, version_id):
        """通过版本 ID 查模型：先拿版本（含 modelId），再取模型详情"""
        v = self.get_model_version(version_id)
        if v.get("modelId"):
            try:
                m = self.get_model(v["modelId"])
                return m, v
            except CivitaiError:
                return None, v
        return None, v

    def resolve_url(self, url):
        """解析 URL，返回 (model_id, version_id)（均可为 None，至少一个非空）或抛错。

        版本号从查询串里取，**不依赖它在 URL 中的位置**：
        真实 C 站链接是 /models/<id>/<slug>?modelVersionId=<vid>（带 slug），
        旧正则要求 ?modelVersionId= 紧跟模型 ID，带 slug 时匹配不到 →
        调用方回退成「最新版」——即用户报的「选了非最新版却下载了最新版」。
        """
        url = (url or "").strip()
        m = DOWNLOAD_URL_RE.search(url)
        if m:
            return None, m.group(1)
        m = MODEL_URL_RE.search(url)
        model_id = m.group(1) if m else None
        version_id = None
        try:
            qs = urllib.parse.urlsplit(url).query
            for k, v in urllib.parse.parse_qsl(qs, keep_blank_values=True):
                if k.lower() in _VERSION_QUERY_KEYS and (v or "").strip().isdigit():
                    version_id = v.strip()
                    break
        except Exception:
            version_id = None
        if model_id:
            return model_id, version_id
        if version_id:
            return None, version_id
        raise CivitaiError("无法解析 URL: %s" % url)

    def pick_file(self, version):
        """从版本信息中挑选主模型文件：优先主文件且非负面嵌入，返回 (file, file_index)"""
        files = version.get("files") or []
        if not files:
            raise CivitaiError("该版本没有文件")
        primary = [f for f in files if f.get("primary")]
        if not primary:
            primary = files
        for f in primary:
            if f.get("type") != "Negative":
                return f, files.index(f)
        return primary[0], files.index(primary[0])

    def model_files(self, version):
        """Only downloadable model variants, not training archives / config attachments."""
        weights = {'.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.gguf', '.onnx', '.sft'}
        def downloadable(f):
            kind = re.sub(r'[\s_-]', '', str(f.get('type', 'Model'))).lower()
            if kind in ('trainingdata', 'config', 'archive', 'image', 'other'):
                return False
            ext = '.' + str(f.get('name', '')).rsplit('.', 1)[-1].lower()
            return kind in ('model', 'negative', 'diffusionmodel', 'vae', 'textencoder', 'clip',
                            'checkpoint', 'lora', 'locon', 'controlnet', 'embedding') or ext in weights
        candidates = [f for f in version.get("files", []) if isinstance(f, dict) and
                      f.get('name') and downloadable(f)]
        if not candidates:
            raise CivitaiError('该版本没有可下载的模型权重文件，未自动下载附件')
        return candidates

    def file_download_url(self, version, file):
        """Use the API's exact per-file URL, preserving format/fp/size selectors."""
        url = str(file.get("downloadUrl") or "")
        if url:
            u = urllib.parse.urlsplit(url)
            if u.scheme != "https" or u.hostname not in ("civitai.com", "civitai.red") or not u.path.startswith("/api/download/models/"):
                raise CivitaiError("文件下载链接不属于 Civitai 官方下载端点")
            query = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
            if file.get("id") is not None:
                existing = [value for key, value in query if key.lower() == "fileid"]
                if existing and any(value != str(file["id"]) for value in existing):
                    raise CivitaiError("文件下载链接与文件 ID 不一致")
                if not existing:
                    query.append(("fileId", str(file["id"])))
                    url = urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(query), u.fragment))
            return url
        if file.get("id") is not None:
            return self.build_download_url(version["id"], file["id"])
        files = self.model_files(version)
        if len(files) == 1:
            return self.build_download_url(version["id"])
        raise CivitaiError("该文件未提供独立下载链接或 ID，拒绝误下主文件")

    def build_download_url(self, version_id, file_id=None):
        # Current website uses fileId for exact variants, including GGUF/quantized files.
        url = "https://civitai.com/api/download/models/%s" % version_id
        return url + "?" + urllib.parse.urlencode({"fileId": file_id}) if file_id is not None else url



def normalize_sha256(s):
    s = (s or "").strip().lower()
    return s if re.fullmatch(r"[0-9a-f]{64}", s) else None


# ============================================================================
# tRPC 结果的归一化与文本渲染（评论.txt 的唯一措辞来源，语义见 docs §2.2）
# 三种情况必须分得开、查得到：① 带 Key 的 tRPC 拿到 N 条 ② 接口返回 null=该实体没有评论区
# ③ 匿名/无 Key/401/限流 → 拿不到受限内容。绝不能把 ②③ 写成「这个模型没有评论」。
# ============================================================================

def comment_plain_text(raw):
    """评论/评价正文是站点**消毒过的 HTML**（<p>…</p>），必须转纯文本再落盘"""
    s = str(raw or "")
    if not s:
        return ""
    s = re.sub(r"(?is)<\s*(?:script|style)[^>]*>.*?<\s*/\s*(?:script|style)\s*>", "", s)
    s = re.sub(r"(?i)<\s*(?:br\s*/?|/p|/div|/li|/blockquote|/tr)\s*>", "\n", s)
    s = re.sub(r"(?s)<[^>]*>", "", s)
    s = _html_mod.unescape(s).replace("\u00a0", " ")
    out = []
    for line in (ln.strip() for ln in s.splitlines()):
        if line:
            out.append(line)
        elif out and out[-1]:
            out.append("")      # 段落之间留一行，连续空行折叠
    return "\n".join(out).strip()


def comment_user_label(node):
    """评论对象的 user 是嵌套对象（顶层**没有** userId）：username → name → 用户#id → 匿名"""
    if not isinstance(node, dict):
        return "匿名"
    u = node.get("user")
    if isinstance(u, dict):
        name = str(u.get("username") or u.get("name") or "").strip()
        if name:
            return name
        if u.get("id") is not None:
            return "用户#%s" % u.get("id")
    if node.get("userId") is not None:
        return "用户#%s" % node.get("userId")
    return "匿名"


def _comment_row(node, reply=False, reply_to=""):
    if not isinstance(node, dict):
        return None
    text = comment_plain_text(node.get("content"))
    if not text:
        return None       # 只剩 <p></p> 的占位不配占一行
    return {"id": node.get("id"), "user": comment_user_label(node), "content": text,
            "created_at": node.get("createdAt") or "", "reply": bool(reply),
            "reply_to": str(reply_to or ""), "nsfw": bool(node.get("nsfw")),
            "hidden": bool(node.get("hidden")), "reactions": node.get("reactionCount")}


def _push_row(items, seen, row):
    """按 id 去重（翻页/兜底可能重叠），同一条绝不写两遍"""
    if row is None:
        return
    key = ("c%s" % row["id"]) if row.get("id") is not None \
        else "x%s|%s|%s" % (row["user"], row["created_at"], row["content"])
    if key in seen:
        return
    seen.add(key)
    items.append(row)


def collect_comment_page(items, data, labels=None):
    """把一页 getInfinite 结果并进 items；返回「回复状态未知」的顶层评论数。

    真实形状（docs §2.2）：主列表是 `data.comments`（不是 items），节点**没有 children**；
    回复挂在 `replyThreads[].comments[]`，父评论 id = `thread.commentId`。
    replyThreads 的站点顺序与阅读顺序无关：按父评论在本页的位置重排，父评论不在本页的排最后。"""
    if not isinstance(data, dict):
        return 0
    labels = labels if labels is not None else {}
    mains = [c for c in (data.get("comments") or []) if isinstance(c, dict)] \
        if isinstance(data.get("comments"), list) else []
    threads = [t for t in (data.get("replyThreads") or []) if isinstance(t, dict)] \
        if isinstance(data.get("replyThreads"), list) else []
    seen = {("c%s" % it.get("id")) for it in items if isinstance(it, dict) and it.get("id") is not None}
    for node in mains:
        if node.get("id") is not None:
            labels[str(node["id"])] = comment_user_label(node)
    for t in threads:
        for node in (t.get("comments") or []):
            if isinstance(node, dict) and node.get("id") is not None:
                labels[str(node["id"])] = comment_user_label(node)
    pos_of = {str(c["id"]): i for i, c in enumerate(mains) if c.get("id") is not None}
    for c in mains:
        _push_row(items, seen, _comment_row(c))
    target = data.get("targetComment")
    if isinstance(target, dict):
        if target.get("id") is not None:
            labels.setdefault(str(target["id"]), comment_user_label(target))
        _push_row(items, seen, _comment_row(target))
    ordered = sorted(enumerate(threads),
                     key=lambda pair: (pos_of.get(str(pair[1].get("commentId")), 1 << 60), pair[0]))
    for _, t in ordered:
        parent_id = t.get("commentId")
        label = labels.get(str(parent_id)) or ("评论#%s" % ("?" if parent_id is None else parent_id))
        for node in (t.get("comments") or []):
            _push_row(items, seen, _comment_row(node, reply=True, reply_to=label))
    # childlessCommentIds 是服务端明确回答「这条没有回复」的名单；两处都不在 = 线程预算没排到它
    thread_parents = {str(t.get("commentId")) for t in threads}
    childless = {str(x) for x in (data.get("childlessCommentIds") or [])} \
        if isinstance(data.get("childlessCommentIds"), list) else set()
    return sum(1 for c in mains
               if c.get("id") is not None
               and str(c["id"]) not in thread_parents
               and str(c["id"]) not in childless)


def collect_review_rows(items, data):
    """getPaged 的条目：{id, rating, details(正文), recommended, createdAt, user, …}。
    旧的 r.recommend / r.content 都不存在，读它们只会得到空正文。返回本页新增条数。"""
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return 0
    seen = {("r%s" % it.get("id")) for it in items if isinstance(it, dict) and it.get("id") is not None}
    added = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = "r%s" % r.get("id") if r.get("id") is not None else \
            "x%s|%s" % (comment_user_label(r), r.get("createdAt") or "")
        if key in seen:
            continue
        seen.add(key)
        items.append({"id": r.get("id"), "user": comment_user_label(r),
                      "rating": r.get("rating"), "recommended": r.get("recommended"),
                      "created_at": r.get("createdAt") or "",
                      "content": comment_plain_text(r.get("details")),
                      "nsfw": bool(r.get("nsfw")), "image_count": r.get("imageCount"),
                      "comment_count": r.get("commentCount"),
                      "model_version_id": r.get("modelVersionId")})
        added += 1
    return added


def _stamp_now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _status_conclusion(result, kind, procedure):
    """[结论] 行：按当次真实状态说话。0 条 / null / 无 Key / 401 / 429 是四种不同的事实。"""
    r = result or {}
    status = r.get("status")
    if status == "no_thread":
        subject = ("这个模型没有评论区" if kind == "评论" else "这个版本没有评价数据")
        return ("[结论] %s 返回 null：%s（是站点对这组参数的回答，不是抓取失败）。"
                % (procedure, subject))
    if status == "no_key":
        return ("[结论] 本机未配置 Civitai API Key：桌面端没有发起在线请求（匿名请求只会拿到空结果）。"
                "本次未保存%s，**这不等于这个模型没有人%s**。" % (kind, kind))
    if status == "unauthorized":
        return ("[结论] 带 Key 的 tRPC 被站点拒绝（HTTP 401/403）：当前 API Key 无效或权限不足，"
                "拿不到受限内容，本次未保存%s。0 条 ≠ 这个模型没有人%s，请更新设置里的 Civitai API Key "
                "后重新下载/归档。" % (kind, kind))
    if status == "rate_limited":
        return ("[结论] 带 Key 的 tRPC 被限流（HTTP 429）：本次未保存%s，稍后重试即可。"
                "0 条 ≠ 这个模型没有人%s。" % (kind, kind))
    if status == "rejected":
        return ("[结论] 站点按当前 schema 拒绝了这组入参（HTTP 400：%s）：本次未保存%s。"
                "0 条 ≠ 这个模型没有人%s。" % (r.get("error") or "入参被 Zod 拒绝", kind, kind))
    if status == "ok":
        return ("[结论] %s 返回 0 条（接口通了，本次结果为空）；这与「返回 null=该实体没有评论区」"
                "不是一回事，也不能写成「这个模型没有人%s」。" % (procedure, kind))
    if status == "bad_id":
        return ("[结论] 缺少可用的模型/版本 ID，桌面端没有发起在线请求，本次未保存%s：%s"
                % (kind, r.get("error") or "ID 无效"))
    return ("[结论] 请求未完成（%s：%s）：本次未保存%s。0 条 ≠ 这个模型没有人%s。"
            % (status or "未知", r.get("error") or "无错误详情", kind, kind))


def render_comments_section(result, model_id, stamp=None):
    r = result or {}
    items = [it for it in (r.get("items") or []) if isinstance(it, dict)]
    lines = ["===== 评论 Comments（模型ID: %s，抓取时间: %s）=====" % (model_id, stamp or _stamp_now())]
    if items:
        replies = sum(1 for it in items if it.get("reply"))
        lines.append("[通道] 数据来自桌面端 tRPC 直连（GET /api/trpc/%s，请求头带已保存的 "
                     "Civitai API Key；不经浏览器、不经扩展、不做页面注入）。" % COMMENTS_PROCEDURE)
        lines.append("[结果] 带 Key 的 tRPC 拿到 %d 条（顶层 %d 条 / 回复 %d 条），翻页 %d 页。"
                     % (len(items), len(items) - replies, replies, r.get("pages") or 0))
        if r.get("truncated"):
            lines.append("[说明] 翻到本文件上限 %s 页为止站点仍给了 nextCursor（还有更多评论），"
                         "以下结果不完整。" % r.get("max_pages"))
        if r.get("reply_unknown"):
            lines.append("[说明] 有 %d 条顶层评论服务端既没返回回复线程、也没列入「无回复」名单"
                         "（站点按预算挑线程），它们的回复可能没保存全。" % r["reply_unknown"])
        blocks = []
        for it in items:
            prefix = "  ↳ 回复 " if it.get("reply") else ""
            tail = " 〔回复 @%s〕" % it["reply_to"] if (it.get("reply") and it.get("reply_to")) else ""
            marks = "".join([" [NSFW]" if it.get("nsfw") else "",
                             " [已隐藏]" if it.get("hidden") else ""])
            blocks.append("%s【%s】(%s)%s%s\n%s\n"
                          % (prefix, it.get("user") or "匿名", it.get("created_at") or "时间未知",
                             tail, marks, it.get("content") or ""))
        lines.append("\n".join(blocks))
    else:
        lines.append(_status_conclusion(r, "评论", COMMENTS_PROCEDURE))
    return "\n".join(lines) + "\n"


def render_reviews_section(result, model_id, version_id, stamp=None):
    r = result or {}
    items = [it for it in (r.get("items") or []) if isinstance(it, dict)]
    label = ("版本ID: %s，模型ID: %s（按版本过滤）" % (version_id, model_id)
             if model_id else ("版本ID: %s（按版本过滤）" % version_id))
    lines = ["===== 评价 Reviews（%s，抓取时间: %s）=====" % (label, stamp or _stamp_now())]
    if items:
        lines.append("[通道] 数据来自桌面端 tRPC 直连（GET /api/trpc/%s，请求头带已保存的 "
                     "Civitai API Key；modelId 与 modelVersionId 同时给，缺一拿到的就是假空）。"
                     % REVIEWS_PROCEDURE)
        lines.append("[结果] 带 Key 的 tRPC 拿到 %d 条评价（接口报告 totalItems=%s、totalPages=%s），"
                     "翻页 %d 页。" % (len(items), r.get("total_items"), r.get("total_pages"),
                                     r.get("pages") or 0))
        if r.get("truncated"):
            lines.append("[说明] 翻到本文件上限 %s 页为止接口仍报告有下一页，以下评价不完整。"
                         % r.get("max_pages"))
        blocks = []
        for it in items:
            bits = []
            if it.get("rating") not in (None, ""):
                bits.append("评分:%s" % it["rating"])
            if isinstance(it.get("recommended"), bool):
                bits.append("推荐:%s" % ("是" if it["recommended"] else "否"))
            marks = " [NSFW]" if it.get("nsfw") else ""
            bits_text = (" " + " ".join(bits)) if bits else ""
            blocks.append("【%s】%s %s%s\n%s\n"
                          % (it.get("user") or "匿名", bits_text,
                             it.get("created_at") or "时间未知", marks,
                             it.get("content") or "(只打了分，没有写文字评价)"))
        lines.append("\n".join(blocks))
    else:
        lines.append(_status_conclusion(r, "评价", REVIEWS_PROCEDURE))
    return "\n".join(lines) + "\n"


def build_comments_document(comments, reviews, model_id, version_id, stamp=None):
    """评论.txt 的完整文本（与浏览器扩展归档的产物同名同义：扩展写 <模型目录>/评论.txt，
    桌面端下载是扁平 sidecar，落成 <模型名>_评论.txt）。"""
    stamp = stamp or _stamp_now()
    head = ("# Civitai 评论与评价（桌面端下载归档）\n"
            "# 模型ID: %s    版本ID: %s    抓取时间: %s\n"
            "# 数据通道: 桌面端直连 C 站 tRPC（Authorization: Bearer <已保存的 Civitai API Key>），"
            "不依赖浏览器扩展\n\n" % (model_id, version_id, stamp))
    return head + render_comments_section(comments, model_id, stamp) + "\n" \
        + render_reviews_section(reviews, model_id, version_id, stamp) + "\n"
