# 局域网无头 Linux 资产接入增强 — 跨模块契约（v1）

> 并行开发唯一契约源。各包代理只实现本文约定，不越界改他人文件。
> 目标：Windows 端 CivitaiFreeTool 经 SMB 路径桥接（映射盘符 / UNC）稳定管理无头 Linux 上的模型资产。

## 0. 文件所有权（互斥）

| 包 | 独占文件 | 内容 |
|---|---|---|
| P1 Python | `path_util.py`(新增)、`posix_compat.py`、`webui.py` | 路径工具 + 扫描根状态 + 缩略图缓存 + check_dir + APP_VERSION |
| P2 前端 | `web/app.js` | 设置页连接测试/浏览按钮/UNC 示例 + 扫描不可达根警告 |
| P3 部署 | `linux-server-setup/install-samba.sh`、`linux-server-setup/README.md`（全新增） | Linux 一键 Samba 部署 |
| P4 文档版本 | `LINUX.md`、`README.md`、`CHANGELOG.md`、`更新说明_v2.2.0.md`(新增)、`version_info.txt` | 文档 + 版本 2.2.0 |
| P5 测试 | `test_browser_bridge.py` | path_util/扫描状态/缩略图缓存用例 |

禁止碰：`chrome-extension/`（插件不动）、`downloader.py`/`civitai_api.py`/`model_manager.py`/`gui.py`/`main.py`/`main_web.py`/`config.py`（不在本次范围）、对方包文件。

## 1. path_util.py（P1 新建，webui 复用）

```python
def norm_root(p: str) -> str:
    """目录根规范化：UNC/盘符根安全去尾。
    禁止用 rstrip('/\\')——会把 'Z:\\' 削成 'Z:'（盘符相对路径语义错误）。
    实现：os.path.normpath(p) 后，若结果是盘符根 'Z:' 则补回 'Z:\\'；
    UNC 根 '\\\\host\\share' 保持；空/None 返回 ''。"""

def is_within(root: str, child: str) -> bool:
    """child 是否在 root 内（含相等）。normcase 归一大小写 + commonpath 前缀比对。
    返回 False 的边界：child==root（允许，True）；root 或 child 空（False）。"""

def is_remote_path(p: str) -> bool:
    """网络路径判定：UNC（'\\\\' 前缀）为 True；
    盘符用 ctypes.windll.kernel32.GetDriveTypeW（DRIVE_REMOTE=4）判定（非 Windows 恒 False，失败回退 False）。"""

def check_reachable(path: str) -> dict:
    """可达性诊断，永不抛异常。返回：
    {"ok": bool, "exists": bool, "writable": bool, "is_remote": bool, "error": str}
    - exists: os.path.isdir(path)
    - writable: exists 时用 tempfile.mkdtemp(dir=path) 建后删（失败 False）；不存在时 False
    - error: 不存在 → '目录不存在或不可达（可能未授权/主机离线/共享名错误）'；存在但不可写 → '目录不可写'
    - ok: exists 且 writable（读管理场景 exists 即可用，写场景需 writable——统一 ok=exists，writable 单列）
    """
```

## 2. webui.py 改动（P1）

1. `APP_VERSION = "2.2.0"`。
2. 新增 `_model_roots_with_status()`：返回 `[{"path", "status": "ok"|"unreachable", "error"}]`——
   对 models_dirs/models_dir 的每个候选 `os.path.isdir`：True → ok；False → unreachable（error=检查提示）。
   **保持 `_models_roots()` 签名与语义不变**（只返回可达目录列表，现有调用方 scan/library_check/_root_of 不动），
   仅把内部 `rstrip("/\\")` 逻辑改为 `path_util.norm_root`。
3. `scan_models`（以 grep 实际行号为准）：改从 `_model_roots_with_status()` 遍历；每个根独立 try/except；
   `self.mm_scan_state` 增加 `"roots"` 字段 = with_status 的列表（unreachable 项直接进状态，不中断其他根）；
   汇总 msg 保持现有"扫描完成：N 个模型"格式（前端据 roots 显示警告）。
