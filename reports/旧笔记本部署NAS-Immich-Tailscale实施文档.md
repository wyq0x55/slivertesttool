# 旧 Dell Inspiron 部署 NAS + Immich + Tailscale 实施文档（国内镜像加速版）

> **本版说明**：针对中国大陆网络环境，全面使用国内镜像源加速。
> - **Docker Hub 镜像**（postgres、redis 等 `docker.io` 镜像）：通过 `/etc/docker/daemon.json` 的 `registry-mirrors` 加速。
> - **Immich 官方镜像**：托管在 `ghcr.io`（GitHub Container Registry）。⚠️ Docker 的 `registry-mirrors` **只能代理 Docker Hub（docker.io）**，无法代理 ghcr.io。因此需在 `docker-compose.yml` 中把 Immich 的镜像地址**改写为 ghcr 代理**（见第 7 章 7.8）。

## 1. 目标说明

**部署目标**：将一台旧 Dell Inspiron 笔记本（约 2013 年）改造为家庭 NAS 服务器，运行 Immich 照片管理系统，并通过 Tailscale 实现安全的外网访问。

**范围**：
- 安装 Ubuntu Server 24.04 LTS
- 部署 Docker + Immich（照片自动备份、替代部分 iCloud Photos 功能）
- 部署 Tailscale 组网（无公网 IP、无需备案即可远程访问）
- iPhone 端配置自动备份

**预期结果**：
- 手机照片可自动备份到 NAS
- 家庭成员可在内网及外网安全访问照片/文件
- 合盖、断电重启后服务自动恢复
- 为后续扩展 Nextcloud、RustFS、Gitea、Jenkins 打好基础

**适用场景与受众**：家庭/个人自建私有云，具备基础 Linux 操作能力的用户。

---

## 2. 硬件要求

| 项目 | 最低配置 | 推荐配置 |
|------|----------|----------|
| CPU | Intel 第 3 代双核（i3-3xxx） | Intel i5/i7 第 3 代及以上 |
| 内存 | 4 GB | **8 GB 以上**（Immich 机器学习较吃内存） |
| 系统盘 | 128 GB SSD | 256 GB SSD |
| 数据盘 | 500 GB HDD | **500 GB 以上 SSD** |
| 网络 | 百兆网口 | **千兆有线网口** |
| 架构 | x86_64（64 位） | x86_64 |

**注意事项**：
- 架构必须为 **x86_64**，Immich 官方镜像不支持 32 位。
- 机械硬盘（HDD）建议升级为 SSD，显著提升缩略图生成与检索速度。
- 长期运行请使用**有线网络**，避免 Wi-Fi 休眠掉线。
- ⚠️ 部署前**务必检查电池是否鼓包**，鼓包电池存在安全隐患，建议取出或更换。
- 建议接入 UPS 或稳定电源，避免异常断电损坏数据库。

---

## 3. 软件要求

| 软件 | 版本要求 |
|------|----------|
| 操作系统 | Ubuntu Server **24.04 LTS**（x86_64） |
| Docker Engine | 最新稳定版（≥ 24.x） |
| Docker Compose | Compose V2（`docker compose`，≥ 2.x） |
| Tailscale | 最新稳定版（≥ 1.60） |
| Immich | latest（官方 release） |
| PostgreSQL / Redis | 由 Immich Compose 自带，无需单独安装 |
| iPhone App | Immich、Tailscale（App Store 最新版） |

**制作启动盘所需**（在另一台电脑上）：
- 8 GB 以上 U 盘
- Rufus（Windows）/ balenaEtcher（macOS/Linux）

**国内镜像源（本版使用）**：

| 用途 | 镜像源 |
|------|--------|
| APT 软件源 | 阿里云 `https://mirrors.aliyun.com/ubuntu/` 或清华 `https://mirrors.tuna.tsinghua.edu.cn/ubuntu/` |
| Docker 安装脚本 | `curl -fsSL https://get.docker.com \| sudo sh -s -- --mirror Aliyun` |
| Docker Hub 镜像（docker.io） | daocloud / dockerproxy.net / 1ms.run / rat.dev（见 7.6） |
| Immich 镜像（ghcr.io） | `ghcr.m.daocloud.io`（备用 `ghcr.nju.edu.cn`，见 7.8） |
| GitHub 文件下载 | `https://ghproxy.net/` 前缀代理 |

---

## 4. 前置检查

部署前请逐项确认：

| 检查项 | 验证方式 |
|--------|----------|
| 硬件架构为 64 位 | 安装后执行 `uname -m`，应返回 `x86_64` |
| 内存 ≥ 4GB | `free -h` |
| 磁盘剩余空间充足 | `df -h /` |
| 网络连通 | `ping -c 4 www.aliyun.com` |
| DNS 解析正常 | `nslookup github.com` |
| 已获取路由器网段与网关 | 登录路由器管理页 / `ip route` |
| 已准备 Tailscale 账号 | Google / Microsoft / GitHub 均可 |
| 已确认笔记本电池状态 | 目视检查是否鼓包 |
| SSH 可远程连接 | `ssh nas@192.168.1.100` |

**记录以下信息备用**（后续步骤会用到）：
```text
本机计划静态 IP：192.168.1.100
路由器网关：192.168.1.1
子网掩码：/24
登录用户名：nas
主机名：dell-nas
```

---

## 5. 详细步骤（含对应命令）

> 以下步骤**必须按顺序执行**。每一步均说明"做什么/为什么"，有对应命令的直接跟在下方。标注 `[sudo]` 的命令需要管理员权限；涉及的配置文件内容见第 7 章。

### 步骤 1 — 下载并制作启动 U 盘
- 做什么：下载 Ubuntu Server 24.04 ISO，用 Rufus 写入 U 盘（分区类型选 **GPT**）。
- 为什么：这是安装操作系统的引导介质。
- 命令：在另一台电脑用图形工具 Rufus 操作，无终端命令。

