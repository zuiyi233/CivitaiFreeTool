# 资源库（生成媒体库）实施契约 v1 — 方案 A

> 用途：跨文件实施契约。每个实现代理**只拥有自己的文件**，一切跨模块形状以本文件为准。
> 与 `docs/media-library-design.md`（设计意图）配套；冲突时以本契约的实现细节为准，并在本文件底部「偏差记录」里写明。

## 0. 文件所有权（严格互斥，禁止越界）

| 代理 | 独占文件 | 禁止改动 |
|---|---|---|
| A1 | `media_meta.py`（新）、`tests/test_media_meta.py`（新） | 其他一切 |
| A2 | `media_library.py`（新）、`tests/test_media_library.py`（新） | `media_meta.py`（只按 §2 契约 import） |
| A3 | `webui.py`（只在文件末尾追加 `lib_` 段）、`config.py`（只加 §6 三个 DEFAULTS 键） | `media_library.py`、`media_meta.py` 的实现 |
| A4 | `web/index.html`、`web/style.css`、`web/workbench.css`（只追加，不改既有一行） | `web/app.js` |
| A5 | `web/app.js`（只追加 `// ===== 资源库 =====` 段 + `switchPage` 内 1 行钩子） | `web/index.html`、CSS |
| A6 | `browser_bridge.py`（只加 `/api/media` GET 分支 + 流式响应 helper）、`tests/test_media_stream.py`（新） | `webui.py` |
| 主控 | 集成、跑测试、修跨文件问题 | — |

约束（全项目通用）：
- 依赖上限：stdlib + 已有 `Pillow`。**禁止**新增 PyAV/ffmpeg/requirements 条目。
- 注释/文档串/dogfooding 文案一律中文，风格对齐 `webui.py`；默认不写「做了什么」注释，只写「为什么」（如 mp4 尾读、sidecar 共享）。
- 大数据负载 **必须以 JSON 字符串返回**（规避 js_api 大 list 转换，见 `get_scan_rows` 文档串）。
- 规格数值见 §7，**照抄执行，不得自行平衡**；有异议先写进「偏差记录」再问主控。

## 1. 分层

```
media_meta.py    纯函数：文件路径 → 归一化元数据 dict（PNG 块 / mp4 atom / sidecar）
media_library.py sqlite 索引 + 增量扫描 + 查询（分面/分组/搜索/标签/收藏）
webui.py Api     lib_* RPC 方法（薄封装 + 缩略图 + 播放 URL）
browser_bridge   GET /api/media  Range 流式（浏览器模式视频播放）
web/*            data-page="library" 资源库页
```

## 2. `media_meta.py` 契约（A1）

### 2.1 唯一入口

```python
def extract(path, ext=None, max_chunk_bytes=8*1024*1024, max_window_bytes=16*1024*1024):
    """返回归一化 dict；任何异常一律吞掉并在 note 里说明，绝不抛给扫描线程。"""
```

返回（键恒定存在，缺值为 `''`/`[]`/`None`）：

```python
{
  "has_meta": bool,          # 只要拿到 prompt/workflow 之一即 True
  "source": str,             # 'png_text' | 'webp_exif' | 'jpeg_exif' | 'mp4_udta' | 'sidecar' | 'none'
  "prompt": str, "negative": str, "note": str,
  "checkpoint": str,         # 底模文件名（带扩展名，basename，无则 ''）
  "loras": [str],            # LoRA 文件名列表（basename）
  "models": [str],           # 全部模型引用（含 vae/clip/unet，来自 extract_model_references）
  "sampler": str, "scheduler": str,
  "seed": int|None, "steps": int|None, "cfg": float|None,
  "width": int|None, "height": int|None,     # 生成尺寸（元数据优先，回落容器/图片头）
  "duration": float|None,                    # 视频秒数
  "extra": dict,             # 原始解析摘要（给详情面板），只允许 ≤ 64KB，超出裁断并置 extra['truncated']=True
}
```

### 2.2 复用（不得重写解析逻辑）

- `from comfy_metadata import extract_prompts, extract_model_references`
- `from image_gallery import comfy_generation` — API graph 走它拿 `prompt/negativePrompt/seed/steps/cfgScale/sampler/scheduler/width/height/resources[{name,type}]`；
  `resources` 里 `type=='CHECKPOINT'|'DIFFUSION_MODEL'` → `checkpoint`（取第一个），`type=='LORA'` → `loras`。
- 只有 UI graph（`{"nodes":[...]}`）时：`extract_prompts(workflow=wf)` + `extract_model_references(workflow=wf)`。
- 判定 API graph：`isinstance(d, dict) and any(isinstance(v,dict) and v.get("class_type") for v in d.values())`。

### 2.3 PNG/WebP/JPEG 文本块

```python
def read_image_text_chunks(path, keys=("prompt","workflow"), max_chunks=64, max_chunk_bytes=8*1024*1024):
    """流式遍历 PNG 块取 tEXt/zTXt/iTXt；命中全部 keys 或遇 IEND 即停。返回 {key: str}。"""
def extract(path)  # PNG 走 read_image_text_chunks；WebP/JPEG 回落 PIL .info / EXIF UserComment
```

