# 安装与升级

本文描述传统宿主辅助安装。Synology Container Manager 及其他全容器环境请使用
[全容器部署指南](CONTAINER_DEPLOYMENT.md)，不要在 NAS 上执行本页的 `install.sh install`。

## 支持环境

- 推荐 ARM64 Debian、Ubuntu 或 Armbian，systemd 可用。
- Docker、USB、内核 TUN、pcscd；蜂窝模块还需要 ModemManager/NetworkManager。
- 已实机验证的三体电子 SCR Prime（`04d9:c001`）提供标准 CCID 接口，但尚未进入 libccid 1.6.2 的设备表。连接该型号时执行 `sudo ./install.sh patchprime`，安装程序会从校验过的固定版本源码构建驱动并加入设备匹配；完成后支持热插拔。
- 根文件系统至少 2 GiB 可用空间；建议使用 8 GB 或更大的系统盘，并在升级前保留约 3 GiB，
  供新镜像、当前镜像和一代回滚镜像在切换期间共存。实测占用：Engine 镜像 607 MB，两代镜像
  共享基础层后合计约 676 MB，源码 checkout 与虚拟环境约 102 MB，运行数据在数月后约 75 MB；
  Docker 控制面模式另需 355 MB。手工执行源码构建（下一条）需要额外数 GiB，不在此列。虚拟机扩展虚拟硬盘后还必须扩展根分区
  与文件系统，以 `df -h /` 为准，不能以控制台显示的虚拟硬盘容量为准。正式源码包的全新安装
  和一键升级都会按主机架构下载 CI 在原生
  ARM64/amd64 runner 构建的 Engine，并按校验和、架构、版本与源码指纹核验；设备不再为
  Engine 编译 Asterisk。Docker 控制面模式还会下载相同架构的 Control 镜像，原生控制面
  模式只下载 Engine。另一种架构的资产不会下载，导入后的压缩包会立即删除。同一 Engine
  以多架构清单发布到 GHCR，供手工安装与独立核验。
- 手工执行全新 Engine 构建时，固定 commit 从项目维护的 GitHub sysmocom 镜像获取；
  镜像只保存构建所需的上游分支，原始项目与许可归属不变。离线迁移仍可使用已经审核的
  `MDD_ENGINE_BASE_IMAGE`，不得关闭 TLS 验证或改用未审核源码。

## 安装

```bash
sudo ./install.sh install                 # 原生控制面 + Docker 引擎
sudo ./install.sh install --mode docker   # 控制面也运行在 Docker
```

从 GitHub Release 下载的正式源码包包含 CI 生成的镜像校验清单，安装程序会默认取得本机架构
的预构建资产。开发 checkout 没有该清单，仍从源码构建；正式源码包也可显式设置
`MDD_BUILD_IMAGES=1` 进行审核用源码构建。Release 资产下载或身份校验失败时安装会停止，不会
悄悄退回一次耗时且占用大量临时空间的编译。

可用环境变量：`MDD_PORT`、`MDD_DATA_DIR`、`MDD_BIND`、`MDD_ADVERTISE_ADDR`、`MDD_SINGBOX_VERSION`、`MDD_XRAY_VERSION`、`MDD_LPAC_VERSION`。安装程序会校验 sing-box 与 Xray-core 归档的 SHA-256；Xray-core 仅用于 Reality/XHTTP 节点的本机回环兼容层。更换固定依赖版本时必须同步审核并更新 SHA-256。离线迁移或显式执行源码构建时，可设置 `MDD_ENGINE_BASE_IMAGE`，从本机已审核的兼容引擎镜像创建只覆盖 MDD 运行脚本与模板的镜像；已经在可信构建机完成 `npm ci && npm run build` 时，也可设置 `MDD_REUSE_WEBUI=1` 复用随源码传入的 `webui/dist`。正式源码包的全新在线安装不需要设置这两项，安装程序会默认使用经校验的预构建镜像。必须执行全量 Engine 构建、但安装网络无法访问默认 GitHub mirror 时，可将 `PJPROJECT_REPOSITORY` 和 `ASTERISK_REPOSITORY` 显式指向另一条经过审核且包含相同固定 commit 的 HTTPS Git 仓库；未设置时继续使用 Dockerfile 中的项目 mirror。不得关闭 TLS 验证或改用未经审核的源码。

