# 发布检查清单

## 代码与版本

- `VERSION`、WebUI `package.json` 与标签保持一致（例如 `1.0.0` / `v1.0.0`）。
- `CHANGELOG.md` 将目标版本从 `Unreleased` 改为发布日期。
- CI 的 Python 测试、WebUI 构建、生产依赖审计和脚本语法检查全部通过。
- Release 必须包含 `mdd-sim-gateway-control-vX.Y.Z-arm64.tar.gz`，且
  `SHA256SUMS` 同时覆盖源码包和控制镜像。发布前用 `docker load` 验证资产为
  `linux/arm64`、版本 label 与 `VERSION` 一致；不得只发源码包。
- 依赖版本、源码提交与二进制 SHA-256 已复核；不得临时改成浮动分支或 `latest`。

## ARM64 实机验收

- 在目标 ARM64 主机执行一次无缓存 Engine 镜像完整构建，并在镜像内运行 `pip check` 与关键模块导入检查；这是发标签前的硬性门槛，不能用 GitHub x86 runner 的 Dockerfile 静态检查替代。
- 全新安装与重复安装均成功，断电重启后管理面自动启动。
- ModemManager/NetworkManager、pcscd、sing-box、lpac 状态符合预期。
- 已有 Docker 与外部容器保持不变；MDD 容器均带归属标签，端口冲突会安全中止。
- 至少验证一个蜂窝模块的 4G 开/关、VoWiFi 开/关、通话与短信。
- 至少验证一个 PC/SC 读卡器仅显示 VoWiFi，不显示虚假的 4G 能力。
- 多模块时逐台切换能力，确认不会改动另一台模块。
- Clash 订阅国家出口通过 UDP 测试，界面显示实际节点名称；无健康节点时故障关闭。
- Webhook GET/POST、Telegram 代理与 PushPlus 测试按钮均验证一次。
- Telegram 仅发送通知，设置页和后端均不存在远程拨号、短信或挂断指令入口；
  直接回复来信通知能给该号码回短信；相关操作出现在审计记录中。
- 自有 TLS 证书、首次管理员设置、修改密码、备份和脱敏支持包均验证一次。

## 隐私与发布

- 运行 `rg` 检查源码、文档与截图，不含真实 IMSI、ICCID、EID、号码、PIN、Token、订阅地址或私钥。
- `data/`、`.env`、证书、pcap、数据库、构建目录和本机日志未被 Git 跟踪。
- 截图仅使用空状态、虚构数据，或已经逐项遮挡设备、线路、运营商、国家出口、号码与消息内容并经人工复核的真实页面。
- 先创建私有仓库完成内部验收；最终确认后再决定是否公开。
- 推送已签名的 `vX.Y.Z` 标签；Release 工作流会生成源码包、ARM64 控制镜像与
  同时覆盖两者的 `SHA256SUMS`。