要求：
- 流式读（`f.read(4)`/`seek`），**不得 `handle.read()` 整文件**（现状 `webui.py:2540` 的做法不适用于几百 MB 视频/大图）。
- `zTXt`：`zlib.decompress`；`iTXt`：按 `keyword\0 flags(1) method(1) lang\0 transword\0 text` 解析，UTF-8；`tEXt`：latin-1。
- 同名 key 首个命中生效；`len(chunk) > max_chunk_bytes` 跳过（不中断）；`max_chunks` 上限防失控。
- WebP：`PIL.Image.open` 的 `.info` + `getex().get(0x9286)`（对齐 `image_gallery.py:260`）；JPEG：`UserComment` 去 `ASCII\0\0\0` 前缀。
- GIF：不解析元数据（`source='none'`），仍入库。

### 2.4 mp4/mov atom（纯 stdlib，本项目零先例，必须自研）

```python
def read_mp4_meta(path, max_window_bytes=16*1024*1024):
    """返回 {"comment":str|None,"description":str|None,"prompt":str|None,"workflow":str|None,
             "duration":float|None,"width":int|None,"height":int|None}"""
```

- 盒遍历：`ftyp/mdat/moov/trak/tmdia/udta/meta/ilst/©cmt/©des/mvhd/tkhd/mdhd/stts`。
- 尺寸：≤`max_window_bytes` 整读；否则**首 16MB + 尾 16MB 两段窗口**（ffmpeg 默认 moov 在 mdat 之后，ComfyUI SaveVideo/VHS 都没开 faststart —— 这是「为什么尾读」的必要注释）。窗口内找不到 moov 视为无元数据。
- 文本标签取值顺序（兼容矩阵，抄 Sidebar-Gallery 语义，不抄它的 PyAV）：容器 `udta` 优先，再逐 `trak`；键名依次 `©cmt` → `comment` → `©des` → `description`；`meta/ilst/©cmt/data` 需跳过 16 字节 data 头（`size(4) 'data' version+flags(4) locale(4)`）。独立 `prompt`/`workflow` 标签（`©cmt` 之外的 `udta` 直接子盒）同样接受。
- comment 若是 JSON dict：只取其中 `prompt`/`workflow` 键（两种、单种、合并都接受）；不是 JSON 则当纯文本忽略（`note` 记录）。
- `duration`：`mvhd`（version 0/1 两种时间基）；`width/height`：首个 `tkhd`（16.16 定点）；无 `stts` 计算 fps 需求 → 不实现 fps（二期再说，属偏差记录项）。
- EBML（`.webm/.mkv`）v1 **不解析**，入库 `kind='video', has_meta=False`。

### 2.5 sidecar 兜底

`extract()` 无内嵌元数据时按序探测 `<base>.json`、`<base>.workflow.json`（`<base>` = 去扩展名），JSON 解析后按 §2.2 判定 graph 形态。
**必须知道**：ComfyUI 会把 `out.png` 与 `out.mp4` 的元数据共享写入同一个 `out.json` —— 所以 sidecar 读取以「媒体文件名」为准，且删除媒体时不得连带删 sidecar（见 §5 `lib_delete`）。
sidecar 文件大小上限 8MB，超限跳过。

## 3. `media_library.py` 契约（A2）

### 3.1 类与入口

```python
DB_NAME = "media_library.db"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
KINDS = {"image": IMAGE_EXTS, "video": VIDEO_EXTS}
FTS_AVAILABLE: bool          # 建库时特性探测（本环境 sqlite 3.49 有 FTS5，仍须降级路径）

def db_path():               # 每次调用现取，便于测试 patch config.APP_DIR
    return os.path.join(config.APP_DIR, DB_NAME)

class MediaLibrary:
    def __init__(self, path=None): ...
    # 扫描
    def scan(self, roots, exclude=(), progress=None, cancel=None): -> dict  # 见 §4
    # 查询
    def list_items(self, flt):  -> dict   # {"items":[...], "total":int}
    def facets(self):           -> dict   # 见 §5.2 facets
    def folder_tree(self):      -> list
    def stats(self):            -> dict
    def get_item(self, item_id): -> dict|None
    def detail(self, item_id):   -> dict|None
    # 写
    def set_star(self, item_id, on): -> bool
    def set_tags(self, item_id, tag_ids): -> list
    def list_tags(self): -> list
    def save_tag(self, tag_id, name, color, parent_id): -> int
    def delete_tag(self, tag_id): -> bool
    def mark_gone(self, item_id): -> bool     # 文件已删 → missing_since
    def purge_missing(self, older_than_days=30): -> int
    def thumbs_ready(self, paths): -> dict     # {path: bool} 图片可直出 / 视频有海报缓存
```

