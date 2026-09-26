<p align="center">
  <img src="assets/logo-lockup.svg" width="520" alt="MDD Sim Gateway">
</p>

<p align="center"><strong>把物理 SIM 和 eSIM 变成自己可控的 VoWiFi、通话、短信与独立网络出口网关。</strong></p>

<p align="center">
  <a href="README.en.md">English</a> ·
  <a href="#快速安装">快速安装</a> ·
  <a href="docs/ARCHITECTURE.md">架构</a> ·
  <a href="docs/INSTALL.md">安装文档</a> ·
  <a href="docs/CONTAINER_DEPLOYMENT.md">容器部署</a> ·
  <a href="https://github.com/MddIdd/mdd-sim-gateway/discussions">社区讨论</a>
</p>

MDD Sim Gateway 是自托管的多 SIM 通信网关，可以直接安装在 Debian / Ubuntu / Armbian ARM64 主机上，也可以在任何能运行 Docker Compose 的 Linux 主机上以全容器方式运行，包括群晖等 NAS。它将蜂窝模块、USB 读卡器、IMS、EAP-AKA、eSIM、ModemManager 和 sing-box 整合进一个中英文 Web 控制台。

| 真实 SIM 鉴权 | 通话与短信 | 多模块管理 | 独立国家出口 |
|---|---|---|---|
| 在物理 SIM/eSIM 内完成 EAP-AKA 与 IMS-AKA，不读取 Ki/OP/OPc | 浏览器软电话、短信收发、通话记录与来电通知 | 统一管理蜂窝模块、PC/SC 读卡器和 eUICC | 每张 SIM 的 ePDG 流量走所选国家出口（宿主安装用独立 TUN，全容器用 SOCKS5），出口不通时不泄漏 |

## 界面导览

![MDD Sim Gateway 中文界面导览（使用虚构演示数据）](assets/product-tour.zh-CN.gif)

<p align="center">概览 → 设备管理 → 浏览器通话 → 短信 → 余额与保号 → 系统更新　·　界面中的身份与内容均为虚构演示数据</p>

## 快速安装

有两种部署方式，功能相同，按宿主机选择：

| | 宿主安装 | 全容器部署 |
|---|---|---|
| 适用 | 树莓派等 Debian / Ubuntu / Armbian ARM64 主机 | 任何能运行 Docker Compose 的 amd64/arm64 Linux，包括群晖等 NAS |
| 宿主上安装什么 | systemd 服务；安装器配置 pcscd、ModemManager、NetworkManager | 除 Docker 外不安装任何项目软件，全部服务都在容器内 |
| 常驻容器 | 每条线路一个 Engine | Control、Hardware、Egress 三个基础容器，加每条线路一个 Engine |
| 国家出口 | 每国独立 TUN 与 ePDG 路由 | 每国 SOCKS5 入口，不修改宿主路由表和 DNS |
| 管理地址 | `https://<网关地址>:8443` | `https://<主机地址>:10443` |
| 状态 | 正式版 | 正式版，已在群晖 DS1621+ 和树莓派实机验证 |

### 方式一：宿主安装

推荐使用具备 systemd、Docker、USB 和稳定网络的 Debian、Ubuntu 或 Armbian ARM64 主机。

存储要求：根文件系统安装前至少应有 **2 GiB 可用空间**；建议使用 **8 GB 或更大**的系统盘，
并在升级前保留约 **3 GiB**，以同时容纳新镜像和一代回滚镜像。实测占用：Engine 镜像 607 MB，
两代镜像共享基础层后合计约 676 MB，源码 checkout 与虚拟环境约 102 MB；Docker 控制面模式
另需 355 MB。**显式源码构建是例外**——在设备上编译 Asterisk 需要额外数 GiB 的构建缓存与
中间产物，与上述数字无关，不适合空间紧张的设备（正常安装与升级都直接下载 CI 构建的镜像，
不会走这条路径）。虚拟机只扩大虚拟硬盘还不够，必须同步
扩展根分区和文件系统，并以 `df -h /` 显示的容量为准。

```bash
git clone https://github.com/MddIdd/mdd-sim-gateway.git
cd mdd-sim-gateway
sudo ./install.sh install
```

安装完成后访问 `https://<网关地址>:8443`，并在受信的局域网或 VPN 中立即创建管理员账号。完整的前置检查、安装过程和升级方式见 [安装与升级](docs/INSTALL.md)。

### 方式二：全容器部署（Docker Compose）

