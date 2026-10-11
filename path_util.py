# -*- coding: utf-8 -*-
"""局域网路径工具：目录根规范化 / 范围判定 / 网络路径 / 可达性诊断。

供 webui 复用（扫描根状态、归档逃逸校验、设置页连接测试）。
纯标准库，无第三方依赖；所有函数永不抛异常（check_reachable）或仅对非法输入返回安全默认值。
"""
import ctypes
import os
import shutil
import tempfile

try:
    import ntpath
except Exception:  # pragma: no cover - 标准库恒可用
    ntpath = None

# 目录不存在/不可达时的统一提示（webui 与测试共用文案）
NOT_FOUND_ERROR = "目录不存在或不可达（可能未授权/主机离线/共享名错误）"
NOT_WRITABLE_ERROR = "目录不可写"


def _is_win_style(s):
    """是否像 Windows 路径（含反斜杠/盘符/UNC），用于跨平台规范化。"""
    if not s:
        return False
    if "\\" in s:
        return True
    if len(s) >= 2 and s[1] == ":":
        return True
    if s.startswith("//"):
        return True
    return False


def norm_root(p):
    """目录根规范化：UNC/盘符根安全去尾。

    禁止用 rstrip('/\\')——会把 'Z:\\' 削成 'Z:'（盘符相对路径语义错误）。
    实现：normpath 后，若结果是盘符根 'Z:' 则补回 'Z:\\'；
    UNC 根 '\\\\host\\share' 保持；空/None 返回 ''。
    """
    if p is None:
        return ""
    if not isinstance(p, str):
        try:
            p = str(p)
        except Exception:
            return ""
    s = p.strip()
    if not s:
        return ""
    try:
        if ntpath is not None and (_is_win_style(s) or os.name == "nt"):
            norm = ntpath.normpath(s)
        else:
            norm = os.path.normpath(s)
    except Exception:
        return s
    # 盘符根 'Z:' 补回分隔符（Windows 语义下 'Z:' 是盘符相对路径，不能当根用）
    if len(norm) == 2 and norm[1] == ":" and norm[0].isalpha():
        return norm + "\\"
    # 非根尾部分隔符去掉（UNC 根 '\\\\host\\share' 与盘符根已在上面保留）
    if len(norm) > 1 and norm.endswith("\\") and norm != "\\":
        # UNC 根形如 '\\\\host\\share' 不带尾斜杠：带尾的是冗余，去掉
        norm = norm.rstrip("\\")
        if not norm:
            return s
        # 盘符 'Z:\\...' 被 rstrip 后若剩 'Z:' 仍需补回（理论只在保尾场景，不补会变语义）
        if len(norm) == 2 and norm[1] == ":" and norm[0].isalpha():
            return norm + "\\"
    if os.name != "nt" and len(norm) > 1 and norm.endswith("/") and norm != "/":
        norm = norm.rstrip("/")
        if not norm:
            return s
    return norm


def _canon(p):
    """is_within 内部归一：norm_root + 统一分隔符 + 小写（跨平台大小写不敏感）。"""
    n = norm_root(p)
    if not n:
        return ""
    # 先 normcase（Windows 下会把 '/' 转回 '\\'），再统一为 '/' 比对，
    # 保证跨平台（Linux 下 Windows 路径）判定一致
    try:
        n = os.path.normcase(n)
    except Exception:
        pass
    n = n.replace("\\", "/").lower()
    # 去尾（根保留）：'/'、'z:/'、'//host/share' 不削
    if n in ("/",):
        return n
    if len(n) == 3 and n[1] == ":" and n.endswith("/"):
        return n
    if n.startswith("//") and n.count("/") == 2:
        return n
    return n.rstrip("/") or n


def is_within(root, child):
    """child 是否在 root 内（含相等）。normcase 归一大小写 + 前缀比对。

    返回 False 的边界：root 或 child 空（False）；相等视为 True。
    """
    if not root or not child:
        return False
    if not isinstance(root, str) or not isinstance(child, str):
        return False
    if not root.strip() or not child.strip():
        return False
    try:
        r = _canon(root)
        c = _canon(child)
    except Exception:
        return False
    if not r or not c:
        return False
    if c == r:
        return True
    # 根以分隔符结尾（'/'、'z:/'、UNC 根）时直接前缀比对
    if r.endswith("/"):
        return c.startswith(r)
    return c.startswith(r + "/")


def is_remote_path(p):
    """网络路径判定：UNC（'\\\\' 前缀）为 True。

    盘符用 ctypes GetDriveTypeW（DRIVE_REMOTE=4）判定；非 Windows 恒 False，失败回退 False。
    """
    if not p or not isinstance(p, str):
        return False
    s = p.strip()
    if not s:
        return False
    # UNC（反斜杠/正斜杠两种写法都算网络路径）
    if s.startswith("\\\\") or s.startswith("//"):
        return True
    # 盘符判定仅 Windows
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        if os.name != "nt":
            return False
        try:
            k = ctypes.windll.kernel32
            k.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
            k.GetDriveTypeW.restype = ctypes.c_uint
            root = s[0].upper() + ":\\"
            return k.GetDriveTypeW(root) == 4  # DRIVE_REMOTE
        except Exception:
            return False
    return False


def check_reachable(path):
    """可达性诊断，永不抛异常。

    返回 {"ok", "exists", "writable", "is_remote", "error"}：
    - exists: os.path.isdir(path)
    - writable: exists 时用 tempfile.mkdtemp(dir=path) 建后删（失败 False）；不存在时 False
    - error: 不存在 → NOT_FOUND_ERROR；存在但不可写 → NOT_WRITABLE_ERROR；否则 ''
    - ok: exists 即 True（读管理场景 exists 即可用，writable 单列供写场景参考）
    """
    result = {"ok": False, "exists": False, "writable": False,
              "is_remote": False, "error": ""}
    try:
        s = path if isinstance(path, str) else (str(path) if path is not None else "")
        if isinstance(s, str):
            s = s.strip()
        else:
            s = ""
    except Exception:
        s = ""
    try:
        result["is_remote"] = is_remote_path(s) if s else False
    except Exception:
        result["is_remote"] = False
    try:
        exists = bool(s) and os.path.isdir(s)
    except Exception:
        exists = False
    result["exists"] = bool(exists)
    if not exists:
        result["ok"] = False
        result["writable"] = False
        result["error"] = NOT_FOUND_ERROR
        return result
    # 存在：探测可写（建后删）
    writable = False
    try:
        tmp = tempfile.mkdtemp(dir=s)
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            try:
                os.rmdir(tmp)
            except Exception:
                pass
        writable = True
    except Exception:
        writable = False
    result["writable"] = bool(writable)
    result["ok"] = True
    result["error"] = "" if writable else NOT_WRITABLE_ERROR
    return result
