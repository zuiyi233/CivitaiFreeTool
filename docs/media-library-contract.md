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
- EBML（`.webm/.mkv`）v1 不解析；**第三轮起解析**（时长 / 帧率 / 宽高 / `Tags→comment` 的提示词与工作流），口径见 §21。

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
| 视频扩展名 | `.mp4 .mov .webm .mkv`（webm/mkv 自第三轮起按 §21 解析 EBML） | §3.1 |
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
14. **移出配置的根目录不再产生幽灵卡片**：`media_roots` 里删掉一个目录后，它已入库的行原先仍会出现在网格与分面计数里，点开却是打不开的文件。现在 `media_library.root_id(abs)` 暴露与扫描同一套根哈希，`Api._media_root_ids()` 按**服务端**当前配置注入 `flt["root_ids"]`（客户端传来的同名键一律覆盖，前端无法自行放宽范围），`_build_where` 与 `facets(root_ids=)` 统一用 `root_id IN (?)` 限定作用域。行本身不删（30 天清理只针对文件真的消失），把目录加回配置即恢复，标签与收藏不丢。实测（冻结 EXE + 浏览器模式真实页面）：库内 2795 行，其中 3 行属已移出的 `L:\tmp\execheck\output`，页面显示「共 2792」，`bare.png` / `a.png` / `clip.mp4` 不再出现，kinds 2260+532=2792 自洽，伪造 `root_ids` 的请求仍返回 2792。

## 14. 验收状态（2026-10-12，第二轮更新）

- 后端门禁：`python -m pytest -q` **694 passed / 50 subtests**（本功能前基线 398）；资源库用例 267 项 = 元数据 121（`test_media_meta.py`，含 EBML 39 项）+ 索引与扫描 104（`test_media_library.py`，含定向补解析 2 项）+ RPC 层 42（`test_media_rpc.py`）。`node --check web/app.js` 与 `py_compile`（webui / media_library / media_meta / browser_bridge / scripts）全通过。
- 真实库验收：`D:\ComfyUI_Mie_2026_V8.0\ComfyUI\output`（2792 条目、2260 图 / 532 视频、4.7GB，**全程只读**）——首扫解析 ~7s、增量重扫 0.2–2s；列表首屏（200 条 + 分面）190ms、续拉跳分面 45ms、分面聚合（带根作用域）130ms、FTS ~50ms；视频页并发 `/api/media` 由 ~200 路降到 1 路。覆盖率与延迟明细见 §15.0 与版本说明实测表。
- 第二轮新增项的实数据验证：v1→v2 迁移 0.36 秒且逐列核对老数据一列未变；`extract_version` 自愈在真实库补解析 2035 行 / 31.1 秒，第二轮 182 毫秒空转，补完覆盖率与全新库逐字段相等；§17 搬家重连用真实库副本造改名（added 3 / relinked 3、旧行仍在、旧文件仍在磁盘时 relinked=0）；§18 标注导出 23ms（40 条 / 6.3KB）、导入试算 38ms、真写 45ms，**试算与真写六项计数逐项一致**。
- 界面走查（浏览器模式真实页面，47531）：第一轮的扫描进度、网格与缩略图、类型 / 底模 / 标签 / 仅收藏 / 仅无元数据、关键词搜索、分组标题、标签建改删、收藏、详情视频播放与 seek（206）、模型引用跳转与未命中、分页续拉、`media_page_size` 生效之外，本轮补验：三组分面 chip 的真实计数与点击生效（euler（1023）→「筛选 1023 项 / 共 2799 项」）、时长备注「只匹配视频（图片没有时长字段）」、多选选 3 项批量收藏后统计条「收藏 3」、「选中筛选结果全部」跨分页取到 1023 个 id 并按 3 片 ×500 执行（状态行「1023 项生效」与服务端 `facets.starred=1023` 一致）、详情 `←`/`→` 走到 `3 / 200`、导出得 `资源库标注-20261012-2151.json`（3 条）、导入先出六项试算（含故意塞的 1 条认不出）再确认写入、标签「导入验收」按名字与颜色重建。控制台无 error。
- 冻结产物复验（`dist/CivitaiFreeToolWeb.exe`，2.6.27 版本资源已同步）：用真实配置的只读副本 + 自造合成小库（`output_synth`）起实例，浏览器模式进资源库自动增量重扫、新界面全部在位；删除只走 UI 删自己的合成副本（2799 → 2797，D 盘 3 个同名原件完好）。
- 窗口模式像素级复检（**原「未覆盖」项已补齐**）：同一冻结 EXE 以 pywebview 窗口模式开真实窗口（1266×783），桌面自动化截图核对——网格 4 列缩略图、三组 chip 行、时长输入 + 备注、导出 / 导入按钮、多选态的卡片勾选角标 + 选择行 + 操作提示条、详情面板（标题「资源详情」+ 位置 `1 / 200` + 前后翻页 / 关闭 + 字段区 +「生成参数按字段名提取」分组）均无重叠、无截断。**这一步查出一个结构检查看不见的问题**：尺寸组两行封顶后 37 个值只露 2 个，组内滚动条几乎无人发现 → 按 §20.3「分面溢出展开」补了控件并重打包复检：没溢出的组不挂控件（采样器 4 值），调度器 / 尺寸组挂「展开（共 5 项）」「展开（共 37 项）」，展开后该组独占一行、37 个值全部露出、文案变「收起」，再点回到与初始一致的两行版位。
- 仍然只以结构证据代替的一步：自动化内置浏览器的 `visibilityState=hidden` 且视口高度为 0，「IntersectionObserver 真实进视口」在浏览器模式无法触发，用「250 张卡全部只有 `data-src`、`src` 数 0、`/api/media` 请求数 0，手动 hydrate 后 readyState=4 且解码出 832×480 首帧」代替，并在窗口模式的真实截图里复核了卡片首帧渲染；视频「播放中 seek」只在浏览器模式实测过 206，窗口模式验到挂载与首帧为止。
- 第三轮（§21）验收：EBML 用 ffmpeg 现造样本与 ffprobe 对账（`webm/vp9 832x1248 24000/1001 5s`、`mkv/h264 640x480 30fps 3s`，时长 5.005/3.0、帧率 23.976/30.0、宽高逐位一致，`meta_source=ebml_tag`），并把同一份 comment 喂 mp4 通路做同形对照；冻结产物端到端复跑（临时根 4 文件：真实 png + 三种容器）统计条「共 4 项 图片 1 视频 3」、尺寸分面 `832x1248（3）+640x480（1）`、卡片时长 5.0/3.0/5.0 秒，真实窗口截屏确认渲染。详情长文本展开在真实 Chromium 量到折叠 `70/187` → 展开 `187/187`、文案与 `aria-pressed` 正确、`Enter` 后焦点留在控件；**它的窗口模式像素图当轮没做**（前台被全屏应用占住，未去抢输入），该缺口如实记在这里。页顶「webm 与 mkv 只入库、不解析元数据」的过时文案已改。
- 批量 RPC 边界：`_LIB_BATCH_MAX=2000` 服务端拒超量、前端按 500 分片；未知 / 已消失 id 只过滤不报错（`_live_ids`）。测试数据：只用合成 PNG / mp4 字节与临时目录，真实库只读扫描，未删除任何用户文件。

