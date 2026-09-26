# 全容器版本发布方案

本文定义 issue #108 完成后的正式发布形态。目标是让普通 Linux、树莓派和 NAS 使用
同一套容器镜像，同时把必须依赖宿主内核的部分限制为可审计的可选驱动包。

## 0. 实现状态（截至 v1.12.0）

本文描述的是**目标形态**，不是当前代码的能力。发布或撰写用户文档前请先看这一节。

已经实现：

- §3 的四种镜像、版本化 Compose 项目文件、八个离线镜像包，以及覆盖它们的统一
  `SHA256SUMS`（`.github/workflows/release.yml`）。
- §6 的下载校验、更新前备份、保留用户 Compose 编辑、基础容器与 Engine 的滚动替换，以及
  失败时恢复上一份 Compose、基础镜像和 Engine（`host/mdd_container_update.py`）。
- §7 中的 Compose 权限断言与非 privileged 检查（`tests/test_container_compose.py`）。

- §3 中 DS1621+ 的驱动包：随每个 Release 发布并纳入 `SHA256SUMS`。CI 用
  `tools/drivers/build-synology-pack.sh` 从固定输入（群晖公开工具链、未修改的 Linux v4.4.302
  源码）重建，任一模块与实机验证值不同即失败；打包可复现，兼容目录固定其 SHA-256。

**尚未实现**，不得在用户文档或发布说明中描述为可用：

- §4 的驱动目录作为 Release 资产和自动下载。`drivers/catalog/` 仍只供人工查阅和 CI 校验，
  没有运行时消费者；`--driver-bundle` 与 `MDD_DRIVER_INDEX_URL` 不存在。驱动包是 tar.gz，
  按部署指南用一次 SSH 手动安装，不是 SPK。
- §5 第 3–5 步的启动硬件预检，以及 `host-native` / `driver_required` 状态。代码中不存在
  这两个状态，WebUI 也不展示兼容键。当前实际行为是：缺少设备节点时 Hardware 容器保持
  unhealthy，Control 因 `depends_on` 不会启动。
- §3 与 §6 中按镜像 digest 验证和回滚。Compose 引用的是版本标签，回滚恢复的是上一份
  Compose 文本；GHCR 标签被移动的情形目前没有防护。
- §6 更新门槛中除 Hardware 健康检查以外的五条：NetworkManager 接管范围、NAS 默认路由
  一致、读卡器与 modem 可见性、Egress 状态指纹、线路 SWu/IMS 恢复。更新器只等待容器
  达到 running/healthy，不校验 IMS 是否重新注册。
- §7 中 Hardware 与 Egress 镜像的 CI 构建、Hardware 内 pcsc-lite/libccid 补丁检查，以及
  `tools/validate_nas_catalog.py` 的 CI 执行。这三项目前只在打标签发布时或本地运行。

## 1. 支持边界

USB 枚举成功不等于设备已经可用。安装器按下面三层判断：

1. **USB 层**：设备出现在 `/sys/bus/usb/devices`。
2. **内核驱动层**：蜂窝模块已经生成 `ttyUSB/ttyACM`、`cdc-wdm` 和 `wwan`
   所需节点；普通 CCID 读卡器已经生成 `/dev/bus/usb` 节点。
3. **用户态协议层**：Hardware 容器内的 ModemManager、NetworkManager、pcscd、
   libccid 和 SIM bridge 能够打开设备。

宿主已经满足第二层时，不安装任何项目驱动。大多数通用 Linux 和树莓派应走这条路径：
发行版内核负责 USB 串口、QMI/MBIM 和 USB 核心支持，所有用户态服务都在 Hardware
容器内运行。

只有设备已枚举、但目标接口没有绑定内核驱动且没有生成所需节点时，安装器才查询驱动
目录。首个正式支持的外部驱动目标限定为已经实测的 DS1621+、v1000、x86_64、
DSM 7.4.1-90080、内核 4.4.302+。其他 Synology 型号或 DSM build 不复用该包。

## 2. 实体读卡器归属

Pi 上使用的实体 USB 读卡器迁移到 NAS 后由 Hardware 容器直接管理，宿主不安装 pcscd
或 libccid。Hardware 镜像固定包含 pcsc-lite 2.3.3、libccid 1.6.2 和项目已有的三项
设备修复：