### 步骤 2 — 进入 BIOS 从 U 盘启动
- 做什么：开机按 `F12` 选择 USB 启动；若无法启动则关闭 Secure Boot、启用 Legacy Boot。
- 为什么：老机型默认从硬盘启动，需手动指定引导设备。
- 命令：BIOS 内操作，无终端命令。

### 步骤 3 — 安装 Ubuntu Server
- 做什么：按第 7.1 节"安装向导选项"完成语言、键盘、网络、镜像源、磁盘、用户设置，**务必勾选安装 OpenSSH Server**。
- 为什么：SSH 是后续远程管理的唯一入口，缺失会导致无法远程操作。
- 命令：安装向导内操作，无终端命令。

### 步骤 3.5 — 更换 APT 国内源
- 做什么：备份原源，将 APT 软件源替换为阿里云国内源（配置见 7.7），再刷新索引。
- 为什么：默认官方源在国内速度慢/易超时。**必须在"步骤 4 系统更新"之前完成。**

方式一：Ubuntu 24.04（DEB822 新格式，推荐）
```bash
# 1) 备份原始源文件
sudo cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.bak

# 2) 一键将官方域名替换为阿里云（sed 直接改写 URI，最省事）
sudo sed -i 's@//.*archive.ubuntu.com@//mirrors.aliyun.com@g; s@//.*security.ubuntu.com@//mirrors.aliyun.com@g' \
  /etc/apt/sources.list.d/ubuntu.sources

# 若上面 sed 未命中（部分镜像默认写法不同），改用手动编辑并粘贴 7.7 的完整内容：
# sudo nano /etc/apt/sources.list.d/ubuntu.sources

# 3) 刷新索引并验证已命中 aliyun
sudo apt update
apt-cache policy | grep -m1 aliyun     # 有输出即表示换源成功
```
方式二：传统 sources.list 格式（可选）
```bash
# 禁用默认 DEB822 源，改用传统格式（二选一，避免重复源）
sudo mv /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.disabled
sudo nano /etc/apt/sources.list        # 粘贴 7.7「方式二」的 4 行内容
sudo apt update
```
> 换成清华源：把上面命令中的 `mirrors.aliyun.com` 换成 `mirrors.tuna.tsinghua.edu.cn` 即可。

### 步骤 4 — 首次系统更新并重启
- 做什么：更新系统软件包。
- 为什么：修复已知漏洞，确保后续安装依赖为最新。
```bash
sudo apt update          # [sudo] 刷新软件包索引
sudo apt upgrade -y      # [sudo] 升级所有已安装软件包
sudo reboot              # [sudo] 重启使内核更新生效
```

### 步骤 5 — 配置静态 IP
- 做什么：修改 netplan（配置见 7.2），将 DHCP 改为固定 IP，再应用。
- 为什么：NAS 必须固定 IP，否则重启后 IP 变化会导致访问失败。
```bash
ip addr                  # 查看网卡名称（如 enp3s0）与当前 IP
sudo nano /etc/netplan/01-netcfg.yaml     # [sudo] 按 7.2 填写静态 IP
sudo netplan apply       # [sudo] 应用 netplan 配置
ip addr                  # 再次确认 IP 已固定为 192.168.1.100
```

### 步骤 6 — 优化 DNS
- 做什么：设置国内 DNS（配置见 7.3），提升解析速度与稳定性。
- 为什么：默认 DNS 在国内环境可能解析缓慢或失败。
```bash
sudo nano /etc/systemd/resolved.conf      # [sudo] 按 7.3 填写 DNS
sudo systemctl restart systemd-resolved   # [sudo] 使 DNS 配置生效
ping -c 4 github.com                       # 测试解析与连通
```

### 步骤 7 — 禁止合盖休眠
- 做什么：修改 logind 配置（见 7.4），合盖不休眠，再重启生效。
- 为什么：笔记本默认合盖会挂起，导致服务中断。
```bash
sudo nano /etc/systemd/logind.conf        # [sudo] 按 7.4 修改三项为 ignore
sudo reboot                                # [sudo] 重启加载 logind 新配置
```

### 步骤 8 — 安装 Docker（国内源）
- 做什么：用阿里云脚本安装 Docker Engine 与 Compose 插件，将当前用户加入 docker 组。
- 为什么：Immich 以容器方式运行，Docker 是运行基础；国内源可避免 `get.docker.com` 拉取缓慢/失败。
```bash
# 方式一（推荐）：官方脚本 + 阿里云镜像源
curl -fsSL https://get.docker.com | sudo sh -s -- --mirror Aliyun

# 方式二（备用）：daocloud 一键脚本
# curl -fsSL https://get.daocloud.io/docker | sh

sudo usermod -aG docker $USER             # [sudo] 将当前用户加入 docker 组
# 执行后请退出并重新登录（或 newgrp docker）使组权限生效
docker version                            # 验证 Docker 已安装
docker compose version                    # 验证 Compose V2 可用
```

### 步骤 8.1 — 配置 Docker Hub 镜像加速
- 做什么：写入 `/etc/docker/daemon.json` 的 `registry-mirrors`（内容见 7.6）并重启 Docker。
- 为什么：加速 postgres、redis 等 `docker.io` 镜像拉取。
```bash
sudo mkdir -p /etc/docker
sudo nano /etc/docker/daemon.json         # [sudo] 写入镜像源（内容见 7.6）
sudo systemctl daemon-reload              # [sudo] 重载 systemd
sudo systemctl restart docker             # [sudo] 重启 Docker 使镜像源生效
docker info | grep -A5 "Registry Mirrors" # 验证镜像源已生效
```