## 15. 功能完善第二轮（v1.2，2026-10-12 实测驱动）

### 15.0 实测依据（真实库重扫进临时库，2792 条在场）

| 项 | 实测 |
|---|---|
| 容器分布 | png 1503 / jpg 756 / mp4 532 / gif 1，**webm/mkv 为 0 个** |
| jpg 实况 | 756 张全为 `sha256_<64hex>.jpg`（合计 39.3MB），首 4KB 无 Exif / XMP / C2PA / ComfyUI 任何标记，`has_meta=0` |
| 参数覆盖（键存在 ≠ 有值） | seed 520/2792、steps 1022、sampler 1022、scheduler 1022、cfg 1289；**视频 532 条五项全为 0** |
| 时长 | 视频 532/532 有 duration，min 4.46 / 中位 8.00 / max 30.67 秒 → 时长口径已够，**fps 全缺** |
| negative | 29/2792（1%），现代工作流确实不写负面提示词 |
| 尺寸 | width/height 100% 有值，Top：768x1280(768) 307x512(742) 480x864(278) 832x1248(262) 608x1056(116) 832x1216(118) 1024x1024(112) |
| mp4 内层 | 426/532 有 `meta_source`（comment 命中），其中 354 有提示词、420 有底模 |

### 15.1 `media_meta.py`：参数按字段名回收 + fps（D1）

复用 §13.1 的「纯字段名 + 值域校验」层，只补参数，**不改链式优先级**：`comfy_generation` 链式已给出值的字段一律不动，字段名层只在字段为空时填。

1. 候选字段名（大小写不敏感、两端去空白）与值域：
   - `seed` ← `seed|noise_seed|rand_seed|random_seed`，`_as_int` 且 `0 <= v < 2**64`；
   - `steps` ← `steps|num_steps|sampling_steps|step_count`，int 且 `1..10000`；
   - `cfg` ← `cfg|cfg_scale|guidance_scale|true_cfg_scale`，float 且 `0..100`；
   - `sampler` ← `sampler|sampler_name|sample_type|solver`，值须落在 `KNOWN_SAMPLERS`（按小写比较）；
   - `scheduler` ← `scheduler|noise_schedule|schedule`，值须落在 `KNOWN_SCHEDULERS`。
