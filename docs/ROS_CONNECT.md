# RP1 ROS2旁路接入

v0.4.0 使用网线 agent→上位机 relay→中央链路，完整拓扑及缓存语义见 [WIRED_RELAY.md](WIRED_RELAY.md)。此文档是联调操作参考，没有对实机执行。第一步是由现场开发者核对运行环境，随后在机器人安全静止状态下只启动观察服务。

## 先记录实际条件

```bash
# 以下均为只读检查；使用实际ROS环境
printenv ROS_DISTRO
python3 --version
ros2 topic list
ros2 topic info /joint_states --verbose
ros2 topic info /action --verbose
ros2 topic info /imu --verbose
```

确认三Topic类型分别为 `sensor_msgs/msg/JointState`、`sensor_msgs/msg/JointState`、`sensor_msgs/msg/Imu`，并记录发布频率和QoS。不要为这次监控而改变现有运控发布器。

参考实现读过 [roboparty_inference](https://github.com/Roboparty/roboparty_inference) 快照 `fbf32e9227d9dbb4cefe3718916c37a3d980bc64`：反馈从关节缓存发布，目标来自策略act，头时间为节点发布时刻，名称为joint_N/action_N。部署到实机前应比对实际版本，不能只凭仓库推断现场接口一致。

当前ROS适配仅支持最多64个具名关节；它不会给无名数组猜物理位置。确认位置/速度/effort的单位与符号、左右侧及闭链关节的含义。Qt表格会同时显示规范索引与ROS名字。跨重启的物理映射仍应固定并审阅，不能单凭名字匹配作为验收。

## 创建匹配ROS的Python环境

机器人环境和桌面环境分开。ROS发行版对应的rclpy通常绑定系统Python，**不可把本机Python 3.12环境直接复制到Python 3.10的机器人上**。示例：

```bash
source /opt/ros/<实际发行版>/setup.bash
# 使用能够import rclpy的同一个python3
python3 -c 'import sys, rclpy; print(sys.version)'
python3 -m venv --system-site-packages .venv-agent
source .venv-agent/bin/activate
python -m pip install 'rp1_monitor-0.4.0-py3-none-any.whl[agent]'
python -c 'import rclpy, sensor_msgs.msg, rp1_monitor'
```

如果系统缺venv或匹配版本的rclpy，应先修正环境；程序不会用模拟数据掩盖ROS初始化失败。桌面无需安装ROS。

## 配置设备和中央地址

在中央管理机运行下面命令，在新目录生成0600令牌文件，目录0700。命令不输出令牌。

```bash
rp1-monitor provision --directory runtime/provision-rp1-001 \
  --robot-id RP1-001 --name 'RP1 001' --source ros2
```

- `devices.json`留中央服务，多个机器人登记需合并为完整列表，每个robot_id和令牌分别唯一。
- `device-token.txt`通过内部安全方式分发到对应上位机电脑，仅该机器人的relay使用；机器人不保存中央令牌。
- `viewer-token.txt`给独立只读页面；平台登录方式不使用这个令牌。
- 更改设备列表并重启中央服务会停用未列出的设备；记录保留。运行中尚无注册/吊销管理UI。

上位机relay配置的中央地址必须HTTPS，有公司CA时给relay传入CA文件。若使用平台挂载，地址应包括 `/monitor`，例如 `https://<实际平台主机>:8443/monitor`。不提供关闭TLS校验的选项。

## 手动启动旁路服务

agent必须运行在机器人侧，sysfs/proc读的才是机器人主机。默认同时读取BMS新journal；需要已有日志读取权限，无权限会显示异常，不自动提权。若现场**已经运行**版本匹配的rp-server，可在下面命令追加 `--rp-server-url http://127.0.0.1:8765`，仅GET读取电池缓存（含温度/保护位），并关闭journal分支避免混源。不要为监控新启动rp-server，它会初始化硬件并可能与现有运控争总线。详细限制见 [健康接入说明](HEALTH_IMPLEMENTATION.md)。

```bash
source /opt/ros/<实际发行版>/setup.bash
# 按现场情况source工作空间，使用相同ROS_DOMAIN_ID/RMW/DDS配置
.venv-agent/bin/rp1-monitor agent --source ros2 --robot-id RP1-001 \
  --state-dir /var/lib/rp1-monitor \
  --profile /etc/rp1-monitor/profile.json
```

agent仅监听127.0.0.1:8891，不访问中央服务或外网。上位机先通过系统ssh核对机器人主机指纹并验证密钥登录，在独立终端保持隧道：

```bash
ssh -N -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18911:127.0.0.1:8891 <现场用户>@<机器人网线IP>
rp1-monitor relay --source ros2 --robot-id RP1-001 \
  --collector http://127.0.0.1:18911 \
  --backend 'https://<实际中央地址>/monitor' \
  --token-file /etc/rp1-monitor/device-token.txt \
  --state-dir /var/lib/rp1-monitor-relay/RP1-001
rp1-monitor desktop --url http://127.0.0.1:8892
```

上位机无需ROS。Qt直接连接电脑relay，机器人隧道不得随窗口关闭。只运行一份对应机器人ID的agent和一份relay；多台设备用独立隧道端口、relay端口及状态目录。

## 现场验收顺序

1. 静止机器人下核对名称、维度、单位、非零姿态和策略目标含义；拔掉或暂停一个**监控数据源**时应变过期，不能填零。
2. 比较原有日志工具与本工程同一时段数据，确认峰值、方向、时间及缺失标记。
3. 关闭App，保持电脑relay和SSH隧道，确认机器人采集仍运行、中央captured_at推进。
4. 分别模拟电脑到中央中断和机器人网线链路中断，确认电脑/机器人对应待传队列增长；恢复后补传且当前状态不倒退。不要为了断网试验修改机器人运控网络或无线配置。
5. 在现场既有安全流程下由操作者用现有手柄走路、跑步、特技动作；监测CPU/内存、DDS丢样、源到画面延迟，检查对运控无可测影响。
6. 完成至少1小时实机稳定运行。随后根据实际工况延长，确认存储保留与磁盘预算。

本项目不含电机停止按钮；退出监控服务也不会停止机器人。现有抓包/原始日志由原来的流程处理，不在此处启停或清理。
