#!/usr/bin/env bash
# CivitaiFreeTool - Linux 一键 Samba 部署脚本
# 用途：把无头 Linux 上的模型目录共享出来，供 Windows 端 CivitaiFreeTool 经 UNC/映射盘符管理。
# 用法：sudo bash install-samba.sh <共享路径> [--share NAME] [--user USER] [--guest]
set -euo pipefail

# ---------- 默认值 ----------
SHARE_NAME="models"        # 默认共享名
SMB_USER="civitai-smb"     # 默认 Samba 用户
GUEST_MODE=0               # 1 = 免密访客模式
SHARE_PATH=""              # 必填：共享目录（绝对路径且必须存在）

# ---------- 小工具：用法说明 ----------
usage() {
  cat <<'EOF'
用法：
  sudo bash install-samba.sh <共享路径> [--share NAME] [--user USER] [--guest]

参数：
  <共享路径>      必填，绝对路径且必须已存在，例如 /data/models
  --share NAME    共享名，默认 models
  --user USER     Samba 登录用户，默认 civitai-smb（--guest 时忽略）
  --guest         免密访客模式（此时追加 guest ok = yes，不建 Samba 用户）
  -h, --help      显示本帮助

示例：
  sudo bash install-samba.sh /data/models
  sudo bash install-samba.sh /data/models --share models --user civitai-smb
  sudo bash install-samba.sh /data/models --share public --guest
EOF
}

# ---------- root 检测：必须 root 运行 ----------
if [ "$(id -u)" -ne 0 ]; then
  echo "错误：请用 root 权限运行，例如：sudo bash install-samba.sh <共享路径>" >&2
  exit 1
fi

# ---------- 参数解析 ----------
while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --share)
      [ "$#" -ge 2 ] || { echo "错误：--share 缺少取值" >&2; usage >&2; exit 1; }
      SHARE_NAME="$2"
      shift 2
      ;;
    --user)
      [ "$#" -ge 2 ] || { echo "错误：--user 缺少取值" >&2; usage >&2; exit 1; }
      SMB_USER="$2"
      shift 2
      ;;
    --guest)
      GUEST_MODE=1
      shift
      ;;
    --*)
      echo "错误：未知参数 $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      # 第一个位置参数为共享路径，多余的位置参数报错
      if [ -z "$SHARE_PATH" ]; then
        SHARE_PATH="$1"
        shift
      else
        echo "错误：多余的位置参数 $1" >&2
        usage >&2
        exit 1
      fi
      ;;
  esac
done

# ---------- 共享路径校验：必须绝对路径且存在 ----------
if [ -z "$SHARE_PATH" ]; then
  echo "错误：缺少 <共享路径> 参数" >&2
  usage >&2
  exit 1