4G 模块收到的短信写入数据库后，新安装默认从 Modem/SIM 存储中删除，以免存储写满后无法再收短信；从旧版本升级的安装首次启动时会在配置中写入 `cellular_sms_storage: keep`，不自动清空模块，需要时手动改为 `delete`。可在控制服务的环境中设置 `MDD_CELLULAR_SMS_STORAGE`：`delete`（默认，入库后删除）、`when_full`（保留在模块中，仅在存储将满时删除最早的已入库短信）或 `keep`（始终保留）。`when_full` 通过 `AT+CPMS?` 读取容量，需要 ModemManager 以 `--debug` 运行；否则按 `MDD_CELLULAR_SMS_STORAGE_LIMIT`（默认 20 条）计算。配置文件 `settings.cellular_sms_storage` 若已设置，则优先于环境变量。

`MDD_DATA_DIR` 在首次安装后会写入系统状态；后续执行 `status`、`reload` 和 `uninstall` 时不必再次填写，避免自定义数据目录被误判为新安装。

如果系统 Docker 已经可以连接，安装脚本只复用它，不升级版本、不修改 daemon 配置，也不
操作其他项目的容器、镜像或卷。MDD 容器带有归属标签；发现同名外部容器、8443 端口冲突或
rootless Docker 时会停止并给出错误。切换到正式预构建镜像后会执行一次保守的
`docker builder prune`（不带 `--all`），清理由旧版现场编译留下且 Docker 已判定为 dangling
的构建缓存；它不删除任何镜像、容器或卷。由于旧版使用 Docker 的共享默认 builder，历史缓存
没有项目标签，Docker 无法进一步只按 MDD 归属筛选。蜂窝与 TUN/PCSC 引擎需要系统级 Docker
daemon，因此不支持 rootless 模式。

“系统设置 → 维护”会分别显示 Docker 镜像和构建缓存的实际可回收空间。“清理构建缓存”只执行
Docker 的保守 dangling-only 清理；“清理旧版与回滚镜像”是显式放弃一键回滚的操作，只删除
未被任何容器使用的 MDD 历史镜像，并保留当前 Engine/Control 与可信 Engine 基础镜像。两项
操作都不会删除容器、卷或其他项目镜像。

版本检查始终使用 GitHub Release API，不读取或发送 GitHub Token。配置的仓库不可访问或尚未发布 Release 时，界面会显示尚无可用发布版本。

安装完成后，在受信的局域网或 VPN 中立即打开 `https://主机地址:8443`，创建至少 10 字符的管理员密码。首次设置完成前，任何能访问该端口的客户端都可申领初始管理员。配置自有证书时，证书和私钥应只允许 root 读取。运行数据目录默认为 `0700`，凭据文件为 `0600`。

浏览器电话与 WebUI 同源：信令走 `wss://主机地址:8443/api/instances/<线路>/softphone/ws`，由控制面经 Docker 网桥转发到对应线路的引擎，引擎不向主机发布信令端口，也不需要单独信任证书。放在反向代理之后时，只需让 WebUI 地址本身转发 WebSocket 升级（`Upgrade`/`Connection` 头），无需为软电话另开路径或端口。控制面只接受来自自身页面的 WebSocket（`Origin` 必须与浏览器访问的主机一致）：代理保留 `Host` 头时无需任何设置；代理把 `Host` 改成内网地址时，需在设置里把代理地址填入"可信反向代理"，并由代理传递 `X-Forwarded-Host`。nginx 默认会改写 `Host`，在网关的 `location` 里加上下面几行即可：