4. 新增 js_api `check_dir(path)`：返回 `{"path": path, **path_util.check_reachable(path)}`。
5. `_safe_archive_path`（约 923 行）与 `_root_of`（约 150 行）改用 `path_util.is_within`/`norm_root`
   （顺带修复：盘符根 `Z:\` 被削、大小写敏感前缀比对两个隐患；行为对正常本地路径不变）。
6. `get_covers(self, paths, size=64, cache_dir=None)`（以 grep 行号为准）加本地缩略图缓存：
   - 缓存目录：`cache_dir` 参数（测试注入）或 `posix_compat.thumb_cache_dir()`；None/空时不做缓存（原逻辑直读）
   - 键：`sha1(normcase(abspath(cover)).encode()) + "_%d_%d" % (int(mtime), int(size)) + ".jpg"`
   - 命中：读缓存图转 b64 返回（缓存图直接是目标 size 缩略图，不再二次缩放）
   - 未命中：现有 Image.open 流程生成 → 写入缓存（写失败/磁盘满静默，不破坏原逻辑）
   - 返回格式保持现有 `[{path, b64, w, h}]` 不变

## 3. posix_compat.py（P1）

新增 `def thumb_cache_dir() -> str`：Windows 返回
`os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "CivitaiFreeToolWeb", "thumb_cache")`；
非 Windows 返回 `~/.local/share/CivitaiFreeToolWeb/thumb_cache`；`os.makedirs(exist_ok=True)` 后返回，
创建失败返回 None。保持文件既有风格（现有函数 try/except + 中文注释）。

## 4. web/app.js 改动（P2）

1. 设置页渲染（约 2195-2216 行）：`models_dirs`（textarea 每行）与 `archive_root`（text input）后各追加
   「测试」小按钮 → `api.call("check_dir", [path])` → 成功用 `showToast`（可复用 76-91 行）显示
   `可达（可写/只读）`，`is_remote` 为 true 时追加 `网络路径`；失败显示 `error`。
2. `models_dirs` 行加「浏览」按钮：复用现有 `pick_dir`（参考 2516-2523 行 obBrowseModels 模式），
   选中的目录**追加**为 textarea 新行（不去重，保存时后端已去重）。
3. `archive_root` 的 tips/placeholder 增加 UNC 示例：`如 \\192.168.1.10\models 或 Z:\models`。
4. 扫描状态（约 691-710 行 pollMmScan 完成分支）：`get_scan_state` 返回含 `roots` 数组时，
   若有 `status != "ok"` 的根 → `setStatus`（黄色警示样式，可复用 setStatus）显示
   `以下模型目录不可达：<path>（<error>）…`（最多列 3 个，其余 `等 N 个`），**不再**显示误导性
   "扫描完成：0 个模型"（0 模型且 roots 全 ok 才正常显示）。
5. 不得改动设置页其他字段结构（SETTING_FIELDS 数组只允许改 tips/说明文案，不增删字段定义）。

## 5. linux-server-setup/（P3，全新增）

`install-samba.sh`（bash，shebang `#!/usr/bin/env bash`，`set -euo pipefail`）：
- 用法：`sudo bash install-samba.sh <共享路径> [--share NAME] [--user USER] [--guest]`
  - `<共享路径>` 必填，`os.path` 校验：必须绝对路径且存在
  - `--share NAME` 默认 `models`；`--user USER` 默认 `civitai-smb`；`--guest` 开启免密（guest ok=yes，此时忽略 --user）