fi
case "$SHARE_PATH" in
  /*) ;; # 绝对路径，以 / 开头
  *)
    echo "错误：共享路径必须是绝对路径：$SHARE_PATH" >&2
    exit 1
    ;;
esac
if [ ! -d "$SHARE_PATH" ]; then
  echo "错误：共享路径不存在或不是目录：$SHARE_PATH" >&2
  exit 1
fi

# 共享名/用户名做最小合法性校验（避免 smb.conf 段名非法）
case "$SHARE_NAME" in
  ""|*" "*|*"["*|*"]"*)
    echo "错误：共享名非法（不能为空，不能含空格或方括号）：$SHARE_NAME" >&2
    exit 1
    ;;
esac
if [ "$GUEST_MODE" -eq 0 ]; then
  case "$SMB_USER" in
    ""|*" "*)
      echo "错误：用户名非法：$SMB_USER" >&2
      exit 1
      ;;
  esac
fi

echo "共享路径：$SHARE_PATH"
echo "共享名：$SHARE_NAME"
if [ "$GUEST_MODE" -eq 1 ]; then
  echo "模式：访客免密（guest）"
else
  echo "模式：用户认证（user=$SMB_USER）"
fi

# ---------- 发行版检测（/etc/os-release 的 ID） ----------
if [ ! -f /etc/os-release ]; then
  echo "错误：未找到 /etc/os-release，无法识别发行版。" >&2
  echo "请手动安装 samba 后再运行本脚本（Debian/Ubuntu: apt install samba；RHEL 系: dnf install samba samba-client；openSUSE: zypper install samba；Arch: pacman -S samba）。" >&2
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
DIST_ID="${ID:-}"
echo "检测到发行版 ID：$DIST_ID"

PKG_MGR=""       # apt | dnf | yum | zypper | pacman
SVC_A=""         # 主服务名
SVC_B=""         # nmb 服务名
IS_RHEL=0        # RHEL 系需要多装 samba-client
case "$DIST_ID" in
  debian|ubuntu)
    PKG_MGR="apt"
    SVC_A="smbd"
    SVC_B="nmbd"
    ;;
  rhel|centos|fedora|rocky|alma*)
    # alma 与 almalinux 兼容：almalinux 的 ID 为 almalinux，加通配
    IS_RHEL=1
    SVC_A="smb"
    SVC_B="nmb"
    if command -v dnf >/dev/null 2>&1; then
      PKG_MGR="dnf"
    else
      PKG_MGR="yum"
    fi
    ;;
  almalinux)
    IS_RHEL=1
    SVC_A="smb"
    SVC_B="nmb"
    if command -v dnf >/dev/null 2>&1; then
      PKG_MGR="dnf"
    else
      PKG_MGR="yum"
    fi
    ;;
  opensuse*|sles)
    PKG_MGR="zypper"
    SVC_A="smb"
    SVC_B="nmb"
    ;;
  arch)
    PKG_MGR="pacman"
    SVC_A="smbd"
    SVC_B="nmbd"
    ;;
  *)
    echo "错误：未识别的发行版 ID：$DIST_ID" >&2
    echo "请手动安装 samba：" >&2
    echo "  Debian/Ubuntu: sudo apt update && sudo apt install -y samba" >&2
    echo "  RHEL/CentOS/Fedora/Rocky/Alma: sudo dnf install -y samba samba-client（无 dnf 则用 yum）" >&2
    echo "  openSUSE/SLES: sudo zypper install -y samba" >&2
    echo "  Arch: sudo pacman -Sy --noconfirm samba" >&2
    echo "安装完成后重新运行本脚本。" >&2
    exit 1
    ;;
esac

# ---------- 安装 samba ----------
echo "使用 $PKG_MGR 安装 samba..."
case "$PKG_MGR" in
  apt)
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y samba
    ;;
  dnf)
    dnf install -y samba samba-client
    ;;
  yum)
    yum install -y samba samba-client
    ;;
  zypper)
    zypper --non-interactive install samba
    ;;
  pacman)
    pacman -Sy --noconfirm samba
    ;;
esac

SMB_CONF="/etc/samba/smb.conf"
if [ ! -f "$SMB_CONF" ]; then
  echo "错误：安装后仍未找到 $SMB_CONF，请检查 samba 是否安装成功。" >&2
  exit 1
fi

# 备份一次（仅首次备份，避免重复覆盖原备份）
if [ ! -f "${SMB_CONF}.bak-civitai" ]; then
  cp -a "$SMB_CONF" "${SMB_CONF}.bak-civitai"
  echo "已备份原配置到 ${SMB_CONF}.bak-civitai"
fi

# ---------- smb.conf：[global] 不存在则写最小段 ----------
if ! grep -qE '^[[:space:]]*\[global\][[:space:]]*$' "$SMB_CONF"; then
  echo "未检测到 [global] 段，写入最小 global 配置..."
  cat >> "$SMB_CONF" <<'EOF'

[global]
   workgroup = WORKGROUP
   server string = CivitaiFreeTool Samba
   map to guest = bad user
EOF
else
  echo "[global] 段已存在，保持不动。"
fi

# ---------- smb.conf：共享段幂等追加 ----------
if grep -qE "^[[:space:]]*\\[${SHARE_NAME}\\][[:space:]]*$" "$SMB_CONF"; then
  echo "共享段 [$SHARE_NAME] 已存在，跳过追加（幂等）。"
else
  echo "追加共享段 [$SHARE_NAME]..."
  {
    echo ""
    echo "[$SHARE_NAME]"
    echo "   path = $SHARE_PATH"
    echo "   browseable = yes"
    echo "   read only = no"
    echo "   create mask = 0664"
    echo "   directory mask = 0775"
    if [ "$GUEST_MODE" -eq 1 ]; then
      echo "   guest ok = yes"
    fi
  } >> "$SMB_CONF"
fi

# 校验配置文件语法（testparm 存在时）
if command -v testparm >/dev/null 2>&1; then
  if ! testparm -s "$SMB_CONF" >/dev/null; then
    echo "警告：testparm 校验 smb.conf 未通过，请检查 $SMB_CONF" >&2
  else
    echo "smb.conf 语法校验通过。"
  fi
fi

# ---------- 用户模式：确保系统用户存在 + 引导设置 Samba 密码 ----------
if [ "$GUEST_MODE" -eq 0 ]; then
  if ! id -u "$SMB_USER" >/dev/null 2>&1; then
    echo "创建系统用户 $SMB_USER（禁止 shell 登录）..."
    useradd -M -s /usr/sbin/nologin "$SMB_USER"
  else
    echo "用户 $SMB_USER 已存在，跳过 useradd。"
  fi
  echo "请为 Samba 用户设置密码（交互式）："
  echo "  smbpasswd -a $SMB_USER"
  smbpasswd -a "$SMB_USER"
else
  echo "访客模式：跳过用户创建与 smbpasswd。"
fi

# ---------- systemd：enable --now，失败仅警告 ----------
echo "启动并设置开机自启：$SVC_A $SVC_B ..."
if ! systemctl enable --now "$SVC_A" "$SVC_B"; then
  echo "警告：systemctl enable --now $SVC_A $SVC_B 失败，请手动执行：" >&2
  echo "  sudo systemctl enable --now $SVC_A $SVC_B" >&2
  echo "  sudo systemctl status $SVC_A $SVC_B" >&2
fi

# ---------- 防火墙：firewalld 存在则放行 samba ----------
if command -v firewall-cmd >/dev/null 2>&1; then
  echo "检测到 firewalld，放行 samba 服务..."
  firewall-cmd --permanent --add-service=samba || echo "警告：firewall-cmd 添加 samba 服务失败。" >&2
  firewall-cmd --reload || echo "警告：firewall-cmd --reload 失败。" >&2
else
  echo "未检测到 firewall-cmd，跳过防火墙配置（如用 ufw/iptables 请手动放行 445/tcp）。"
fi

# ---------- SELinux：Enforcing 下允许 samba 读写导出 ----------
if command -v getenforce >/dev/null 2>&1; then
  SELINUX_STATE="$(getenforce 2>/dev/null || echo Disabled)"
  if [ "$SELINUX_STATE" != "Disabled" ]; then
    echo "SELinux 状态：$SELINUX_STATE，设置 samba_export_all_rw..."
    setsebool -P samba_export_all_rw 1 || echo "警告：setsebool -P samba_export_all_rw 1 失败，如遇权限拒绝请手动执行。" >&2
  else
    echo "SELinux 已禁用，跳过 setsebool。"
  fi
else
  echo "未检测到 getenforce，跳过 SELinux 配置。"
fi

# ---------- 收尾：验证命令 + Windows 对接指引 + 安全提示 ----------
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$SERVER_IP" ] && SERVER_IP="<本机IP>"

echo ""
echo "================ 部署完成 ================"
echo "共享：\\\\${SERVER_IP}\\${SHARE_NAME}  （本地路径 $SHARE_PATH）"
echo ""
echo "验证命令（本机）："
if [ "$GUEST_MODE" -eq 1 ]; then
  echo "  smbclient -L //localhost -N"
  echo "  smbclient //localhost/${SHARE_NAME} -N -c 'ls'"
else
  echo "  smbclient -L //localhost -U ${SMB_USER}"
  echo "  smbclient //localhost/${SHARE_NAME} -U ${SMB_USER} -c 'ls'"
fi
echo ""
echo "Windows 端对接（CMD 管理员或普通终端执行，Z 盘符可换）："
if [ "$GUEST_MODE" -eq 1 ]; then
  echo "  net use Z: \\\\${SERVER_IP}\\${SHARE_NAME}"
else
  echo "  net use Z: \\\\${SERVER_IP}\\${SHARE_NAME} /user:${SMB_USER} <密码>"
  echo "  （<密码> 为刚才 smbpasswd 设置的密码；也可在文件资源管理器地址栏输入 \\\\${SERVER_IP}\\${SHARE_NAME} 访问）"
fi
echo ""
echo "安全提示："
echo "  1. 仅建议在可信局域网内使用 Samba 共享，不要暴露到公网。"
echo "  2. 建议使用用户+密码模式（默认），避免长期使用 --guest 免密模式。"
echo "  3. 共享目录权限建议 0775 + 归属到 Samba 用户组，不要直接 0777。"
echo "=========================================="