### 步骤 9 — 安装并登录 Tailscale
- 做什么：安装 Tailscale 并授权登录，获取虚拟 IP。
- 为什么：实现安全的外网访问，避免暴露公网端口。
```bash
curl -fsSL https://tailscale.com/install.sh | sh   # [sudo] 安装 Tailscale
sudo tailscale up                                  # [sudo] 启动并输出授权链接（浏览器打开授权）
tailscale ip -4                                     # 查看分配的 100.x.x.x 虚拟 IP
```

### 步骤 10 — 创建存储目录并部署 Immich（含 ghcr 代理改写）
- 做什么：创建照片/数据库目录，下载官方 Compose 文件，**将 Immich 镜像地址改写为 ghcr 代理**，修改 `.env`（见 7.5），启动容器。
- 为什么：这是核心业务服务；ghcr.io 在国内直连不稳定，改写为代理可保证镜像拉取成功。
```bash
# 创建存储目录
mkdir -p ~/immich/library ~/immich/postgres

# 创建工作目录并进入
mkdir -p ~/immich-app && cd ~/immich-app

# 下载官方 compose 与环境变量示例（github 直连慢时可加 https://ghproxy.net/ 前缀）
wget -O docker-compose.yml https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml
wget -O example.env https://github.com/immich-app/immich/releases/latest/download/example.env
# 备用（ghproxy 代理）：
# wget -O docker-compose.yml https://ghproxy.net/https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml
# wget -O example.env https://ghproxy.net/https://github.com/immich-app/immich/releases/latest/download/example.env

# 【关键】将 Immich 的 ghcr.io 镜像地址改写为 ghcr 代理（见 7.8）
sed -i 's#ghcr.io/immich-app#ghcr.m.daocloud.io/immich-app#g' docker-compose.yml
grep image docker-compose.yml            # 验证镜像地址已改写为 ghcr.m.daocloud.io

# 生成并编辑 .env（见 7.5）
cp example.env .env
nano .env

# 启动服务（首次会拉取镜像，耗时较长）
docker compose up -d
```

### 步骤 11 — 验证与 iPhone 端配置
- 做什么：验证容器状态、创建管理员、配置手机自动备份。
- 为什么：确认整套系统端到端可用。
```bash
docker ps                              # 查看容器运行状态（应有 4 个 Up）
docker logs immich_server              # 查看 Immich 服务日志
```
- 浏览器访问 `http://192.168.1.100:2283` 或 `http://100.x.x.x:2283` → 创建管理员账号。
- iPhone 装 Immich 与 Tailscale（同一账号），Immich 填 Server 地址登录，`Settings → Backup` 开启自动备份。详细验证见第 8 章。

---

## 7. 配置文件

### 7.1 安装阶段选项（Ubuntu Server 安装向导）
```text
Language        : English
Keyboard        : English (US)
Network         : DHCP（安装期间先用 DHCP，安装后再改静态）
Ubuntu Mirror   : https://mirrors.aliyun.com/ubuntu/
                  （或 https://mirrors.tuna.tsinghua.edu.cn/ubuntu/）
Storage         : Use An Entire Disk
Hostname        : dell-nas
Username        : nas
SSH             : ✅ Install OpenSSH Server（必须勾选）
```

### 7.2 静态 IP 配置
**文件路径**：`/etc/netplan/01-netcfg.yaml`
> 注意：YAML 严格使用**空格缩进**（禁止 Tab）。请将 `enp3s0` 替换为 `ip addr` 中的真实网卡名。
```yaml
network:
  version: 2
  ethernets:
    enp3s0:                     # 网卡名，需与 ip addr 显示一致
      dhcp4: no                 # 关闭 DHCP，使用静态地址
      addresses:
        - 192.168.1.100/24      # 本机固定 IP 与掩码
      routes:
        - to: default
          via: 192.168.1.1      # 路由器网关地址
      nameservers:
        addresses:
          - 223.5.5.5           # 阿里 DNS
          - 119.29.29.29        # 腾讯 DNS
```
> 修改后建议执行 `sudo chmod 600 /etc/netplan/01-netcfg.yaml` 消除权限告警。

### 7.3 DNS 配置
**文件路径**：`/etc/systemd/resolved.conf`
```ini
[Resolve]
DNS=223.5.5.5 119.29.29.29     # 主 DNS（阿里、腾讯）
FallbackDNS=8.8.8.8            # 备用 DNS
```

### 7.4 合盖不休眠配置
**文件路径**：`/etc/systemd/logind.conf`
```ini
[Login]
HandleLidSwitch=ignore                 # 合盖时忽略（不休眠）
HandleLidSwitchExternalPower=ignore    # 接电源合盖时忽略
HandleLidSwitchDocked=ignore           # 扩展坞状态合盖时忽略
```

### 7.5 Immich 环境变量
**文件路径**：`~/immich-app/.env`（仅展示需修改的关键项，其余保持官方默认）
```ini
# 照片/视频上传存储位置
UPLOAD_LOCATION=/home/nas/immich/library

# PostgreSQL 数据库数据位置
DB_DATA_LOCATION=/home/nas/immich/postgres

# 镜像版本（保持 release 或指定固定版本，便于回滚）
IMMICH_VERSION=release

# 数据库密码（默认已有随机值，生产环境建议改为强密码）
DB_PASSWORD=postgres

# 时区（建议设置为本地时区）
TZ=Asia/Shanghai
```
> ⚠️ `UPLOAD_LOCATION` 与 `DB_DATA_LOCATION` 必须使用**绝对路径**（`/home/nas/...`），不要用 `~`，否则容器无法正确挂载。

### 7.6 Docker Hub 镜像加速配置（国内加速核心）
**文件路径**：`/etc/docker/daemon.json`
> `registry-mirrors` 仅对 **Docker Hub（docker.io）** 生效，用于加速 postgres、redis 等镜像。国内公共镜像源时有变动，下方为当前较稳定的组合，可保留多个作为冗余。
```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.net",
    "https://docker.1ms.run",
    "https://hub.rat.dev"
  ],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```