- HSIC `1d99:0016` 卡在位检测；
- HSIC 缺失 ATR TCK 的修复；
- SCR Prime `04d9:c001` 设备表支持。

Compose 只向 Hardware 开放 USB 字符设备 major 189。Hardware 的 pcscd 同时发布实体
读卡器和蜂窝模块产生的 VPCD reader，Control 与 Engine 继续通过命名卷中的 PC/SC
socket 使用它们。实体读卡器只显示 VoWiFi/eSIM 能力，不显示 4G、飞行模式或蜂窝信号。

如果宿主已有 pcscd 占用读卡器，预检应报告冲突并停止，不自动结束宿主服务。发行版安装
不再要求宿主 pcsc-lite 版本与容器一致。

## 3. 发布资产

每个版本发布以下资产，镜像标签使用完整版本，不发布 `latest`：

| 资产 | 架构 | 用途 |
| --- | --- | --- |
| Control 镜像 | amd64、arm64 | WebUI、API、状态、lpac 和 Engine 编排 |
| Hardware 镜像 | amd64、arm64 | D-Bus、ModemManager、NetworkManager、PC/SC、CCID、SIM bridge |
| Egress 镜像 | amd64、arm64 | 国家出口和 SOCKS5 TCP/UDP |
| Engine 镜像 | amd64、arm64 | 每条线路的 SWu、IMS、语音和短信引擎 |
| Compose 项目文件 | 架构无关 | 已写入本版本镜像标签、可粘贴到容器管理器的 `compose.yaml` |
| 离线镜像包 | 每种架构 | 无法访问 GHCR 时由 `docker load` 导入四个镜像 |
| 驱动目录 | 架构无关 | 可选宿主驱动包的兼容键、URL、大小和 SHA-256 |
| 驱动包 | 按宿主精确组合 | `.ko`、加载顺序、兼容信息、许可证和对应源码/构建说明 |

四个镜像同时发布到 GHCR，并在 Release 中提供压缩归档。Release workflow 从仓库模板生成
写死当前标签的 Compose 项目文件，不使用 `latest` 或开发标签。Control 在首次启动时记录
镜像 digest；更新过程中按 digest 验证，不能因标签移动换入其他内容。

常驻数量仍为 `3 + N`：Control、Hardware、Egress 加每条启用线路一个 Engine。

## 4. 驱动目录与下载

不能只配置一个任意 `.ko` 下载地址。驱动选择必须经过一个随 Release 校验的目录，例如：

```json
{
  "schema": 1,
  "packs": [{
    "id": "synology-v1000-dsm7.4.1-90080-k4.4.302-plus-x86_64",
    "match": {
      "os": "synology-dsm",
      "architecture": "x86_64",
      "platform": "v1000",
      "product": "DS1621+",
      "dsm": "7.4.1-90080",
      "kernel_release": "4.4.302+"
    },
    "asset": "mdd-driver-synology-v1000-dsm7.4.1-90080-k4.4.302-plus-x86_64.tar.gz",
    "sha256": "<release checksum>",
    "size": 0
  }]
}
```

默认目录随 Release 发布，资产从同一个 GitHub Release 下载。允许两种显式覆盖：

- `--driver-bundle /absolute/path/to/bundle.tar.gz`：离线安装或内部审计后的驱动包；
- `MDD_DRIVER_INDEX_URL=https://.../driver-index.json`：企业镜像站，目录本身仍需匹配
  Release 中固定的 SHA-256。

不提供“忽略平台/内核版本”开关。没有精确匹配时，安装器输出脱敏后的宿主兼容键和缺失
节点，继续部署管理面但把 Hardware 标记为 `driver_required`，不会尝试附近版本。

驱动包解压前检查路径穿越、文件类型、大小和整体 SHA-256；安装后再次检查每个模块的
SHA-256、`vermagic`、架构和允许的模块名。启动脚本每次开机重复检查 DSM build、产品、
平台和内核，任何一项变化都不加载。DSM 升级后旧包保留但停用，直到目录提供新精确包。

## 5. 安装流程

正式容器版以容器管理器的 Compose 项目作为安装入口，不要求先下载或执行项目安装脚本，
也不复用当前会在普通 Linux 宿主安装 pcscd、ModemManager 和 NetworkManager 的本地模式：

