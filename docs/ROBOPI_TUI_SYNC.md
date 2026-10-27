# RoboPi TUI 真机同步记录

2026-10-26，经用户明确要求“在真机上进行同步”，已将 14 项硬件自检接入现有真机监控。监控、SSH 隧道、上位机 relay 和中央服务保持运行；没有安装开机自启。

## 机器人上具体改了哪里

原监控目录保持为 `/home/robo/.local/share/rp1-monitor/20261025-r1/`。Python 文件位于其 `.venv/lib/python3.12/site-packages/rp1_monitor/`，本次仅变更 5 个 Python 文件及启动器：

| 文件 | 操作 | 具体含义 |
| --- | --- | --- |
| `robopi_tui.py` | 新增 | 固定 14 项被动白名单、原插件及依赖校验、直接调用已有执行器；RP022 增加每进程地址空间限制 |
| `board_diagnostics.py` | 新增 | 单项任务、10 秒冷却、私有报告缓存、分项历史和本机接口 |
| `agent.py` | 替换 | 加入自检组件初始化、退出清理和可选配置字段；原采集逻辑保持 |
| `cli.py` | 替换 | 加入执行器参数并传入采集端配置 |
| `local_operations.py` | 替换 | 注册自检状态、单项执行和报告读取接口 |
| `start_observer.sh` | 替换 | 仅在原 agent 命令末尾追加下方参数及续行 |

```text
--robopi-tui-executor /opt/roboparty/lib/robopi-tui-source/706df3d/loader/plugin_executor.py
```

原启动器的 ROS 环境、Fast DDS 配置、Python、设备档案、状态目录、缓存限额、8891 回环端口、锁与 PID 保护均保留。采集端保持 CPU1、2、3、7，nice=10，没有扩大允许核心集合。

同步前真机监控未运行，8891 空闲。本次更新后启动一个实例，没有停止或启动原运控、推理、驱动、BMS、CAN 或其他原服务。没有安装 Python/系统依赖，没有修改已有 TUI、开发者模式、策略、模型、`robot.yaml`、网络或全局调度。

## 备份及正常生成的文件

```text
/home/robo/.local/share/rp1-monitor/20261025-r1/updates/20261026-tui-r2/
```

- `before/`：原 `agent.py`、`cli.py`、`local_operations.py` 和 `start_observer.sh` 共 4 份，已在同步后独立读回核对 SHA256。
- `stage/`、`manifest.json`、`applied.json`：新文件副本、前后哈希及应用记录。
- 原 `deployment.json`、`collector.pid` 更新为新监控进程身份；`observer.lock` 继续防止重复启动。
- 原 `collector.log` 正常追加；原 `state/` 保留，新增 `board-diagnostics.sqlite` 保存报告。没有清除既有缓存或原始数据。

两个新增 Python 模块原先不存在，没有原文件备份。如需回退，先用原 `stop_observer.py` 核对并停止本项目监控，再还原上述 4 份原文件；新模块可保留为不被旧代码引用的文件，无需删除报告缓存。没有在生产环境演练回退。

旧 `20261026-imu1` 的 46 文件源码包不包含本次 TUI 接入，不应直接覆盖当前安装。后续更新以本次清单和当前工程源码为准。

## 本机同步范围与入口

恢复 SSH 隧道，并核对进程身份后，仅重启本项目真机中央服务和 relay，加载当前工程代码：

| 链路 | 地址或端口 |
| --- | --- |
| 机器人采集端 | `127.0.0.1:8891`，仅机器人回环 |
| 本机 SSH 隧道 | `127.0.0.1:18971` → 机器人 8891 |
| 本机真机 relay | `http://127.0.0.1:18972` |
| 真机中央网页 | `http://127.0.0.1:18970` |

中央保持原 `--no-initialize`，没有 PostgreSQL DDL 或数据库重建。原数据库、设备配置、存储挂载、凭据及 relay 缓存保留，相关配置和凭据文件哈希一致。机器人没有配置中央地址或上传凭据。18940 演示环境未操作，真实报告没有混入 DEMO 机器人。

上位机入口：

```bash
cd /home/shn/Desktop/rp1-app
.venv/bin/rp1-monitor desktop --url http://127.0.0.1:18972
```

连接后打开左侧“硬件自检”。真机网页为 [RP1-ROBOPI2 诊断页](http://127.0.0.1:18970/robots/RP1-ROBOPI2/diagnostics)，继续使用原只读页面凭据。

机器人重启后，如需手动启动监控，仍使用原入口；已有实例运行时会拒绝重复启动：

```bash
bash /home/robo/.local/share/rp1-monitor/20261025-r1/start_observer.sh
```

本次没有新增 systemd 服务或开机自启。

## 真机验证结果与边界

- 目标虚拟环境导入成功，CLI 识别新参数；检查接口启用，目录和分项列表各 14 项，连续 GET 不产生新检查。
- 经上位机 relay 间隔至少 10 秒运行 RP034 和新增 RP029，直接调用原 TUI。RP034 返回 `pass`，RP029 返回 `needs_review`；串口枚举不等于通信通过。
- 两份真实报告均经机器人缓存、SSH、relay 缓存进入现有中央数据库，并获得匹配确认。分项历史分别保留，来源为 `ros2`，范围仍为 `software_observation_not_hardware_acceptance`。
- 上位机实际 GET 真机 relay，显示 14 项及上述历史，所选详情与来源正确；界面验证没有额外发起插件。
- 最终独立回读完整 48 个包文件，确认只有所列 5 个 Python 文件发生预期变更；69 个保护文件哈希一致，包含已列运控配置、原 IMU 修复模块、设备档案及 TUI 加载器/插件。BMS MainPID/InvocationID、电机包版本保持一致；这是明确集合核对，不是全盘审计。
- 启动后一次 3 秒样本约为单核 9.7%、RSS 121 MiB，当时 27 个线程全部限于 CPU1、2、3、7；最终复查仍全部排除 CPU0。该短时样本不能作为运动工况峰值或控制周期保证。

其余项目已可按需选择调用，本次没有逐一运行全部 14 项。没有发送 CAN 控制帧、使能电机或开启推理，没有动作或长期稳定性验收。RP040 仍未开放，电机温度不会因本次同步自动具备来源。

完整真实报告、哈希、PID、备份回读及真机界面截图仅保存在本机私有 `runtime/real-robopi2/tui-sync-20261026-r2/`，不提交真实采集数据或凭据。独立复核见 同步证据审查（本机历史材料，不随源码发布）。