2. **先测后定表**：`KNOWN_SAMPLERS` / `KNOWN_SCHEDULERS` 取「真实库实测出现值 ∩ 看起来就是采样器/调度器的形式」与 ComfyUI 官方枚举的并集，提交说明里必须给出实测命中数；表外的值不采用、不猜。
3. 诚实性：同一文件里同名字段出现**两个以上不同值**时不选边——字段留空，并把冲突写进 `note`（文案模板：`工作流里有 N 个不同的 <字段>，未按字段名猜测。`），链式结果不受影响。
4. provenance：`extra.params_source` = `chain|field_name`（任一参数由字段名层填入即为 `field_name`）；`extra.params_fields` 列出实际命中的字段名，供 UI 如实标注。
5. fps：`read_mp4_meta` 顺路读视频轨（`hdlr == 'vide'` 或 `tkhd` 宽 > 0）的 `mdhd`(timescale) 与 `stts`，`fps = 采样数合计 / (delta 合计 / timescale)`，保留 3 位小数；拿不到就是 `None`，**不得**默认 30。
6. `blank_result()` 必须新增 `"fps": None`——所有输出键恒定存在，否则写库与详情会炸。

### 15.2 `media_library.py`：schema v2、筛选与分面（D2）

1. 新列：`seed INTEGER`、`steps INTEGER`、`cfg REAL`、`sampler TEXT NOT NULL DEFAULT ''`、`scheduler TEXT NOT NULL DEFAULT ''`、`fps REAL`；建表 SQL 里同步，`PRAGMA user_version = 2`。
2. 迁移（**禁止**要求用户删库重扫）：对 v1 库逐列 `ALTER TABLE items ADD COLUMN`（重复列名吞掉），再用 `json_extract(extra_json,'$.seed')` 等回填；JSON1 不可用时把该行 `meta_mtime` 置 0，交给下次增量扫描重解析。
3. 新索引（partial，`WHERE missing_since IS NULL`）：`ix_items_sampler`、`ix_items_scheduler`。
4. `_build_where` 新维度，全部 bind 参数、同维度 OR、跨维度 AND：`samplers` / `schedulers` 列表 → `sampler IN (%s)`；`sizes` 列表（形如 `"768x1280"`，先按 `^\d+x\d+$` 校验再拆）→ OR 连接的 `(width = ? AND height = ?)` 组；`duration_min` / `duration_max` → `duration >= ?` / `duration <= ?`（非法值忽略该维度，不报错）。
5. `facets(root_ids=None)` 新增 `samplers` / `schedulers` / `sizes`，形状与 `checkpoints` 完全一致：`[{"name","count"}]`（sizes 的 name 是 `"WxH"`），`count DESC, name ASC`、`FACET_LIMIT=300`、只统计在场文件并与 `list_items` 同 root_ids 作用域。
6. 写库路径（行构造 / upsert）带上 6 个新键；`list_items` 若有显式列清单要同步补齐，行里直接给列值。`detail()` 以列为准、列为空时回落 `extra_json`（既有 29 条 negative 等的展示口径不能退化）。

### 15.3 前端（D3：`web/app.js` / `web/index.html` / `web/style.css` / `web/workbench.css`）

1. 左栏在底模 / LoRA 分面之后加**采样器 / 调度器 / 尺寸**三组，复用既有 `.lib-facet-*` 与 chip 多选交互（同维度多选 = OR），计数取 facets；筛选区加时长上下限两个 `number` 输入（单位秒，留空 = 不限）。
2. `lib.filter` 的新键名与服务端严格一致：`samplers` / `schedulers` / `sizes` / `duration_min` / `duration_max`；筛选生效时统计条继续显示「筛选 N 项 / 共 M 项」。
3. 详情参数区：种子 / 步数 / CFG / 采样器 / 调度器 / 帧率 / 时长 **只在有值时渲染**；`extra.params_source == 'field_name'` 时给该组标注「按字段名提取」（沿用提示词的文案与样式）。
4. 一切 metadata 派生字符串过 `esc()`；不新增硬编码颜色，只用现有主题 token；`node --check web/app.js` 必须过。

### 15.4 本轮明确不做（取舍写死）

- ~~**EBML（webm/mkv）元数据**~~：当时以「真实库 0 条、没有验收样本」缓做；**第三轮已实现**（§21），验收样本改用 ffmpeg 现造 + ffprobe 对账，不再依赖用户库里恰好有这类文件。其余三条取舍（JPEG EXIF、自动标签、伪造 negative）维持。
- **JPEG EXIF/XMP 解析器**：756 张全是无标记的 `sha256_*.jpg`，为它们造解析器零收益。
- **自动标签（底模即标签）**：沿用 §13.12 的两份真相问题，不做。
- **伪造 negative**：现代工作流确实没有负面提示词（29/2792），不从采样参数以外的地方凑。

## 16. 第二轮补充口径（自愈、批量与偏差确认，2026-10-12）

### 16.1 提取语义版本自愈（`media_meta.EXTRACT_VERSION` = 2）

