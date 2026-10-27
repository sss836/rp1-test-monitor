# RP1 Test Monitor

RP1整机只读监控：机器人持续采集、网线SSH、电脑relay、Qt上位机与中文网页。支持24/28关节展示、CPU逐核/进程监控、电池与关节曲线、测点伪彩温度、RoboPi TUI被动单项检查，以及可配置的只读AI诊断。

## 电脑三行启动

```bash
git clone https://github.com/sss836/rp1-test-monitor.git
cd rp1-test-monitor
./start-monitor.sh
```

机器人已经开启本项目监听后，在上位机设备设置输入`ssh robo@192.168.137.250`及机器人密码。首次核对主机指纹后连接，电脑自动接入relay，不需要手工拼SSH转发命令。密码只用于本次认证，不保存。

网页入口：

```bash
./start-monitor.sh web
```

浏览器访问`http://127.0.0.1:18980/connect`，连接成功后进入整机、电池、关节、主机负载等独立页面。Qt与网页共享同一电脑后台；关闭窗口后后台继续接收，断网需重新认证。

首次安装需要Ubuntu电脑已具备Python 3.10+、venv及可用包源。脚本在仓库`.venv/`安装依赖，不修改系统Python或使用sudo。快捷模式数据保存在私有`runtime/station/`独立SQLite，不修改现有生产PostgreSQL。

## 机器人首次安装

已有监听时跳过。只有TUI、尚未安装本项目的新机器人，在审阅[首次安装范围](docs/GITHUB_QUICKSTART.md)并核对ROS/DDS/Python后：

```bash
git clone https://github.com/sss836/rp1-test-monitor.git
cd rp1-test-monitor
bash scripts/install-robot.sh
bash "$HOME/.local/share/rp1-monitor/github/start-observer.sh"
```

机器人脚本只创建新的普通用户监控目录，已有目录拒绝覆盖；不安装自启、不更改运控、驱动或BMS。运行期间机器人仅需网线，不配置中央地址、设备上传Token或模型Key。无外网机器可用匹配ARM64/Python的离线wheel安装。

**现有RP1-ROBOPI2继续使用其原启动器，不用新脚本覆盖已有安装。** 同一采集端应只有一个持久relay；当前电脑已有18972 relay时继续连接原relay，迁移前保留并交完旧队列。

## 架构与范围

```text
机器人 ROS / BMS日志 / proc / sys
  → 只读agent（机器人回环8891，CPU0排除）
  → 网线SSH（仅端口转发）
  → 电脑后台relay（持久接收/确认/上传）
  → 本机或正式中央服务
  → Qt / 网页 / 可选AI证据分析
```

AI诊断运行在电脑/中央服务，调用DeepSeek、千问等需在界面配置实际模型和API Key。机器人采集端不运行LLM。

电机温度、MOS温度和故障必须由真实生产者提供，并核对物理映射；安装TUI或开启推理不等于这些数据已提供。“热像”为测点伪彩示意，不是红外相机。缺失、过期和未定位数据保持未知；模拟来源显著标注。

不发布`/joy`、`/cmd_vel`或`/action`，不调用使能/标零/推理启停。只读监听仍有资源开销，软件测试不能代替推理工况与长期实机验收。当前无自动月度删除或PostgreSQL分区；不删除原始运控日志或rosbag。

## 文档

- [启动、密码连接、机器人改动范围和排错](docs/GITHUB_QUICKSTART.md)
- [完整技术架构](docs/TECHNICAL_ARCHITECTURE.md)
- [现有部署恢复及通信说明](docs/DEPLOY_AFTER_ROBOPI_TUI.md)
- [RoboPi TUI硬件自检](docs/HARDWARE_CHECKS.md)
- [诊断多模型连接](docs/MODEL_CONNECTIONS.md)
- [本轮软件与模拟验证](docs/QUICK_CONNECT_VERIFICATION.md)
- [机器人健康集成材料](integrations/robot-health/README.md)

## 测试

```bash
.venv/bin/python -m pip install -e '.[dev,desktop,server,station]'
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest -q
```

密码SSH链路测试运行在本机回环的模拟服务器，不连接实机。仓库不包含运行凭据、数据库、机器人真实遥测或运行日志。第三方前端依赖保留各自许可证。