> 说明：`log-opts` 限制容器日志大小，避免旧笔记本磁盘被日志占满。修改后需 `sudo systemctl restart docker`。

### 7.7 APT 软件源换国内源（完整配置）

Ubuntu **24.04 默认使用 DEB822 新格式**，源文件为 `/etc/apt/sources.list.d/ubuntu.sources`（传统的 `/etc/apt/sources.list` 默认为空）。请按你的实际系统选择对应方式。

**换源前先备份**：
```bash
sudo cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.bak
```

**方式一（Ubuntu 24.04 推荐，DEB822 格式）**
**文件路径**：`/etc/apt/sources.list.d/ubuntu.sources`
```text
# 阿里云镜像源（noble = Ubuntu 24.04 代号）
Types: deb
URIs: https://mirrors.aliyun.com/ubuntu/
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

# 安全更新源
Types: deb
URIs: https://mirrors.aliyun.com/ubuntu/
Suites: noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
```
> 如需清华源，将上方两处 `https://mirrors.aliyun.com/ubuntu/` 替换为 `https://mirrors.tuna.tsinghua.edu.cn/ubuntu/` 即可。

**方式二（传统 sources.list 格式，兼容旧习惯）**
若你更习惯传统格式，可清空 `ubuntu.sources` 后写入 `/etc/apt/sources.list`：
**文件路径**：`/etc/apt/sources.list`
```text
deb https://mirrors.aliyun.com/ubuntu/ noble main restricted universe multiverse
deb https://mirrors.aliyun.com/ubuntu/ noble-updates main restricted universe multiverse
deb https://mirrors.aliyun.com/ubuntu/ noble-backports main restricted universe multiverse
deb https://mirrors.aliyun.com/ubuntu/ noble-security main restricted universe multiverse
```
> ⚠️ DEB822 与传统格式**二选一**，若两者同时定义相同源会产生重复告警。用传统格式时，请将 `ubuntu.sources` 内容清空或注释。

**换源后刷新并验证**：
```bash
sudo apt update                 # [sudo] 刷新索引，观察是否命中 aliyun
apt-cache policy | grep aliyun  # 验证源已切换为国内地址
```

### 7.8 Immich 镜像地址改写（ghcr 代理）
**文件路径**：`~/immich-app/docker-compose.yml`
> Immich 官方镜像在 `ghcr.io`，无法通过 `registry-mirrors` 加速，需将镜像地址前缀替换为 ghcr 代理。第 5 章步骤 10 的 `sed` 命令会自动完成，改写后各服务镜像应形如：
```yaml
services:
  immich-server:
    image: ghcr.m.daocloud.io/immich-app/immich-server:${IMMICH_VERSION:-release}
  immich-machine-learning:
    image: ghcr.m.daocloud.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}
```
> `database`(postgres) 与 `redis` 两个服务仍使用 `docker.io` 镜像，由 7.6 的 `registry-mirrors` 自动加速，无需改写。
> 备用 ghcr 代理（若 daocloud 拉取失败，可替换前缀）：`ghcr.nju.edu.cn/immich-app`。

---

## 8. 验证步骤

**① 系统与网络**
```bash
uname -m        # 预期：x86_64
ip addr         # 预期：网卡显示 192.168.1.100/24
ping -c 4 github.com   # 预期：有正常延迟回包，0% packet loss
```

**② Docker**
```bash
docker version          # 预期：Client / Server 版本均正常显示
docker compose version  # 预期：Docker Compose version v2.x.x
```

**③ Tailscale**
```bash
tailscale status        # 预期：显示本机及手机节点，状态为在线
tailscale ip -4         # 预期：返回 100.x.x.x 形式的 IP
```

**④ Immich 容器**
```bash
docker ps
```
预期输出应包含以下 4 个容器且状态为 `Up`：
```text
immich_server
immich_machine_learning
immich_postgres
immich_redis
```

**⑤ Web 访问**
- 局域网：`http://192.168.1.100:2283`
- Tailscale：`http://100.x.x.x:2283`（替换为你的 Tailscale IP）
- 预期：首次打开显示"创建管理员账号"页面。

**⑥ iPhone 自动备份**
- App Store 安装 **Immich** 与 **Tailscale**，Tailscale 登录同一账号。
- Immich 中填写 Server：`http://100.x.x.x:2283`，用管理员账号登录。
- 进入 `Settings → Backup`，开启 `Auto Backup`、`Background Backup`，选择 Photos / Videos。
- 预期：拍摄新照片后，在 Immich 中可看到上传记录。

**⑦ 高可用验证**
- 合上笔记本盖子 → `docker ps` 服务仍在运行。
- `sudo reboot` 重启后 → 容器随 Docker 自动恢复（Immich Compose 默认 `restart: always`）。

---

## 9. 故障排查

| 现象 | 排查步骤 |
|------|----------|
| U 盘无法引导 | 关闭 Secure Boot、启用 Legacy Boot；重新用 Rufus 以 GPT 方式制作 |
| `netplan apply` 后断网 | 检查网卡名、网关是否正确；用 `sudo netplan try` 可 120 秒内自动回滚 |
| DNS 解析失败 | `resolvectl status` 查看生效 DNS；确认 `resolved.conf` 已重启 |
| `docker` 命令报权限错误 | 确认已执行 `usermod -aG docker $USER` 并**重新登录** |
| Tailscale 手机看不到 NAS | 确认两端登录**同一账号**；`sudo tailscale up` 重新授权 |
| Immich 容器反复重启 | `docker logs immich_postgres` 查看数据库；确认 `DB_DATA_LOCATION` 路径存在且有写权限 |
| 镜像拉取超时（docker.io） | 检查 `/etc/docker/daemon.json` 镜像源；`docker info` 确认 Registry Mirrors 生效；重启 Docker |
| Immich 镜像拉取失败（ghcr） | 确认已执行 `sed` 改写为 `ghcr.m.daocloud.io`；`grep image docker-compose.yml` 核对；可换用 `ghcr.nju.edu.cn` 前缀 |
| 某镜像源失效 | 国内公共源时有变动，编辑 `daemon.json` 移除失效项或更换新源后重启 Docker |
| Web 打不开 2283 | `docker ps` 确认 server 在运行；`sudo ss -tlnp | grep 2283` 查看端口监听 |
| 上传照片失败/无空间 | `df -h` 查看磁盘；`docker logs immich_server` 查看错误 |
| 合盖仍休眠 | 确认 `logind.conf` 三项均为 `ignore` 且已 `reboot` |