只加列不重扫是不够的：增量扫描按 mtime+size 对账，**文件没改就永不重解析**，而参数回收与 fps 只有新提取器才产出。所以库层记一个 `meta.extract_version`，落后于 `media_meta.EXTRACT_VERSION` 时把待补的行 `meta_mtime=0`，本轮扫描顺路重解析；版本**只在一轮完整扫描后**落盘（取消/崩溃的那轮不算，下轮继续补，代价是已补过的行再解析一次——解析幂等，有界）。

- 重解析范围：`NOT (kind='image' AND has_meta=0)`，即无元数据的下载图/gif 不值得重扫。
- 真实库实测：v1 库打开即迁移 **0.36s**（逐列核对：老列一列未变，只加列 + 从 `extra_json` 回填）；自愈扫描 **31.1s**，重解析 2035 行、跳过 757 行、errors 0；第二轮扫描 **182ms**，`reparse=0 updated=0 unchanged=2792`。
- 自愈后的覆盖率与全新库**逐字段相等**：seed 1907(68%)、steps 1659(59%)、cfg 1289(46%)、sampler 1722(62%)、scheduler 1653(59%)、fps 532/532（视频全覆盖）。对照回收前：seed 520(19%)、steps/sampler/scheduler 各 1022(37%)、fps 0。
- 31s 比全新扫描 7.5s 慢，是因为走的是 2035 行 `UPDATE` + FTS 逐行 delete/insert 的改写路径（写库在同一事务里 `executemany`，不是逐行提交）；这是一次性代价，且进度可见、可取消。

### 16.2 批量操作（§15 之后的第二轮 UI）

新增 4 个 RPC，`idsJson` 一律是 id 数组的 JSON 字符串，服务端单次上限 `_LIB_BATCH_MAX = 2000`，前端按 500 一片分片：

| 方法 | 返回 | 语义 |
|---|---|---|
| `lib_set_star_batch(idsJson, on)` | `{ok, count}` | 一条 `UPDATE` 覆盖整批，`count` = 库里真实存在的行数 |
| `lib_tag_add_batch(idsJson, tagIdsJson)` | `{ok, count, tags}` | **并集**，不动条目原有标签；库里不存在的 `tag_id` 直接忽略 |
| `lib_tag_remove_batch(idsJson, tagIdsJson)` | `{ok, count, tags}` | 只摘指定的那几个 |
| `lib_delete_batch(idsJson)` | `{ok, deleted, missing}` | **一次**回收站调用带走整批 + 逐行 `mark_gone`；仍绝不连带共用 `.json` |

`tags` 是 `{item_id: [{id,name,color}]}`（§5.1 形状）。闸门在委托之前：非数组、空数组、坏 JSON、全是非正整数都直接 `ok:false`，一次库调用都不发。

### 16.3 本轮偏差与语义确认

1. **分面 chip 是新建的**：契约 §15.3 说「复用既有 `.lib-facet-*`」，但仓库里此前**没有**这套 DOM/类（底模/LoRA 是单选 `<select>`）。实际落地为新建 `.lib-facet-group/-title/-opts/-empty/-chip`，chip 本体与选中态复用既有 `.lib-chips` / `.lib-chip[aria-pressed]` / `.lib-chip-x`；资源库页也没有「左栏」，三组分面放在筛选 row3 之后的新行。
2. **时长区间会把图片整维滤掉**：`duration >= ?` 对 `duration IS NULL` 的图片恒不成立，这是既定语义（图片没有时长字段），界面必须在输入框旁写明「只匹配视频」。实测：真实库 `duration_min=5, duration_max=10` → 429 项，全是视频。
3. **`meta.db_version` 随 `user_version` 一起记为 "2"**（§3.2 未钉死，本轮确认）。
4. **迁移必须 `rebuild` FTS**：v1 库的 `items` 先于 `items_fts` 存在时，external-content 索引是空的，重解析的 `UPDATE` 触发器要 `'delete'` 一个不存在的 rowid，sqlite 直接 `database disk image is malformed`。已在 `_ensure_schema` 里对「表本来就有行、FTS 表新建」的情况补 `INSERT INTO items_fts(items_fts) VALUES('rebuild')`。
5. **超大 seed 的精度**：JS number 超过 2^53 会掉精度，`lib_detail` 对这种值改发字符串。真实库实测 **0 行**超过 2^53（最大 seed 1.0e15），所以这只是防御性口径，不改变现有数据显示。
6. **超出 int63 的 seed 不进列**：sqlite INTEGER 是 63 位，这类值列留 NULL、原值仍在 `extra_json` 由 `detail()` 回落展示，避免整批 `executemany` 抛 OverflowError。



## 17. 目录搬家重连（`MediaLibrary.relink_moved_roots`）

独立审计在真实库结构上找到的隐患：条目身份是 `path UNIQUE` + `root_id = sha1(abspath)`，而用户的库目录名里带版本号（`D:\ComfyUI_Mie_2026_V8.0\ComfyUI\output`）。目录一旦改名/换盘符/换机器，旧行整体失去作用域，新行是全新 id，**收藏与标签整片留在旧行上陪葬**——用户看到的是一个"清空了策展"的库。

