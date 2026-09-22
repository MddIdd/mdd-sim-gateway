# 故障排查

- 虚拟机已扩容、但诊断页仍显示磁盘接近 100%：虚拟磁盘容量、分区和文件系统是三层，
  管理平台只放大第一层不会自动扩展后两层。先用 `lsblk -f`、`findmnt /` 和 `df -hT /`
  确认根分区及文件系统；普通 ext4 分区可用 `growpart` 扩分区后再用 `resize2fs`，
  XFS 用 `xfs_growfs /`，LVM 则需先 `pvresize` 再 `lvextend -r`。目标设备名必须以
  `lsblk` 的实际结果为准，不要盲目复制 `/dev/sda` 或分区号。扩容完成前反复重试镜像
  构建只会继续填满旧文件系统。

- 4G 不在线：检查“设备 → 详情”的 ModemManager 对象、注册、APN 和 bearer；运行设备诊断。
- VoWiFi 停在部分连接：检查国家出口 UDP 验证、ePDG、SIM 是否开通 Wi‑Fi Calling、PIN 剩余次数及引擎日志。
- 服务更新后 VoWiFi 突然停止：确认引擎容器仍存在，并检查控制面日志中是否把虚拟读卡器维护误判为 `card removed`。当前版本会在编排器退出信号到达时立即发布维护标记，并保留 45 秒重建窗口；旧版本应先恢复读卡桥再重新启动线路。
- 浏览器电话一直未注册：它与 WebUI 同源连接 `/api/instances/<线路>/softphone/ws`。经反向代理访问时确认代理转发了 WebSocket 升级头；直连时确认线路引擎在运行。控制面日志中的 `softphone relay: engine ... unreachable` 表示引擎的 Asterisk 未在网桥地址 8088 上监听，通常是引擎镜像未随本版本刷新。
- 能振铃但没声音：确认 `MDD_ADVERTISE_ADDR` 是软电话可达的主机地址，并检查 RTP 端口与浏览器麦克风权限。
- 读卡器未出现：先用 `lsusb` 确认 USB 层，再运行 `pcsc_scan` 检查 PC/SC 层。SCR Prime（`04d9:c001`）需执行一次 `sudo ./install.sh patchprime` 加入 libccid 设备表；之后支持热插拔。读卡器没有 4G 开关属于正常设计。
- SIM 逻辑通道分配失败：查看“设备 → 硬件”中的已分配数量、通道用途和明确错误。系统会自动释放本轮部分分配；若持续失败，先重启对应线路，确认仍失败后再安排模块复位，不要只按底层 QMI 错误码猜测原因。
- Telegram 失败：选择手动 HTTP/SOCKS 代理或已就绪的国家出口，并使用“测试”。
- Telegram 机器人不响应指令：先确认“通知 → Telegram → 聊天指令”已开启，且发送者的数字 ID
  在授权列表里（向 `@userinfobot` 索取自己的 ID；群聊需填群 ID，且群 ID 为负数）。指令走与推送
  相同的代理设置，推送“测试”通过即说明链路可用。停机期间积压的指令会被丢弃而不是延迟执行，
  因此重启后需要重新发送。号码必须写完整 E.164（如 `+447700900123`），运营商会拒绝或误路由
  只有国内格式的号码。
- 更新显示“尚无公开发布版本”：仓库仍为私有或尚未发布正式 Release 时属于正常情况；版本查询不需要 GitHub 认证。
- 升级在下载阶段超时或被远端断开：保持默认“自动”，系统会先直连，再尝试代理库中的可用
  条目；也可固定选择一个代理库条目后先点击“检查更新”验证链路。检查成功的线路会继续用于
  源码包、Engine 和控制镜像下载。
- 升级停在 `engine_image`：检查界面显示的下载线路与数据目录下
  `update/engine-image.log`。Engine 使用与其他 Release 资产相同的直连或代理回退，不要求
  Docker daemon 直接访问 GHCR；旧 Engine 在新镜像通过校验和及完整身份检查前不会被替换。
- 手工 Engine 构建在克隆 pjproject 或 Asterisk 时失败：确认主机能访问项目维护的两个
  GitHub sysmocom 镜像。它们固定保存本项目使用的上游 commit；不得关闭证书验证或改用
  未审核镜像。


## 虚拟化环境部署（PVE / QEMU）

本节来自一次完整的现场排障（issue #1），配方均经实机验证。

### 网络前置：Docker Hub 不可达

