# 容器运行环境开发入口

当前已提供三基础服务 Compose，并在 DS1621+ 上完成实体 SIM 的 SWu、IMS 注册和
NetworkManager 蜂窝数据承载验证。它仍是开发部署，DSM 驱动按严格版本清单持久化。
开发分支：`feat/108-container-runtime`。详细进度见
[设计与实测记录](../docs/design/issue-108-container-runtime.md)，正式发行资产、宿主驱动包与
升级回滚规则见[全容器版本发布方案](../docs/design/container-release-plan.md)。

## 准备和构建出口镜像

在装有 pip 的 Python 3 环境执行，版本与二进制 SHA256 从现有安装器读取：

```sh
python tools/container-runtime/prepare_egress_assets.py --arch amd64
```

产物保存在被 Git 忽略的 `runtime/vendor/amd64`，包括 sing-box、Xray、
Python 3.12 的 PyYAML wheel 和校验清单。ARM64 可准备资产，但尚未实机验证。

本机构建（需要 Docker）：

```sh
docker build -f runtime/Dockerfile.egress --build-arg TARGETARCH=amd64 \
  -t mdd-sim-gateway/egress:dev .
```

NAS 已有合适的 Python 3.12 glibc 镜像时，可从开发机远程构建：

```sh
python tools/container-runtime/build_remote_egress.py \
  --host USER@NAS --port SSH_PORT --identity /path/to/key \
  --base-image python:3.12.11-slim-bookworm
```

远程构建只传送白名单内代码和产物，锁定本地基础镜像 ID，构建网络为 none。
不会安装宿主机软件、发送项目数据目录或仓库凭据。

## 可重复的离线 UDP 实验

本地 Python 环境需要 PySocks（项目 control/requirements.txt 已包含）。

```sh
python tools/container-runtime/probe_udp.py \
  --host USER@NAS --port SSH_PORT --identity /path/to/key \
  --image mdd-sim-gateway/egress:dev \
  --client-image mdd-sim-gateway/engine:dev \
  --singbox runtime/vendor/amd64/bin/sing-box
```

脚本创建临时容器和两个内部网络，测试成功路径、真实 Engine 内的 SWu socket、直接访问阻断、
出口中断、既有会话失效、上游中断和恢复，并在结束时清理。
使用已存在的镜像，不拉取镜像，不发布端口，不使用真实代理凭据或运营商流量。

`host_routes_rules_restored: true` 代表实验前后路由表和策略规则一致；
创建 Docker 网络期间仍会存在 Docker 自身生成的 bridge、路由与隔离规则。

## 单独运行开发出口

`compose.egress-lab.yaml` 只用于验证出口服务。使用独立数据目录，
确保目录 UID/GID 和权限允许容器写入（默认 UID 0、cap_drop ALL；
不能依赖 root 绕过其他用户的 0700 目录权限）。不要挂载生产数据。

```sh
MDD_LAB_DATA=/absolute/dedicated/data \
  docker compose -f runtime/compose.egress-lab.yaml up -d --no-build
```

期望配置文件为 `/data/orchestrator/desired.json`，沿用原有 `proxy` 配置结构。
无配置时发布 disabled 心跳，不启动代理。国家端口沿用项目现有分配规则，
GB 是 22157。SOCKS 端口仅供项目网络内受信客户端使用，没有发布到 NAS 端口。
外部代理域名由 Egress 的正常 DNS 解析；DNS 经国家出口的完整策略尚待接入。

Control 的后续部署应设置：

- `MDD_ENGINE_NETWORK`：Control 和 Engine 都加入的专用 bridge 网络名。
- `MDD_MANAGER_URL`：该网络内可访问的 Control URL，不依赖 NAS 端口回流。
- `MDD_HOST_DATA`：Docker daemon 可见的数据目录绝对路径。
- `MDD_HOST_PCSCD_DIR`：Docker daemon 可见的共享 PC/SC socket 目录。

Control 已实现实验性 `MDD_EGRESS_TRANSPORT=socks5` 状态检查（默认 `host`）。
它要求版本匹配、15 秒内心跳、当前代理配置指纹、对应国家出口 ready。
配置失配或状态过期不会放行；只有显式选择 direct 的国家可以直连。
代理进程 ready 尚不代表 UDP 或运营商可达。

启动代理线路前还检查 Engine 镜像标签
`io.mdd-sim-gateway.egress-transports` 是否包含 `socks5`，然后使用核验过的镜像 ID
创建容器。旧镜像会被拒绝，已有容器不会因此被删除。
Engine 已接入该传输并声明 `socks5` 能力：IKE/500 和 NAT-T/4500 使用独立 association，
代理模式不创建 raw ESP socket；PMTU 计算计入 SOCKS5 UDP 的 10 字节 IPv4 目标头。
仍应使用当前源码构建的镜像，不能给旧镜像手工补标签绕过检查。

可从已经加载到 NAS 的发行版 Engine 离线构建当前运行层：