**关键日志/诊断命令**：
```bash
docker logs immich_server            # Immich 主服务日志
docker logs immich_postgres          # 数据库日志
docker compose logs -f               # 实时查看全部服务日志（在 ~/immich-app 下执行）
journalctl -u tailscaled -e          # Tailscale 服务日志
journalctl -xe                       # 系统级错误日志
df -h && free -h                     # 磁盘与内存状态
```

---

## 10. 回滚方案

> 按"从局部到整体"的顺序回滚，尽量保留数据。

**① 回滚 Immich（保留数据）**
```bash
cd ~/immich-app
docker compose down          # 停止并移除容器（不删除数据卷/照片目录）
```
如需回滚到旧版本镜像，在 `.env` 中将 `IMMICH_VERSION` 改为具体版本号（如 `v1.xxx.x`），再执行：
```bash
docker compose pull
docker compose up -d
```

**② 彻底卸载 Immich（删除数据，谨慎）**
```bash
cd ~/immich-app
docker compose down -v       # 移除容器及匿名卷
# ⚠️ 以下会删除照片与数据库，务必先备份
# rm -rf ~/immich/library ~/immich/postgres
```

**③ 回滚 Tailscale**
```bash
sudo tailscale down                    # 断开组网
sudo apt remove --purge tailscale -y   # [sudo] 卸载 Tailscale
```

**④ 回滚 Docker（含镜像源配置）**
```bash
# 仅回退镜像加速配置（保留 Docker）
sudo rm -f /etc/docker/daemon.json
sudo systemctl restart docker

# 或彻底卸载 Docker
sudo apt remove --purge docker-ce docker-ce-cli containerd.io docker-compose-plugin -y
```

> 如需将 Immich 镜像地址改回官方 ghcr.io：
> ```bash
> sed -i 's#ghcr.m.daocloud.io/immich-app#ghcr.io/immich-app#g' ~/immich-app/docker-compose.yml
> ```

**⑤ 回滚网络/系统配置**
```bash
# 恢复静态 IP 前，先备份再还原
sudo cp /etc/netplan/01-netcfg.yaml /etc/netplan/01-netcfg.yaml.bak
# 将 dhcp4: no 改回 dhcp4: yes 并删除 addresses/routes，然后：
sudo netplan apply
```
> **建议**：正式部署前，对关键配置文件先执行 `sudo cp 原文件 原文件.bak` 备份，回滚时直接 `cp .bak` 覆盖还原即可。

**⑥ 数据备份建议（回滚/迁移前）**
```bash
# 备份照片库与数据库目录到外接盘
sudo tar czvf ~/immich-backup-$(date +%F).tar.gz ~/immich/library ~/immich/postgres
```

---

## 11. 局域网 WiFi 加速传输（同一 WiFi 走内网直连）

> **目标**：手机连在家里 WiFi 时，照片备份走**局域网直连**（`http://192.168.1.100:2283`，速度快、不占用外网/流量）；离开家时自动切换到 **Tailscale 地址**（`http://100.x.x.x:2283`）远程访问。
> **原理**：Immich App 的 **Local Network（本地网络）** 功能可绑定指定 WiFi 名称（SSID）与内网地址；检测到手机连接该 WiFi 时自动用内网地址通信，否则回退到外部地址。

### 步骤 1 — 确认内网与外网两个地址均可访问
- 做什么：分别测试局域网地址与 Tailscale 地址是否可打开。
- 为什么：本地网络功能依赖两个可用地址；任一不通都会导致切换失败。
```bash
# 在与 NAS 同一 WiFi 的电脑/手机上测试内网地址
curl -I http://192.168.1.100:2283      # 预期返回 HTTP 200/302
# 用 Tailscale 网络测试外网地址（替换为你的 100.x IP）
curl -I http://100.x.x.x:2283          # 预期返回 HTTP 200/302
```

### 步骤 2 — 在 iPhone Immich App 中开启并配置本地网络
- 做什么：打开 Local Network 开关，填入家里 WiFi 名称、内网地址、外部地址三项。
- 为什么：告诉 App 在该 WiFi 下走内网、离开该 WiFi 走外网。
- 操作（App 内，无终端命令）：
```text
Immich App
  → 底部 Settings（设置）
    → Networking / Advanced（网络 / 高级）
      → 打开 “Local Network / 本地网络” 开关
        → Local Network Wi-Fi Name（本地 WiFi 名称）：家里 WiFi 的 SSID
        → Local Network Server Endpoint（本地地址）：http://192.168.1.100:2283
      → External / Server Endpoint（外部地址）：http://100.x.x.x:2283
```
> 不同 App 版本菜单文案略有差异，核心是三项：**本地 WiFi 名称、本地地址、外部地址**。

### 步骤 3 — 验证自动切换是否生效
- 做什么：分别在家 WiFi 与蜂窝网络下观察 App 使用的地址。
- 为什么：确认内外网切换按预期工作，内网传输更快。
- 操作：
  - 连家里 WiFi → App 内当前连接地址应为 `192.168.1.100`（内网），大批量备份明显更快。
  - 关闭 WiFi 用蜂窝网络 → 应自动切到 `100.x.x.x`（Tailscale）。