国内网络环境下引擎镜像的基础层（`fedora`、`node`）常无法从 `registry-1.docker.io` 拉取，
表现为安装/升级时 `dial tcp ... i/o timeout`。给 Docker 配置镜像加速后重试：

```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://docker.m.daocloud.io"] }
EOF
sudo systemctl restart docker
```

加速地址时效性强，哪个可用因网络而异，任选一个能用的填入即可。

### 虚拟机（QEMU/PVE）单模块

- 用完整虚拟机而不是 LXC 时，模块的 QMI 网口在客户机内核中创建，ModemManager 可完整工作（4G + VoWiFi）。
- USB 直通**按物理端口映射**（不要按厂商/设备 ID —— 两个同型模块的 ID 完全相同，按 ID 映射行为不确定），并**取消勾选「使用 USB3」**（这类模块是 USB2 设备，挂到模拟 xHCI 上控制传输可能失败，症状为设置 DTR 报 `Errno 71 Protocol error`、AT 无响应）。

### 虚拟机双模块（多模块）

两个模块共享一个模拟 USB 控制器时可能同时静默失效。已验证的完整配方：

1. **宿主机拉黑模块驱动**，防止宿主机与直通抢设备（历史上多次「模块全哑」由此而来）：

   ```bash
   printf 'blacklist option\nblacklist qmi_wwan\n' > /etc/modprobe.d/mdd-passthrough-blacklist.conf
   modprobe -r option qmi_wwan
   ```

2. **一模块一个独立模拟控制器**（PVE 网页界面做不到，需命令行；VM 需关机）：

   ```bash
   qm set <vmid> --delete usb0 --delete usb1
   qm set <vmid> --args '-device qemu-xhci,id=x1 -device qemu-xhci,id=x2 -device usb-host,hostbus=3,hostport=3,bus=x1.0 -device usb-host,hostbus=3,hostport=4,bus=x2.0'
   ```

   `hostbus`/`hostport` 按宿主机 `lsusb -t` 里模块实际所在的总线和端口填写。注意：`--args`
   定义的 USB 设备不会显示在 PVE 网页硬件列表中；回退用 `qm set <vmid> --delete args`。

   两个模块更换到其他物理 USB 接口后，可在 **PVE 宿主机**用仓库脚本重新发现并绑定：

   ```bash
   # 只预览，不修改
   bash tools/pve-bind-ec25-modems.sh 104

   # 确认后应用；若 VM 原本运行，会正常关机、更新绑定并重新启动
   bash tools/pve-bind-ec25-modems.sh --apply 104
   ```

   脚本只在恰好发现两块 `2c7c:0125` 时工作，并拒绝覆盖未知的 QEMU `args`、已有
   `usbN` 配置，以及已由其他 VM 配置或持有的相同物理 USB 口。它依据 sysfs 的
   `busnum + devpath` 绑定，不使用每次插拔都会变化的 `Device` 编号。脚本需要在换口后
   手动执行；不会因 USB 瞬断自动关闭生产 VM。

3. 可选：调高宿主机 usbfs 缓冲上限（无害保险）：内核参数 `usbcore.usbfs_memory_mb=1000`。

验证：客户机 `lsusb -t` 中两个模块应挂在**两个不同的 xhci** 下、各 5 个接口；`mmcli -L` 应列出两个 Modem 对象。

### LXC 容器

- LXC 内看不到模块的 QMI 网口（网络接口属于宿主机命名空间），ModemManager 无法创建 modem 对象，**4G 不可用**。
- 自 v1.3.9 起这是受支持的纯 VoWiFi 路径：编排服务读到 ModemManager 的拒绝记录后立即降级为直连串口，并停掉 ModemManager；SIM 访问与 VoWiFi 正常。
- LXC 的 USB 为宿主内核直驱，多模块无虚拟化层限制。

### 直通排障纪律

- **每一步观测都必须从已知状态出发**：先关 VM（`qm status` 确认 `stopped`），设备冷复位（物理重插或重启宿主机），再测。带电测试得到的现象几乎都是上一步的残影。
- VM 带直通运行期间，宿主机上**不要** `modprobe option` 或访问那些串口 —— 宿主机驱动与 QEMU 抢同一设备会把它推入「接口被两个系统瓜分」的分裂态，两侧同时失灵。
- 宿主机侧快速自检（VM 关机状态下）：`modprobe option` 后 8 个 `ttyUSB` 应齐全，`echo 'ATI' | socat - /dev/ttyUSB2,crnl` 应返回模块固件信息；测完 `modprobe -r option` 再启 VM。

## 线路认证失败（SW=9862 / 读卡器绑定错位）