```sh
python tools/container-runtime/build_remote_engine.py \
  --host USER@NAS --port SSH_PORT --identity /path/to/key
```

## Hardware 镜像与实体模块

远程构建只发送 Dockerfile、主管与现有 bridge 源码。默认使用官方 Debian 源；
网络较慢时可以显式指定镜像：

```sh
python tools/container-runtime/build_remote_hardware.py \
  --host USER@NAS --port SSH_PORT --identity /path/to/key \
  --debian-mirror https://mirrors.tuna.tsinghua.edu.cn/debian \
  --debian-security-mirror https://mirrors.tuna.tsinghua.edu.cn/debian-security
```

若 NAS 上已有核验过的 Hardware 镜像，只需迭代主管和蜂窝依赖，可用 overlay 构建
避免重复编译 PC/SC：

```sh
python tools/container-runtime/build_remote_hardware_overlay.py \
  --host USER@NAS --port SSH_PORT --identity /path/to/key \
  --base-image mdd-sim-gateway/hardware:VALIDATED_TAG \
  --tag mdd-sim-gateway/hardware:dev
```

源码压缩包始终按 Dockerfile 中的 SHA256 校验。`compose.hardware-lab.yaml`
使用共享 pcscd volume；Control 与 Engine 后续挂载同一 volume 即可访问虚拟卡。
Hardware 以显式 capability 运行 NetworkManager，只允许它管理 `wwan*` 与
`cdc-wdm*`；启动时发现任何其他接口被认领即停止。GSM profile 始终为
`connection.autoconnect=no`、IPv4/IPv6 `never-default=yes`，因此不会替换 NAS 默认
出口。实体 USB 读卡器直接由 Hardware 内的 pcscd 和带项目修复的 libccid 管理，宿主
不需要安装 PC/SC 服务。DSM 必须先提供匹配内核的串口和 QMI 驱动；当前 DS1621+ 验证版本见
`synology-v1000-7.4-modules.json`。

对清单中的 DS1621+ / DSM 7.4.1-90080，使用随 Release 发布的驱动包安装（见部署指南第 2 节）。
本地重建驱动包：`sh tools/drivers/build-synology-pack.sh dist`（Linux x86_64，需要写 `/work`）。
加载器每次启动都检查架构、内核、DSM build、平台标记和全部模块 SHA256；任一项不符
即拒绝加载。DSM 升级后必须重新构建和验证，不能绕过版本检查。

## 晚上插入设备后

从开发机执行只读探测：

```sh
ssh -i /path/to/key -p SSH_PORT USER@NAS 'sh -s' \
  < tools/container-runtime/probe_hardware.sh
```

检查 USB VID/PID、串口/QMI/MBIM 节点、已加载驱动和潜在占用服务。
脚本不读取 SIM 标识、不打开串口、不发送 AT 命令、不加载驱动、不停止服务。
先确认宿主驱动能够创建需要的设备节点，再进行 Hardware 容器验证。

## 三容器开发部署

先构建当前 Control、Engine、Hardware 和 Egress 镜像，再使用绝对数据目录启动：

```sh
MDD_DATA_DIR=/volume1/docker/mdd-sim-gateway-dev \
MDD_ADVERTISE_ADDR=10.0.0.100 \
MDD_HTTP_PORT=10443 \
MDD_NAS_HOSTNAME=nas.example.com \
MDD_IMAGE_TAG=v1.12.0 \
docker compose -f runtime/compose.yaml up -d --no-build
```

正式 Release 会额外发布 `mdd-sim-gateway-compose-vX.Y.Z.yaml`，其中四个 GHCR 镜像已经
固定为对应 Release 标签。群晖用户可以把该 YAML 直接粘贴到 Container Manager 新项目，
把文件开头标出的示例 LAN 地址改成自己的 NAS 地址，按需修改数据目录、管理端口和 NAS
域名映射后启动；不需要额外的 `.env` 或应用安装脚本。只有宿主没有生成所需设备节点时，
才需要在宿主单独安装完全匹配的驱动；DS1621+ 的驱动包随 Release 发布。

常驻容器为 Control、Hardware、Egress，加每条已启用线路一个 Engine，即 `3 + N`。
Control 只发布管理端口；Hardware 和 Egress 不发布宿主端口。Engine 的国家 SOCKS
线路只连接内部网络；明确 direct 的线路才额外连接 `mdd-sim-gateway-uplink`。
PC/SC socket 使用固定命名卷 `mdd-sim-gateway-pcscd` 在 Hardware、Control 和动态
Engine 之间共享。容器部署默认从 UDP 30000 分配 RTP，避开 NAS 上常见的 10000 段冲突。

宿主管理端口默认使用 `10443`，容器内 Control 仍监听 `8443`：
`https://NAS地址:10443/`。第一次访问由用户设置管理员口令。
若代理订阅由 NAS 自己提供，`MDD_NAS_HOSTNAME` 会把该域名仅在 Egress 容器内映射到
Docker 宿主网关，保留原 URL 和 HTTP Host 头，避免公网回环失败。
