# Linux 一键 Samba 部署（供 Windows 端 CivitaiFreeTool 桥接无头 Linux 资产）

本目录提供 `install-samba.sh`：在无头 Linux 服务器上一键安装并配置 Samba，
把模型目录共享出来，Windows 端通过 UNC 路径或映射盘符接入 CivitaiFreeTool。

## 快速开始

```bash
# 1. 把脚本传到服务器（示例）
scp install-samba.sh user@服务器IP:/tmp/

# 2. 登录服务器并执行（共享路径必须是已存在的绝对路径）
sudo bash /tmp/install-samba.sh /data/models
```

执行中脚本会交互式调用 `smbpasswd -a civitai-smb` 设置 Samba 密码（访客模式除外）。

## 完整参数表

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `<共享路径>` | 是 | 无 | 必须绝对路径且目录已存在，例如 `/data/models` |
| `--share NAME` | 否 | `models` | 共享名，即 Windows 端看到的 `\\IP\NAME`；不能为空，不能含空格或方括号 |
| `--user USER` | 否 | `civitai-smb` | Samba 登录用户；`--guest` 模式下忽略 |
| `--guest` | 否 | 关闭 | 访客免密模式：追加 `guest ok = yes`，不建用户、不调 `smbpasswd` |
| `-h, --help` | 否 | 无 | 显示帮助并退出 |

更多示例：

```bash
# 自定义共享名与用户
sudo bash install-samba.sh /data/models --share models --user civitai-smb

# 访客免密（仅测试/可信内网，见安全说明）
sudo bash install-samba.sh /data/models --share public --guest
```

脚本行为要点：

- 发行版检测读取 `/etc/os-release` 的 `ID`：debian/ubuntu 走 apt；rhel/centos/fedora/rocky/alma/almalinux 走 dnf（无 dnf 回退 yum）；opensuse/sles 走 zypper；arch 走 pacman；未识别则打印各发行版手动安装命令并退出 1。
- RHEL 系额外安装 `samba-client`（提供 `smbclient` 验证命令）。
- 幂等：`smb.conf` 已有 `[<share>]` 段则跳过追加；`[global]` 不存在才写最小 global 段，已存在不动；首次运行备份原文件为 `/etc/samba/smb.conf.bak-civitai`。
- 用户模式：用户不存在则 `useradd -M -s /usr/sbin/nologin <USER>`，然后交互式 `smbpasswd -a <USER>`。
- 服务：Debian/Arch 为 `smbd nmbd`，RHEL/openSUSE 为 `smb nmb`，`systemctl enable --now`，失败仅警告并给出手动命令。
- 防火墙：检测到 `firewall-cmd` 则 `--add-service=samba` + `--reload`。
- SELinux：`getenforce` 非 Disabled 时执行 `setsebool -P samba_export_all_rw 1`，失败仅警告。

## 安全说明

1. 仅在可信局域网内使用，不要把 445 端口暴露到公网。
2. 优先使用默认的用户+密码模式；`--guest` 免密模式任何能连通网络的人都可读写，仅适合临时测试。
3. 共享目录归属建议设为 Samba 用户或其组，权限 0775 + 文件 0664（脚本写入的 `create mask`/`directory mask` 即此意），不要图省事用 0777。
4. 定期改密码：`sudo smbpasswd civitai-smb`；删用户：`sudo smbpasswd -x USER`。

## Windows 端对接

```cmd
:: 用户模式（Z 盘符可换，<密码> 为 smbpasswd 设置的密码）
net use Z: \\192.168.1.10\models /user:civitai-smb <密码>

:: 访客模式
net use Z: \\192.168.1.10\public

:: 断开
net use Z: /delete
```

然后在 CivitaiFreeTool 设置页把模型目录填为 `Z:\models`，
或直接填 UNC 路径 `\\192.168.1.10\models`，用「测试」按钮验证可达性。

## 验证

服务器本机：

```bash
# 用户模式
smbclient -L //localhost -U civitai-smb
smbclient //localhost/models -U civitai-smb -c 'ls'

# 访客模式
smbclient -L //localhost -N
smbclient //localhost/public -N -c 'ls'

# 配置语法
testparm -s /etc/samba/smb.conf

# 服务状态
sudo systemctl status smbd   # RHEL/openSUSE 改为 smb
```

Windows 端：文件资源管理器地址栏输入 `\\服务器IP\共享名` 能列出文件；
`net use` 映射后 CivitaiFreeTool 设置页「测试」显示可达。

## 常见问题

- **445 端口被占用 / 连不上**：先确认服务在跑（`systemctl status`）；再查端口
  `ss -ltn | grep 445`；云主机注意安全组放行 445；Windows 端 `net use` 报错
  53/67 多为 IP 或共享名写错，报错 1326 多为用户名或密码错。
- **SELinux 拒绝访问（能连上但打不开目录）**：确认脚本已执行
  `setsebool -P samba_export_all_rw 1`；仍不行可临时
  `chcon -R -t samba_share_t /data/models` 验证是否为标签问题；
  审计日志看 `ausearch -m avc -ts recent | grep smb`。
- **权限问题（能读不能写 / 新文件权限怪）**：目录本身需要写权限，
  推荐 `chown -R root:civitai-smb /data/models && chmod -R 0775 /data/models`；
  不要用 0777——0777 会让任何本地用户都可写，脚本默认的
  `create mask = 0664` / `directory mask = 0775` 已保证新建文件组可写。
- **重复执行脚本**：安全。共享段已存在会跳过追加；`[global]` 已存在不动；
  用户已存在跳过 `useradd`；但 `smbpasswd -a` 每次都会交互式要求设密码。
- **改共享路径/改名**：直接编辑 `/etc/samba/smb.conf` 对应段的 `path`，
  然后 `testparm -s` 校验 + `sudo systemctl restart smbd`（RHEL 系为 `smb`）。
