# -*- coding: utf-8 -*-
"""资源库索引层：sqlite 索引 + 增量扫描对账 + 分面/分组/搜索/标签查询。

契约出处：docs/media-library-contract.md §3（表结构与类 API）、§4（扫描管线）、§5.1/§5.3（返回形状）。
本文件只管「数据」：缩略图生成、RPC 封装在 webui.py，元数据解析在 media_meta.py。
"""
import contextlib
import hashlib
import json
import os
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
    """CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL DEFAULT '', parent_id INTEGER)""",
    "CREATE TABLE IF NOT EXISTS item_tags(item_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY(item_id, tag_id))",
    "CREATE INDEX IF NOT EXISTS ix_item_tags_tag ON item_tags(tag_id)",
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",
)

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
                 "missing_since, checkpoint, loras_json, models_json, width, height, duration, prompt, "
                 "negative, search_text, has_meta, meta_source, starred, has_poster, extra_json")

# 命名列参数：位置参数在下面这种列数下极易错位，字典键即列名
_INSERT_ITEM_SQL = """INSERT OR REPLACE INTO items(
  path, root_id, relpath, dir_path, filename, ext, kind, size, mtime, meta_mtime, added_at, missing_since,
  checkpoint, loras_json, models_json, width, height, duration, prompt, negative, search_text, has_meta,
  meta_source, starred, has_poster, extra_json)
VALUES(:path, :root_id, :relpath, :dir_path, :filename, :ext, :kind, :size, :mtime, :meta_mtime, :added_at,
  :missing_since, :checkpoint, :loras_json, :models_json, :width, :height, :duration, :prompt, :negative,
  :search_text, :has_meta, :meta_source, :starred, :has_poster, :extra_json)"""

# 重解析不得动 added_at/starred：用户收藏与「加入时间」与文件内容无关
_UPDATE_ITEM_SQL = """UPDATE items SET path=:path, size=:size, mtime=:mtime, meta_mtime=:meta_mtime,
  missing_since=NULL, checkpoint=:checkpoint, loras_json=:loras_json, models_json=:models_json,
  width=:width, height=:height, duration=:duration, prompt=:prompt, negative=:negative,
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
        conn.executescript("PRAGMA user_version = 1;\n" + ";\n".join(_SCHEMA_SQL) + ";")
        global FTS_AVAILABLE
        try:
            conn.execute(_FTS_TABLE_SQL)
            for sql in _FTS_TRIGGERS_SQL:
                conn.execute(sql)
            self._fts_ok = True
            FTS_AVAILABLE = True
        except sqlite3.OperationalError:
            # 老 sqlite / 精简构建没编 FTS5：搜索整体降级为 LIKE，其余功能不受影响
            self._fts_ok = False
            FTS_AVAILABLE = False
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('db_version', '1')")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('fts', ?)", ("1" if self._fts_ok else "0",))
        self._ready = True

    # ------------------------------------------------------------------ 扫描
    def scan(self, roots, exclude=(), progress=None, cancel=None):
        """增量扫描 + 对账（§4）。roots=绝对目录列表，exclude=相对根的 posix 目录。"""
        root_list = [_norm_root(r) for r in _as_list(roots)]
        root_list, skipped_roots = self._drop_nested_roots(root_list)
        excl = self._norm_exclude(exclude)
        result = {"roots": [], "skipped_roots": skipped_roots, "total": 0,
                  "added": 0, "updated": 0, "unchanged": 0, "missing": 0, "errors": []}
        errors = _ErrorSink(result["errors"])
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
        for key in ("seed", "steps", "cfg", "sampler", "scheduler", "note"):
            # items 表没有这些标量列（§3.2 平列只到尺寸/时长），随 extra_json 落库供 detail 回读
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
            "duration": self._maybe_float(meta.get("duration")), "prompt": prompt, "negative": negative,
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

    def facets(self):
        """分面聚合（§5.3），全部只统计在场文件；facet 下拉上限 300 项。"""
        with self._db() as conn:
            checkpoints = [{"name": r["checkpoint"], "count": r["n"]} for r in conn.execute(
                "SELECT checkpoint, COUNT(*) AS n FROM items WHERE missing_since IS NULL AND checkpoint <> '' "
                "GROUP BY checkpoint ORDER BY n DESC, checkpoint ASC LIMIT ?", (FACET_LIMIT,))]
            loras_map = {}
            for r in conn.execute("SELECT loras_json FROM items WHERE missing_since IS NULL AND loras_json <> '[]'"):
                for name in _json_loads(r["loras_json"], []):
                    if isinstance(name, str) and name:
                        loras_map[name] = loras_map.get(name, 0) + 1
            loras = [{"name": n, "count": c} for n, c in
                     sorted(loras_map.items(), key=lambda kv: (-kv[1], kv[0]))[:FACET_LIMIT]]
            dirs = [{"name": r["dir_path"], "count": r["n"], "path": r["dir_path"]} for r in conn.execute(
                "SELECT dir_path, COUNT(*) AS n FROM items WHERE missing_since IS NULL "
                "GROUP BY dir_path ORDER BY n DESC, dir_path ASC LIMIT ?", (FACET_LIMIT,))]
            # 相关子查询统计「在场文件」的标签数：LEFT JOIN + INNER JOIN 混写会漏掉只有缺失文件的标签
            tags = [{"id": r["id"], "name": r["name"], "color": r["color"], "count": r["count"]} for r in conn.execute(
                "SELECT tags.id AS id, tags.name AS name, tags.color AS color, "
                "(SELECT COUNT(*) FROM item_tags WHERE item_tags.tag_id = tags.id "
                " AND item_tags.item_id IN (SELECT id FROM items WHERE missing_since IS NULL)) AS count "
                "FROM tags ORDER BY count DESC, tags.name ASC LIMIT ?", (FACET_LIMIT,))]
            kinds = {"image": 0, "video": 0}
            for r in conn.execute("SELECT kind, COUNT(*) AS n FROM items WHERE missing_since IS NULL GROUP BY kind"):
                kinds[r["kind"]] = r["n"]
            total = conn.execute("SELECT COUNT(*) FROM items WHERE missing_since IS NULL").fetchone()[0]
            starred = conn.execute("SELECT COUNT(*) FROM items WHERE missing_since IS NULL AND starred = 1").fetchone()[0]
            no_meta = conn.execute("SELECT COUNT(*) FROM items WHERE missing_since IS NULL AND has_meta = 0").fetchone()[0]
            missing = conn.execute("SELECT COUNT(*) FROM items WHERE missing_since IS NOT NULL").fetchone()[0]
        return {"checkpoints": checkpoints, "loras": loras, "dirs": dirs, "tags": tags,
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
        for key in ("seed", "steps", "cfg", "sampler", "scheduler", "note"):
            item[key] = extras.get(key)
        item["seed"] = self._maybe_int(item["seed"])
        item["steps"] = self._maybe_int(item["steps"])
        item["cfg"] = self._maybe_float(item["cfg"])
        item["extra"] = inner if isinstance(inner, dict) else {}
        item["exists"] = os.path.exists(row["path"])
        return item

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