规则（写死，不许放宽）：

1. 触发时机：`scan()` 完整走完、判缺失之后，按本次扫描的根调用；结果回 `result["relinked"]`，扫描完成文案追加「目录搬家已重连 N 项的收藏与标签」。
2. 候选：`o.root_id` **不在**本次扫描的根里（旧根已移出配置）+ 另一个在管根里有 `relpath`、`size`、`ext` 全等的在场行。为此新增 `ix_items_relpath`（否则 sqlite 每次为 join 现造自动索引，实测 2792 行 27ms，万级会退化）。
3. 必须是"搬家"而不是"两套库"：旧行 `path` 指向的文件**还在磁盘上**就一律不动（实测反例 `relinked=0`）。
4. 歧义不猜：一个旧行对上多个新行、或一个新行被多个旧行争，都跳过（实测：把同一份文件同时放进两个在管根 → `relinked=0`，新行谁也没收到收藏）。
5. **只复制不删旧行**：移出配置的根按 §13.14 是故意保留的（加回即恢复），删旧行会打破这条承诺。
6. 复制内容 = `starred` 与 `item_tags`（`INSERT OR IGNORE`，幂等）；海报不跟随（按 abspath 散列），新行首次进视口重新抓帧。
7. 实测（真实索引副本，relpath/size 取真实值）：搬家扫描 0.28s 重连 3 行，收藏与标签都落到新行，旧行仍在库里，作用域内总数 2789。重复导入/重复扫描幂等。

## 18. 标注导出与导入（`export_annotations` / `import_annotations`）

收藏与标签目前只活在 `media_library.db` 一个文件里：文件损坏、误删、换机、`purge_missing` 都会连带丢掉策展成果，且没有任何出路。本期补一条**只含人工标注**的导出/导入通路（机器解析出的元数据不属于它，重扫就能拿回来）。

1. 档案形状：`{kind:"cft-media-annotations", version:1, exported_at, tags:[{name,color,parent_path:[祖先名…]}], items:[{relpath,filename,ext,size,starred,tags:[名字]}]}`。`items` 只包含 `starred=1` 或有任何标签的行。
2. **身份键是 `relpath` + `size`，不是绝对路径**——与 §17 同一条思路，目录改名后档案仍认得回来。`size` 对不上时退回"`relpath` 唯一命中"；仍不唯一就记 `ambiguous` 不猜；库里没有的记 `unmatched`。
3. `parent_path` 是**祖先名字列表**，不是 `"父/子"` 字符串：标签名本身可以含 `/`，拼串再拆会把一层拆成两层（实测踩过并修掉）。
4. 导入**只补不删**：绝不取消库里已有的收藏或标签；同名标签按名字合并；缺失的标签（含祖先）按档案里的名字与颜色新建。
5. `dry_run` 先行，且**试算计数必须与真写一致**（祖先层也要计数，否则预览少报）；`unmatched`/`ambiguous` 如实回报，不静默丢弃。
6. RPC：`lib_export_annotations()` → `{ok,count,data}`；`lib_import_annotations(payload_json, dry_run)` → `{ok,…stats}`。闸门：非 JSON、顶层非对象、>32MB 直接拒；库层返回带 `msg`（不是本程序的档案）时 `ok` 必须为 `false`——**坏档案不许报成功**（这条是用例抓出来的：早期实现因 `dry_run=True` 把它算成成功）。
7. 前端：导出走 Blob + `a.download`（复用 `image-viewer.js` 已验证的下载写法，两种界面模式都可用）；导入 = 选文件 → 试算 → 展示六个计数 → 确认才真写 → 刷新列表/分面/标签下拉。
8. 实测（真实索引副本，40 条标注 + 父子标签）：导出 23ms / 6.3KB；试算 38ms 与真写 45ms 的 `{matched:40, starred:40, tags_applied:30, tags_created:2, unmatched:0, ambiguous:0}` 完全一致；清空收藏与标签后导入全部恢复（含颜色与父级）；重复导入幂等。

## 19. 本轮明确留观察的项（不实现）

- **同路径文件被就地替换且 mtime 不比旧值大**：对账只看 size+mtime，无内容哈希，元数据不会刷新。真实库里 756 张下载图按 sha256 命名不受影响，属低概率；补哈希的成本是每次扫描多读整片文件。
- **`facets()` 的 loras 逐行 Python 解析**：2792 行 ~60ms 无压力，触及设计自设的 10 万级上限时才需要 EAV facts 索引（§3.2 已缓期的那条）。
- **include/exclude 分面、批次聚合、对比视图、CLIP 以图搜图**：前者在 2792 规模收益不明确；后者要下模型跑 GPU，与本功能"stdlib + Pillow"的边界冲突，是另一个产品。
- **§15.4 的取舍**：jpg 全为无标记下载图、negative 仅 1% 不伪造、自动标签两份真相不做——这三条维持；webm/mkv 一条已在第三轮翻案（§21），理由是「用户以后会有」而不是「现在就有」。