除 Docker 外不在宿主上安装任何项目软件，也不需要运行安装脚本。宿主需要是 Linux：Docker Desktop
（macOS/Windows）无法把 USB 设备交给容器，不能使用；rootless Docker 也不支持。

1. **确认宿主已识别蜂窝模块。**插入模块后宿主应出现 `/dev/ttyUSB*`、`/dev/cdc-wdm*` 和
   `wwan*` 网卡；普通 PC/SC 读卡器只需出现在 `/dev/bus/usb`。节点缺失说明宿主缺少内核驱动，
   主流发行版内核一般自带所需驱动；群晖等精简内核可能缺失，DS1621+ 可使用随 Release 发布的驱动包，
   见 [兼容性与驱动目录](drivers/README.md)。
   宿主若已运行 ModemManager（Ubuntu 等发行版默认启用），先停用，否则它会和容器抢占模块。
2. **下载 Compose 文件。**从 [Releases](https://github.com/MddIdd/mdd-sim-gateway/releases) 下载
   `mdd-sim-gateway-compose-vX.Y.Z.yaml`，四个镜像已固定为该版本。
3. **修改文件开头标出的两项。**`MDD_ADVERTISE_ADDR` 改成主机的局域网地址（浏览器通话的媒体
   地址）；数据目录改成你的实际路径，文件中的 `/volume1/docker/mdd-sim-gateway` 是群晖的示例。
   管理端口默认 `10443`。
4. **启动。**普通 Linux 把文件保存为数据目录下的 `docker-compose.yml`，在该目录执行
   `docker compose up -d`；群晖在 Container Manager 中新建项目并粘贴 YAML。
5. **打开 `https://<主机地址>:10443`**，立即创建管理员账号。

启动或重建 Hardware 时，它可能要先重置一次蜂窝模块，约一到两分钟后才变为健康，属于正常现象。四个镜像
解压后每代约 1.3 GB，一键更新时新旧两代并存，请为 Docker 存储保留至少 6 GiB。更新、回滚和
问题排查见 [全容器部署指南](docs/CONTAINER_DEPLOYMENT.md)。目前实机验证过的是群晖 DS1621+，
其他主机的验证记录会补充到 [兼容性目录](drivers/README.md)。

> 本项目直接控制蜂窝模块、SIM、网络路由和 IMS。运营商是否开放 Wi‑Fi Calling 仍取决于套餐、区域、设备身份和网络策略。

## 系统架构

![MDD Sim Gateway 系统架构](docs/architecture.svg)

## 完整截图

<details>
<summary>查看概览、设备、通话、短信、余额与保号及系统更新页面</summary>

![MDD Sim Gateway 中文概览（使用虚构演示数据）](screenshots/overview-redacted.zh-CN.png)

![MDD Sim Gateway 中文设备页（使用虚构演示数据）](screenshots/devices-redacted.zh-CN.png)

![MDD Sim Gateway 中文通话页（使用虚构演示数据）](screenshots/calls-redacted.zh-CN.png)

![MDD Sim Gateway 中文短信页（使用虚构演示数据）](screenshots/sms-redacted.zh-CN.png)

![MDD Sim Gateway 中文余额与保号页（使用虚构演示数据）](screenshots/keepalive-redacted.zh-CN.png)

![MDD Sim Gateway 中文系统更新页（使用虚构演示数据）](screenshots/settings-redacted.zh-CN.png)

</details>

## 核心能力

- 自动识别蜂窝模块与普通 PC/SC 读卡器；模块可同时管理 4G 和 VoWiFi，读卡器仅显示其支持的 VoWiFi 能力。
- 每个物理模块独立保存 4G、飞行模式和 VoWiFi 期望状态：4G 开关只控制移动数据承载，飞行模式单独控制射频，VoWiFi 独立启停；状态按各自 ModemManager 对象读取。
- 在“余额与保号”页统一查看余额、套餐到期、在线状态和保号结果；预付费线路可定时发送一条真实计费短信，套餐线路可监测续费余额并在不足时提醒。
- 后台每 6 小时检查新版本；可选择自动更新或仅提示更新。“全部版本”跟随获准推送的最新 Release，“仅主版本”跟随独立配置的稳定主版本，即使已有更新补丁也能补装该主版本。无人值守安装仍须由 `update-policy.json` 明确许可具体版本和最早执行时间。
- 使用物理 SIM/eSIM 完成 EAP-AKA 与 IMS-AKA；不读取、不保存 Ki/OP/OPc，也不使用演示鉴权向量。
- DITO Telecommunity（515-66）使用该 ePDG 公布的 AES-CBC-128/HMAC-SHA1/MODP-1024
  IKE 套件；该旧套件仅作用于该 PLMN，其他运营商仍使用现有的 MODP-2048 强套件。
- 按 RFC 4187 响应 EAP-AKA 的永久/完整身份请求，兼容要求此标准身份流程的运营商。
- 自动读取 IMSI、ICCID、MCC/MNC、SIM SPN/GID 和模块 IMEI；使用内置 AOSP Carrier ID 数据离线识别宿主网络与部分 MVNO，PIN 开启时仅在本机加密边界内使用。
- 每张模块 SIM 显式展示三条逻辑通道的容量、实际分配、用途和错误；部分分配失败会主动释放已打开通道。
- 登录后使用的浏览器软电话、短信与彩信收发、通话记录、未接来电通知和可按线路启用的本地语音留言；录音只保存在网关，不随通知或支持包发送；不开放独立 SIP 客户端接入。
- 可建立包含多个订阅、具体节点和 SOCKS5 的代理库，再为国家出口复用其中一项；Reality/XHTTP 节点由 Xray-core 兼容层承载，其余节点与国家 TUN 由 sing-box 管理。候选节点必须通过 UDP 健康检查，失败时按 SIM 故障关闭，不泄漏到错误国家。
- 标准 GET/POST Webhook、Telegram（直连/手动代理/国家出口）、PushPlus 和多个飞书/Lark
  自定义机器人（支持可选签名校验及按 SIM 线路路由）；四类通道都可按
  事件自定义标题与正文模板，并提供预览及对应事件的测试推送。
- Telegram 仅用于单向推送来电、短信和设备状态通知，不接受远程控制指令。
- 使用 lpac 管理 eUICC 配置文件；支持需要显式选择安全元件的双 SE 卡。
- 中英文界面、HTTPS、首次管理员设置、可持久化的 12 小时或 30 天会话登录、CSRF、防暴力登录、审计记录、脱敏支持包、备份与版本检查。

## 硬件模型

| 设备 | 4G 数据 | Wi‑Fi Calling | SIM 访问方式 |
|---|---:|---:|---|
| 支持 ModemManager 的蜂窝模块 | ✓ | ✓ | 模块 AT/逻辑通道桥接 |
| 大疆/Quectel EC25 类模块 | ✓ | ✓ | 自动识别并创建所需虚拟读卡通道 |
| USB PC/SC 读卡器 | — | ✓ | 直接 PC/SC |
| 三体电子 SCR Prime（`04d9:c001`） | — | ✓ | 直接 PC/SC；安装时使用 `patchprime` 驱动补丁 |
| eUICC/eSIM 读卡器 | — | ✓ | PC/SC + lpac |

三体电子 SCR Prime 已通过本项目实机验证；“支持”表示系统具备相应技术路径，不代表所有 SIM、固件或运营商都会放行。多模块 4G 使用独立 ModemManager 对象、NetworkManager 连接和 bearer。


## 安装器会做什么

以下只适用于宿主安装；全容器部署不运行安装脚本。安装脚本会自动：

1. 检查并复用现有系统 Docker（没有时才从发行版安装），安装 pcscd、ModemManager/NetworkManager；
2. 按架构下载 sing-box 1.13.15 与 Xray-core 26.3.27 并验证 SHA-256；
3. 下载固定版本 lpac 2.3.0 源码并本地构建；
4. 构建 MDD 控制面、WebUI 与每 SIM VoWiFi 引擎；
5. 安装 systemd 服务并设置开机启动。

已有 Docker 不会被升级、重配或清理；安装前会检查 rootless 模式、端口占用与容器归属，只管理带 MDD 标记的容器。

常用命令：

```bash
sudo ./install.sh status
sudo ./install.sh logs
sudo ./install.sh reload
sudo ./install.sh build-lpac
sudo ./install.sh uninstall
```

完整说明见 [安装与升级](docs/INSTALL.md)，系统边界见 [架构说明](docs/ARCHITECTURE.md)，问题排查见 [故障排查](docs/TROUBLESHOOTING.md)。参与开发前请先读 [开发与协作规范](docs/DEVELOPMENT.md)。

## 使用边界

> **合规警告：** 本软件仅供号码实名持有人在运营商明确允许的范围内自用。严禁用于诈骗、群呼、营销骚扰、验证码接收、号码或线路出租、代拨转接、隐藏实际控制地点，或向第三人提供电信服务。使用者必须遵守所在地法律、电话实名制和运营商协议；本项目不构成任何电信业务许可或运营商授权。MDD Sim Gateway 最多保存和运行 **10 条 SIM 线路**，不提供独立 SIP 账号或 Telegram 远程拨号、发短信及挂断功能。技术限制不代表某种使用方式当然合法。

## 社区与反馈

- 安装、硬件和运营商兼容性讨论：[GitHub Discussions](https://github.com/MddIdd/mdd-sim-gateway/discussions)
- 可复现的缺陷或明确的功能请求：[GitHub Issues](https://github.com/MddIdd/mdd-sim-gateway/issues/new/choose)
- 参与代码或文档贡献：[CONTRIBUTING.md](CONTRIBUTING.md)

如果项目对你有用，欢迎在 GitHub 上收藏它，并分享经过脱敏的硬件或运营商兼容性结果。

## 国家出口如何工作

先在“网络出口”的代理库中添加一个或多个订阅、具体节点或 SOCKS5 代理，再为 SIM 国家选择出口。订阅模式继续用关键词匹配**节点名称**并允许自动/固定节点；选择具体节点或 SOCKS5 时则直接使用该项。Reality/XHTTP 分享链接通过本机回环上的 Xray-core 接入，不向局域网开放端口。界面中的眼睛开关默认关闭，订阅地址、节点链接和 SOCKS5 信息均以星号遮挡。

系统对所有非直连出口额外验证 UDP 能力，因为 IKEv2/ESP NAT 穿越依赖 UDP 500/4500。每个国家使用独立 TUN（例如 `mdd-jp`），只有对应 SIM 的 ePDG 路由进入该接口。

## 安全与隐私

- 管理端默认 HTTPS，首次设置管理员密码，密码使用 scrypt 加盐保存。
- 会话 Cookie 为 HttpOnly/Secure/SameSite=Strict；修改请求要求 CSRF 令牌。
- 引擎事件使用安装级随机令牌，不接受未认证回调。
- 支持包会移除 IMSI、ICCID、EID、号码、PIN、Token、URL、激活码、密钥与消息正文；分享前仍应人工复核。
- 运行数据目录只允许 root 访问，含凭据的配置与线路文件以 `0600` 权限原子写入。
- 不提供 Ki/OP/OPc 输入或软件 Milenage 路径，AKA 密钥留在 SIM/eSIM 内。
- 订阅 URL、通知 Token、SIM PIN 和运营商身份属于敏感数据；不要提交 `data/`、`.env` 或真实截图。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，数据处理边界见 [PRIVACY.md](PRIVACY.md)。

## 开源与致谢

MDD Sim Gateway 以 **GPL-3.0-only** 发布。它包含或调用多个独立上游组件，各自仍遵循原许可证；完整清单见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) 和 [NOTICE](NOTICE)。

特别感谢：

- [pagecat/vowifi_gateway](https://github.com/pagecat/vowifi_gateway)：本项目的上游基础（MIT）——VoWiFi 引擎与管理端/引擎/WebUI 的整体架构源自该项目；本项目在其之上增加了 4G 蜂窝数据与短信、按国家的网络出口路由、统一设备管理与自动开通、故障转移以及测试体系；
- [fasferraz/SWu-IKEv2](https://github.com/fasferraz/SWu-IKEv2)：SWu IKEv2/IPsec 基础实现；
- [phcoder/asterisk-docker](https://github.com/phcoder/asterisk-docker) 与 [sysmocom Asterisk](https://gitea.sysmocom.de/sysmocom/asterisk)、[sysmocom pjproject](https://gitea.sysmocom.de/sysmocom/pjproject)：IMS-AKA、语音和短信；
- [mitshell/card](https://github.com/mitshell/card)：USIM/PCSC 辅助代码；
- [SagerNet/sing-box](https://github.com/SagerNet/sing-box)：国家代理出口；
- [estkme-group/lpac](https://github.com/estkme-group/lpac)：eSIM LPA；
- [LudovicRousseau/PCSC](https://github.com/LudovicRousseau/PCSC)、[CCID](https://github.com/LudovicRousseau/CCID) 与 [pyscard](https://github.com/LudovicRousseau/pyscard)：智能卡基础设施；
- [frankmorgner/vsmartcard](https://github.com/frankmorgner/vsmartcard)：虚拟 PC/SC 驱动（vpcd），4G 模组 SIM 槽位的基础。

本项目不是上述项目、运营商或设备厂商的官方产品，也不受其背书。