- 连接策略：**每操作新建短连接**（`sqlite3.connect(path, timeout=15)` + `contextlib.closing`），首次建库执行 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=15000; PRAGMA foreign_keys=ON;`。理由：RPC 线程池 + 扫描线程并发，共享连接反而要加锁；页面级 QPS 极低。
- 所有对外行 dict 见 §5.1 `items` 形状，键名不得偏离。

### 3.2 表结构（`PRAGMA user_version = 1`）

```sql
CREATE TABLE IF NOT EXISTS items(
  id            INTEGER PRIMARY KEY,
  path          TEXT NOT NULL UNIQUE,
  root_id       TEXT NOT NULL,
  relpath       TEXT NOT NULL,          -- posix，含文件名
  dir_path      TEXT NOT NULL,          -- posix 相对目录，''=根
  filename      TEXT NOT NULL,
  ext           TEXT NOT NULL,          -- 小写含点
  kind          TEXT NOT NULL CHECK (kind IN ('image','video')),
  size          INTEGER NOT NULL,
  mtime         REAL NOT NULL,
  meta_mtime    REAL NOT NULL DEFAULT 0,-- 元数据解析时刻（对账：mtime>meta_mtime 才重解析）
  added_at      REAL NOT NULL,
  missing_since REAL,
  checkpoint    TEXT NOT NULL DEFAULT '',
  loras_json    TEXT NOT NULL DEFAULT '[]',
  models_json   TEXT NOT NULL DEFAULT '[]',
  width         INTEGER, height INTEGER, duration REAL,
  prompt        TEXT NOT NULL DEFAULT '',
  negative      TEXT NOT NULL DEFAULT '',
  search_text   TEXT NOT NULL DEFAULT '',
  has_meta      INTEGER NOT NULL DEFAULT 0,
  meta_source   TEXT NOT NULL DEFAULT '',
  starred       INTEGER NOT NULL DEFAULT 0,
  has_poster    INTEGER NOT NULL DEFAULT 0,
  extra_json    TEXT NOT NULL DEFAULT '{}'
);
-- 部分索引（只覆盖在场文件，对齐 Sidebar-Gallery 的 missing_since 索引思路）
CREATE INDEX IF NOT EXISTS ix_items_kind     ON items(kind)      WHERE missing_since IS NULL;
CREATE INDEX IF NOT EXISTS ix_items_ck       ON items(checkpoint) WHERE missing_since IS NULL;
CREATE INDEX IF NOT EXISTS ix_items_dir      ON items(dir_path)  WHERE missing_since IS NULL;
CREATE INDEX IF NOT EXISTS ix_items_star     ON items(starred)   WHERE missing_since IS NULL;
CREATE INDEX IF NOT EXISTS ix_items_mtime    ON items(mtime DESC);
CREATE INDEX IF NOT EXISTS ix_items_missing  ON items(missing_since) WHERE missing_since IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_items_loras    ON items(loras_json) WHERE missing_since IS NULL;  -- LIKE 分面用

CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL DEFAULT '', parent_id INTEGER);
CREATE TABLE IF NOT EXISTS item_tags(
  item_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY(item_id, tag_id));
CREATE INDEX IF NOT EXISTS ix_item_tags_tag ON item_tags(tag_id);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
-- FTS5（探测成功才建 + 三个同步触发器；失败则 list_items 用 LIKE）
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(search_text, content='items', content_rowid='id', tokenize='unicode61');
```

`meta` 用途固定：`db_version`、`root_version:<root_id>`、`last_scan_at`、`scan_stats`（JSON）。
**偏差记录**：设计文档 §3 的 `item_meta(item_id,key,value_json)` EAV 表被 `items` 平列 + `extra_json` 取代 —— 字段集是已知闭集，EAV 只增加 join 成本；二期若上 facts 分面索引再引 EAV。

## 4. 扫描管线（A2 实现，A3 转发）

```python
def scan(self, roots, exclude=(), progress=None, cancel=None):
    roots  = [绝对目录]；exclude = [相对根的 posix 目录]
    progress(dict) 回调：{"phase": "walk"|"parse"|"reconcile"|"done", "done": int, "total": int, "msg": str}
    cancel() -> bool
    返回 {"roots":[...], "total":int, "added":int, "updated":int, "unchanged":int, "missing":int, "errors":[str]}
```

规则（照抄）：
1. `root_id = sha1(normcase(abspath(root)))[:16]`。跳过目录：名字以 `.` 开头、`__pycache__`、`node_modules`、`.images` 结尾、用户 `exclude` 命中（自身及子目录）。
2. 单次 walk 收集 `(relpath, abspath, size, mtime)`；同一遍记录**见过的目录集合** `seen_dirs` 与 `listed_dirs`。
3. 对账：
   - 新文件 → 解析元数据入库（`added_at=now`, `meta_mtime=now`）。
   - `mtime` 或 `size` 变化 → 重解析并更新。
   - 未变 → 只刷 `missing_since=NULL`（重新出现），**不重解析**。
   - 库里在场但本次 walk「**确定看见且不存在**」→ 置 `missing_since=now`；目录不可读/枚举抛 `OSError` 的行**保持原状**（不判缺失，抄 Sidebar-Gallery 的 `_row_is_absent` 语义）。
   - 未列出的根（root 目录本身不存在）→ 该根全部行不判缺失，只在 errors 里记 `unreachable`。
4. `cancel()` 为真时在文件边界退出，已入库内容保留（增量安全）。
5. 单次扫描解析上限 **不截断文件数**（用户明确：规模 1w–10w），但每文件解析异常记 `errors`（≤200 条，超出计数合并），不得中断整体。
6. `purge_missing(30)` 在扫描末尾调用一次。

## 5. RPC 契约（A3；方法名前端直接 `api.call("lib_x", ...)`）

### 5.1 行/详情形状

```jsonc
// items 行（lib_list 返回）
{"id":1,"path":"F:\\...\\00001-1.png","relpath":"sub/00001-1.png","filename":"00001-1.png",
 "kind":"image","ext":".png","size":2048576,"mtime":1728700000.0,"added_at":1728700001.0,
 "checkpoint":"animagine.safetensors","loras":["a.safetensors"],"width":1024,"height":1024,
 "duration":null,"has_meta":true,"meta_source":"png_text","starred":false,
 "tags":[{"id":1,"name":"精选","color":"#888"}],"snippet":"1girl, solo...",   // prompt 前 120 字
 "thumb":true,                        // 可直接出图（图片 或 视频有海报）
 "url_local":"file:///F:/...",        // pywebview 模式用
 "url_http":"http://127.0.0.1:47531/api/media?p=<pct-enc abspath>"}  // 浏览器模式用