## 20. 本轮接口与界面增量（覆盖 §5.2 的方法清单，§5.2 的表以本节 + §13.2 为准）

### 20.1 新增 RPC（`lib_` 前缀，反射可见）

| 方法 | 签名 | 返回 |
|---|---|---|
| `lib_set_star_batch` | `(ids_json:str, on:bool)` | `{"ok":bool,"count":int}` |
| `lib_tag_add_batch` | `(ids_json:str, tag_ids_json:str)` | `{"ok":bool,"count":int,"tags":{item_id:[§5.1 tags]}}`（并集） |
| `lib_tag_remove_batch` | `(ids_json:str, tag_ids_json:str)` | 同上形状（只摘指定标签） |
| `lib_delete_batch` | `(ids_json:str)` | `{"ok":bool,"deleted":int,"missing":int}`；一次回收站调用，仍不连带 `.json` |
| `lib_export_annotations` | `()` | `{"ok":bool,"count":int,"data":§18 档案}` |
| `lib_import_annotations` | `(payload_json:str, dry_run:bool=True)` | `{"ok":bool,…stats}`；库层回 `msg` 时 `ok` 必为 false |

- `ids_json` 是 id 数组的 JSON 字符串；服务端单次上限 `_LIB_BATCH_MAX = 2000`，前端按 `LIB_BATCH_CHUNK = 500` 分片。
- 入参闸门（非数组 / 空 / 坏 JSON / 全非正整数 / 超限）一律 `ok:false` 且**一次库调用都不发**。

### 20.2 `lib_list` 的入参与回包增量

- `flt` 新键：`samplers`、`schedulers`、`sizes`（`"WxH"`）、`duration_min`、`duration_max`、`with_facets`。
- 回包新键：`facets_skipped`（bool）。`with_facets` 传 `0/false/no/""` 时**不算分面**并回 `facets:{}`；前端只在首屏、筛选变化与写操作后带分面，续拉分页跳过。
- `detail` 里超过 2^53 的 `seed` 以字符串下发（JS number 会掉精度；真实库实测 0 行命中，属防御口径）。

### 20.3 新增 DOM id（§9.1 清单之外）

- 分面与时长：`#libSamplerFacet` `#libSchedulerFacet` `#libSizeFacet` `#libDurMin` `#libDurMax`，筛选组类 `.lib-facet-group/-title/-opts/-empty/-chip`（chip 本体复用 `.lib-chips` / `.lib-chip[aria-pressed]` / `.lib-chip-x`）。
- 分面溢出展开（窗口模式像素复检后补）：`.lib-facet-opts` 默认两行封顶 + 组内滚动，但真实窗口 1266px 下尺寸组一屏只露 4 个值、滚动条几乎看不见。改为渲染后量一次真实溢出（`scrollHeight > clientHeight + 2`），**只在真没露全时**往 `.lib-facet-group` 追加 `[data-lib-facetmore]`（`.lib-facet-more`，`role=button` + `tabindex=0`，文案「展开（共 N 项）」/「收起」）。控件挂组上而不是滚动容器里——塞进容器它自己就落在折行以下，等于看不见。展开态是 `slot._open` + `.lib-facet-opts.is-open`（`max-height: min(60vh, 560px)`），并且 `.lib-facet-group:has(> .lib-facet-opts.is-open) { flex: 1 1 100% }` 让展开那一组独占一行（留在三列里展开会竖成一列，比折叠态更难扫）；重绘前记焦点、重绘后把焦点接回同一个控件（展开钮与 chip 都是被整个替换的元素）。并入重绘签名，`resize` 与进页时 `libFacetRemeasure()` 重量一次（隐藏态量到的是 0，会让判断停在旧布局）。委托顺序：`[data-lib-facetmore]` 在 `.lib-facet-chip[data-lib-facetval]` 之前判，两者互不误伤。
- 多选与批量：`#libSelectMode` `#libSelRow` `#libSelState` `#libSelProg` `#libSelAllPage` `#libSelAllFilter` `#libSelClear` `#libSelBar` `#libSelBarCount` `#libSelStar` `#libSelUnstar` `#libSelTagAdd` `#libSelTagRemove` `#libSelReverse` `#libSelWorkflow` `#libSelDelete` `#libSelTagPop` `#libSelTagHint` `#libSelTagCount` `#libSelTagApply`，卡片勾选角标 `.ms-check` / `.cbox`（由 `#libGrid.lib-selecting` 控制显隐）。
- 标注转移：`#libExportAnn` `#libImportAnn` `#libImportFile`。
- 详情长文本展开（正面 / 负面提示词共用 `libDetailDescHtml` + `libFillDescMore`）：正文块加 `.lib-desc`（折叠态 `#detailPanel .detail-desc.lib-desc { max-height: calc(13px*1.8*3*var(--type-scale,1)); overflow:hidden }`），**共享类 `.detail-desc` 的全局行为不动**（模型库详情也在用，它是 180px 固定高滚动框）。控件 `#detailPanel` 内的 `[data-lib-descmore]`（`.lib-desc-more`）与正文块**同级**挂在 `.dt-sec` 里，文案「展开全文（共 N 字）」/「收起」，`role=button` + `tabindex=0` + `aria-pressed`，`Enter`/`Space` 与鼠标同权，重画后焦点接回；展开态 `min(60vh, 560px)` + 块内滚动，所以 `title` 里如实写「没看全就滚动」，不承诺「展开即全文」。三条硬口径：① 必须在详情浮层 `display:flex` **之后**量（隐藏态 `scrollHeight`/`clientHeight` 都是 0，会误判成没溢出）；② 内嵌 `@font-face` 换字会改行高，`document.fonts.ready` 后用同一个判定重测一次，令牌 `lib.detailRequest` 对不上或已离开资源库页就不动 DOM；③ 展开态按 `String(detail.id)` 存在 `lib.descOpen` + `lib.descOpenId`，翻页换条目与关详情都回折叠态。空正文（只有占位文案）与没溢出的正文一律不挂控件。
- 详情翻页：`#libDetailPos` `#libDetailPrev` `#libDetailNext`（详情头动态生成）。
- 对应状态键：`lib.filters.{samplers,schedulers,sizes,duration_min,duration_max}`、`lib.{selectMode,sel,selAnchor,byId,prog,batchRunning,selTagIds,selTagMode,selFetching,selFetchToken,descOpen,descOpenId}`。

