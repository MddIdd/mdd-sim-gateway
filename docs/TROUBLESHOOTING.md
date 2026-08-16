# 故障排查

- 4G 不在线：检查“设备 → 详情”的 ModemManager 对象、注册、APN 和 bearer；运行设备诊断。
- VoWiFi 停在部分连接：检查国家出口 UDP 验证、ePDG、SIM 是否开通 Wi‑Fi Calling、PIN 剩余次数及引擎日志。
- 服务更新后 VoWiFi 突然停止：确认引擎容器仍存在，并检查控制面日志中是否把虚拟读卡器维护误判为 `card removed`。当前版本会在编排器退出信号到达时立即发布维护标记，并保留 45 秒重建窗口；旧版本应先恢复读卡桥再重新启动线路。
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
- 升级在“正在下载新版本”阶段超时或被远端断开：保持默认“自动”，系统会先直连，再尝试代理库中的可用条目；也可固定选择一个代理库条目后先点击“检查更新”验证链路。检查成功的线路会继续用于源码包和控制镜像下载。
- Engine 构建在克隆 pjproject 或 Asterisk 时出现 TLS EOF/连接被关闭：先确认安装主机能
  通过 HTTPS 访问 `gitea.sysmocom.de`。这通常是上游对当前网络出口的可达性问题，重复
  重试不能替代可用网络；请换到可访问上游的可信 ARM64 构建机，或使用已审核的
  `MDD_ENGINE_BASE_IMAGE`。不得关闭证书验证或改用未审核镜像。


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

提交问题前下载“诊断 → 脱敏支持包”，并再次确认其中没有个人信息。