- 发行版检测：`/etc/os-release` 的 `ID`（debian/ubuntu→apt，rhel/centos/fedora/rocky/alma→dnf（无则 yum），opensuse/sles→zypper，arch→pacman），未识别报错退出并给手动安装指引
- 安装包：samba（各发行版同名，RHEL 系 `samba` + `samba-client`）
- smb.conf：检测已有共享段（`[NAME]`）则跳过追加（幂等）；新增段写：
  ```
  [NAME]
     path = <共享路径>
     browseable = yes
     read only = no
     create mask = 0664
     directory mask = 0775
  ```
  guest 模式追加 `guest ok = yes`；user 模式：检查用户存在（`id -u`），不存在则 `useradd -M -s /usr/sbin/nologin <USER>`，然后 `smbpasswd -a <USER>`（交互设密码，提示先运行 `smbpasswd -a` 或脚本内调用）；文件开头 `[global]` 存在则不动，否则写最小 global 段（workgroup、server string）
- systemd：`systemctl enable --now smbd nmbd`（Debian/Arch）或 `smb nmb`（RHEL/zypper），失败提示手动
- 防火墙：`firewall-cmd --permanent --add-service=samba && firewall-cmd --reload`（命令存在时）
- SELinux：`getenforce` 非 Disabled 时 `setsebool -P samba_export_all_rw 1`（失败仅警告）
- 结束打印：验证命令 `smbclient -L //localhost -U <USER>`、Windows 对接指引一行、安全提示（仅局域网/建议用户+密码）

`linux-server-setup/README.md`：快速开始（两行命令）、参数表、安全说明、Windows 端 `net use` 映射示例、
验证步骤、常见问题（445 被占、SELinux、权限 0777 说明）。

## 6. 版本（P4）

- `version_info.txt`：filevers/prodvers 与 FileVersion/ProductVersion 全改 `2.2.0`（格式参考现 2.1.12）
- `更新说明_v2.2.0.md`：标题 `# CivitaiFreeTool v2.2.0 更新说明（2026-09-10）`，口语化分节：新增「局域网资产接入（Samba 桥接）」+「网络路径健壮性（扫描状态/缩略图缓存/连接测试）」
- `CHANGELOG.md`：顶部新增 v2.2.0 条目，沿用四段式（标题/背景/功能/运维提示）
- `LINUX.md`：新增「局域网无头服务器接入（Samba 桥接）」章节（部署脚本用法 + Windows 端映射配置 + 诊断），
  插到「已知限制」之前；不删除既有内容
- `README.md`：徽章 `version-2.2.0-blue`；「模型管理」与「设置」小节各补一句网络路径支持说明（含 UNC 示例）

## 7. 测试（P5，追加到 test_browser_bridge.py，保持现有 35 个全绿）

- `PathUtilTest`：norm_root（`"Z:\\"` 保尾、`"\\\\host\\share\\"` 去尾、普通路径不变、空串）、
  is_within（同路径 True/大小写 True/子路径 True/兄弟 False/空 False）、check_reachable（临时目录 ok+writable；
  不存在 exists=False；is_remote 在 CI 不依赖实际盘符——只断言返回 dict 键齐全）
- `_models_roots` 状态：`Api.__new__` + cfg 含一个不存在目录 → `_model_roots_with_status` 含 unreachable 项、
  `_models_roots` 不含它
- `scan_models` roots 状态：扫后 `mm_scan_state["roots"]` 含 ok/unreachable（unreachable 根不中断）
- `get_covers` 缓存：临时目录 + PIL 造 8x8 PNG → 首次（cache_dir=临时缓存目录）未命中生成、二次命中
  （断言第二次调用后缓存目录出现 jpg、返回结构与原一致）；mtime 变化后键变化（touch 后再次调用生成新键）

## 8. 集成与验收

- 主控集成：pytest 全绿（35+新增）、`node --check` 不适用（app.js 是浏览器脚本，用 `node --check web/app.js` 仍可做语法校验）、
  `bash -n install-samba.sh` 语法校验
- 重建 exe（webui.py/posix_compat.py/web/app.js 变更必须重建）→ 冒烟：health（version 2.2.0）+ archive/config + check_dir 端点
- 不改插件；不改 downloader/civitai_api/model_manager/config