```

`lib_detail(id)` 另加：`negative、models、seed/steps/cfg/sampler/scheduler、note、extra`、`exists`（bool）。

### 5.2 方法清单（14 个，全部 `lib_` 前缀；返回除注明外均为 dict）

| 方法 | 签名 | 返回 |
|---|---|---|
| `lib_scan` | `()` | `{"started":bool,"msg":str}`；已在跑 → `started=False` |
| `lib_scan_state` | `()` | `{"running":bool,"phase":str,"done":int,"total":int,"msg":str,"roots":[{"path":str,"status":"ok"\|"unreachable","count":int}],"errors":[str]}`（**轻量，不含行**） |
| `lib_cancel_scan` | `()` | `{"ok":true}` |
| `lib_rescan_if_stale` | `()` | `{"started":bool,"reason":str}`（页面首次进入用；`last_scan_at` 超 60s 或有新文件迹象才起） |
| `lib_list` | `(flt_json:str)` | **JSON 字符串** `{"ok":true,"items":[§5.1],"total":int,"offset":int,"limit":int,"facets":§5.3}` |
| `lib_facets` | `()` | **JSON 字符串** §5.3 |
| `lib_detail` | `(item_id:int)` | **JSON 字符串** |
| `lib_get_thumbs` | `(paths_json:str, size:int=320)` | **JSON 字符串** `{path: "data:image/jpeg;base64,..." \| ""}`；单次上限 40 路径，超出截断 |
| `lib_save_poster` | `(path:str, data_b64:str)` | `{"ok":bool,"msg":str}`；仅接受库内已登记的媒体路径，写入 §7 poster 缓存 |
| `lib_set_star` | `(item_id:int, on:bool)` | `{"ok":true,"starred":bool}` |
| `lib_set_tags` | `(item_id:int, tag_ids_json:str)` | `{"ok":true,"tags":[§5.1 tags]}` |
| `lib_tags` | `()` | **JSON 字符串** `[{"id":1,"name":"精选","color":"","parent_id":null,"count":12}]` |
| `lib_tag_save` | `(tag_id:int, name:str, color:str, parent_id:int=-1)` | `{"ok":bool,"id":int,"msg":str}`；`tag_id=0` 新建；`parent_id=-1` 表示无父 |
| `lib_tag_delete` | `(tag_id:int)` | `{"ok":true,"removed:int}` |
| `lib_delete` | `(item_id:int)` | `{"ok":bool,"msg":str}`；**只回收媒体文件本体**（`posix_compat` 回收站，复用 `rm_file` 的落回收站语义但**不得连带 sidecar**），成功后 `mark_gone` |
| `lib_stats` | `()` | **JSON 字符串** `{"total":int,"image":int,"video":int,"starred":int,"tagged":int,"missing":int,"no_meta":int,"db_bytes":int,"checkpoints":int,"tags":int}` |

（表内 16 行含 2 个只读汇总；`lib_stats`/`lib_facets` 前端可合并进 `lib_list` 的 facets 使用，保留独立方法供页头刷新。）

`flt` 入参（前端 JSON.stringify）：
```jsonc
{"kind":"", "checkpoints":["a.sft"], "loras":[], "dirs":[], "tag_ids":[], "starred":false,
 "no_meta":false, "q":"", "sort":"time_desc", "group_by":"", "offset":0, "limit":200}
```
- `sort` 白名单：`time_desc|time_asc|name_asc|size_desc|added_desc`，未知值回落 `time_desc`（SQL 拼接必须走白名单映射，**禁止**字面量拼接）。
- `group_by` 白名单：`""|date|checkpoint|dir`；非空时 items 每项加 `"group":"2026-10-12"`（date 用 localtime）。
- 文本 `q`：FTS 可用 → `items_fts MATCH`（前缀查询 `"tok"*` 拼接需转义，失败回落 LIKE）；否则 `search_text LIKE %q%`（转义 `%_`）。
- `dirs`/`checkpoints`/`loras`/`tag_ids` 都是 IN 语义（同维度 OR，跨维度 AND）；`loras` 命中用 `loras_json LIKE '%"name"%'` 参数化。

### 5.3 facets 形状

```jsonc
{"checkpoints":[{"name":"a.safetensors","count":120}],  // 降序，最多 300 项
 "loras":[], "dirs":[{"name":"sub","count":40,"path":"sub"}],
 "tags":[{"id":1,"name":"精选","color":"","count":12}],
 "kinds":{"image":900,"video":40}, "starred":7, "no_meta":15, "missing":3, "total":940}
```

`folder_tree()` 形状对齐模型库 `get_folders`，便于抄面板：`[{"name","path","children":[...],"count"}]`。

### 5.4 Api 侧要求（A3）

- 只允许**追加**：在 `webui.py` 末尾 `class Api` 之外新增 `lib_` 段落的方式不可行（方法必须在类内）→ 实际做法：在 `class Api` 内**末尾**（`test_api` 之后）追加一个清晰分节注释 + 方法组。禁止修改既有方法体。
- 实例状态追加（`__init__` 内，紧跟现有字段块末尾，只加不删）：
  ```python
  self.media_lib = None            # 懒建（见 _media_lib()）
  self.lib_scan_state = {"running": False, "phase": "", "done": 0, "total": 0,
                         "msg": "", "roots": [], "errors": []}
  self._lib_cancel = threading.Event()
  self._lib_thumb_sem = threading.Lock()
  ```
- `_media_lib()` 懒加载：每次调用比对 `media_library.db_path()` 与缓存实例路径，不等则重建（测试 patch `config.APP_DIR` 依赖这条）。
- 缩略图：图片项**必须复用** `self._cover_thumb(p, p, size)`（源即媒体文件本身，缓存键天然按 abspath+mtime+size，与模型封面共用 `thumb_cache_dir()`）；视频项查 poster 缓存（`media_library.poster_path(path)`），无则返回 `""`。
- 播放 URL：`url_local` 用 `pathlib.Path(p).as_uri()`；`url_http` 用 `"http://127.0.0.1:%d/api/media?p=%s" % (browser_bridge.port(), urllib.parse.quote(abspath))`；`browser_bridge.port()` 返回 0 时 `url_http` 置 `""`。
- 扫描线程：`threading.Thread(target=work, daemon=True).start()`，模式对齐 `scan_models`（`webui.py:1383`），`finally` 里 `running=False`；进度回调写 `self.lib_scan_state`。
- 根目录来源：`self.cfg.get("media_roots")`（绝对路径列表）；为空时 `lib_rescan_if_stale` 返回 `{"started":False,"reason":"no_roots"}`，前端提示去设置页配置。
- 与模型库的隔离：不得读写 `model_rows`/`mm_scan_state`；跨功能占用检查照抄 `migrate_data_storage` 的做法——`lib_scan` 与 `scan_models` **互相拒绝**（`mm_scan_state["running"]` 时返回 `{"started":False,"msg":"模型扫描进行中"}`）。

## 6. 配置键（A3，只加 DEFAULTS 三行 + 归一化）

```python
"media_roots": [],          # 资源库扫描根：ComfyUI output 等，每行一个绝对路径
"media_exclude_dirs": [],   # 资源库排除目录（相对根的 posix 路径，含子目录生效）
"media_page_size": 200,     # 资源库每批条数（1..500）
```
- 归一化：`media_page_size` 走 `config.py:209` 的 `(key,default,lo,hi)` 元组表，`lo=20, hi=500`。
- 自动探测：`Api.lib_autodetect_roots()` → `{"ok":true,"roots":[str]}`，候选按序取**存在**者：`<INSTALL_DIR>/ComfyUI/output`、`~/ComfyUI/output`、每个 `models_dirs` 的**父目录**下 `ComfyUI/output`。前端「设置」页放「自动探测」按钮。

## 7. 规格数值（照抄，不得自行平衡）

| 项 | 值 | 出处 |
|---|---|---|
| DB 文件 | `<config.APP_DIR>/media_library.db` | 设计文档 §3 |
| 图片扩展名 | `.png .jpg .jpeg .webp .gif` | §3.1 |
| 视频扩展名 | `.mp4 .mov .webm .mkv`（后两者仅入库不解析） | §3.1 |
| PNG 文本块 | 单块上限 8MB，最多 64 块，命中即停 | §2.3 |
| mp4 窗口 | 整读 ≤16MB，否则首/尾各 16MB | §2.4 |
| sidecar | 上限 8MB，`<base>.json` → `<base>.workflow.json` | §2.5 |
| `extra` 上限 | 64KB，超出截断并置 `truncated` | §2.1 |
| 列表分页 | 默认 200，硬上限 500，缩略图批 40 | §5.2 |
| 网格缩略图边长 | 320（详情 800） | §5.4 |
| poster 回传 | 前端最长边 640，JPEG q=0.82，≤2MB | §5.2 |
| 缺失确认期 | 30 天后物理清行 | §4.6 |
| 页面进入重扫阈值 | 距 `last_scan_at` ≥60s | §5.2 |
| 轮询间隔 | 扫描进度 1s | 对齐模型库 |
| 卡片列宽 | 复用 `--model-card-width`（默认 220px） | workbench.css:167 |
| facet 下拉上限 | 300 项，超出按 count 截断 | §5.3 |
| errors 上限 | 每次扫描 200 条明细，超出折叠计数 | §4.5 |

## 8. `/api/media`（A6）

```
GET /api/media?p=<urlencode(绝对路径)
```
- 允许条件（三条全过，否则 403/404）：`p` 非空；`os.path.realpath(p)` 位于 `Api` 配置的某个 media_root 的 realpath 之内；扩展名 ∈ §3.1 集合。**禁止**接受相对路径、查询 `path` 变体。
- media_root 列表由 A6 通过 `browser_bridge.set_media_roots(fn)` 注入（新增模块级 setter，与既有 `set_archive_handlers` 同风格；`Api.__init__` 里由 A3 调一次 `browser_bridge.set_media_roots(self._media_roots_for_bridge)`）。
- Range：支持 `bytes=a-b`/`bytes=a-`；无 Range → 200 + `Content-Length` + 全文。有 Range → 206 + `Content-Range: bytes a-b/size`，分块写（64KB），客户端断开（`BrokenPipeError`/`ConnectionResetError`）静默退出。非法 Range → 416 + `Content-Range: bytes */size`。
- 头：`Accept-Ranges: bytes`、`Content-Type`（mimetypes，回落 `video/mp4`）、`Cache-Control: private, max-age=3600`（媒体不可变，和静态资源 `no-store` 有意不同 —— 需要注释说明）。
- 必须在 `do_GET` 里、`_serve_static` **之前**分支（`/api/` 前缀已跳过静态，但显式排序更稳）。
- 只读，不写；不改 `_serve_static`；不动 `/api/rpc`。

## 9. 前端契约（A4 标记 + CSS，A5 逻辑）

### 9.1 DOM id 清单（A5 只用这些，A4 必须全部提供）

导航：`<button class="nav-tab" data-page="library" data-nav-icon="layers">资源库</button>`（插在 `models` 之后；`data-nav-icon` 必须用 `web/index.html` 里已存在的 symbol id，A4 需先 grep 再定）。

`<section class="page" id="page-library"><div class="card">`：
- 页头 `#libTitle`、`#libScanBtn`、`#libStopScanBtn`、`#libScanHint`（进度/结果文本）、`#libStatsBar`（统计条）、`#libRootHint`
- 搜索/排序 `#libQ`（input，防抖 300ms）、`#libSort`、`#libGroup`
- 筛选行（结构对齐模型库 `.mm-row1/2/3` 的 select 写法）：`#libKindF`、`#libCkF`、`#libLoraF`、`#libTagF`、`#libDirF`、`#libStarOnly`（checkbox）、`#libNoMeta`（checkbox）、`#libFoldersBtn`
- 网格：`#libViewport`（滚动容器）> `#libGrid`（class 复用 `masonry`）；末尾哨兵 `#libMore`（「加载更多」）；空态 `#libEmpty`；骨架 `#libLoading`
- 标签编辑浮层 `#libTagPop`（默认 `hidden`）；目录面板 `#libFoldersPanel`（默认 `hidden`，结构对齐 `#mmFoldersPanel`）
- 详情与查看器**复用全局** `#detailMask`/`#detailPanel`/`openImageViewer`，A4 不新建详情面板。

卡片 DOM（A5 生成，A4 供样式；类名复用既有 `.ms-card/.ms-img-wrap/.ms-img/.ms-info/.ms-titles/.ms-name/.ms-sub/.ms-footer` 以免重写基础样式）：
```html
<div class="ms-card lib-card" tabindex="0" data-id data-path data-kind>
  <div class="ms-img-wrap" data-ph="loading">
    <img class="ms-img" alt=""> 或 <video class="ms-img lib-vid" muted loop playsinline preload="metadata">
    <span class="thumb-video-badge">▶</span>       <!-- 视频角标，复用既有类 -->
    <span class="lib-badge-meta" title="无内嵌元数据">?</span>  <!-- 仅 has_meta=false -->
  </div>
  <div class="ms-info"><div class="ms-titles"><div class="ms-name" title=…>文件名</div>
    <div class="ms-sub">底模 · 尺寸/时长</div></div>
    <div class="ms-footer"><span class="ms-size">1.9 MB</span><span class="lib-time">10-12</span></div></div>
</div>
```
A4 新增 CSS 仅限：`.lib-card` 差异（视频角标定位、`?` 元数据徽标 `.lib-badge-meta`、标签 chip `.lib-chip`、统计条 `.lib-stats`、分组标题 `.lib-group-title`、加载哨兵 `.lib-more`）。**不得**改既有选择器。

### 9.2 A5 行为要求

- `switchPage` 内加 1 行：`if (name === "library") libScanIfNeeded();`（放在既有 hooks 旁，不改别的分支）。检查 `app.js:5044` 附近的页面清单/onboarding 数组，若存在需要同步 `library` 一项。
- 状态隔离：新增顶层 `const lib = {rows:[], total:0, offset:0, limit:200, facets:null, tags:[], filters:{...}, loading:false, thumbCache:new Map(), sel:new Set(), detail:null};`。**禁止**复用 `state.models/state.display/state.coverCache`。
- 加载：`libLoad(reset)` → 组 `flt` → `api.call("lib_list", JSON.stringify(flt))` → `JSON.parse` → 渲染 → `libPrefetchThumbs()`（40 条一批，`lib_get_thumbs`）。滚动到底（IntersectionObserver 或 `#libViewport` scroll，节流 300ms）→ `offset += limit` 续拉。
- 视频海报：`thumb === false && kind === "video"` 时，卡片内放 `<video preload="metadata" muted>`（`#t=0.1` 片段触发首帧解码），并**一次性**离屏 canvas 抓帧（`videoWidth/Height` 缩到长边 640，`toDataURL("image/jpeg",0.82)`）→ `lib_save_poster`。抓帧失败（跨域/解码错误）静默跳过，卡片保留 `<video>`。同一 path 会话内不重复请求。
- 详情：`libShowDetail(id)` → `lib_detail` → 写 `#detailPanel`（大图或 `<video controls>`，`window.__browserMode ? url_http : (url_local || url_http)`）→ 元数据表（prompt/negative 可复制、seed/steps/cfg/sampler/scheduler、尺寸/时长、来源 `meta_source`）→ 模型引用列表（`models`/`loras`）点击：`workflow_model_matches(JSON.stringify([name]))` → 命中则 `switchPage("models")` + `showModelDetail(path)`；未命中提示。
- 动作按钮：收藏（`lib_set_star`）、打标签（`#libTagPop`，`lib_tags`/`lib_set_tags`）、打开所在文件夹（`open_in_folder`）、送工作流分析（`analyze_workflow` 后 `switchPage("workflow")`）、删除（`lib_delete`，**必须 `confirm()`**，文案说明进回收站）。
- 轮询：`libScanIfNeeded()` → `lib_rescan_if_stale`；`running` 时 1s 轮 `lib_scan_state` 更新 `#libScanHint`，结束自动 `libLoad(true)`。
- 空态：无 `media_roots` → `#libEmpty` 文案「去设置页添加 ComfyUI 输出目录」；有库无结果 → 「无匹配项」；扫描中 → `#libLoading`。
- 全部新代码放在文件末尾一个 `// ===== 资源库 =====` 注释块内，除 `switchPage` 那 1 行（+必要时 onboarding 清单 1 项）外**不得改动既有行**。
- 自验：`node --check web/app.js` 必须通过。

## 10. 测试约定

- Python：`tests/test_*.py`，`unittest` 风格，`webui.Api.__new__(webui.Api)` 绕过 `__init__` 后手工挂属性（见 `tests/test_api_access_lora.py`），`TemporaryDirectory` + `patch.object(config, "APP_DIR", ...)`，**禁止网络**（`Mock(side_effect=AssertionError)`）。
-  fixtures 自造：A1 需在测试里手写 PNG（`zlib.crc32` + `struct.pack(">I4s")` 组 tEXt/zTXt/iTXt）与 mp4（`ftyp/mdat/moov/mvhd/udta/meta/ilst/©cmt`），**不得依赖 ffmpeg**。
- JS：不新增 Playwright 测试（本机 `require('playwright')` 目前因 ESM 报错不可用，属存量问题）；A5 以 `node --check` + 主控的浏览器实机验收为准。
- 全量门禁：`python -m pytest -q` 全绿（当前基线 398 passed）。

## 11. 偏差记录（相对 `docs/media-library-design.md`）

1. §3 的 `item_meta` EAV 表 → `items` 平列 + `extra_json`（理由见 §3.2）。
2. §5 顶栏「3 秒轮播」不适用：媒体库每张卡就是单个文件，无多图帧 → 不实现轮播，复用卡片样式即可。
3. §4 视频海报「前端抓帧回传」保留，但增加 `<video preload=metadata>` 直显回落（首屏不必等回传）。
4. §2 参考项目 Sidebar-Gallery 的 mp4 读取实为 PyAV（**不是**纯 atom 解析）→ 本项目自研 atom 读取，兼容矩阵照其语义；其 fps 提取（stts）本期不做。
5. §5 「~10 个 RPC」→ 16 个（分面/标签/海报回传各自独立，合并反而增加协议分支）。
6. 新增 `/api/media` Range 流式端点：设计文档未提，但浏览器模式（http origin）下 `file:///` 不可用，且既有 `_local_video_uri` 明确只在 pywebview 下生效 —— 无此端点则浏览器模式视频无法播放。

## 13. 实机验收后的修订（v1.1，2026-10-12）

在用户的真实库上验收（`D:\ComfyUI_Mie_2026_V8.0\ComfyUI\output`，2792 个在库条目：2260 图 / 532 视频，4.7GB），并做了一轮独立质量复核，以下以实测为准修订本契约：

1. **元数据覆盖按字段名回收（新增 §2.6）。** 实测发现 1129/2792（40%）条目虽有内嵌 workflow 却提不出提示词与底模：这些工作流走 `SamplerCustomAdvanced` + `BasicGuider.conditioning` + 自定义节点（如 `MiniMaxH3ReferenceToVideo.prompt`、`H3VMMasterLoader.unet_name`、`CLIPLoader.clip_name`），而 `image_gallery.comfy_generation` 只从 KSampler 家族的 `positive` 链出发。`media_meta` 增加**纯字段名**兜底层（不猜节点语义、不改 `comfy_metadata` 的“未知节点不猜”原则）：
   - 底模 `ckpt_name|unet_name|diffusion_model`，LoRA `lora_name|lora_*|*_lora`（值须落在 `comfy_metadata.WEIGHT_SUFFIXES`）；
   - 提示词 `prompt|text|caption|positive_text`，跳过 Note/Markdown/TextPanel/Primitive*/Comfy*Expression/Reroute/AnythingIndex 类文档节点，去重后以 `" | "` 连接、上限 4000 字，**不**据此伪造 negative；
   - 链式结果优先，兜底只在链式为空时生效，并把来源写进 `extra`：`prompt_source`=`chain|name_fallback`、`classification_source`=`chain|field_name`；UI 对 `name_fallback` 明确标注「按字段名提取」。
   实测效果：提示词 800→1820（29%→65%），底模分类 1279→1910（46%→68%），LoRA 759→1369，distinct 底模 21→32，mp4 提示词 0/426→354/426。
2. **§5.2 的 `lib_stats` / `lib_facets` 已删除**：页头统计与分面都由 `lib_list` 的 `facets` 一次带回，两个独立 RPC 无调用点。§3.1 的 `folder_tree()` / `thumbs_ready()` 同样删除——目录面板按 `facets.dirs` 建树（§5.3），缩略图就绪判定在 `Api._lib_enrich` 里按文件系统实况算（海报文件可能被缓存清理，DB 标记会说谎）。`§5.2` 表内其余方法不变。
3. **§7 网格缩略图边长补一句**：CSS 像素按规格 320，设备像素按 `dpr` 放大到最多 960（`Math.min(960, 320*dpr)`）以免高分屏发虚；详情 800 不变。这是对规格的解释，不是改数值。
4. **播放 URL 单一来源**：`url_local` / `url_http` 只由 `Api._lib_play_urls` 生成（传输层职责），`media_library` 不再拼 URL、不再 import `browser_bridge`。`thumb` 同理只在 `_lib_enrich` 判定。
5. **海报写入归库层**：`lib_save_poster` 只做守卫（视频扩展名 + 配置的媒体根之内 + 已入库登记），落盘、2MB 上限、`has_poster` 同步统一由 `MediaLibrary.set_poster(path, data)` 负责；未入库路径**不得**先在海报目录造文件。根目录判定两侧都过 `normcase(realpath)`，与 `browser_bridge._media_within_roots` 一致。
6. **扫描启动竞态**：`lib_scan` 的检查-占位改在 `self._lib_scan_lock` 内完成，状态字典原地 `update` 不再重新绑定（原实现两个并发调用可各起一个全量 walk 线程，输家的 `finally` 会清掉赢家的 `running`）。
7. **重叠根目录**：`MediaLibrary.scan` 先剔除严格嵌套在另一根内的根（等值重复保留先出现的），结果里回 `skipped_roots`，避免同一 `path` 的 `INSERT OR REPLACE` 重置 `added_at` / `starred`。
8. **折叠错误行**：`_ErrorSink.finish` 先把明细裁到 `ERRORS_MAX` 再追加「另有 N 条错误已折叠」，webui 不再二次截断（原实现会把折叠行截掉，UI 少报）。
9. **视频卡懒挂载（性能）**：`<video>` 只带 `data-src`，由单个 `IntersectionObserver`（root `#libViewport`、`rootMargin:150%`）在进入视口附近时才赋 `src` 并启动海报抓帧，滚远即 `pause()+removeAttribute('src')+load()` 释放解码器。实测：250 张视频卡从 ~200 路并发 `/api/media` 流降到 1 路。
10. **卡片动作补「送逆向解析」**（设计文档 §5 要求，走 `rp_add_paths` + 反向解析页）。标签补齐**删除**入口（`lib_tag_delete` 原先无调用点）。
11. 新增配置项 `media_page_size` 实测生效（改 250 后页面每批 250）；`lib_rescan_if_stale` 的 60s 阈值与增量重扫实测：2792 条目全量首扫解析约 7s，增量重扫 0.2–2s。
12. **设计文档 §7 P3 的「自动标签（底模 / LoRA 即标签）」不单独实现**：底模 / LoRA 已经是 `facets.checkpoints` / `facets.loras` 两个可直接筛选、带计数的分面维度（§13.1 的字段名回收把覆盖率从 46% 提到 68%），再把同样的名字物化成 `tags` 行只会让同一事实有两份真相、并让标签表被模型文件名淹没。用户自建标签仍是独立维度（父子标签、颜色、计数）。分组视图与卡片跳工作流分析 / 逆向解析都已实现。
13. **两种界面模式的海报抓帧都可用**（实测）：浏览器模式 `http://127.0.0.1:port` 页面 + `url_http` 同源，canvas 可直接回传（已落盘 23 张海报验证）；窗口模式（pywebview，`file://` 页面 + `url_local` 的 `file:///` 视频）用一个等价探针页确认 Blink 允许 `file → file` 视频绘入 canvas 并 `toDataURL()` 成功，不会被跨源污染挡掉。因此不需要为窗口模式另设海报通路；若哪天某个 WebView2 版本收紧该规则，表现是海报缓存不增长、卡片仍走懒挂载的 `<video>` 直显，功能不坏。

## 14. 验收状态（2026-10-12）

- 后端门禁：`python -m pytest -q` 575 passed / 50 subtests（本功能前基线 398）；新增 177 项资源库用例。
- 真实库验收：`D:\ComfyUI_Mie_2026_V8.0\ComfyUI\output`（2792 条目、2260 图 / 532 视频、4.7GB）——首扫解析 ~7s、增量重扫 0.2–2s、列表 ~110ms、分面 ~100ms、FTS ~50ms；视频页并发 `/api/media` 由 ~200 路降到 1 路；元数据覆盖见 §13.1。
- 界面走查（浏览器模式真实页面）：扫描进度、网格与缩略图、类型 / 底模 / 标签 / 仅收藏 / 仅无元数据筛选、关键词搜索、分组标题、标签建改删、收藏、详情视频播放与 seek（206）、模型引用跳转与未命中提示、分页续拉、`media_page_size` 生效、控制台无报错。
- **未覆盖**：内置浏览器 `visibilityState=hidden` 且视口高度为 0，截图与「IntersectionObserver 真实进视口」这两步只能以结构证据代替；像素级观感（卡片间距、角标位置、明暗主题下的对比）需人工开一次窗口确认。


## 12. 补充钉死（A4/A5 共用，与 §6/§9 同权重）

- 测试性注入：`media_library.py` 内部一律通过模块级 `_extract(path, ext)` 调元数据提取（函数体内 `import media_meta`，异常吞掉返回 `{}`）。A2 的单测 monkeypatch `media_library._extract`，从而**不依赖 A1 完成**。
- A3 的单测一律 `patch.object(webui.Api, "_media_lib", ...)` 注入假库，不依赖 A2 完成。
- 设置页控件 id（镜像现有 `models_dirs` 控件的写法与位置，A4 提供、A5 绑定）：
  `#setMediaRoots`（textarea，每行一个绝对路径）、`#setMediaExclude`（textarea，每行一个相对目录）、`#setMediaPageSize`（number）、`#libRootDetect`（button，调 `lib_autodetect_roots` 后回填 `#setMediaRoots`）。
- `Api` 侧新增非 RPC 辅助（下划线开头即对桥接不可见）：`_media_lib()`、`_media_roots_for_bridge()`。