### 步骤 4 — 常见问题排查
| 现象 | 处理 |
|------|------|
| 连 WiFi 仍走 Tailscale | 确认填写的 SSID 与手机实际连接的 WiFi 名**完全一致**（区分大小写、含空格） |
| 内网地址打不开 | 手机与 NAS 是否在同一网段；`curl http://192.168.1.100:2283` 测试；确认未开路由器 AP 隔离 |
| iOS 需授权本地网络权限 | iOS「设置 → Immich → 本地网络」需允许，否则无法访问 `192.168.x.x` |

---

## 12. AI 智能分类与搜索（机器学习功能）

> Immich 的 AI 能力由 `immich-machine-learning` 容器提供，主要包含三类：

| 功能 | 说明 |
|------|------|
| **智能搜索（Smart Search / CLIP）** | 用自然语言搜图，如"海边""生日蛋糕""发票"；基于 CLIP 模型对图片语义向量化 |
| **人脸识别（Facial Recognition）** | 自动检测人脸并聚类，可为人物命名，按人物浏览 |
| **图像标签分类（Smart / Object Tagging）** | 自动识别物体/场景并打标签，实现"AI 分类" |

### 步骤 1 — 在管理员后台开启机器学习
- 做什么：以管理员登录 Web 端，确认机器学习、智能搜索、人脸识别均为开启。
- 为什么：这是所有 AI 功能的总开关；默认开启，若曾禁用需在此打开。
- 操作（Web 端 `http://192.168.1.100:2283`，无终端命令）：
```text
管理（Administration）
  → 设置（Settings）
    → 机器学习设置（Machine Learning Settings）
      → Enabled（启用）：开
      → Smart Search（智能搜索）：开
      → Facial Recognition（人脸识别）：开
```

### 步骤 2 —（可选，强烈推荐）配置模型下载国内加速
- 做什么：在 `.env` 追加 Hugging Face 国内镜像端点，再重启容器。
- 为什么：模型文件首次使用时从 Hugging Face 下载，国内直连慢；换镜像端点可显著提速。ML 镜像本身已由 7.8 的 ghcr 代理加速，无需再改。
```bash
cd ~/immich-app
# 在 .env 末尾追加两行（也可用 nano 手动添加）
cat >> .env <<'EOF'
MACHINE_LEARNING_MODEL_INFERENCE_DEVICE=cpu
HF_ENDPOINT=https://hf-mirror.com
EOF
docker compose up -d                   # 重启使 .env 生效
```

### 步骤 3 — 换用支持中文的 CLIP 模型
- 做什么：将智能搜索的 CLIP 模型改为多语言模型。
- 为什么：默认 CLIP **只支持英文**；换多语言模型后才能用**中文**搜索（如"猫""海边"）。
- 操作（Web 端，无终端命令）：
```text
管理 → 设置 → 机器学习设置 → Smart Search
  → CLIP 模型（CLIP model）改为多语言模型，例如：
      XLM-Roberta-Large-Vit-B-16Plus
    （内存紧张可选更轻量的 nllb-clip-base-siglip__v1）
  → 保存
```
> ⚠️ 更换模型后**必须执行步骤 4 重建索引**，否则旧照片仍用旧模型向量，中文搜索无效。
> 老笔记本注意：多语言大模型更吃 CPU/内存，首次索引较慢，建议空闲时段运行。

### 步骤 4 — 对存量照片重建 AI 数据
- 做什么：在任务页对智能搜索、人脸检测、人脸识别点"全部"重新生成。
- 为什么：新照片自动处理，但**已存在照片**或**更换模型后**需手动重建才会生效。
- 操作（Web 端，无终端命令）：
```text
管理 → 任务（Jobs）
  → Smart Search（智能搜索）    → 点击 “All / 全部”
  → Face Detection（人脸检测）  → 点击 “All / 全部”
  → Facial Recognition          → 运行以完成人物聚类
```
- 可用命令观察资源占用（老机器任务较耗时，可后台慢跑）：
```bash
docker stats immich_machine_learning immich_server   # 观察 CPU/内存占用
```

### 步骤 5 — 使用与验证
- 做什么：验证 ML 容器正常，并试用搜索/人物/标签功能。
- 为什么：确认 AI 分类与搜索端到端可用。
```bash
docker ps | grep machine-learning     # 预期：容器状态 Up
docker logs immich_machine_learning   # 预期：模型加载成功、无反复报错（首次会下载模型）
```
- 使用效果：
  - **搜索**：顶部搜索框输入"海边的狗""红色汽车"（换中文模型后可用中文）。
  - **人物**：搜索页 →「People / 人物」，点头像命名后可按人物浏览。
  - **地点/物体**：搜索页展示自动归类的地点与物体标签。

---

## 13. 在主力机用 Agent 通过 SSH 远程自动搭建

> **场景**：不在 NAS 上手动逐条敲命令，而是在你的**主力机**（Mac/Windows/Linux）上运行一个 **AI Agent（如 GitHub Copilot CLI、Claude/Cursor 等具备终端执行能力的助手）**，让它通过 **SSH** 远程连接到 NAS，**照着本文档自动完成搭建**。
> **前提**：NAS 已完成第 3 章系统安装、能联网、已勾选 OpenSSH Server，且你知道其 IP（局域网 `192.168.1.100` 或 Tailscale `100.x.x.x`）与登录用户 `nas`。

### 步骤 1 — 主力机配置 SSH 免密登录
- 做什么：在主力机生成密钥并拷贝到 NAS，实现免密码 SSH。
- 为什么：Agent 需要**非交互**登录；密码交互会打断自动化。
```bash
# 在【主力机】执行
ssh-keygen -t ed25519 -C "nas-agent"          # 一路回车，生成密钥（已有可跳过）
ssh-copy-id nas@192.168.1.100                 # 输入一次密码，写入公钥
ssh nas@192.168.1.100 'echo SSH_OK'           # 验证：应输出 SSH_OK 且不再要密码
```
> 走 Tailscale 时把 IP 换成 `100.x.x.x`；两端都需先装好 Tailscale（第 9 章）。