### 20.4 延迟实测（真实库 2792 + 合成根 7 = 2799 条，同机同盘）

| 路径 | 实测 |
|---|---|
| `lib_list` 首屏 200 条（含分面） | 190 ms |
| `lib_list` 续拉 200 条（`with_facets=0`） | 45 ms |
| `facets()` 不带作用域 / 带 `root_ids` | 62 ms / 130 ms |
| `list_items(200)` / `_lib_enrich×200` / `json.dumps` | 15 / 8 / 1 ms |
| v1 → v2 迁移 | 0.36 s（老列逐列核对未变） |
| 升级后补解析扫描 | 31.1 s（重解析 2035 行，跳过 757 行）；第二轮 182 ms |
| 标注导出 40 条 / 导入试算 / 真写 | 23 / 38 / 45 ms（试算与真写计数逐项一致） |
| 搬家重连扫描 | 0.28 s（3 行重连，收藏与标签到位，旧行保留） |

给分面加 `(root_id, col)` 复合部分索引实测只把 133ms 降到 128ms（噪声级），**不做**；首屏 190ms 里那 130ms 是分面本身，翻页已经跳过。真要再压只能按查询签名缓存分面，本期不引入这层状态。

## 21. 第三轮：EBML 解析与详情长文本展开（v1.3，2026-10-13）

### 21.1 `media_meta.py`：`.webm` / `.mkv` 的 EBML（`EXTRACT_VERSION = 3`）

纯 stdlib 流式读取，风格与 `read_mp4_meta` 一致：不抛异常、畸形输入降级为部分结果、有读取上限、不整文件读进内存。

