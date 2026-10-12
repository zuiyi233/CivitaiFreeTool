# -*- coding: utf-8 -*-
"""资源库索引层：sqlite 索引 + 增量扫描对账 + 分面/分组/搜索/标签查询。

契约出处：docs/media-library-contract.md §3（表结构与类 API，schema v2 见 §15.2）、§4（扫描管线）、
§5.1/§5.3（返回形状，参数分面见 §15.2.5）。
本文件只管「数据」：缩略图生成、RPC 封装在 webui.py，元数据解析在 media_meta.py。
"""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time

import config

DB_NAME = "media_library.db"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
KINDS = {"image": IMAGE_EXTS, "video": VIDEO_EXTS}

# 建库时特性探测的结果（本环境 sqlite 3.49 有 FTS5，但降级路径必须始终可用）
FTS_AVAILABLE = True

# 目录跳过规则（§4.1）：以 . 开头 / 这些固定名字 / 以 .images 结尾
SKIP_DIR_NAMES = ("__pycache__", "node_modules")

POSTER_DIR_NAME = "media_posters"
POSTER_BYTES_LIMIT = 2 * 1024 * 1024          # §7 poster 回传上限
SNIPPET_LEN = 120                              # §5.1 snippet
PAGE_SIZE_DEFAULT = 200                        # §7 列表分页
PAGE_SIZE_MAX = 500                            # §7 硬上限
THUMB_BATCH_MAX = 40                           # §7 缩略图批
FACET_LIMIT = 300                              # §7 facet 下拉上限
ERRORS_MAX = 200                               # §7 errors 明细上限
MISSING_PURGE_DAYS = 30                        # §7 缺失确认期
SCHEMA_VERSION = 2                             # §15.2.1：v1 → v2 加参数列，PRAGMA user_version 即此值

_EXT_KIND = {}
for _kind, _exts in KINDS.items():
    for _ext in _exts:
        _EXT_KIND[_ext] = _kind

# 排序白名单（§5.2）：任何用户输入都只能命中这里的键，绝不拼接原始字符串
SORT_SQL = {
    "time_desc": "mtime DESC, id DESC",
    "time_asc": "mtime ASC, id ASC",
    "name_asc": "filename COLLATE NOCASE ASC, id ASC",
    "size_desc": "size DESC, id DESC",
    "added_desc": "added_at DESC, id DESC",
}
SORT_DEFAULT = "time_desc"
GROUP_KEYS = ("", "date", "checkpoint", "dir")

# §15.2.4：sizes 维度必须先校验 ^\d+x\d+$ 再拆 —— 用 fullmatch 等价该锚定式，
# 且不被 re.match 里 '$' 会放行尾部换行的怪癖绕过（"768x1280\n"）。校验失败的值直接忽略。
_SIZE_RE = re.compile(r"\d+x\d+")

# sqlite INTEGER 是有符号 64 位；超出这个域的 seed 只能落 NULL（见 _db_int）
_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1

_SCHEMA_SQL = (
    """CREATE TABLE IF NOT EXISTS items(
  id            INTEGER PRIMARY KEY,
  path          TEXT NOT NULL UNIQUE,
  root_id       TEXT NOT NULL,
  relpath       TEXT NOT NULL,
  dir_path      TEXT NOT NULL,
  filename      TEXT NOT NULL,
  ext           TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('image','video')),
  size          INTEGER NOT NULL,
  mtime         REAL NOT NULL,
  meta_mtime    REAL NOT NULL DEFAULT 0,
  added_at      REAL NOT NULL,
  missing_since REAL,
  checkpoint    TEXT NOT NULL DEFAULT '',
  loras_json    TEXT NOT NULL DEFAULT '[]',
  models_json   TEXT NOT NULL DEFAULT '[]',
  width         INTEGER, height INTEGER, duration REAL,
  seed          INTEGER, steps INTEGER, cfg REAL,
  sampler       TEXT NOT NULL DEFAULT '',
  scheduler     TEXT NOT NULL DEFAULT '',
  fps           REAL,
  prompt        TEXT NOT NULL DEFAULT '',
  negative      TEXT NOT NULL DEFAULT '',
  search_text   TEXT NOT NULL DEFAULT '',
  has_meta      INTEGER NOT NULL DEFAULT 0,
  meta_source   TEXT NOT NULL DEFAULT '',
  starred       INTEGER NOT NULL DEFAULT 0,
  has_poster    INTEGER NOT NULL DEFAULT 0,
  extra_json    TEXT NOT NULL DEFAULT '{}'
)""",
    "CREATE INDEX IF NOT EXISTS ix_items_kind ON items(kind) WHERE missing_since IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_items_ck ON items(checkpoint) WHERE missing_since IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_items_dir ON items(dir_path) WHERE missing_since IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_items_star ON items(starred) WHERE missing_since IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_items_mtime ON items(mtime DESC)",
    "CREATE INDEX IF NOT EXISTS ix_items_missing ON items(missing_since) WHERE missing_since IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_items_loras ON items(loras_json) WHERE missing_since IS NULL",
    # §17 搬家重连按 relpath 找候选：没有这条索引，sqlite 只能为 join 现造自动索引（万级库会退化）
    "CREATE INDEX IF NOT EXISTS ix_items_relpath ON items(relpath)",
    "CREATE INDEX IF NOT EXISTS ix_items_sampler ON items(sampler) WHERE missing_since IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_items_scheduler ON items(scheduler) WHERE missing_since IS NULL",
    """CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL DEFAULT '', parent_id INTEGER)""",
    "CREATE TABLE IF NOT EXISTS item_tags(item_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY(item_id, tag_id))",
    "CREATE INDEX IF NOT EXISTS ix_item_tags_tag ON item_tags(tag_id)",
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",
)

# §15.2.1/§15.2.2：v1 库加列的确切类型与默认值（顺序即 ALTER 顺序，与上面建表 SQL 一致）
_V2_NEW_COLUMNS = (("seed", "INTEGER"), ("steps", "INTEGER"), ("cfg", "REAL"),
                   ("sampler", "TEXT NOT NULL DEFAULT ''"), ("scheduler", "TEXT NOT NULL DEFAULT ''"),
                   ("fps", "REAL"))
# 参数原本只活在 extra_json 里（§3.2 平列只到尺寸/时长），迁移就是把它提到列上。
# json_valid 守卫：库里只要有一行 extra_json 是坏 JSON，json_extract 就会整条 UPDATE 报错，
# 那样反而把所有正常行的回填一起赔进去 —— 坏行留给下面的 meta_mtime=0 走增量重扫。
_V2_BACKFILL_SQL = """UPDATE items SET
  seed = json_extract(extra_json, '$.seed'),
  steps = json_extract(extra_json, '$.steps'),
  cfg = json_extract(extra_json, '$.cfg'),
  sampler = COALESCE(json_extract(extra_json, '$.sampler'), ''),
  scheduler = COALESCE(json_extract(extra_json, '$.scheduler'), ''),
  fps = json_extract(extra_json, '$.fps')
  WHERE json_valid(extra_json)"""
# 回填没覆盖到的行（extra_json 非法）交回增量扫描：meta_mtime=0 让 §3.2 的 mtime>meta_mtime 成立
_V2_REPARSE_BAD_ROWS_SQL = ("UPDATE items SET meta_mtime = 0 "
                            "WHERE extra_json IS NULL OR extra_json = '' OR json_valid(extra_json) = 0")
# JSON1 不可用（精简构建的 sqlite）时：整库置 0，下次扫描逐行重解析回填（§15.2.2）
_V2_REPARSE_ALL_SQL = "UPDATE items SET meta_mtime = 0"
# §15.1 改了提取语义（参数按字段名回收、stts 帧率）→ 已入库的老行必须重解析才有新字段。
# 「图片且没元数据」的行（下载来的 jpg / gif）重解析也拿不到东西，跳过省时间。
_EXTRACT_STALE_REPARSE_SQL = ("UPDATE items SET meta_mtime = 0 "
                              "WHERE NOT (kind = 'image' AND has_meta = 0)")
# 版本 → 「这一版只改了这些扩展名的解析路径」。跨过的版本里只要有一个不在表里，就退回整库补解析；
# 全是表内的定向版本时只补这些扩展名——真实库 0 个 webm/mkv，为它们让 2792 行白重扫 31 秒不值。
_EXTRACT_SCOPED_REPARSE = {3: (".webm", ".mkv")}


def _stale_reparse_stmt(current, version):
    """返回 (SQL, 参数)。宁可多标不可漏标：定向口径只在「跨过的每个版本都声明了扩展名」时才用。"""
    exts = []
    for ver in range(int(current) + 1, int(version) + 1):
        scoped = _EXTRACT_SCOPED_REPARSE.get(ver)
        if scoped is None:
            return _EXTRACT_STALE_REPARSE_SQL, ()
        exts.extend(scoped)
    if not exts:
        return _EXTRACT_STALE_REPARSE_SQL, ()
    return ("UPDATE items SET meta_mtime = 0 WHERE ext IN (%s)" % ",".join("?" * len(exts)),
            tuple(sorted(set(exts))))
