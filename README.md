# RP1 机器人端只读监听

这是 [rp1-test-monitor](https://github.com/sss836/rp1-test-monitor) 的机器人专用代码，Python 包名为 `rp1-monitor-robot`，源码在 `src/rp1_robot/`。

电脑上位机、网页和开启教程位于另一个仓库：[rp1-monitor-app](https://github.com/sss836/rp1-monitor-app)。**不要把电脑仓库或电脑 `.venv` 安装到机器人。**

## 新机器人首次安装

先确认已有 ROS/Python、与运控一致的 DDS/ROS Domain、机器人唯一编号，以及 CPU1、2、3、7 可用。以下是已安装 Jazzy 的示例；先按[逐步启动教程](docs/STARTUP.md)核对现场环境。

在机器人普通用户终端执行，机器人编号换成这台机器的真实唯一编号：

```bash
git clone https://github.com/sss836/rp1-test-monitor.git
cd rp1-test-monitor
export RP1_ROBOT_ID=RP1-NEW-01
export RP1_ROS_SETUP=/opt/ros/jazzy/setup.bash
bash scripts/install-robot.sh
bash "$HOME/.local/share/rp1-monitor/github/start-observer.sh"
```

克隆和在线安装需要维护时可访问 GitHub/包源，日常监控只需网线。机器人无外网时，按教程传入源码和匹配 ARM64/Python 的依赖 wheel，用 `RP1_WHEELHOUSE` 离线安装。

安装只写入新的 `~/.local/share/rp1-monitor/github/`，已有目录拒绝覆盖。**没有开机自启**；每次上电后手动运行最后一条命令，保持前台终端打开，按 `Ctrl+C` 停止本次监听。锁和端口检查阻止重复启动，不会结束占用端口的原程序。

## 两端怎样连接

机器人监听 `127.0.0.1:8891` → 电脑通过网线建立 SSH 转发 → 电脑后台接收和保存 → 上位机/网页显示。电脑连接步骤见 [rp1-monitor-app 开启教程](https://github.com/sss836/rp1-monitor-app#readme)。克隆和安装成功不等于已经连通或完成实机验收。

## 运行范围

- 只读 ROS 订阅、已有 BMS 日志、主机状态、名单内进程观察、有限摘要缓存，以及人工触发的 TUI 被动自检。
- 默认限制到 CPU1、2、3、7，排除 CPU0；`nice=10` 是降低调度优先级，不是 CPU 占用上限。
- 不安装、启停或修改原运控、推理、驱动、CAN、BMS 服务；不发送电机控制或主动温度查询命令。
- 不包含 Qt、网页、电脑 relay、AI 或 PostgreSQL；机器人不配置数据库、中央上传凭据或模型 Key。
- 没有发布者或缺失温度、力矩、电压时保留未知，不编造数据，也不据此直接判定机器人故障。

当前现场旧目录 `/home/robo/.local/share/rp1-monitor/20261027-recordings/` **不会被本仓库自动更新或切换**。已有旧监听时继续使用其原启动器，不同时启动新旧两份；切换前先审阅差异。详细写入范围、离线依赖、档案和排查步骤见[逐步启动教程](docs/STARTUP.md)。