### 步骤 2 — 为自动化配置 sudo 免密（限本次搭建，务必事后收回）
- 做什么：临时给 `nas` 用户 sudo 免密，便于 Agent 连续执行 `apt`、`netplan`、`docker` 等特权命令。
- 为什么：脚本含大量 `sudo`，逐条询问密码无法自动化。**这是临时授权，搭建完成后按步骤 6 收回。**
```bash
# 在【NAS】执行一次（交互输入密码）
echo "nas ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/90-nas-agent
sudo chmod 440 /etc/sudoers.d/90-nas-agent
```
> ⚠️ 免密 sudo 会降低安全性，仅用于可信内网的一次性搭建；完成后**立即删除**（步骤 6）。

### 步骤 3 — 给 Agent 的任务提示词（Prompt）
- 做什么：把本文档连同下方约束交给主力机上的 Agent。
- 为什么：明确边界，避免 Agent 误操作或改动无关内容。
- 建议提示词（复制给你的 Agent）：
```text
你将通过 SSH 远程搭建一台家庭 NAS。目标主机：nas@192.168.1.100（或 Tailscale 100.x.x.x）。
请严格按照《旧 Dell Inspiron 部署 NAS + Immich + Tailscale 实施文档》执行第 4–12 章：
- 所有命令通过 `ssh nas@192.168.1.100 '<command>'` 远程执行；
- 每步执行后必须运行文档中的“验证命令”确认成功，失败则按第 9 章故障排查处理后再继续；
- 使用文档中的国内镜像源与 ghcr 代理；配置文件按第 7 章原样写入；
- 涉及需要浏览器授权的步骤（Tailscale 登录、Immich 建管理员、iPhone 端）请停下并提示我手动完成；
- 不要修改文档范围以外的系统配置；关键配置改动前先备份（cp 原文件 .bak）。
完成后输出第 8 章“验证步骤”的实际结果与验收清单勾选情况。
```

### 步骤 4 — 一键幂等引导脚本（可选，供 Agent 调用）
- 做什么：把第 4–11 章的非交互部分固化为一个**可重复执行**的脚本，Agent 传到 NAS 运行即可。
- 为什么：脚本幂等、可重跑，比逐条 SSH 更稳；交互步骤（授权类）留给人工。
- 在主力机保存为 `bootstrap-nas.sh`，用 `scp` 传到 NAS 后执行：
```bash
#!/usr/bin/env bash
# bootstrap-nas.sh —— 家庭 NAS 自动搭建（非交互部分）。可重复执行。
set -euo pipefail

echo "==> [1/6] 更换 APT 国内源 + 系统更新"
sudo cp -n /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.bak 2>/dev/null || true
sudo sed -i 's@//.*archive.ubuntu.com@//mirrors.aliyun.com@g; s@//.*security.ubuntu.com@//mirrors.aliyun.com@g' \
  /etc/apt/sources.list.d/ubuntu.sources || true
sudo apt update && sudo DEBIAN_FRONTEND=noninteractive apt upgrade -y

echo "==> [2/6] 合盖不休眠"
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/; \
  s/^#\?HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/; \
  s/^#\?HandleLidSwitchDocked=.*/HandleLidSwitchDocked=ignore/' /etc/systemd/logind.conf

echo "==> [3/6] 安装 Docker（阿里云源）"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh -s -- --mirror Aliyun
fi
sudo usermod -aG docker "$USER" || true

echo "==> [4/6] 配置 Docker Hub 镜像加速"
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "registry-mirrors": ["https://docker.m.daocloud.io","https://dockerproxy.net","https://docker.1ms.run","https://hub.rat.dev"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
sudo systemctl daemon-reload && sudo systemctl restart docker

echo "==> [5/6] 安装 Tailscale（登录需人工授权）"
command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh

echo "==> [6/6] 准备 Immich（下载 compose + 改写 ghcr 代理）"
mkdir -p "$HOME/immich/library" "$HOME/immich/postgres" "$HOME/immich-app"
cd "$HOME/immich-app"
wget -qO docker-compose.yml https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml
wget -qO example.env       https://github.com/immich-app/immich/releases/latest/download/example.env
sed -i 's#ghcr.io/immich-app#ghcr.m.daocloud.io/immich-app#g' docker-compose.yml
[ -f .env ] || cp example.env .env
sed -i "s#^UPLOAD_LOCATION=.*#UPLOAD_LOCATION=$HOME/immich/library#" .env
sed -i "s#^DB_DATA_LOCATION=.*#DB_DATA_LOCATION=$HOME/immich/postgres#" .env
grep -q '^TZ=' .env && sed -i 's#^TZ=.*#TZ=Asia/Shanghai#' .env || echo 'TZ=Asia/Shanghai' >> .env

echo "==> 完成。请人工完成：静态 IP/DNS（如需）、tailscale up 授权、docker compose up -d、建管理员。"
```
传输并运行：
```bash
# 在【主力机】执行
scp bootstrap-nas.sh nas@192.168.1.100:~/
ssh nas@192.168.1.100 'bash ~/bootstrap-nas.sh'
# Docker 组权限需重登，随后启动 Immich：
ssh nas@192.168.1.100 'cd ~/immich-app && docker compose up -d'
```
> 静态 IP（7.2）与 DNS（7.3）建议**人工或让 Agent 单独处理并 `netplan try`**：远程改网络有断连风险，脚本默认未包含，避免把自己“锁在门外”。

### 步骤 5 — 人工完成的授权类步骤
- 做什么：由你在浏览器/手机上完成无法自动化的授权。
- 为什么：这些步骤涉及账号登录，Agent 无法代办。
```bash
ssh nas@192.168.1.100 'sudo tailscale up'      # 打开输出的链接，浏览器授权
```
- 浏览器打开 `http://192.168.1.100:2283` → 创建 Immich 管理员。
- 按第 11 章配置 iPhone 局域网加速、第 12 章开启 AI 并换中文模型。