# §17「搬家」候选：旧行所属根已不在本次扫描的根里，而某个在管的根里有 relpath/size/ext 全等的在场行
_RELINK_CANDIDATES_SQL = """SELECT o.id AS old_id, o.path AS old_path, n.id AS new_id
FROM items o
JOIN items n ON n.relpath = o.relpath AND n.size = o.size AND n.ext = o.ext
WHERE n.missing_since IS NULL AND n.root_id IN (%s)
  AND o.root_id NOT IN (%s) AND o.path <> n.path"""

_FTS_TABLE_SQL = ("CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5("
                  "search_text, content='items', content_rowid='id', tokenize='unicode61')")
# external-content FTS5 必须靠触发器与 items 同步，否则 MATCH 结果与行内容脱节
_FTS_TRIGGERS_SQL = (
    """CREATE TRIGGER IF NOT EXISTS trg_items_fts_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, search_text) VALUES (new.id, new.search_text);
END""",
    """CREATE TRIGGER IF NOT EXISTS trg_items_fts_au AFTER UPDATE OF search_text ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, search_text) VALUES('delete', old.id, old.search_text);
  INSERT INTO items_fts(rowid, search_text) VALUES (new.id, new.search_text);
END""",
    """CREATE TRIGGER IF NOT EXISTS trg_items_fts_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, search_text) VALUES('delete', old.id, old.search_text);
END""",
)

_ITEM_COLUMNS = ("id, path, root_id, relpath, dir_path, filename, ext, kind, size, mtime, added_at, "
                 "missing_since, checkpoint, loras_json, models_json, width, height, duration, "
                 "seed, steps, cfg, sampler, scheduler, fps, prompt, "
                 "negative, search_text, has_meta, meta_source, starred, has_poster, extra_json")

# 命名列参数：位置参数在下面这种列数下极易错位，字典键即列名
_INSERT_ITEM_SQL = """INSERT OR REPLACE INTO items(
  path, root_id, relpath, dir_path, filename, ext, kind, size, mtime, meta_mtime, added_at, missing_since,
  checkpoint, loras_json, models_json, width, height, duration, seed, steps, cfg, sampler, scheduler, fps,
  prompt, negative, search_text, has_meta, meta_source, starred, has_poster, extra_json)
VALUES(:path, :root_id, :relpath, :dir_path, :filename, :ext, :kind, :size, :mtime, :meta_mtime, :added_at,
  :missing_since, :checkpoint, :loras_json, :models_json, :width, :height, :duration, :seed, :steps, :cfg,
  :sampler, :scheduler, :fps, :prompt, :negative, :search_text, :has_meta, :meta_source, :starred,
  :has_poster, :extra_json)"""

# 重解析不得动 added_at/starred：用户收藏与「加入时间」与文件内容无关
_UPDATE_ITEM_SQL = """UPDATE items SET path=:path, size=:size, mtime=:mtime, meta_mtime=:meta_mtime,
  missing_since=NULL, checkpoint=:checkpoint, loras_json=:loras_json, models_json=:models_json,
  width=:width, height=:height, duration=:duration, seed=:seed, steps=:steps, cfg=:cfg,
  sampler=:sampler, scheduler=:scheduler, fps=:fps, prompt=:prompt, negative=:negative,
  search_text=:search_text, has_meta=:has_meta, meta_source=:meta_source, has_poster=:has_poster,
  extra_json=:extra_json, ext=:ext, kind=:kind, filename=:filename, dir_path=:dir_path
  WHERE root_id=:root_id AND relpath=:relpath"""


def db_path():
    """每次调用现取 config.APP_DIR，测试 patch 目录与数据迁移都靠这条。"""
    return os.path.join(config.APP_DIR, DB_NAME)


def poster_path(path):
    """视频海报缓存路径：键取归一化绝对路径，与 A3 的 _cover_thumb 缓存思路一致（不改名不换键）。"""
    key = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8", "ignore")).hexdigest()[:32]
    return os.path.join(config.APP_DIR, POSTER_DIR_NAME, key + ".jpg")


def _extract(path, ext=None):
    """元数据提取的唯一间接层。

    为什么存在：media_meta 由另一个代理并行开发，扫描线程绝不能因为它缺失/报错而崩；
    单测也只需要 patch 这一个函数（契约 §12）。
    """
    try:
        import media_meta
        result = media_meta.extract(path, ext)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _to_posix(path):
    return str(path).replace("\\", "/")


def _rel_posix(abs_path, root_abs):
    """相对根的 posix 路径（''=根目录本身），入库键，跨平台一致。"""
    try:
        rel = os.path.relpath(abs_path, root_abs)
    except ValueError:
        return _to_posix(abs_path)
    rel = _to_posix(rel)
    return "" if rel == "." else rel


def _norm_root(root):
    return os.path.abspath(os.path.expanduser(str(root).strip().strip('"')))


def _root_id(root_abs):
    return hashlib.sha1(os.path.normcase(root_abs).encode("utf-8", "ignore")).hexdigest()[:16]


def root_id(root_abs):
    """配置根 → root_id：调用方（RPC 层）据此把「已移出配置的根」的行从列表里排除。"""
    return _root_id(os.path.normpath(os.path.abspath(str(root_abs or ""))))


def _ext_of(filename):
    return os.path.splitext(filename)[1].lower()


def _like_escape(text):
    """LIKE 通配符转义：用户输入里的 % 和 _ 必须按字面量匹配（配合 SQL 的 ESCAPE '\\\\'）。"""
    return (str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            .replace("[", "\\[").replace("]", "\\]"))


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [x for x in value if x not in (None, "")]
    return [value]


def _as_int_list(value):
    out = []
    for item in _as_list(value):
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _json_loads(text, default):
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _placeholders(values):
    """参数化 IN 子句：占位符数量只由集合大小决定，值永远走 bind 参数。"""
    return ",".join("?" * len(values))


class _ErrorSink(object):
    """扫描错误收集器：明细最多 ERRORS_MAX 条，超出折叠成一条计数（§4.5）。"""

    def __init__(self, target):
        self.target = target
        self.extra = 0

    def add(self, msg):
        if len(self.target) < ERRORS_MAX:
            self.target.append(str(msg))
        else:
            self.extra += 1

    def finish(self):
        if not self.extra:
            return
        # 先截断再补折叠行：折叠提示是「第 201 条」，若让调用方按 ERRORS_MAX 切片就会被当成明细丢掉，
        # UI 于是少报（§4.5 明细 200 条 + 1 条折叠说明）。
        del self.target[ERRORS_MAX:]
        self.target.append("另有 %d 条错误已折叠" % self.extra)