一条线反复 `reg_rejected` 并每几分钟重建容器，`usim_status.json` 是
`AUTH_FAIL / sw=9862`，而 SWu 隧道却是 `CONNECTED` —— 这不是运营商拒绝，
是这条线打开了**另一条线的 SIM**。`9862` 是 AKA 的 MAC 校验失败，运营商用它
回应"这张卡算出的响应不对"，和"这个用户被拒"在报文层面无法区分。

自 v1.3.13 起引擎会自己拆穿这种情况：pin_keeper、ami_usim 和 swu_ike 在动卡之前
先读一次免 PIN 的 EF.ICCID，与线路自己的 ICCID 比对，不符就拒绝并把两个 ICCID
一起写进状态文件（`WRONG_CARD`），控制面也不再把它算作出口节点的过错。

排查顺序：

1. 看状态文件是否已经直接给出答案：

   ```bash
   cat data/instances/<id>/run/pin_status.json
   cat data/instances/<id>/run/usim_status.json
   ```

2. 若引擎版本较旧、只报 `9862`，手动比对配置与运行时：

   ```bash
   # 线路被绑到哪个 reader
   python3 -c "import json;d=json.load(open('data/instances/<id>/instance.json'));print(d['pin_reader'])"
   # 引擎实际打开了哪个
   python3 -c "import json;print(json.load(open('data/instances/<id>/run/pin_status.json'))['reader'])"
   ```

   两者不一致 = 容器内解析错位。**先查引擎镜像是不是旧的**——`git pull` 只更新控制面
   的 Python，不会更新镜像：

   ```bash
   docker images --format '{{.Repository}}:{{.Tag}}  {{.CreatedAt}}' | grep engine
   git log -1 --date=short --format='%ad %s' -- engine/
   ```

   镜像早于 `engine/` 的最后一次提交,就用 overlay 重建（只 COPY 运行时脚本，
   几十秒，不重编 Asterisk）。`RUNTIME_FP`/`BASE_FP` 必须带上，否则下次
   `install.sh reload` 会因标签为空而触发一次全量重建：

   ```bash
   ./install.sh reload --engines
   ```

3. 换完镜像**每条线都要重建**。恰好绑在 reader 索引 0 上的那条线在旧镜像下
   "看起来正常"，其实是回退撞对的，不换同样不可信。

## 彩信（MMS）收不到或发不出

彩信分两步：通知以 WAP Push 短信到达（VoWiFi 或 4G 模块均可），正文再从运营商 MMSC 通过 HTTP 取回。MMSC 通常只在运营商的彩信 APN 内可达，公网和普通上网 APN 访问不到。

1. **设置是否已识别**：短信页“彩信设置”会显示按 SIM 网络代码从 `mobile-broadband-provider-info` 查到的 APN、MMSC 与代理。查不到时需手动填写 MMSC（`http://` 开头）、APN 和代理（`host:port`）。
2. **模块通道**：“自动”在 SIM 所在模块支持 Quectel 内置 TCP/IP 协议栈时，于模块内部临时激活彩信 APN，不影响主机的数据连接和路由。它通过 ModemManager 命令通道下发 AT 指令，要求 ModemManager 以 `--debug` 运行：

   ```bash
   mmcli -m 0 --command='AT+QICSGP=?'
   ```

   返回 `+QICSGP:` 即可用；报错说明命令通道未开启或模块不支持，此时只能选择“主机网络”，且需保证主机能访问 MMSC。
3. **发送彩信需要独占 AT 口**：经 ModemManager 转发时，模块的上传指令以 `SEND OK` 结束，ModemManager 不认，每段都要等超时，速度约 100 字节/秒，而且连续超时 10 次会被 ModemManager 判定模块失效。因此这条通道只用于下载和回执，超过 4 KB 的请求直接拒绝。安装程序会写入 `/etc/udev/rules.d/78-mdd-mms-at-port.rules`，让 ModemManager 放开 Quectel 模块中被它标记为**备用** AT 口（`ID_MM_PORT_TYPE_AT_SECONDARY`）的端口；主 AT 口和 QMI 仍归 ModemManager，只有一个 AT 口的模块不受影响。网关会在每台模块的端口列表中找到这个口并独占使用（也可用 `MDD_MMS_AT_PORT` 指定），100 KB 约 3 秒。插多台模块时每台各用自己的端口，不同模块的彩信收发并行，同一模块上依次进行。

   检查是否生效：`mmcli -m 0` 的端口列表中备用 AT 口显示为 `(ignored)`。只有一个 AT 口的模块无法独占，发送彩信会报错说明原因，下载不受影响。规则写入或卸载时会重启 ModemManager，4G 数据连接会短暂断开。