1. 用户从 Release 复制当前版本的 Compose YAML，在 Container Manager 中新建项目。发布
   文件自带群晖常用数据目录和示例 LAN 地址；用户在 YAML 顶部说明指引下修改 NAS LAN
   地址，按需修改数据目录、管理端口和 NAS 域名映射，不需要另建 `.env` 文件。
2. Container Manager 检查 Compose、端口和数据目录，拉取四个同版本镜像；离线环境可先
   导入同一 Release 的本机架构镜像包。
3. Control 启动后运行只读硬件预检，记录 USB VID/PID、接口绑定和所需节点，不读取 IMSI、
   ICCID、IMEI、EID 或 USB 序列号。
4. 若宿主已经生成所需节点，状态显示 `host-native`，无需宿主安装项目软件。
5. 若缺少节点，状态显示 `driver_required` 和脱敏兼容键。只有这时管理员才从同一 Release
   取得精确匹配的驱动包并在宿主执行一次安装；没有匹配项时不尝试相近包。
6. Control 核对四个镜像的架构、版本 label 和 digest；Control 镜像必须自带
   固定版本且已应用读卡器选择补丁的 lpac，宿主不再构建或安装 lpac。
7. Compose 创建三个基础服务。Hardware 健康后 Control 才启动，
   已启用线路由 Control 创建对应 Engine。
8. 管理面验收默认路由、NetworkManager 接管范围、读卡器列表、蜂窝模块和国家出口。

普通安装只在宿主留下数据目录和 Container Manager 保存的项目配置。确实缺少内核节点时，
才额外留下精确驱动包和启动脚本。NAS 用户应先安装厂商的 Container Manager；项目不负责
安装 Docker，也不要求通过 SSH 运行应用安装脚本。

## 6. 更新与回滚

更新先下载新资产并验证，不覆盖当前运行标签。停止新 Engine 创建后，备份数据目录中的
配置和数据库，依次更新 Egress、Hardware、Control；已有 Engine 在 Control 确认新版本
健康后按线路滚动重建。

更新门槛包括：

- Hardware 私有 D-Bus、pcscd、ModemManager 和 NetworkManager 健康；
- NetworkManager 没有接管任何非 `wwan/cdc-wdm` 接口；
- NAS 默认路由与更新前一致；
- Control 能看到预期读卡器和 modem；
- Egress 状态版本与配置指纹一致；
- 已启用线路恢复 SWu/IMS，或明确报告运营商侧故障。

任一基础服务未通过门槛时，Compose 回到上一组 digest。驱动包不在普通应用更新中替换；
只有宿主兼容键完全一致且新包已通过单独验收时才升级。卸载默认保留数据，移除项目容器、
网络和卷；驱动包使用独立命令移除，避免应用卸载过程中强拆正在使用的内核模块。

## 7. CI 与发布门槛

Release workflow 在原生 amd64、arm64 runner 分别构建四个镜像，并执行：

- 全量 Python 与 WebUI 测试；
- 镜像架构、版本、入口、healthcheck、OCI label 和依赖检查；
- Compose 渲染与权限断言，Hardware 必须保持非 privileged；
- Hardware 镜像内 pcsc-lite、libccid 三项补丁、ModemManager 和 NetworkManager 检查；
- 无实体硬件的 USB/PCSC、D-Bus、QMI 状态契约测试；
- 订阅者标识符与私钥扫描；
- 镜像归档、版本化 Compose 项目文件、驱动目录和驱动包统一写入 `SHA256SUMS`。

发布候选版必须完成两类实机验收：

1. ARM64/Pi 或普通 Linux：不安装项目内核驱动，实体读卡器、模块、VoWiFi 和 4G 正常。
2. DS1621+：从空的容器部署开始，分别验证已有驱动节点和安装精确驱动包两条路径；重启
   DSM 后模块、实体读卡器、4G、国家出口、SWu 和 IMS 自动恢复，NAS 默认网络不变。

首版建议先发布 `vX.Y.Z-rc.1`。驱动目录首期只列已经验证的 DS1621+ 组合；普通 Linux
走内核自带驱动。完成 DSM 冷启动、实体读卡器和一次真实蜂窝短信/通话验收后再转正式版，
不把“理论相容”的 Synology 型号列入支持矩阵。