1. **元素 ID 以实测钉死**（两条曾被按记忆写错过，用真实文件才试得出来）：`Segment 0x18538067`、`Info 0x1549A966`、`TimecodeScale 0x2AD7B1`、`Duration 0x4489`、**`Tracks 0x1654AE6B`**（不是 0x1654AE67）、`TrackEntry 0xAE`、`TrackType 0x83`、`DefaultDuration 0x23E383`、`Video 0xE0`、`PixelWidth 0xB0`、`PixelHeight 0xBA`、`DisplayWidth 0xB5` / `DisplayHeight 0xB6`、**`Tags 0x1254C367`**（不是 0x1254C076）、`Tag 0x7373`、`SimpleTag 0x67C8`、`TagName 0x45A3`、`TagString 0x4487`、`Cluster 0x1F43B675`。ID 与数据长度都是变长（前导零位数定长；`0xFF...` 表未知长度）。不依赖 `SeekHead` / `Cues`。
2. **时长**：`duration = TimecodeScale × Duration / 1e9`（`TimecodeScale` 缺省 1,000,000）。`Duration` 的 FLOAT **宽度不固定**：4 / 8 字节都常见，0 / 1 / 2 字节的畸形实现按 IEEE 754 高位补零处理；解出非正或 `>= 1e9` 秒的一律丢弃。
3. **帧率**：视频轨（`TrackType == 1`）的 `DefaultDuration` **单位是纳秒**，`fps = 1e9 / DefaultDuration`，保留 3 位（实测 ffmpeg 24000/1001→41708375、25→40000000、30→33333333 都能精确还原；按 `1e6/DD` 会得到 0.008–0.024 的荒谬值）。合法区间 `0.01 <= fps <= 10000`，越界或拿不到都留 `None`，**不默认 30**。
4. **宽高**：同一条视频轨 `Video → PixelWidth/PixelHeight`；只有 `DisplayWidth/DisplayHeight` 时也接受，并在 `extra.ebml.sizing = "display"` 标出来。
5. **提示词 / 工作流**：`Tags → Tag → SimpleTag`，`TagName` 大小写不敏感命中白名单 `comment|description|title|prompt|workflow`，取值优先序 `comment → title → description → prompt → workflow`；值就是 mp4 `©cmt` 那条路的同一个 `{"prompt":…,"workflow":…}` JSON，**必须复用同一个后处理入口**（`_absorb`），所以 `prompt/negative/checkpoint/loras/models/seed/steps/cfg/sampler/scheduler` 的语义与 mp4 完全同形，差异只在 `source = "ebml_tag"`。**不读 Segment 级 `Title(0x7BA9)`**：ffmpeg 的 `-metadata title=` 落在那里，而 ComfyUI / VHS 写 `comment`；把随机库的标题也当 JSON 试只会误报「不是 ComfyUI 文本记录」。
6. **窗口与上限**：`MAX_EBML_ELEMENTS = 20000`、`MAX_EBML_READ_BYTES = 8MB`、`MAX_EBML_LEAF_BYTES = 4MB`（更大的叶子＝附件/字幕，跳过不读）。`Cluster` 按声明长度整块跳过；未知长度的 Cluster 只能逐个子元素读到下一个顶层元素，靠 `MAX_EBML_ELEMENTS` 兜住不死循环。`Info` 通常在头部，`Tags` 有的封装器写在最后一个 Cluster 之后 → 头部窗口没拿到标签时再扫尾部窗口。
7. **provenance**：`extra.ebml = {doctype, segment_found, window(whole|head+tail), elements, clusters, read_bytes, used[实际取值的元素名], tags[命中的 TagName], tags_at(whole|head|tail), [sizing], [big_leaves], [stopped]}`。旧的 `"v1 不解析 EBML"` 字符串不再存在。
8. **升级自愈改成定向口径**：`media_library._EXTRACT_SCOPED_REPARSE = {3: (".webm", ".mkv")}` + `_stale_reparse_stmt(current, version)`。跨过的版本里只要有一个是「整库级」改动就退回整库补解析（宁可多标不可漏标）；全是定向版本时只 `WHERE ext IN (...)`。**理由**：真实库 2792 行里 webm/mkv 是 0 个，为这一版让全库重解析是实测 2035 行 / 31 秒的纯浪费。
9. **验收样本**（用户库里 0 条，所以自己造真样本对账）：ffmpeg 生成 `webm/vp9 832x1248 24000/1001 5s` 与 `mkv/h264 640x480 30fps 3s`，`-metadata comment=` 写入 ComfyUI 形状 JSON；与 ffprobe 对账 `duration 5.005 / 3.0`、`fps 23.976 / 30.0`、宽高逐位一致，`comment` 里的 `seed/steps` 回收成功。同一份 comment 喂 mp4 通路得到同样的字段（差异只在 `source`），证明「复用同一入口」不是口号。

### 21.2 详情长文本展开的实测数字（口径见 §20.3）

真实库条目：3464 字提示词折叠 `clientHeight 70 / scrollHeight 1497` → 展开 `403 / 1497`（60vh 上限，块内可滚）；418 字条目 `70 / 187` → 展开 `187 / 187` → 再点回 `70`；36 字条目 `23 / 23` **不挂控件**；无提示词条目只有占位文案、不挂控件；`←/→` 翻页与关详情重开都回到折叠态；`Enter` / `Space` 与鼠标同权且焦点留在控件上；同面板里非 `lib-desc` 的 `.detail-desc` computed 仍是 `180px / auto`（共享类未动）。**负面提示词**：真实库 0 条带 negative，该分支是用页面内注入 1233 字长文本走同一个 helper 验的，没有真实样本这一点如实记下。

## 12. 补充钉死（A4/A5 共用，与 §6/§9 同权重）

- 测试性注入：`media_library.py` 内部一律通过模块级 `_extract(path, ext)` 调元数据提取（函数体内 `import media_meta`，异常吞掉返回 `{}`）。A2 的单测 monkeypatch `media_library._extract`，从而**不依赖 A1 完成**。
- A3 的单测一律 `patch.object(webui.Api, "_media_lib", ...)` 注入假库，不依赖 A2 完成。
- 设置页控件 id（镜像现有 `models_dirs` 控件的写法与位置，A4 提供、A5 绑定）：
  `#setMediaRoots`（textarea，每行一个绝对路径）、`#setMediaExclude`（textarea，每行一个相对目录）、`#setMediaPageSize`（number）、`#libRootDetect`（button，调 `lib_autodetect_roots` 后回填 `#setMediaRoots`）。
- `Api` 侧新增非 RPC 辅助（下划线开头即对桥接不可见）：`_media_lib()`、`_media_roots_for_bridge()`。
