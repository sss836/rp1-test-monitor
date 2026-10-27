# RP1 健康监测数据源核查

核查日期：2026-10-16。核查方式：只读 GitHub 源码及本机已存在源码；没有 SSH、没有连接机器人、没有启动硬件服务。本文区分“源码中可接入”“当前公开接口提供”“实机尚待确认”，不把代码存在等同于已经接入实机。

## 结论与当前最短接入路径

整机健康监测不能只依赖 `/joint_states`、`/action`、`/imu`。电池、主机热区和电机温度确实已有相关代码，原型之前遗漏了这些入口。

1. **主机温度、CPU/内存/磁盘**：在机器人上的独立采集进程读取 Linux sysfs/proc；现有 `robopi_analyze` 已有热区读取实现。不能在上位机读取本机温度后标成机器人温度。
2. **电池最低改动接入**：跟随现有 `bms.service` journal；能获得电压、电流、SoC、部分机型的供电状态。它拿不到电池温度和保护位，且没有逐字段采样时间，界面必须明确显示来源限制。
3. **电池完整观测接入**：机器人已运行并版本匹配时，读取现有 BMS daemon 的 Unix socket，可取得电池温度和保护位等。已有 `rp-server` Python 解析器可作为协议依据，不需要重新打开串口。该协议尚未版本化，不能默认自动启动或盲猜结构。
4. **已有 rp-server 时的远程入口**：可选仅 GET `/api/status` 和 `/sysinfo`，读取电池温度、保护状态与主机负载。必须标注“缓存快照，字段采样时间未知”；服务返回的 `hw_ready` 不能视为实时整机健康。GET 当前不返回各电机温度。
5. **运控中的电机温度/故障**：现有驱动能解析，当前运控 ROS 发布没有带出。应在实际运控进程内增加缓存快照和只读 diagnostics 发布，不在旁路创建新的电机控制实例。

**不能为读取状态而新启动 rp-server。** 它的启动流程会初始化电机、IMU、手柄和头部；文档明确它与 `start_robot.sh` 竞争 CAN。GET 适配器仅连接用户指定的、已经运行的服务，不启动/停止服务，不使用控制混合的 WS/UDP，不发送 AT 控制命令。

## 公开仓库版本与导航

远程读取的仓库 SHA 已通过 HEAD commit 接口核对；motors、inference、analyze 三项记录本机克隆的 Git HEAD。下列链接均固定核查版本，不随 main/master 变化。