4. **状态“未知”**：请求已发出但没有收到 MMSC 答复。网关不会自动重发，以免对方收到两条彩信。
5. **早已过期的通知**：模块离线期间积压、已超过 MMSC 保存期限的通知直接标记为“已过期”，不请求 MMSC，也不推送；仍可手动重试。
6. **附件被拒绝**：网关按文件内容（而不是扩展名或浏览器声明的类型）判断附件，添加附件时就会检查，被拒绝的文件不会进入待发送列表。网页端、直接调用 API 的客户端走同一套检查。当前格式表随“彩信设置”接口的 `formats` 字段返回，定义在 `control/app/mms_media.py`。

   | 策略 | 格式 |
   |---|---|
   | 发送 | JPEG、GIF、PNG；AMR、AMR-WB、MP3、AAC（m4a）；3GP / MP4 视频（H.263、MPEG-4、H.264，配 AMR 或 AAC 声音）；纯文本；vCard 2.1/3.0；vCalendar 1.0、iCalendar 2.0 |
   | 转换 | WebP、BMP、HEIC/HEIF、AVIF：网关转成 JPEG 后发送 |
   | 仅接收 | 其他格式（如 MOV、WAV、WBMP）：收到后保存并提供下载，浏览器无法播放时显示“不支持预览” |

   常见拒绝原因：iPhone 默认录制的 HEVC（H.265）视频（请导出为 H.264）、vCard 4.0（请导出为 3.0）、内容与声明类型不符或已损坏的文件。
7. **图片由网关压缩**：添加附件时文件即上传到网关，网关在线路限额内为所有图片分配空间，从原图按“先尺寸（最长边 1600 像素起）、后画质”重新编码为 JPEG，界面显示每张图压缩前后的大小和整条彩信的合计。已符合手机显示要求、放得下且不超过 1600 像素的图片不重新编码，只无损去掉 EXIF、XMP 等元数据；发出的图片都不带拍摄位置等元数据（带旋转信息的照片会重新编码为正向）。编写期间原图暂存在数据目录 `mms-staging/`，发送后只保存实际发出的版本，未发送的暂存文件 24 小时后清理；每条线路最多同时暂存 20 个附件、合计 64 MB，整机合计 256 MB，超出时会提示先发送或移除已添加的附件。声音、视频和动态 GIF 不压缩，超限时直接提示；视频压缩暂未实现。
   线路的大小上限是**每条彩信**一个：多个附件放在同一条彩信里时共用这个上限，附件越多每张越小。添加两个以上附件时可以选择“合并为一条”（默认）或“每张单独发送”：后者每个附件单独成为一条彩信，各自用满上限，正文和主题随第一条发送，各条按附件顺序依次提交。两种方式各自从原图压缩，切换时界面显示的大小和缩略图都会更新为对应方式的版本。
8. **大小限额按最终报文计算**：线路的彩信大小上限针对打包后的整条 m-send-req（含 SMIL、收件人、主题和各类头部），而不只是附件之和；发送时网关按最终的正文和收件人再计算一次，超限在提交 MMSC 前就会拒绝。
9. **兼容范围**：发送的报文为 MMS 封装 1.2（OMA-TS-MMS_ENC），SMIL 按 OMA MMS 一致性文档的布局（`Image` / `Text` 区域；每个部分有消息内唯一的 Content-ID 和 ASCII Content-Location，SMIL 按 Content-Location 引用，与实测 iPhone 经运营商发来的彩信一致；原文件名只用于显示和下载；音视频幻灯片时长取媒体实际时长）。格式表参照 OMA 内容类别至 Video Rich 选取，另加手机普遍支持的 MP3/AAC 和名片、日历。这是结构性检查，不等于通过了标准一致性认证；各运营商和手机之间的互通仍以实测为准。
10. **附件存储与备份**：附件文件保存在数据目录 `mms/<消息 ID>/`，文件名由网关生成，原始文件名只作为元数据。升级前的数据库备份（`backups/*.sqlite`）旁边同名的 `.mms` 目录是对应的附件；恢复时二者一起放回（`.sqlite` 改回 `mdd-sim-gateway.sqlite`，`.mms` 目录改名为 `mms`）。设置“备份与更新”中“创建本地备份”生成的完整备份包含一致的数据库快照和它引用的全部附件，不含编写中的暂存附件。

提交问题前下载“诊断 → 脱敏支持包”，并再次确认其中没有个人信息。