```nginx
proxy_set_header Host $host;
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

不符合时，WebUI 顶部会出现"实时更新和软电话已停用"的提示，控制面日志记录 `refused WebSocket ... from origin`。通话音频仍使用各线路的 RTP 端口。

## 更新

系统设置可在“自动更新”和“提示更新”中二选一，并分别选择全部版本或主版本。`update-policy.json` 为两类用户保存独立目标：`channels.all` 始终指向获准推送的最新正式 Release，`channels.main` 指向当前获准推送的主版本，即使其后已经发布补丁，落后的主版本设备仍能按 tag 找到并安装该版本。新安装默认自动更新主版本；每个通道仍须匹配目标版本并到达自己的 `not_before` 时间，单纯发布 Release 不会触发安装。提示模式默认提示全部版本，左下角版本号出现红点后，由管理员查看说明并确认“立即升级”。更新时控制面把请求写入编排器目录，主机上的 `mdd-sim-gateway-orchestrator` 以独立的临时 systemd 单元（`mdd-sim-gateway-update`）运行 `host/mdd_update.py` —— 下载对应 `vX.Y.Z` Release 资产、校验 SHA-256 和版本，并比较新源码与本机 Engine 指纹。Engine 输入发生变化时，更新器通过同一条直连或代理回退线路下载该版本与主机架构匹配的 Engine 资产，校验后导入 Docker，再核对架构、版本和两类指纹；输入未变化时不会重复下载。备份与覆盖源码后，安装器保存旧 Engine 的 `:previous` 回滚标签，启用新镜像并只重建旧镜像上的线路，控制面重新扫描在位 SIM 使线路自愈。Docker 控制面模式还会取得同架构、已校验的 Control 镜像并执行 `docker load`。`data/`、`.env`、`.git` 和虚拟环境均保留。日志见 `journalctl -u mdd-sim-gateway-update`、数据目录下 `update/reload.log` 与 `update/engine-image.log`。

“系统设置 → 备份与更新”默认使用“自动”联网：先直连 GitHub，连接失败、超时或被限流时，再按代理库顺序尝试可用条目；检查成功的线路会继续用于更新下载。也可选择“仅直连”或指定一个代理库条目。SOCKS5 条目可直接使用；订阅、具体节点和导入的 outbound 需已分配给一个已启用且就绪的国家出口。代理凭据只保存一份，并只通过主机权限为 `0600` 的配置/临时文件传递，不写入 systemd 命令行或升级状态。
控制面不依赖浏览器登录，每 6 小时检查一次 Release。提示更新模式会通过已启用的 Webhook、Telegram 或 PushPlus 通道发送一次去重通知；“全部版本”检查 GitHub 最新 Release，“仅主版本”检查策略中独立配置的主版本 tag，不从版本号位数推断。
正式 Release 归档包内含 CI 预构建的 `webui/dist`，一键升级校验整个归档后直接复用，因此不需要在树莓派上下载 Node 镜像或编译前端。GitHub `main` 与其 Release 是唯一支持的更新通道。

`v1.4.1` 的升级器早于多架构 Release 资产，完成源码校验后会调用新版本安装器并要求保留旧
Engine。为允许用户直接跨级，正式源码包额外携带镜像校验清单；新安装器会读取旧升级任务
尚未删除的私有线路文件，以相同的直连和代理候选下载、校验并导入与主机架构、实际安装模式
匹配的 Engine 与 Control。从 v1.5.3 起 ARM64 与 amd64 都会取得各自的预构建镜像。接力只在
旧升级任务的私有网络文件仍存在时触发，不改变日常手工执行 `--no-engines` 的含义；校验清单
作为正式包元数据保留，供重复安装继续使用预构建资产。

已经安装的 **amd64 + Docker 控制面 v1.4.x** 还有一个旧升级器自身无法由目标版本修补的
前置问题：它会在覆盖新源码之前固定下载 ARM64 Control 资产并中止。此类设备升级到 v1.5.3
前需执行一次以下引导，让旧升级器跳过这一步；这不会停止或迁移当前 Docker 控制面：

```bash
MDD_DATA_DIR=$(sudo sed -n '1p' /etc/mdd-sim-gateway/data-dir)
test -n "$MDD_DATA_DIR" && test -d "$MDD_DATA_DIR"
sudo cp -p "$MDD_DATA_DIR/install-mode" "$MDD_DATA_DIR/install-mode.pre-v1.5.3"
printf 'local\n' | sudo tee "$MDD_DATA_DIR/install-mode" >/dev/null
```

随后从 WebUI 正常执行更新。v1.5.3 安装器会从仍在运行的 Control 容器识别真实 Docker 模式，
下载 amd64 Engine 与 Control，并在服务成功恢复后把 `install-mode` 自动写回 `docker`。若更新
在新源码接管前失败，可用
`sudo cp "$MDD_DATA_DIR/install-mode.pre-v1.5.3" "$MDD_DATA_DIR/install-mode"` 恢复标记后排查。
ARM64 Docker、原生控制面以及已进入 v1.5.x 的安装不需要这一步，也不需要先安装其他桥接版本。

也可以随时在主机上手动更新：备份并用受信任来源更新源码后执行：

```bash
sudo ./install.sh reload --engines
```

该方式保留数据并从固定源码重建依赖与引擎；正式一键升级优先使用 CI 分发镜像。

正式发布前请逐项完成 [发布检查清单](RELEASE_CHECKLIST.md)。推送与 `VERSION` 一致的 `vX.Y.Z` 标签后，Release 工作流会运行全套测试，并生成带 SHA-256 校验文件的源码包。

## 卸载

`sudo ./install.sh uninstall` 保留数据；`--purge` 会删除运行数据与虚拟环境，无法恢复。卸载只移除确认属于 MDD 的容器；Docker 本身及其他项目不受影响。