| 仓库 | 核查 commit | 与本需求的关系 |
|---|---|---|
| [RP1](https://github.com/Roboparty/RP1/tree/ef132918ce24d52f95d8fb7784c7e9930575efef) | `ef132918ce24d52f95d8fb7784c7e9930575efef` | 全栈导航；不是运行时状态服务 |
| [roboparty_bms](https://github.com/Roboparty/roboparty_bms/tree/bc6be959e0ea9b27644e38c6dece80e9a6377685) | `bc6be959e0ea9b27644e38c6dece80e9a6377685` | 电池 daemon、socket、串口/CAN/NRF 数据 |
| [roboparty_rp_server](https://github.com/Roboparty/roboparty_rp_server/tree/e82295b19a80dcbb45425571a0c2d50838d521de) | `e82295b19a80dcbb45425571a0c2d50838d521de` | 已有 GET 状态、BMS socket reader、遥测推送 |
| [roboparty_motors](https://github.com/Roboparty/roboparty_motors/tree/45eac3ff3dc860e3bd688c1e62a3c5368166849a) | `45eac3ff3dc860e3bd688c1e62a3c5368166849a` | 本机克隆的驱动源码 |
| [roboparty_inference](https://github.com/Roboparty/roboparty_inference/tree/fbf32e9227d9dbb4cefe3718916c37a3d980bc64) | `fbf32e9227d9dbb4cefe3718916c37a3d980bc64` | 本机运控源码、ROS 发布、机器人配置 |
| [robopi_analyze](https://github.com/Roboparty/robopi_analyze/tree/68a3c829fb7aa149aada8f643b3f8e627efa0611) | `68a3c829fb7aa149aada8f643b3f8e627efa0611` | 本机抓包/健康日志与热区采集 |
| [rp1_test](https://github.com/Roboparty/rp1_test/tree/d142aff0622fc00153363b87d0b4227e3a805b99) | `d142aff0622fc00153363b87d0b4227e3a805b99` | 现有测试上位机及其修改过的 SDK |
| [ROMOMO](https://github.com/Roboparty/ROMOMO/tree/a9140df7b4e93631d755b5afa1e2c7dff2cfe439) | `a9140df7b4e93631d755b5afa1e2c7dff2cfe439` | 模组 CAN-FD 协议、上位机手册等 PDF |
| [rp1_hardware](https://github.com/Roboparty/rp1_hardware/tree/99c17acdeb9ddffe55e927d6f6646787a648f762) | `99c17acdeb9ddffe55e927d6f6646787a648f762` | 电气/硬件配置和料号依据 |
| [roboparty_example](https://github.com/Roboparty/roboparty_example/tree/68a0ba9c3c2aff18060dfbe39b7e621b79c24cc1) | `68a0ba9c3c2aff18060dfbe39b7e621b79c24cc1` | 已有 Web 演示 UI 和 BMS 示例 |
| [Party_OS](https://github.com/Roboparty/Party_OS/tree/44596a2c5d7a6534df06588558408300c3591601) | `44596a2c5d7a6534df06588558408300c3591601` | 路线与子项目导航，无本次可接入状态服务 |

RP1 [中文导航第 72–77 行](https://github.com/Roboparty/RP1/blob/ef132918ce24d52f95d8fb7784c7e9930575efef/README.zh-CN.md#L72) 指向硬件、ROMOMO、测试与手柄；[第 102–105 行](https://github.com/Roboparty/RP1/blob/ef132918ce24d52f95d8fb7784c7e9930575efef/README.zh-CN.md#L102) 指向部署与固件。固件仓库再经 `roboparty_repo/manifest.xml` 关联 `roboparty_bms`、`roboparty_rp_server`、`robopi_analyze`。因此不能只读部署仓库的三条 ROS 话题便判断电池接口不存在。

## 已有 rp-server：GET 接口与边界

依据 [ws_server.py:190–228](https://github.com/Roboparty/roboparty_rp_server/blob/e82295b19a80dcbb45425571a0c2d50838d521de/src/rp_server/transport/ws_server.py#L190)。

- 默认 HTTP 地址为 `http://<机器人地址>:8765`；配置/环境变量/CLI 可覆盖。依据 [__main__.py:116–117](https://github.com/Roboparty/roboparty_rp_server/blob/e82295b19a80dcbb45425571a0c2d50838d521de/src/rp_server/__main__.py#L116)。
- 核查的 GET 路由没有认证依赖或认证中间件；外部反向代理/实机网络可能另有限制，尚未核实。不能由此假设任意设备都能访问。
- `GET /health` 返回 `status, hw_ready, hardware, required_hardware, mock, version`。所核查包版本为 1.4.0。`hardware` 有 `motors/imu/bms/joy/head` 布尔项，真实模式主要在启动时初始化更新；`required_hardware` 默认 motors、imu。
- `GET /api/status` 返回 `hw_ready, hardware, mock, policy, policy_running, joy_device, motor_errors, battery, imu`。
- `policy` 默认空字符串，`joy_device` 未初始化为空字符串；`battery/imu` 没有数据时为 null。模拟模式能返回生成的电池/IMU，应检查 `mock`，不能记作实机。
- `motor_errors` 为非零错误列表，项包含 `id/code/name/type`；空列表也可能是无电机、读取失败被跳过、缓存未更新，不能据此推出所有电机健康。
- `GET /sysinfo` 返回 `cpu`（CPU 百分数）、`mem`（内存百分数）、`load`（1、5、15 分钟负载）。读取 load 异常时源代码回退全零，没有有效位；适配器不能将该零作为已证实正常。
- GET **不包含各电机温度、完整电机状态，也不包含主机热区温度**。负载与温度是两个来源。

电池对象来自 [drivers/bms.py:114–167](https://github.com/Roboparty/roboparty_rp_server/blob/e82295b19a80dcbb45425571a0c2d50838d521de/src/rp_server/drivers/bms.py#L114)：

| 字段 | 含义 |
|---|---|
| `voltage/current/soc/temp` | V、A、百分数、摄氏度 |
| `capacity` | 源 BatteryStatus 的容量值；应按实机 BMS 协议确认容量标度 |
| `soh/cycles` | 健康度数值/循环数；不支持的协议可能填 0 |
| `state/protect/io_state` | 原始工作状态字符串/保护位整数/IO 位整数，需按实际协议解码 |
| `power_on` | 电池供电开关状态，不是功率 |

该 BMS reader 的 `ready` 仅意味着 socket 连上；它没有缓存到达时间。遥测循环按配置 50 Hz 重复读取约 1 Hz 的 daemon 缓存；`/api/status` 优先返回 `telemetry.last_battery`，断开后旧缓存仍可能存在。不能用 HTTP 响应时间、50 Hz 推送时间代替电池采样时间。现 GET 缺少 `observed_at/valid_fields/data_age`，应展示缓存来源限制。

电机缓存温度在 [drivers/motors.py:133–150](https://github.com/Roboparty/roboparty_rp_server/blob/e82295b19a80dcbb45425571a0c2d50838d521de/src/rp_server/drivers/motors.py#L133) 已经读取，但仅被 monitor 缓存和 [UDP 推送:234–249](https://github.com/Roboparty/roboparty_rp_server/blob/e82295b19a80dcbb45425571a0c2d50838d521de/src/rp_server/transport/udp_listener.py#L234) 使用。UDP 的 `timestamp` 是发送时刻，电机 ID 也未附总线名，重复 CAN ID 会产生歧义。本次只读 GET 接入不使用它。

既有 [Web 演示 UI](https://github.com/Roboparty/roboparty_example/blob/68a0ba9c3c2aff18060dfbe39b7e621b79c24cc1/web/full.html#L301) 接收 `@BAT`、`@IMU`，同时包含控制按钮。它证明已有电池 UI 接口，不代表已有可信的整机健康判断、逐字段新鲜度或电机温度 GET。

## BMS daemon：日志与完整 socket

[日志输出:378–386](https://github.com/Roboparty/roboparty_bms/blob/bc6be959e0ea9b27644e38c6dece80e9a6377685/src/bms_daemon.cpp#L378) 提供电压、电流、SoC，以及可选供电开关状态。

- 电流可为负数，SoC 可为小数，Power 可缺省。原抓包分析脚本的旧正则只匹配正电流、整数 SoC、必有 Power，不能照抄。
- `Power: ON/OFF` 不是瓦数。若展示 `V×A`，必须标为计算功率并保留电流符号。
- GFCAN 日志前缀为 `[CAN BMS Data]`，SoC 本身已是百分数；NRF 前缀为 `[NRF PMIC]`，`48V=ON/5V=ON` 是电源轨状态，电池数值在后面的 `| BMS ...V ...A ...%`。
- 日志没有电池温度、保护位和逐字段有效位；journald 时间是日志记录时间。
- [TWS/GF 合并循环:334–369](https://github.com/Roboparty/roboparty_bms/blob/bc6be959e0ea9b27644e38c6dece80e9a6377685/src/bms_daemon.cpp#L334) 仅替换成功读取的字段，失败字段可能沿用旧值。新日志不保证所有字段刚采样。

串口 daemon 广播 [BatteryStatus](https://github.com/Roboparty/roboparty_bms/blob/bc6be959e0ea9b27644e38c6dece80e9a6377685/include/bms_driver.hpp#L26)：

- TWS/SCUD485：`/tmp/bms.sock`；GF485：`/tmp/gf_bms.sock`。
- `AF_UNIX/SOCK_STREAM`，daemon 单向广播。已有 Python 格式为 `struct.Struct("=7dIH2d33sHHHIIB")`，126 字节、标准大小、无对齐、宿主字节序。
- 流读取须处理分片和多帧拼接；不能把一次 recv 当一帧。
- 包含电压、电流、温度、SOC 比例、剩余/满充/设计容量、保护位、工作状态、最高/最低单体电压、序列号、软件/硬件版本、SOH、循环、IO、供电状态。
- 无 magic、帧版本、校验、字段有效位、采样时间。必须确认安装版本和协议后显式启用；连接后持续读取、不发任何字节，不重开 BMS 串口。
- GFCAN 的当前 `run_can_daemon` 虽有 socket_path 参数，但没有真正创建该 socket；不能假定存在 `/tmp/can_bms.sock`。
- TWS/GF socket 记录也可能混合旧字段；SCUD485 在 [read_all:248–280](https://github.com/Roboparty/roboparty_bms/blob/bc6be959e0ea9b27644e38c6dece80e9a6377685/src/drivers/scud485/scud_bms_driver.cpp#L248) 中整帧读取。其电流充电为正、放电为负，温度为最高电芯温度，SoC 存成 0..1。
- SCUD485 不提供 SOH/HW/SW/IO 位图，源代码填零。这些零必须显示为不支持，不能显示成真实健康度 0%。

完整可靠的实时健康结论需要源输出携带 `protocol/valid_fields/field_observed_at`；仅加 HTTP 轮询不能凭空补全这些事实。

## 主机热区与上电状态

[08_thermal.sh:13–18](https://github.com/Roboparty/robopi_analyze/blob/68a3c829fb7aa149aada8f643b3f8e627efa0611/dimensions/08_thermal.sh#L13) 已读取每个 `thermal_zone*/type,temp` 并将毫摄氏度转摄氏度。应保留内核热区名字，不把所有热区都叫 CPU，不把无法读取显示成 0 °C。RK3588/GPU/NPU 实际暴露哪些 zone 需现场确认。

电池的 `power_on` 与 NRF 的 `power48/power5` 不是 Linux 主机是否启动，也不是电机使能。主机启动时间可通过机器人端 `/proc/uptime`、boot ID 观测；机器人离线时只能显示最后在线时间和失联状态，不能推断是断电、网络断开还是进程退出。

[rp1_hardware 板卡目录](https://github.com/Roboparty/rp1_hardware/blob/99c17acdeb9ddffe55e927d6f6646787a648f762/02_PCB/README.md#L13) 确有 RK3588S 主控、FAN、HOTSWAP、MCAP、电池板等硬件版本；这属于硬件依据，不构成这些部件已经有软件遥测 API 的证据。ROMOMO 仓库当前只含 PDF/规格/测试材料，未将手册标题当成已运行服务。本次未解读这些二进制 PDF 中的阈值。

## 电机健康桥接：现有依据与最小修改点

现有运控 [rp1_24dof 配置:14–17](https://github.com/Roboparty/roboparty_inference/blob/fbf32e9227d9dbb4cefe3718916c37a3d980bc64/robots/rp1_24dof/robot.yaml#L14) 是 LRO/CAN-FD；仍需实机配置确认，不能由 RP1 名称自动选择协议。

- [LRO 回调:138–163](https://github.com/Roboparty/roboparty_motors/blob/45eac3ff3dc860e3bd688c1e62a3c5368166849a/src/drivers/lro/lro_motor_driver.cpp#L138) 已解析原始错误码、电机温度、MOS 温度。电机温度由反馈字节减 25 得到。
- [RP/ROMOMO 回调:188–213](https://github.com/Roboparty/roboparty_motors/blob/45eac3ff3dc860e3bd688c1e62a3c5368166849a/src/drivers/rp/rp_motor_driver.cpp#L188) 也解析电机/MOS 温度，signed16×0.01 °C。其 16 位故障值写入基类 8 位 `error_id_` 会截断，需保留完整原值再宣称保护位支持。
- LRO 私有 MOS 温度变量是 uint8，而计算后可能为负；不应把此缓存未经修正直接发布为有效摄氏度。
- [当前 ROS 发布:643–654](https://github.com/Roboparty/roboparty_inference/blob/fbf32e9227d9dbb4cefe3718916c37a3d980bc64/src/ros_interface.cpp#L643) 只输出位置、速度、effort。温度/错误码没有在这条消息里。
- 新 `rp1_test` 内置的 [LRO SDK](https://github.com/Roboparty/rp1_test/blob/d142aff0622fc00153363b87d0b4227e3a805b99/software/host/motors/src/drivers/lro/lro_motor_driver.cpp#L175) 已有单调 `feedback_count`；它与本机独立 motors 仓库版本不同，可参考其新鲜度思路。它没有原始接收时间和统一帧快照锁，且回调的 fb_type 尚未做严格过滤，不能照搬后声称完整有效帧验证。

建议最小补丁限于实际运控使用的驱动与运控发布：

1. 驱动在校验 CAN ID、长度、反馈类型并完整解析后，一次发布不可变 `MotorFeedbackSnapshot`：总线、motor ID、协议、电机/MOS 温度、原始 fault code、各字段有效位、反馈序号、真实接收的 monotonic 时间。首次反馈前无效。
2. 字段、序号和接收时间要同一同步单元更新。当前多个 atomic getter 只能防单字段 data race，不能保证一组值来自同一帧；`RobotInterface::motors_mutex_` 也不保护驱动接收回调。不能只在读端加锁便声称快照一致。实现需评估驱动回调开销，使用短临界区或经过验证的缓存交换。
3. 在现有 `RobotInterface` 暴露缓存读取方法；在低频 ROS timer 读取快照并发 `diagnostic_msgs/DiagnosticArray`。不可在控制高频循环中做网络/数据库操作。
4. diagnostics 同时携带反馈年龄、字段支持情况、协议原始错误码；过期/未初始化显示未知。故障温度阈值来自实际驱动规格和公司确认的测试策略，不凭空设定。
5. 监控 collector 仅订阅。禁止调用 `init_motor/refresh_motor_status/set_motor_control_mode/clear_motor_error`；部分 refresh 实现会发送使能帧，名称里的“read”不等于纯观测。

本文只给出桥接实施依据，未修改运控仓库或驱动，不声称该补丁已接入。

## 现场待确认与验收边界

当前没有 SSH，以下必须现场核对后才能宣称接通：

- 机器人实际配置、motor_type、BMS_TYPE 与已安装包版本。
- 是否已经运行 rp-server；地址/端口、mock=false、GET 可达性。
- BMS daemon/socket 是否存在，记录结构是否匹配、实际保护位协议、未支持字段。
- 真实健康数据是否随发热/负载变化，断开后是否及时显示失联，重连是否恢复。
- 运控 diagnostics 是否提供有效反馈时间、同帧数据和唯一的总线+ID身份。
- 上位机关闭后机器人采集、落库、大屏汇总是否仍独立持续。

源代码核查可以确定接入路径，不能替代实机数据验收。页面未获取的数据应呈现“未接入/源未提供/过期”，不得用模拟数据补成正常。