### 步骤 6 — 收回临时权限（搭建完成后必做）
- 做什么：删除 sudo 免密，恢复正常安全策略。
- 为什么：避免长期保留免密 sudo 带来的风险。
```bash
ssh nas@192.168.1.100 'sudo rm -f /etc/sudoers.d/90-nas-agent'
```

### 安全注意
- Agent 的每条远程命令**应可审计**：让它输出实际执行的命令与返回结果。
- 只在**可信内网/Tailscale**内进行；切勿把 SSH 端口暴露公网。
- 破坏性命令（`rm -rf`、`docker compose down -v`、`netplan apply`）需人工确认后再执行。
- 搭建完成后，除删除 sudo 免密外，可考虑关闭密码登录、仅保留密钥登录（`/etc/ssh/sshd_config` 设 `PasswordAuthentication no`）。

---

## 附：验收检查清单

- [ ] Ubuntu 正常启动，`uname -m` 为 x86_64
- [ ] 固定 IP `192.168.1.100` 生效
- [ ] DNS 解析正常
- [ ] Docker / Compose 正常
- [ ] Tailscale 在线，手机可见 NAS
- [ ] Immich 4 个容器均 `Up`
- [ ] 局域网/Tailscale 均可访问 2283
- [ ] 手机照片自动上传成功
- [ ] 合盖后服务不中断
- [ ] 系统重启后服务自动恢复
- [ ] 局域网加速生效：连家里 WiFi 时 App 走内网 `192.168.1.100`，离开自动切 Tailscale `100.x.x.x`（第 11 章）
- [ ] 机器学习容器 `immich_machine_learning` 运行正常（第 12 章）
- [ ] 已换多语言 CLIP 模型并重建索引，**中文搜索可用**（如搜"海边"）（第 12 章）
- [ ] 人脸识别聚类正常，可为人物命名（第 12 章）

---

## 安全建议（重点）

- ❌ 不要将 2283 端口开放到公网
- ❌ 不要在路由器做端口映射
- ✅ 一律通过 **Tailscale 私有网络**访问
- 优点：无需公网 IP、无需 ICP 备案、无需 DDNS、安全性更高

---

## 后续扩展规划

```text
Ubuntu Server
│
├── Immich       照片管理
├── Nextcloud    文档同步
├── RustFS       固件与测试日志仓库
├── Gitea        私有 Git
├── Jenkins      自动化测试
└── Tailscale    远程访问
```

---

## 附：Watchtower 自动更新（可选，走国内镜像）

Watchtower 可自动检测并更新容器镜像。**镜像 `containrrr/watchtower` 托管在 Docker Hub，由 7.6 的 `registry-mirrors` 自动加速，无需额外改写。**

> ⚠️ **重要提醒**：Immich 官方**不建议**对 `immich-server` / `immich-machine-learning` 使用自动更新——跨大版本升级会执行不可逆的数据库迁移，自动更新可能导致数据损坏。**推荐做法**：让 Watchtower **只监控**并排除 Immich 容器，或仅用于开启了通知、由你手动确认后再更新。Immich 的更新请手动执行（见回滚方案第 ① 项），并在更新前备份。

### 部署方式（推荐：只更新非 Immich 容器）

Watchtower 默认更新所有容器。用**标签白名单**模式，只更新显式打了标签的容器，从而天然排除 Immich。

**方式 A — docker run（简单）**
```bash
docker run -d \
  --name watchtower \
  --restart always \
  -v /var/run/docker.sock:/var/run/docker.sock \
  containrrr/watchtower \
  --label-enable \
  --cleanup \
  --schedule "0 0 4 * * *"
```
参数说明：
- `--label-enable`：**仅更新**带 `com.centurylinklabs.watchtower.enable=true` 标签的容器（Immich 未打标签 → 不会被更新）。
- `--cleanup`：更新后删除旧镜像，节省磁盘。
- `--schedule "0 0 4 * * *"`：每天凌晨 4:00 检查（6 位 cron：秒 分 时 日 月 周）。

给"允许自动更新"的容器打标签示例（如某个无状态服务）：
```bash
docker run -d --name some-app \
  --label com.centurylinklabs.watchtower.enable=true \
  some-image:latest
```

**方式 B — 用 Compose 独立部署 Watchtower**
**文件路径**：`~/watchtower/docker-compose.yml`
```yaml
services:
  watchtower:
    image: containrrr/watchtower       # 由 daemon.json 镜像源加速拉取
    container_name: watchtower
    restart: always
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: >
      --label-enable
      --cleanup
      --schedule "0 0 4 * * *"
    environment:
      TZ: Asia/Shanghai              # 使 cron 按本地时间执行
```
启动：
```bash
mkdir -p ~/watchtower && cd ~/watchtower
nano docker-compose.yml            # 粘贴上方内容
docker compose up -d
```

### 显式排除 Immich（若你使用"更新全部"模式）
如果你不想用白名单、而是让 Watchtower 更新全部容器，则**必须**给 Immich 各容器加排除标签，在 `~/immich-app/docker-compose.yml` 对应服务下添加：
```yaml
    labels:
      - "com.centurylinklabs.watchtower.enable=false"
```
然后 `cd ~/immich-app && docker compose up -d` 重建生效。

### 验证与日志
```bash
docker ps | grep watchtower          # 确认容器运行中
docker logs watchtower               # 查看检查/更新记录
```
预期日志包含 `Scheduling first run` 及每次运行的 `Found N containers` / `Session done`。

### 回滚 Watchtower
```bash
# docker run 方式
docker rm -f watchtower
# 或 compose 方式
cd ~/watchtower && docker compose down
```