class MediaLibrary(object):
    """资源库索引：一次操作一条短连接（理由见 _db）。"""

    def __init__(self, path=None):
        self.path = os.path.abspath(path) if path else db_path()
        self._fts_ok = False
        self._ready = False
        with self._db():                         # 立即建表：首次查询不用等 DDL
            pass

    # ------------------------------------------------------------------ 连接
    @contextlib.contextmanager
    def _db(self):
        # 每操作新建短连接：扫描线程与 RPC 线程池共用同一个实例，长连接反而要自己加锁串行；
        # 页面级 QPS 极低，开连接的开销可以忽略。
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        if self._ready and not os.path.exists(self.path):
            self._ready = False                  # 库文件被外部清掉（数据迁移/测试）→ 重新建表
        with contextlib.closing(sqlite3.connect(self.path, timeout=15)) as conn:
            conn.row_factory = sqlite3.Row
            # busy_timeout/foreign_keys 是连接级设置，journal_mode=WAL 落库后持久，仍每次重设以防遗漏
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                self._ensure_schema(conn)
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _ensure_schema(self, conn):
        if self._ready:
            return
        self._migrate_to_v2(conn)
        conn.executescript("PRAGMA user_version = %d;\n" % SCHEMA_VERSION + ";\n".join(_SCHEMA_SQL) + ";")
        global FTS_AVAILABLE
        had_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items_fts'"
                               ).fetchone()
        try:
            conn.execute(_FTS_TABLE_SQL)
            for sql in _FTS_TRIGGERS_SQL:
                conn.execute(sql)
            if had_fts is None:
                # 「先有行、后建 FTS 表」的库（迁移来的 v1 库，或老构建没编 FTS5 后来才有的）：
                # external-content 索引此时是空的，而 UPDATE 触发器要 'delete' 一个不存在的 rowid，
                # sqlite 直接回 database disk image is malformed —— 必须 rebuild 把历史行灌进索引。
                conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
            self._fts_ok = True
            FTS_AVAILABLE = True
        except sqlite3.OperationalError:
            # 老 sqlite / 精简构建没编 FTS5：搜索整体降级为 LIKE，其余功能不受影响
            self._fts_ok = False
            FTS_AVAILABLE = False
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('db_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('fts', ?)", ("1" if self._fts_ok else "0",))
        self._ready = True

    @staticmethod
    def _migrate_to_v2(conn):
        """v1 → v2：加参数列 + 从 extra_json 回填（§15.2.2）。**禁止**要求用户删库重扫。

        幂等：ALTER 撞已存在的列（上次崩在中间）只吞掉；回填只按 extra_json 计算，重跑结果相同。
        user_version 由后面的建表脚本统一置 2，所以中途崩溃的库下次打开还会再走一遍这里。
        """
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        except (sqlite3.Error, TypeError, ValueError):
            version = 0
        if version >= SCHEMA_VERSION:
            return
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'items'").fetchone() is None:
            return                               # 空库/新库：建表脚本自带新列，无需迁移
        for name, ddl_type in _V2_NEW_COLUMNS:
            try:
                conn.execute("ALTER TABLE items ADD COLUMN %s %s" % (name, ddl_type))
            except sqlite3.OperationalError:
                pass                             # duplicate column = 半个迁移的库，继续
        try:
            conn.execute(_V2_BACKFILL_SQL)
            conn.execute(_V2_REPARSE_BAD_ROWS_SQL)
        except sqlite3.OperationalError:
            # 这次 execute 抛错只有两种可能：构建没编 JSON1，或 extra_json 里有 json_valid 挡不住的怪值。
            # 与其猜哪一行坏，不如整库 meta_mtime=0：下次增量扫描按 §3.2 的 mtime > meta_mtime 逐行重解析回填。
            conn.execute(_V2_REPARSE_ALL_SQL)

    @staticmethod
    def _extract_version():
        """当前提取语义版本（`media_meta.EXTRACT_VERSION`）；读不到就返回 0，表示不做补解析。"""
        try:
            import media_meta
            return int(getattr(media_meta, "EXTRACT_VERSION", 0) or 0)
        except Exception:
            return 0

    def _mark_stale_extract(self, version):
        """库里记录的提取版本落后 → 把老行标成待重解析，本轮扫描顺路补新字段（§15.1）。

        必须走这条路：v1 索引迁移只是把 extra_json 里的旧值提到列上，而增量扫描按 mtime+size
        对账，文件没改就永远不会重新解析，参数回收和帧率这些新字段便永远补不上。
        反向也要省：某一版只改了 webm/mkv 的解析时，整库重扫就是纯浪费（见 `_stale_reparse_stmt`）。
        """
        if version <= 0:
            return 0
        with self._db() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'extract_version'").fetchone()
            try:
                got = int(row["value"]) if row is not None else 0
            except (TypeError, ValueError):
                got = 0
            if got >= version:
                return 0
            sql, params = _stale_reparse_stmt(got, version)
            cur = conn.execute(sql, params)
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ 扫描
    def scan(self, roots, exclude=(), progress=None, cancel=None):
        """增量扫描 + 对账（§4）。roots=绝对目录列表，exclude=相对根的 posix 目录。"""
        root_list = [_norm_root(r) for r in _as_list(roots)]
        root_list, skipped_roots = self._drop_nested_roots(root_list)
        excl = self._norm_exclude(exclude)
        result = {"roots": [], "skipped_roots": skipped_roots, "total": 0,
                  "added": 0, "updated": 0, "unchanged": 0, "missing": 0,
                  "relinked": 0, "reparse": 0, "errors": []}
        errors = _ErrorSink(result["errors"])
        extract_version = self._extract_version()
        result["reparse"] = self._mark_stale_extract(extract_version)
        self._progress(progress, "walk", 0, 0, "正在枚举文件")

        cancelled = False
        for root_abs in root_list:
            entry = {"path": root_abs, "status": "ok", "count": 0}
            result["roots"].append(entry)
            if not os.path.isdir(root_abs):
                # 根本身不可达：该根的行一律不判缺失（§4.3），只记 errors
                entry["status"] = "unreachable"
                errors.add("unreachable: %s" % root_abs)
                continue
            walk = self._walk(root_abs, excl, cancel, errors)
            if walk is None:                      # 取消：本次 walk 知识不完整，绝不判缺失
                entry["status"] = "cancelled"
                cancelled = True
                break
            entry["count"] = len(walk["disk"])
            result["total"] += len(walk["disk"])
            if self._reconcile(root_abs, walk, excl, progress, cancel, result, errors):
                cancelled = True                 # 解析阶段被取消：本轮不完整，不刷新 last_scan_at
                break
        if not cancelled:
            # 只有完整 walk 才算一次成功扫描：取消/中断不刷新 last_scan_at，页面进入时会立刻补扫
            with self._db() as conn:
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_scan_at', ?)",
                             (str(time.time()),))
                # 版本只在本轮完整解析后落盘：中途取消/崩溃时留在旧值，下轮继续补解析
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('extract_version', ?)",
                             (str(extract_version),))
            # 搬家必须在判缺失之后：旧根被改名/换盘符时把收藏与标签复制到全等的新行上
            result["relinked"] = self.relink_moved_roots([_root_id(r) for r in root_list])
            self.purge_missing(MISSING_PURGE_DAYS)   # §4.6：扫描末尾清一次超期缺失行
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('scan_stats', ?)",
                         (json.dumps({k: result[k] for k in
                                      ("total", "added", "updated", "unchanged", "missing")}, ensure_ascii=False),))
        errors.finish()
        self._progress(progress, "done", result["total"], result["total"], "扫描完成")
        return result

    @staticmethod
    def _drop_nested_roots(root_list):
        """重叠根剪枝：items.path 是 UNIQUE 列，内层根扫到同一绝对路径时走 INSERT OR REPLACE，
        会把外层根刚写入的 added_at/starred 一起清零（§3.2 的「重解析不动收藏」只覆盖 UPDATE 路径）。

        规则：保留最外层根；大小写/链接归一后等价的重复根保留列表中更靠前的那个；
        被丢弃的根进 skipped_roots 供 RPC 层在 msg 里说明。不存在的根没有建行能力，不参与剪枝，
        好让「unreachable」继续按原样上报（§4.3）。
        """
        keys = []
        for root_abs in root_list:
            try:
                key = os.path.normcase(os.path.realpath(root_abs))
            except (OSError, ValueError):
                key = os.path.normcase(root_abs)
            while len(key) > 1 and key.endswith(("/", "\\")):
                key = key[:-1]
            keys.append(key)
        keep, dropped = [], []
        for index, root_abs in enumerate(root_list):
            if not os.path.isdir(root_abs):
                keep.append(root_abs)
                continue
            outer = None
            for other, key in enumerate(keys):
                if other == index or not key:
                    continue
                # 等价重复：只丢靠后的那个，否则两个都会被对方「包含」而双双消失
                same = keys[index] == key and other < index
                inside = keys[index].startswith(key + os.sep)
                if same or inside:
                    if outer is None or len(key) < len(keys[outer]) or (len(key) == len(keys[outer]) and other < outer):
                        outer = other
            if outer is None:
                keep.append(root_abs)
            else:
                dropped.append({"path": root_abs, "inside": root_list[outer]})
        return keep, dropped

    @staticmethod
    def _norm_exclude(exclude):
        out = set()
        for item in _as_list(exclude):
            rel = _to_posix(str(item).strip()).strip("/")
            if rel and rel != ".":
                out.add(rel)
        return out

    @staticmethod
    def _skip_dir(name, rel_dir, excl):
        if name.startswith("."):
            return True
        if name in SKIP_DIR_NAMES:
            return True
        if name.lower().endswith(".images"):
            return True
        return any(rel_dir == d or rel_dir.startswith(d + "/") for d in excl)

    @staticmethod
    def _is_excluded_rel(rel, excl):
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        return any(parent == d or parent.startswith(d + "/") for d in excl)

    def _walk(self, root_abs, excl, cancel, errors):
        """单次遍历：收集文件 + 记录 seen_dirs（尝试枚举）与 listed_dirs（枚举成功）。

        两者的差集是「不判缺失」的依据：枚举失败的目录里到底有没有文件，本次 walk 无权知道。
        """
        disk = {}
        seen_dirs = set()
        listed_dirs = set()
        unknown = set()
        stack = [root_abs]
        while stack:
            if cancel and cancel():
                return None
            dirpath = stack.pop()
            rel_dir = _rel_posix(dirpath, root_abs)
            seen_dirs.add(rel_dir)
            try:
                with contextlib.closing(os.scandir(dirpath)) as it:
                    entries = list(it)
            except OSError as exc:
                errors.add("无法读取目录：%s（%s）" % (dirpath, getattr(exc, "strerror", "") or exc))
                continue
            listed_dirs.add(rel_dir)
            for dentry in entries:
                if cancel and cancel():
                    return None                  # 文件边界退出（§4.4）
                name = dentry.name
                try:
                    is_dir = dentry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                child_rel = "%s/%s" % (rel_dir, name) if rel_dir else name
                if is_dir:
                    # 不跟随符号链接/junction 目录：软链指向库外时会让 relpath 与根失去对应关系
                    if not self._skip_dir(name, child_rel, excl):
                        stack.append(dentry.path)
                    continue
                ext = _ext_of(name)
                if ext not in _EXT_KIND:
                    continue
                try:
                    st = dentry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue                     # 枚举后消失 = 确实不存在，不列入 unknown
                except OSError:
                    unknown.add(child_rel)       # 读不到状态就不下「缺失」结论
                    continue
                disk[child_rel] = (os.path.abspath(dentry.path), ext, st.st_size, st.st_mtime)
        return {"disk": disk, "seen_dirs": seen_dirs, "listed_dirs": listed_dirs, "unknown": unknown}

    @staticmethod
    def _row_is_absent(rel, disk_files, listed_dirs, seen_dirs, excl, unknown=frozenset()):
        """本次 walk 是否「有资格」判定该行的文件已消失（抄 Sidebar-Gallery 的 _row_is_absent 语义）。

        只有当根到该文件所在目录的每一级都被成功枚举（或某级明确不在已枚举目录里）时才可判缺失；
        枚举失败（OSError）的目录一律保持原状。
        """
        if MediaLibrary._is_excluded_rel(rel, excl):
            return True                          # 目录如今被排除 = 不再属于库
        if rel in unknown or any(rel.startswith(u + "/") for u in unknown):
            return False
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        prefix = ""
        for seg in (parent.split("/") if parent else []):
            nxt = "%s/%s" % (prefix, seg) if prefix else seg
            if nxt not in seen_dirs:
                return prefix in listed_dirs     # 上级被成功列过却没出现这一级 → 目录被删了
            prefix = nxt
        if parent in listed_dirs:
            return rel not in disk_files
        return False

    def _reconcile(self, root_abs, walk, excl, progress, cancel, result, errors):
        """把本次 walk 结果与库对账：新增/重解析/仅复位 missing_since/判缺失。"""
        rid = _root_id(root_abs)
        disk = walk["disk"]
        with self._db() as conn:
            known = {r["relpath"]: (r["size"], r["mtime"], r["meta_mtime"], r["missing_since"])
                     for r in conn.execute("SELECT relpath, size, mtime, meta_mtime, missing_since "
                                           "FROM items WHERE root_id = ?", (rid,))}
        now = time.time()
        plan = []
        for rel, (abspath, ext, size, mtime) in disk.items():
            info = known.get(rel)
            if info is None:
                plan.append(("add", rel, abspath, ext, size, mtime))
                continue
            row_size, row_mtime, row_meta_mtime, row_missing = info
            # 对账三条件任一成立即重解析：mtime/size 变了，或解析时刻追不上文件时间（§3.2 meta_mtime）
            stale = (int(size) != int(row_size) or abs(float(mtime) - float(row_mtime)) > 1e-6
                     or float(mtime) > float(row_meta_mtime))
            if stale:
                plan.append(("reparse", rel, abspath, ext, size, mtime))
            elif row_missing is not None:
                plan.append(("unhide", rel, abspath, ext, size, mtime))
            else:
                plan.append(("keep", rel, abspath, ext, size, mtime))

        total = len(plan)
        inserts = []
        updates = []
        unhides = []
        kept = 0
        stopped = False
        for index, item in enumerate(plan):
            if cancel and cancel():
                stopped = True
                break                            # 文件边界退出：已解析的部分照常入库
            if index % 20 == 0:
                self._progress(progress, "parse", index, total, "解析元数据 %d/%d" % (index, total))
            op, rel, abspath, ext, size, mtime = item
            if op == "keep":
                kept += 1
                continue
            if op == "unhide":
                # 重新出现只复位 missing_since，元数据原样保留，绝不重解析
                unhides.append(rel)
                continue
            try:
                meta = _extract(abspath, ext)
            except Exception as exc:              # _extract 自带兜底，这里只拦 monkeypatch 出来的怪值
                meta = {}
                errors.add("元数据解析异常：%s（%s）" % (rel, exc))
            if not isinstance(meta, dict):
                errors.add("元数据解析异常：%s（返回类型不是 dict）" % rel)
                meta = {}
            fields = self._meta_fields(rid, abspath, rel, ext, size, mtime, meta)
            (inserts if op == "add" else updates).append(fields)

        with self._db() as conn:
            if inserts:
                conn.executemany(_INSERT_ITEM_SQL, inserts)
            if updates:
                conn.executemany(_UPDATE_ITEM_SQL, updates)
            if unhides:
                conn.executemany("UPDATE items SET missing_since = NULL WHERE root_id = ? AND relpath = ?",
                                 [(rid, rel) for rel in unhides])
            result["added"] += len(inserts)
            # unhide 计入 updated：行确实被改写（missing_since 复位），但没有重解析
            result["updated"] += len(updates) + len(unhides)
            result["unchanged"] += kept

            disk_set = set(disk)
            marked = []
            for row in conn.execute("SELECT relpath, missing_since FROM items WHERE root_id = ?", (rid,)):
                rel = row["relpath"]
                if rel in disk_set:
                    continue                     # 在场文件已在上面处理过（含重新出现）
                if row["missing_since"] is not None:
                    continue                     # 已隐藏的保持原时间戳，purge 计时不被刷新
                if self._row_is_absent(rel, disk_set, walk["listed_dirs"], walk["seen_dirs"], excl,
                                       walk["unknown"]):
                    marked.append(rel)
            conn.executemany("UPDATE items SET missing_since = ? WHERE root_id = ? AND relpath = ?",
                             [(now, rid, rel) for rel in marked])
            result["missing"] += len(marked)
        self._progress(progress, "reconcile", total, total, "对账完成")
        # 返回值 = 是否在解析阶段被取消；缺失判定不受影响（disk 视图来自完整 walk，本就可靠）
        return stopped

    def _meta_fields(self, rid, abspath, rel, ext, size, mtime, meta):
        """meta dict → items 行字典（键即列名，见 _INSERT_ITEM_SQL）。"""
        now = time.time()
        filename = rel.rsplit("/", 1)[-1] if "/" in rel else rel
        dir_path = rel.rsplit("/", 1)[0] if "/" in rel else ""
        kind = _EXT_KIND.get(ext, "image")
        loras = [str(x) for x in _as_list(meta.get("loras"))]
        models = [str(x) for x in _as_list(meta.get("models"))]
        prompt = str(meta.get("prompt") or "")
        negative = str(meta.get("negative") or "")
        checkpoint = str(meta.get("checkpoint") or "")
        search_text = " ".join([x for x in (filename, checkpoint, " ".join(loras), " ".join(models),
                                            prompt, negative) if x])
        extras = {}
        for key in ("seed", "steps", "cfg", "sampler", "scheduler", "fps", "note"):
            # 列（§15.2.1）之外仍留一份 extra_json 副本：列装不下的超大 seed 靠它回落展示
            extras[key] = meta.get(key)
        raw_extra = meta.get("extra")
        extras["extra"] = raw_extra if isinstance(raw_extra, dict) else {}
        # meta_mtime 取「解析时刻」，但不得小于 mtime，否则未来时间戳的文件每轮都被判定需要重解析
        meta_mtime = max(now, float(mtime))
        return {
            "path": abspath, "root_id": rid, "relpath": rel, "dir_path": dir_path, "filename": filename,
            "ext": ext, "kind": kind, "size": int(size), "mtime": float(mtime), "meta_mtime": meta_mtime,
            "added_at": now, "missing_since": None, "checkpoint": checkpoint,
            "loras_json": json.dumps(loras, ensure_ascii=False),
            "models_json": json.dumps(models, ensure_ascii=False),
            "width": self._maybe_int(meta.get("width")), "height": self._maybe_int(meta.get("height")),
            "duration": self._maybe_float(meta.get("duration")),
            "seed": self._db_int(meta.get("seed")), "steps": self._db_int(meta.get("steps")),
            "cfg": self._db_float(meta.get("cfg")), "sampler": str(meta.get("sampler") or ""),
            "scheduler": str(meta.get("scheduler") or ""), "fps": self._db_float(meta.get("fps")),
            "prompt": prompt, "negative": negative,
            "search_text": search_text, "has_meta": 1 if meta.get("has_meta") else 0,
            "meta_source": str(meta.get("source") or ""), "starred": 0,
            "has_poster": 1 if os.path.exists(poster_path(abspath)) else 0,
            "extra_json": json.dumps(extras, ensure_ascii=False),
        }

    @staticmethod
    def _maybe_int(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _maybe_float(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _db_int(value):
        """列绑定用的整数：sqlite INTEGER 是有符号 64 位，而 media_meta 契约允许 seed 到 2**64。

        越界值绑定时会抛 OverflowError 并把整批 executemany 一起干掉，所以这里提前落成 NULL；
        extra_json 里有原值，detail() 的回落照样把它展示出来。
        """
        number = MediaLibrary._maybe_int(value)
        if number is None or not (_INT64_MIN <= number <= _INT64_MAX):
            return None
        return number

    @staticmethod
    def _db_float(value):
        """列绑定用的浮点：inf/nan 不是 SQL 里的 REAL（会写坏聚合），一并落成 NULL。"""
        number = MediaLibrary._maybe_float(value)
        if number is None or number != number or number in (float("inf"), float("-inf")):
            return None
        return number

    @staticmethod
    def _progress(progress, phase, done, total, msg):
        if not progress:
            return
        try:
            progress({"phase": phase, "done": int(done), "total": int(total), "msg": msg})
        except Exception:
            pass                                 # 进度回调属于调用方，不得拖垮扫描

    # ------------------------------------------------------------------ 查询
    def list_items(self, flt):
        """按 flt 过滤/排序/分页，返回 {"items": [...], "total": 过滤后总数}。"""
        flt = flt if isinstance(flt, dict) else {}
        limit = self._maybe_int(flt.get("limit")) or PAGE_SIZE_DEFAULT
        limit = PAGE_SIZE_DEFAULT if limit < 1 else min(PAGE_SIZE_MAX, limit)   # §7：默认 200，硬上限 500
        offset = max(0, self._maybe_int(flt.get("offset")) or 0)
        order = SORT_SQL.get(str(flt.get("sort") or SORT_DEFAULT), SORT_SQL[SORT_DEFAULT])
        group_by = str(flt.get("group_by") or "")
        if group_by not in GROUP_KEYS:
            group_by = ""

        where, params = self._build_where(flt)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = "SELECT %s FROM items%s ORDER BY %s LIMIT ? OFFSET ?" % (_ITEM_COLUMNS, clause, order)
        with self._db() as conn:
            try:
                total = conn.execute("SELECT COUNT(*) FROM items" + clause, params).fetchone()[0]
                rows = conn.execute(sql, list(params) + [limit, offset]).fetchall()
            except sqlite3.OperationalError:
                # FTS MATCH 语法/表异常 → 整条查询按 LIKE 重建（降级路径必须走得通）
                self._fts_ok = False
                where, params = self._build_where(flt)
                clause = (" WHERE " + " AND ".join(where)) if where else ""
                sql = "SELECT %s FROM items%s ORDER BY %s LIMIT ? OFFSET ?" % (_ITEM_COLUMNS, clause, order)
                total = conn.execute("SELECT COUNT(*) FROM items" + clause, params).fetchone()[0]
                rows = conn.execute(sql, list(params) + [limit, offset]).fetchall()
            items = self._attach_tags(conn, rows, group_by)
        return {"items": items, "total": int(total)}

    def _build_where(self, flt):
        """所有用户输入只进 bind 参数；集合条件用 ? 占位符拼 IN，跨维度 AND、同维度 OR。"""
        where = ["missing_since IS NULL"]
        params = []
        if flt.get("root_ids") is not None:
            # 移出配置的根：行保留（重新加回目录即恢复，标签/收藏不丢），但不再出现在列表里
            ids = [str(x) for x in _as_list(flt.get("root_ids"))]
            if ids:
                where.append("root_id IN (%s)" % _placeholders(ids))
                params.extend(ids)
            else:
                where.append("1 = 0")
        kind = str(flt.get("kind") or "").strip().lower()
        if kind in ("image", "video"):
            where.append("kind = ?")
            params.append(kind)
        checkpoints = [str(x) for x in _as_list(flt.get("checkpoints"))]
        if checkpoints:
            where.append("checkpoint IN (%s)" % _placeholders(checkpoints))
            params.extend(checkpoints)
        loras = [str(x) for x in _as_list(flt.get("loras"))]
        if loras:
            where.append("(" + " OR ".join(["loras_json LIKE ? ESCAPE '\\'" for _ in loras]) + ")")
            params.extend('%%"%s"%%' % _like_escape(name) for name in loras)
        for key, column in (("samplers", "sampler"), ("schedulers", "scheduler")):
            values = [str(x) for x in _as_list(flt.get(key))]     # §15.2.4：同维度 OR、跨维度 AND
            if values:
                where.append("%s IN (%s)" % (column, _placeholders(values)))
                params.extend(values)
        sizes = []
        for value in _as_list(flt.get("sizes")):
            text = str(value).strip()
            if not _SIZE_RE.fullmatch(text):
                continue                         # 不合 "宽x高" 的值忽略这一维里的这一项，不报错也不放宽匹配
            width, height = (int(part) for part in text.split("x"))
            if width > _INT64_MAX or height > _INT64_MAX:
                continue                         # 纯数字但超出 sqlite INTEGER：同样按非法值忽略，别让绑定抛穿到 RPC
            sizes.append((width, height))
        if sizes:
            where.append("(" + " OR ".join(["(width = ? AND height = ?)"] * len(sizes)) + ")")
            for width, height in sizes:
                params.extend([width, height])
        for key, op in (("duration_min", "duration >= ?"), ("duration_max", "duration <= ?")):
            raw = flt.get(key)
            if raw is None or raw == "":
                continue
            bound = self._maybe_float(raw)
            if bound is None:
                continue                         # 非法时长边界：忽略该维度（§15.2.4）
            where.append(op)
            params.append(bound)
        # dirs 允许空串（''=根目录，§3.2），所以这里不能走 _as_list 的空值过滤
        raw_dirs = flt.get("dirs")
        raw_dirs = list(raw_dirs) if isinstance(raw_dirs, (list, tuple)) else ([] if raw_dirs is None else [raw_dirs])
        dirs = [_to_posix(str(x)).strip("/") for x in raw_dirs if x is not None]
        if dirs:
            where.append("dir_path IN (%s)" % _placeholders(dirs))
            params.extend(dirs)
        tag_ids = _as_int_list(flt.get("tag_ids"))
        if tag_ids:
            where.append("id IN (SELECT item_id FROM item_tags WHERE tag_id IN (%s))" % _placeholders(tag_ids))
            params.extend(tag_ids)
        if flt.get("starred"):
            where.append("starred = 1")
        if flt.get("no_meta"):
            where.append("has_meta = 0")
        q = str(flt.get("q") or "").strip()
        if q:
            tokens = q.split()
            if self._fts_ok:
                where.append("rowid IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
                params.append(" ".join('"%s"*' % t.replace('"', '""') for t in tokens))
            else:
                for token in tokens:
                    where.append("search_text LIKE ? ESCAPE '\\'")
                    params.append("%" + _like_escape(token) + "%")
        return where, params

    def _attach_tags(self, conn, rows, group_by):
        ids = [r["id"] for r in rows]
        tags_by_item = {}
        if ids:
            for r in conn.execute(
                    "SELECT item_tags.item_id AS item_id, tags.id AS id, tags.name AS name, "
                    "tags.color AS color FROM tags JOIN item_tags ON item_tags.tag_id = tags.id "
                    "WHERE item_tags.item_id IN (%s)" % _placeholders(ids), ids):
                tags_by_item.setdefault(r["item_id"], []).append(
                    {"id": r["id"], "name": r["name"], "color": r["color"]})
        return [self._item_dict(r, tags_by_item.get(r["id"], []), group_by) for r in rows]

    def _item_dict(self, row, tags, group_by=""):
        abspath = row["path"]
        item = {
            "id": row["id"],
            "path": abspath,
            "relpath": row["relpath"],
            "dir_path": row["dir_path"],
            "filename": row["filename"],
            "kind": row["kind"],
            "ext": row["ext"],
            "size": row["size"],
            "mtime": row["mtime"],
            "added_at": row["added_at"],
            "checkpoint": row["checkpoint"],
            "loras": [str(x) for x in _json_loads(row["loras_json"], [])],
            "width": row["width"],
            "height": row["height"],
            "duration": row["duration"],
            "seed": row["seed"],
            "steps": row["steps"],
            "cfg": row["cfg"],
            "sampler": row["sampler"],
            "scheduler": row["scheduler"],
            "fps": row["fps"],
            "has_meta": bool(row["has_meta"]),
            "meta_source": row["meta_source"],
            "starred": bool(row["starred"]),
            "missing_since": row["missing_since"],
            "tags": list(tags or []),
            "snippet": (row["prompt"] or "")[:SNIPPET_LEN],
            # 播放 URL 不在这里拼：file:/// 与 /api/media 的选择属于传输层（webui._lib_play_urls），
            # 数据层引 browser_bridge 会让 sqlite 模块依赖 HTTP 桥。
        }
        if group_by:
            item["group"] = self._group_key(group_by, row)
        return item

    @staticmethod
    def _group_key(group_by, row):
        if group_by == "date":
            try:
                return time.strftime("%Y-%m-%d", time.localtime(row["mtime"]))
            except (ValueError, OSError):
                return ""
        if group_by == "checkpoint":
            return row["checkpoint"] or ""
        if group_by == "dir":
            return row["dir_path"] or ""
        return ""

    def facets(self, root_ids=None):
        """分面聚合（§5.3），全部只统计在场文件；facet 下拉上限 300 项。
        root_ids 非 None 时与 list_items 同口径限定在配置的根之内，分面计数才不会算进幽灵行。"""
        args = []
        scope = "missing_since IS NULL"
        if root_ids is not None:
            ids = [str(x) for x in _as_list(root_ids)]
            if ids:
                scope += " AND root_id IN (%s)" % _placeholders(ids)
                args = ids
            else:
                scope += " AND 1 = 0"
        with self._db() as conn:
            checkpoints = [{"name": r["checkpoint"], "count": r["n"]} for r in conn.execute(
                "SELECT checkpoint, COUNT(*) AS n FROM items WHERE " + scope + " AND checkpoint <> '' "
                "GROUP BY checkpoint ORDER BY n DESC, checkpoint ASC LIMIT ?", list(args) + [FACET_LIMIT])]
            loras_map = {}
            for r in conn.execute("SELECT loras_json FROM items WHERE " + scope + " AND loras_json <> '[]'", args):
                for name in _json_loads(r["loras_json"], []):
                    if isinstance(name, str) and name:
                        loras_map[name] = loras_map.get(name, 0) + 1
            loras = [{"name": n, "count": c} for n, c in
                     sorted(loras_map.items(), key=lambda kv: (-kv[1], kv[0]))[:FACET_LIMIT]]
            # §15.2.5：采样器/调度器/尺寸三个新分面与 checkpoints 完全同形（name/count、n DESC、name ASC、300）
            samplers = [{"name": r["sampler"], "count": r["n"]} for r in conn.execute(
                "SELECT sampler, COUNT(*) AS n FROM items WHERE " + scope + " AND sampler <> '' "
                "GROUP BY sampler ORDER BY n DESC, sampler ASC LIMIT ?", list(args) + [FACET_LIMIT])]
            schedulers = [{"name": r["scheduler"], "count": r["n"]} for r in conn.execute(
                "SELECT scheduler, COUNT(*) AS n FROM items WHERE " + scope + " AND scheduler <> '' "
                "GROUP BY scheduler ORDER BY n DESC, scheduler ASC LIMIT ?", list(args) + [FACET_LIMIT])]
            # 尺寸用列拼出 "768x1280"，与 list_items 的 sizes 维度同一套字面量，前端选了就能筛
            sizes = [{"name": r["size_name"], "count": r["n"]} for r in conn.execute(
                "SELECT width || 'x' || height AS size_name, COUNT(*) AS n FROM items WHERE " + scope +
                " AND width IS NOT NULL AND height IS NOT NULL "
                "GROUP BY size_name ORDER BY n DESC, size_name ASC LIMIT ?",
                list(args) + [FACET_LIMIT])]
            dirs = [{"name": r["dir_path"], "count": r["n"], "path": r["dir_path"]} for r in conn.execute(
                "SELECT dir_path, COUNT(*) AS n FROM items WHERE " + scope + " "
                "GROUP BY dir_path ORDER BY n DESC, dir_path ASC LIMIT ?", list(args) + [FACET_LIMIT])]
            # 相关子查询统计「在场文件」的标签数：LEFT JOIN + INNER JOIN 混写会漏掉只有缺失文件的标签
            tags = [{"id": r["id"], "name": r["name"], "color": r["color"], "count": r["count"]} for r in conn.execute(
                "SELECT tags.id AS id, tags.name AS name, tags.color AS color, "
                "(SELECT COUNT(*) FROM item_tags WHERE item_tags.tag_id = tags.id "
                " AND item_tags.item_id IN (SELECT id FROM items WHERE " + scope + ")) AS count "
                "FROM tags ORDER BY count DESC, tags.name ASC LIMIT ?", list(args) + [FACET_LIMIT])]
            kinds = {"image": 0, "video": 0}
            for r in conn.execute("SELECT kind, COUNT(*) AS n FROM items WHERE " + scope + " GROUP BY kind", args):
                kinds[r["kind"]] = r["n"]
            total = conn.execute("SELECT COUNT(*) FROM items WHERE " + scope, args).fetchone()[0]
            starred = conn.execute("SELECT COUNT(*) FROM items WHERE " + scope + " AND starred = 1", args).fetchone()[0]
            no_meta = conn.execute("SELECT COUNT(*) FROM items WHERE " + scope + " AND has_meta = 0", args).fetchone()[0]
            missing = conn.execute("SELECT COUNT(*) FROM items WHERE missing_since IS NOT NULL"
                                   + (" AND root_id IN (%s)" % _placeholders(args) if args else ""), args).fetchone()[0]
        return {"checkpoints": checkpoints, "loras": loras, "dirs": dirs, "tags": tags,
                "samplers": samplers, "schedulers": schedulers, "sizes": sizes,
                "kinds": kinds, "starred": int(starred), "no_meta": int(no_meta),
                "missing": int(missing), "total": int(total)}

    def stats(self):
        with self._db() as conn:
            def count(clause, params=()):
                return int(conn.execute("SELECT COUNT(*) FROM items WHERE " + clause, params).fetchone()[0])
            data = {
                "total": count("missing_since IS NULL"),
                "image": count("missing_since IS NULL AND kind = 'image'"),
                "video": count("missing_since IS NULL AND kind = 'video'"),
                "starred": count("missing_since IS NULL AND starred = 1"),
                "tagged": int(conn.execute("SELECT COUNT(DISTINCT item_tags.item_id) FROM item_tags JOIN items "
                                           "ON items.id = item_tags.item_id "
                                           "WHERE items.missing_since IS NULL").fetchone()[0]),
                "missing": count("missing_since IS NOT NULL"),
                "no_meta": count("missing_since IS NULL AND has_meta = 0"),
                "checkpoints": int(conn.execute(
                    "SELECT COUNT(DISTINCT checkpoint) FROM items "
                    "WHERE missing_since IS NULL AND checkpoint <> ''").fetchone()[0]),
                "tags": int(conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]),
            }
        try:
            data["db_bytes"] = int(os.path.getsize(self.path))
        except OSError:
            data["db_bytes"] = 0
        return data

    def meta_value(self, key):
        """读 meta 表（db_version / last_scan_at / scan_stats / fts）。"""
        with self._db() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
        return row["value"] if row is not None else ""

    def last_scan_at(self):
        """A3 的 lib_rescan_if_stale 需要「距上次扫描多久」，数据只有这里能读。"""
        try:
            return float(self.meta_value("last_scan_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    def get_item(self, item_id):
        with self._db() as conn:
            row = conn.execute("SELECT %s FROM items WHERE id = ?" % _ITEM_COLUMNS,
                               (self._maybe_int(item_id) or 0,)).fetchone()
            if row is None:
                return None
            tags = self._tags_for(conn, [row["id"]]).get(row["id"], [])
            return self._item_dict(row, tags)

    def get_item_by_path(self, path):
        """按绝对路径取行：A3 的 lib_save_poster 需要「库内已登记」这道越权校验（契约 §5.2）。"""
        if not isinstance(path, str) or not path.strip():
            return None
        target = os.path.normcase(os.path.abspath(path))
        with self._db() as conn:
            # 先按 filename 缩小候选（path 列本身没索引，filename 选择度足够）再逐条 normcase 比对
            for row in conn.execute("SELECT %s FROM items WHERE filename = ?" % _ITEM_COLUMNS,
                                    (os.path.basename(target),)):
                if os.path.normcase(row["path"]) == target:
                    tags = self._tags_for(conn, [row["id"]]).get(row["id"], [])
                    return self._item_dict(row, tags)
        return None

    def detail(self, item_id):
        """详情：§5.1 items 行 + negative/models/seed/steps/cfg/sampler/scheduler/note/extra/exists。"""
        with self._db() as conn:
            row = conn.execute("SELECT %s FROM items WHERE id = ?" % _ITEM_COLUMNS,
                               (self._maybe_int(item_id) or 0,)).fetchone()
            if row is None:
                return None
            tags = self._tags_for(conn, [row["id"]]).get(row["id"], [])
            item = self._item_dict(row, tags)
            extras = _json_loads(row["extra_json"], {})
            inner = extras.get("extra")
        item["negative"] = row["negative"] or ""
        item["prompt"] = row["prompt"] or ""
        item["models"] = [str(x) for x in _json_loads(row["models_json"], [])]
        # §15.2.6：参数以列为准，列为空（NULL / 空串）才回落 extra_json ——
        # 迁移前后写的老行、以及 _db_int 装不下的超大 seed 都靠回落保住既有展示口径。
        for key in ("seed", "steps", "cfg", "sampler", "scheduler", "fps"):
            value = row[key]
            if value is None or value == "":
                value = extras.get(key)
            if key in ("cfg", "fps"):
                item[key] = self._maybe_float(value)
            elif key in ("sampler", "scheduler"):
                item[key] = "" if value is None else str(value)
            else:
                item[key] = self._display_int(value)
        item["note"] = extras.get("note")
        item["extra"] = inner if isinstance(inner, dict) else {}
        item["exists"] = os.path.exists(row["path"])
        return item

    @staticmethod
    def _display_int(value):
        """详情里的整数：不受 sqlite 64 位列宽限制（超大 seed 从 extra_json 原样回读），非法值给 None。"""
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _tags_for(conn, ids):
        out = {}
        if not ids:
            return out
        for r in conn.execute(
                "SELECT item_tags.item_id AS item_id, tags.id AS id, tags.name AS name, tags.color AS color "
                "FROM tags JOIN item_tags ON item_tags.tag_id = tags.id "
                "WHERE item_tags.item_id IN (%s) ORDER BY tags.name ASC" % _placeholders(ids), ids):
            out.setdefault(r["item_id"], []).append({"id": r["id"], "name": r["name"], "color": r["color"]})
        return out

    # ------------------------------------------------------------------ 写
    def set_star(self, item_id, on):
        item_id = self._maybe_int(item_id) or 0
        flag = 1 if bool(on) else 0
        with self._db() as conn:
            cur = conn.execute("UPDATE items SET starred = ? WHERE id = ?", (flag, item_id))
            return bool(cur.rowcount) and flag == 1

    def set_tags(self, item_id, tag_ids):
        """整组替换该条目的标签，返回新的 tags 列表（§5.1 tags 形状）。"""
        item_id = self._maybe_int(item_id) or 0
        wanted = sorted(set(_as_int_list(tag_ids)))
        with self._db() as conn:
            if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is None:
                return []
            if wanted:
                # 只保留 wanted 与真实存在的标签：库里查不到的 id 直接忽略，不写脏关联
                keep_ids = self._known_tag_ids(conn, wanted)
                conn.execute("DELETE FROM item_tags WHERE item_id = ? AND tag_id NOT IN (%s)"
                             % _placeholders(keep_ids or [0]), [item_id] + (keep_ids or [0]))
                if keep_ids:
                    conn.executemany("INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES(?, ?)",
                                     [(item_id, tid) for tid in keep_ids])
            else:
                conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
            return self._tags_for(conn, [item_id]).get(item_id, [])

    @staticmethod
    def _known_tag_ids(conn, ids):
        if not ids:
            return []
        return [r["id"] for r in conn.execute(
            "SELECT id FROM tags WHERE id IN (%s)" % _placeholders(ids), ids)]

    def _live_ids(self, conn, item_ids):
        """批量写之前先过滤出库里真的存在的 id：客户端传来的 id 可能是已删条目。"""
        ids = _as_int_list(item_ids)
        if not ids:
            return []
        return [r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE id IN (%s)" % _placeholders(ids), ids)]

    def set_starred(self, item_ids, on):
        """批量收藏：一条 UPDATE 覆盖整批，返回库里真实存在的行数。"""
        with self._db() as conn:
            live = self._live_ids(conn, item_ids)
            if not live:
                return 0
            conn.execute("UPDATE items SET starred = ? WHERE id IN (%s)" % _placeholders(live),
                         [1 if on else 0] + live)
            return len(live)

    def add_tags(self, item_ids, tag_ids):
        """批量加标签（并集，不动条目原有的标签），返回 {item_id: tags}。"""
        return self._apply_tag_batch(item_ids, tag_ids, add=True)

    def remove_tags(self, item_ids, tag_ids):
        """批量摘标签，返回 {item_id: tags}（摘完的条目回空列表）。"""
        return self._apply_tag_batch(item_ids, tag_ids, add=False)

    def _apply_tag_batch(self, item_ids, tag_ids, add):
        wanted = sorted(set(_as_int_list(tag_ids)))
        with self._db() as conn:
            live = self._live_ids(conn, item_ids)
            if not live:
                return {}
            tags = self._known_tag_ids(conn, wanted)     # 库里不存在的 tag_id 直接忽略，不写脏关联
            if tags:
                pairs = [(i, t) for i in live for t in tags]
                if add:
                    conn.executemany("INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES(?, ?)", pairs)
                else:
                    conn.executemany("DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?", pairs)
            return self._tags_for(conn, live)

    def list_tags(self):
        with self._db() as conn:
            return [{"id": r["id"], "name": r["name"], "color": r["color"], "parent_id": r["parent_id"],
                     "count": r["count"]} for r in conn.execute(
                "SELECT tags.id AS id, tags.name AS name, tags.color AS color, tags.parent_id AS parent_id, "
                "(SELECT COUNT(*) FROM item_tags WHERE item_tags.tag_id = tags.id "
                " AND item_tags.item_id IN (SELECT id FROM items WHERE missing_since IS NULL)) AS count "
                "FROM tags ORDER BY tags.name ASC")]

    def save_tag(self, tag_id, name, color, parent_id):
        """tag_id=0 新建；重名视为同一标签（返回既有 id，避免 UNIQUE 抛给 RPC 层）。"""
        name = str(name or "").strip()
        if not name:
            return int(tag_id or 0) or 0
        color = str(color or "").strip()
        parent = self._maybe_int(parent_id)
        if parent is None or parent <= 0:
            parent = None
        tag_id = self._maybe_int(tag_id) or 0
        with self._db() as conn:
            clash = conn.execute("SELECT id FROM tags WHERE name = ? AND id <> ?", (name, tag_id)).fetchone()
            if tag_id == 0:
                if clash is not None:
                    return clash["id"]             # 重名视为同一标签：不报错、不建重复项
                cur = conn.execute("INSERT INTO tags(name, color, parent_id) VALUES(?,?,?)", (name, color, parent))
                return int(cur.lastrowid)
            if parent is not None and (parent == tag_id or self._is_descendant(conn, parent, tag_id)):
                parent = None                      # 拒绝把标签挂到自己或自己的子孙下（环会让树渲染死循环）
            if clash is not None:
                # 改名撞上已有标签：先 UPDATE 会撞 UNIQUE，所以直接合并到既有标签
                self._merge_tag(conn, tag_id, clash["id"])
                return clash["id"]
            conn.execute("UPDATE tags SET name = ?, color = ?, parent_id = ? WHERE id = ?",
                         (name, color, parent, tag_id))
            return tag_id

    @staticmethod
    def _is_descendant(conn, candidate, tag_id):
        if not tag_id:
            return False
        seen = set()
        node = candidate
        while node is not None and node not in seen:
            if node == tag_id:
                return True
            seen.add(node)
            row = conn.execute("SELECT parent_id FROM tags WHERE id = ?", (node,)).fetchone()
            node = row["parent_id"] if row is not None else None
        return False

    def _merge_tag(self, conn, from_id, to_id):
        conn.execute("UPDATE OR IGNORE item_tags SET tag_id = ? WHERE tag_id = ?", (to_id, from_id))
        conn.execute("DELETE FROM item_tags WHERE tag_id = ?", (from_id,))
        conn.execute("UPDATE tags SET parent_id = ? WHERE parent_id = ?", (to_id, from_id))
        conn.execute("DELETE FROM tags WHERE id = ?", (from_id,))

    def delete_tag(self, tag_id):
        """删标签并级联清理 item_tags（item_tags 未声明 FK，级联必须显式做）。"""
        tag_id = self._maybe_int(tag_id) or 0
        with self._db() as conn:
            cur = conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            conn.execute("DELETE FROM item_tags WHERE tag_id = ?", (tag_id,))
            # 子标签上提到被删标签的父级，避免整棵子树变成孤儿
            conn.execute("UPDATE tags SET parent_id = NULL WHERE parent_id = ?", (tag_id,))
            return bool(cur.rowcount)

    def export_annotations(self):
        """导出收藏与标签（§18）：只导「人工做过的事」，不导机器解析出来的元数据。

        身份键用 relpath + size 而不是绝对路径：库目录改名/换盘符后导出的档案仍然认得回来
        （与 §17 搬家重连同一条思路）。标签带父级名字路径，导入时按名重建层级。
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, relpath, filename, ext, size, starred FROM items "
                "WHERE starred = 1 OR id IN (SELECT item_id FROM item_tags) ORDER BY relpath").fetchall()
            tag_rows = conn.execute(
                "SELECT id, name, color, parent_id FROM tags ORDER BY name").fetchall()
            by_id = {r["id"]: r for r in tag_rows}
            tags = [{"name": r["name"], "color": r["color"] or "",
                     "parent_path": self._tag_path(by_id, r["parent_id"])} for r in tag_rows]
            items = []
            for r in rows:
                names = [t["name"] for t in conn.execute(
                    "SELECT tags.name AS name FROM tags JOIN item_tags ON item_tags.tag_id = tags.id "
                    "WHERE item_tags.item_id = ? ORDER BY tags.name", (r["id"],))]
                items.append({"relpath": r["relpath"], "filename": r["filename"], "ext": r["ext"],
                              "size": int(r["size"]), "starred": int(r["starred"] or 0), "tags": names})
        return {"kind": "cft-media-annotations", "version": 1, "exported_at": time.time(),
                "tags": tags, "items": items}

    @staticmethod
    def _tag_path(by_id, parent_id, depth=0):
        """祖先名字列表（根在前）。用列表而不是 "父/子" 字符串：标签名本身可以含 "/"，
        拼起来再拆会把一层拆成两层（实测踩过）。层级成环（脏数据）按深度截断。"""
        parts = []
        while parent_id and depth < 16:
            parent = by_id.get(int(parent_id))
            if parent is None:
                break
            parts.insert(0, parent["name"])
            parent_id = parent["parent_id"]
            depth += 1
        return parts

    def import_annotations(self, payload, dry_run=False):
        """把导出的档案写回当前库：按 relpath(+size) 认条目，按名建/取标签。

        只补不加——导入绝不取消库里已有的收藏或标签（那是 §16 批量层的职责）；
        认不出的条目计入 unmatched 回给前端，绝不静默丢弃。
        """
        stats = {"matched": 0, "starred": 0, "tags_applied": 0, "tags_created": 0,
                 "unmatched": 0, "ambiguous": 0, "dry_run": bool(dry_run)}
        if not isinstance(payload, dict) or payload.get("kind") != "cft-media-annotations":
            stats["msg"] = "文件不是资源库标注导出"
            return stats
        items = payload.get("items")
        if not isinstance(items, list):
            stats["msg"] = "导出内容缺少条目列表"
            return stats
        with self._db() as conn:
            tag_cache = {}
            existing = {r["name"]: r for r in conn.execute("SELECT id, name, color FROM tags")}
            path_cache = {}
            for r in conn.execute("SELECT relpath, size, id FROM items"):
                path_cache.setdefault((r["relpath"], int(r["size"])), []).append(r["id"])
            rel_cache = {}
            for (relpath, _size), ids in list(path_cache.items()):
                rel_cache.setdefault(relpath, []).extend(ids)

            def resolve(item):
                rel = str(item.get("relpath") or "")
                try:
                    size = int(item.get("size") or 0)
                except (TypeError, ValueError):
                    return None, "unmatched"
                hits = path_cache.get((rel, size)) or []
                if len(hits) == 1:
                    return hits[0], ""
                if len(hits) > 1:
                    return None, "ambiguous"
                fallback = rel_cache.get(rel) or []          # 文件改过大小：relpath 唯一命中才算
                if len(fallback) == 1:
                    return fallback[0], ""
                if len(fallback) > 1:
                    return None, "ambiguous"
                return None, "unmatched"

            def tag_id(name, color, parent_path):
                name = str(name or "").strip()
                if not name:
                    return 0
                if name in tag_cache:
                    return tag_cache[name]
                row = existing.get(name)
                if row is None:
                    # 先递归建/计祖先，再看是否试算：两条路径的计数口径必须一致，
                    # 否则「试算说 1 个、真写 2 个」会让用户以为预览在骗他。
                    parent = 0
                    for seg in (parent_path or []):
                        pid = tag_id(seg, "", [])
                        if pid:
                            parent = pid
                    if dry_run:                              # 试算：给个伪 id，让计数与真写完全一致
                        pseudo = -len(tag_cache) - 1
                        tag_cache[name] = pseudo
                        existing[name] = {"id": pseudo, "name": name, "color": color or ""}
                        stats["tags_created"] += 1
                        return pseudo
                    conn.execute("INSERT OR IGNORE INTO tags(name, color, parent_id) VALUES(?,?,?)",
                                 (name, color or "", parent))
                    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
                    tid = int(row["id"]) if row else 0
                    existing[name] = {"id": tid, "name": name, "color": color or ""}
                    stats["tags_created"] += 1
                    tag_cache[name] = tid
                    return tid
                tid = int(row["id"])
                tag_cache[name] = tid
                return tid

            exported_tags = {}
            for t in (payload.get("tags") or []):
                if isinstance(t, dict) and t.get("name"):
                    exported_tags[str(t["name"])] = (str(t.get("color") or ""),
                                                     [str(s) for s in (t.get("parent_path") or []) if s])

            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id, why = resolve(item)
                if item_id is None:
                    stats[why if why in ("ambiguous", "unmatched") else "unmatched"] += 1
                    continue
                stats["matched"] += 1
                names = item.get("tags")
                if item.get("starred"):
                    if not dry_run:
                        conn.execute("UPDATE items SET starred = 1 WHERE id = ?", (item_id,))
                    stats["starred"] += 1
                if isinstance(names, list):
                    for name in names:
                        color, parent = exported_tags.get(str(name), ("", ""))
                        tid = tag_id(name, color, parent)
                        if tid == 0:
                            continue                           # 只有空标签名才不计数（试算用负伪 id）
                        if not dry_run:
                            conn.execute("INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES(?,?)",
                                         (item_id, tid))
                        stats["tags_applied"] += 1
        return stats

    def relink_moved_roots(self, root_ids):
        """根目录改名/换盘符后重新加进配置时，把旧行的收藏与标签**复制**到全等的新行上。

        判据（契约 §17）：旧行所属根已不在本次扫描的根里；旧路径的文件在磁盘上已经不存在；
        在管的某个根里有 relpath+size+ext 全等的在场行。两个方向的一对多都跳过——只按 relpath
        猜会把两个真库的同名文件互相接走。**只复制不删旧行**：移出配置的根是故意留着的
        （§13.14：重新加回即恢复），删掉就打破了这条承诺。海报不跟随（按 abspath 散列），
        新行首次进视口时重新抓帧。
        """
        ids = [str(x) for x in _as_list(root_ids)]
        if not ids:
            return 0
        sql = _RELINK_CANDIDATES_SQL % (_placeholders(ids), _placeholders(ids))
        moved = 0
        with self._db() as conn:
            old_to_new = {}
            new_to_olds = {}
            for r in conn.execute(sql, list(ids) + list(ids)):
                old_to_new.setdefault(r["old_id"], set()).add(r["new_id"])
                new_to_olds.setdefault(r["new_id"], set()).add(r["old_id"])
            for old_id, new_ids in old_to_new.items():
                if len(new_ids) != 1:
                    continue                     # 一个旧行对上多个新行：歧义，不猜
                new_id = next(iter(new_ids))
                if len(new_to_olds[new_id]) != 1:
                    continue                     # 多个旧行争同一个新行：同样不猜
                old = conn.execute("SELECT starred, path FROM items WHERE id = ?", (old_id,)).fetchone()
                if old is None or os.path.exists(str(old["path"])):
                    continue                     # 旧文件还在原地：这是两套库，不是搬家
                conn.execute("INSERT OR IGNORE INTO item_tags(item_id, tag_id)"
                             " SELECT ?, tag_id FROM item_tags WHERE item_id = ?", (new_id, old_id))
                if old["starred"]:
                    conn.execute("UPDATE items SET starred = 1 WHERE id = ?", (new_id,))
                moved += 1
            return moved

    def mark_gone(self, item_id):
        item_id = self._maybe_int(item_id) or 0
        with self._db() as conn:
            cur = conn.execute("UPDATE items SET missing_since = ? WHERE id = ? AND missing_since IS NULL",
                               (time.time(), item_id))
            return bool(cur.rowcount)

    def purge_missing(self, older_than_days=MISSING_PURGE_DAYS):
        try:
            days = float(older_than_days)
        except (TypeError, ValueError):
            days = float(MISSING_PURGE_DAYS)
        cutoff = time.time() - max(0.0, days) * 86400.0
        with self._db() as conn:
            cur = conn.execute("DELETE FROM items WHERE missing_since IS NOT NULL AND missing_since < ?", (cutoff,))
            return int(cur.rowcount)

    def set_poster(self, path, data=None):
        """写视频海报并同步 has_poster（契约 §3.1 未列此方法，A3 的 lib_save_poster 需要落盘入口）。
        先探登记再落盘：未入库的路径不得在海报目录里造文件（海报目录按 abspath 散列，写进去就是持久占用）。"""
        abspath = os.path.abspath(path)
        target = poster_path(abspath)
        with self._db() as conn:
            registered = conn.execute("SELECT 1 FROM items WHERE path = ?", (abspath,)).fetchone()
            if not registered:
                return {"ok": False, "path": "", "msg": "路径不在库内"}
            if data:
                try:
                    payload = bytes(data)
                except (TypeError, ValueError):
                    return {"ok": False, "path": "", "msg": "海报数据无效"}
                if len(payload) > POSTER_BYTES_LIMIT:
                    return {"ok": False, "path": "", "msg": "海报超过 2MB 上限"}
                os.makedirs(os.path.dirname(target), exist_ok=True)
                tmp = target + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(payload)
                os.replace(tmp, target)              # 先写临时再改名：避免并发读到半张海报
            cur = conn.execute("UPDATE items SET has_poster = ? WHERE path = ?",
                               (1 if os.path.exists(target) else 0, abspath))
        return {"ok": bool(cur.rowcount), "path": target,
                "msg": "已保存海报" if cur.rowcount else "路径不在库内"}
