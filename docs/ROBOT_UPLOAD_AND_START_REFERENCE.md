# RP1 监听程序上传与启动：运维参考

更新日期：2026-10-26。

**TUI接入后的现行操作以[部署与通信手册](DEPLOY_AFTER_ROBOPI_TUI.md)为准。** 本文第3节保留IMU修复阶段的旧包更新记录；`20261026-imu1`包缺少当前TUI接入，不应再批量覆盖现有采集包。当前差异及备份见[真机同步记录](ROBOPI_TUI_SYNC.md)。

日常操作请先看[逐步讲解版](ROBOT_UPLOAD_AND_START.md)。本文保留详细检查、上位机隧道和relay恢复命令，供排查和运维时使用。

本手册适用于**已经完成首次依赖安装的当前机器人** `robo@192.168.137.250`，机器人编号为 `RP1-ROBOPI2`，24关节，ROS 2 Jazzy、ARM64、Python 3.12。

机器人只通过网线与上位机通信。监听只读已有ROS消息、BMS日志和系统指标，不启动策略、不使能关节、不操作CAN控制。以下操作仅涉及本项目的独立目录和监听进程。

> 只需要日常启动时，直接看第2节；需要上传新代码时，按第3节操作。已有监听正在运行时，不要再启动第二份。

## 1. 程序与路径

| 项目 | 当前路径或地址 |
| --- | --- |
| 上位机工程目录 | `/home/shn/Desktop/rp1-app` |
| 上位机上的源码包 | `runtime/rp1-monitor-agent-source-20261026-imu1.tar.gz` |
| 机器人上的独立目录 | `/home/robo/.local/share/rp1-monitor/20261025-r1` |
| 机器人Python环境 | 上述目录内的 `.venv` |
| 机器人启动脚本 | 上述目录内的 `start_observer.sh` |
| 机器人停止工具 | 上述目录内的 `stop_observer.py` |
| 机器人设备档案 | 上述目录内的 `profile.json` |
| 机器人本地监听 | `127.0.0.1:8891` |
| 上位机SSH隧道入口 | `127.0.0.1:18971` |
| 上位机relay | `http://127.0.0.1:18972` |
| 真机网页 | `http://127.0.0.1:18970/robots/RP1-ROBOPI2/overview` |

源码包包含本项目Python代码和`SOURCE-MANIFEST.json`校验清单，不包含ROS、隔离环境依赖、设备档案、启动/停止工具、凭据、数据库或采集数据。当前机器人已经具备这些运行条件；新机器人首次安装不能只套用本手册。

当前包已修复真实ROS IMU协方差数组导致观察线程退出的问题。旧`20261025-cpu1`包保留作历史版本，不再用于更新当前安装。启动器是独立文件，本次现场已将其CPU允许列表收窄到`1-3,7`，不在源码压缩包内。

监听程序入口是 `python -m rp1_monitor agent`。原有 `robopi-uart-bridge` 是另外的串口服务，不是本项目监听程序。

## 2. 日常启动：机器人已经安装好程序

### 2.1 从上位机登录机器人

在**上位机终端**执行：

```bash
ssh robo@192.168.137.250
```

按终端提示完成认证；本手册不保存密码或私钥。后续本节命令均在**机器人终端**执行。

```bash
base=/home/robo/.local/share/rp1-monitor/20261025-r1
```

可先查看是否已经监听：

```bash
ss -ltn '( sport = :8891 )'
```

出现`LISTEN`时，先按第4节核对是否就是本项目服务；确认已经运行后无需重复启动。

### 2.2 前台启动：便于查看输出

```bash
base=/home/robo/.local/share/rp1-monitor/20261025-r1
cd "$base"
env -u PYTHONPATH bash "$base/start_observer.sh"
```

此终端会被监听程序占用，按`Ctrl+C`请求本次监听退出。关闭SSH会话不作为可靠的常驻运行方式。

### 2.3 后台启动：日常持续监听

前台和后台两种方式**二选一**。希望关闭终端后继续运行时执行：

```bash
base=/home/robo/.local/share/rp1-monitor/20261025-r1
cd "$base"
nohup env -u PYTHONPATH bash "$base/start_observer.sh" \
  >> "$base/collector.log" 2>&1 < /dev/null &
```

启动命令返回不等于启动成功，必须继续按第4节检查。若日志提示“监听仍在运行”“启动器已在运行”或“8891端口已占用”，先核对原进程，不要反复启动或结束其他程序。

启动脚本在Python和ROS初始化前通过`taskset --cpu-list 1-3,7`限制CPU，保留本次策略运行期间已有的CPU集合。全部监听线程及其自有日志读取子进程继承该限制；优先级为`nice=10`。不要继续使用没有CPU限制或扩大CPU集合的旧启动命令。

以上两种启动命令先切换到独立目录，并在加载ROS前清除继承的`PYTHONPATH`，避免旧源码目录遮蔽隔离环境中的已更新代码。随后由启动脚本重新加载Jazzy所需路径。

脚本会加载`/opt/ros/jazzy/setup.bash`及本次核对的Fast DDS配置。`ROS_DOMAIN_ID`继承调用终端，未设置时使用ROS默认值0；必须与实际运控一致。不要为了让监控连通而改动正在运行的运控环境。

当前没有安装开机自启。机器人重启后，需要重新启动监听；上位机的SSH隧道也需要恢复。

## 3. 上传并更新代码

本节用于更新本项目采集代码。已有依赖继续使用隔离`.venv`，不向系统Python安装包，不覆盖`roboparty_deploy`。

### 3.1 上位机：上传源码包

在**上位机终端**执行：

```bash
cd /home/shn/Desktop/rp1-app

scp runtime/rp1-monitor-agent-source-20261026-imu1.tar.gz \
  robo@192.168.137.250:~/
```

这份包是2026-10-25整理的源码快照，包含每核CPU采集功能。以后源码发生变化时，应重新打包并使用新的版本名称，不要把旧包当作新版本。

### 3.2 机器人：解压到新目录并校验

在**机器人终端**执行。可以保持原监听运行，先完成解压和校验：

```bash
base=/home/robo/.local/share/rp1-monitor/20261025-r1
mkdir -p "$base/releases"
release="$(mktemp -d "$base/releases/source-XXXXXXXX")"

tar -xzf "$HOME/rp1-monitor-agent-source-20261026-imu1.tar.gz" \
  -C "$release"

python3 - "$release" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
manifest = json.loads((root / 'SOURCE-MANIFEST.json').read_text())
for name, expected in manifest['files'].items():
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise SystemExit('校验路径异常，停止更新。')
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit(f'文件校验失败：{name}，停止更新。')
print('源码校验通过。')
PY
```

只有出现“源码校验通过”后才能继续。校验失败时保留原监听，不执行后面的代码替换。

### 3.3 机器人：停止本项目监听，等待退出

```bash
python3 "$base/stop_observer.py"
```

停止工具先核对PID、启动时间与本项目解释器，再向本次监听发送退出请求；缓存和原有运控程序保留。它是异步退出请求，需要等待进程释放端口：

```bash
ss -ltn '( sport = :8891 )'
```

确认8891不再出现`LISTEN`，且下面命令不再显示本次监听的活动进程后，才进行替换：

```bash
ps -p "$(cat "$base/collector.pid")" -o pid=,stat=,args=
```

若停止工具报告“PID身份不匹配”，停止操作并核对部署记录，不使用`killall python`、`pkill python`或结束运控来腾出端口。

### 3.4 机器人：备份并替换隔离环境中的本项目代码

继续在**同一个机器人终端**执行，保留前面的`base`和`release`变量：

```bash
package_dir="$base/.venv/lib/python3.12/site-packages/rp1_monitor"

test -f "$package_dir/__main__.py"
test -f "$release/rp1_monitor/__main__.py"
```

两个检查均应成功；失败时不要继续复制。然后备份原代码并替换：

```bash
mkdir -p "$base/backups"
backup="$(mktemp -d "$base/backups/code-XXXXXXXX")"

cp -a "$package_dir" "$backup/rp1_monitor"
cp "$release/rp1_monitor/"*.py "$package_dir/"
```

这一步只替换隔离环境内的`rp1_monitor/*.py`。本次包包含46个Python文件；完整清单与哈希在解压目录的`SOURCE-MANIFEST.json`。不覆盖`.venv`依赖、`profile.json`、`state/`缓存、启动/停止脚本及原有机器人代码。

导入检查：

```bash
cd "$base"
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  "$base/.venv/bin/python" -B - "$package_dir" <<'PY'
import sys
from pathlib import Path
import rp1_monitor
from rp1_monitor.agent import AgentSettings
from rp1_monitor.host_cpu import CPUObserver

actual = Path(rp1_monitor.__file__).resolve().parent
expected = Path(sys.argv[1]).resolve()
if actual != expected:
    raise SystemExit(f'导入来源异常：{actual}，停止启动。')
print(f'采集代码导入通过：{actual}')
PY
```

实际启动时仍使用第2节带`env -u PYTHONPATH`的命令。仅通过新开SSH终端不能保证清除启动配置中持久设置的旧路径。启动前还须确认与现场相同的`ROS_DOMAIN_ID`。

### 3.5 机器人：重新启动

导入检查通过后，按第2.2节或第2.3节启动，再完成第4节验证。不必重启机器人、运控、策略、BMS或串口桥接服务。

若新代码导入失败，保持监听停止，在仍保留本次`backup`和`package_dir`变量的终端恢复本项目Python文件，再重新导入检查：

```bash
cp "$backup/rp1_monitor/"*.py "$package_dir/"
```

若终端已关闭，先从`$base/backups/`核对本次备份目录，不能随意选择其他版本。不要删除缓存、卸载系统依赖或改动原运控来修复监控。

## 4. 验证机器人监听是否正常

以下命令在**机器人终端**执行。

### 4.1 核对服务身份和采集状态

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:8891/api/v1/state | python3 -m json.tool
```

应能看到：

- `robot_id`为`RP1-ROBOPI2`，`source_kind`为`ros2`。
- `upload.stage`为`robot_buffer`。
- `upload.collector_error`没有采集错误。
- 主机、电池按实际来源更新；缺失字段保持未知。

关节尚未使能、原节点未发布或ROS环境不匹配时，关节话题可能没有观测。没有关节数据不能直接判为电机故障；电机温度和故障还需要诊断发布与已核对的电机映射。

### 4.2 核对全部监听线程均排除CPU0

```bash
pid="$(cat "$base/collector.pid")"
taskset --all-tasks --pid --cpu-list "$pid"
```

当前每个线程的允许列表均应为`1-3,7`，不能包含`0`。`ps`中某一瞬间的运行核编号不能替代这个检查。

本项目还会启动一个读取BMS日志的`journalctl`子进程，可以先查看：

```bash
pgrep -P "$pid" -a
```

对其中确认属于本项目的`journalctl` PID，执行`taskset -pc <该PID>`，当前结果也应为`1-3,7`。不要对原有`bms_daemon`、运控或系统服务执行修改CPU设置的命令。

### 4.3 查看启动日志

后台方式启动时：

```bash
tail -n 60 "$base/collector.log"
```

仅有PID文件不代表启动成功，需结合实际进程、端口和状态接口。分享问题时应先去除凭据和真实采集数据，不提交整个运行目录。

## 5. 上位机接收并显示数据

机器人监听正常后，还需要上位机上的SSH隧道、relay和中央服务。已有对应端口监听时，不重复启动。

### 5.1 上位机：检查或恢复SSH隧道

在**上位机终端**执行：

```bash
ss -ltn '( sport = :18971 )'
```

没有监听时，用独立终端建立隧道，并保持这个终端运行：

```bash
ssh -N \
  -o StrictHostKeyChecking=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18971:127.0.0.1:8891 \
  robo@192.168.137.250
```

主机指纹不匹配时先按正常SSH流程核对，不关闭主机密钥检查。

### 5.2 上位机：检查或恢复relay

另开**上位机终端**：

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:18972/api/v1/state | python3 -m json.tool
```

正常应为`upload.stage=upper_computer`、机器人身份正确且`upload.collector_connected=true`。中央上传状态另看`upload.error`、`last_success_at`等字段，网线连通不代表中央入库已经成功。

仅在18972没有已有relay、当前中央服务18970已经运行、私有配置文件仍在时，执行：

```bash
cd /home/shn/Desktop/rp1-app

.venv/bin/python -m rp1_monitor relay \
  --source ros2 \
  --robot-id RP1-ROBOPI2 \
  --collector http://127.0.0.1:18971 \
  --backend http://127.0.0.1:18970 \
  --token-file runtime/real-robopi2/credentials/device-token.txt \
  --state-dir runtime/real-robopi2/relay \
  --port 18972
```

该方式前台运行，关闭终端会影响持续转发。当前已有独立后台relay时无需执行。设备写入凭据只留在上位机，不能上传到机器人。本手册不重建中央服务或数据库。

### 5.3 上位机桌面和网页

桌面程序连接relay：

```bash
cd /home/shn/Desktop/rp1-app
.venv/bin/python -m rp1_monitor desktop --url http://127.0.0.1:18972
```

真机网页：`http://127.0.0.1:18970/robots/RP1-ROBOPI2/overview`。需要平台登录或本机私有只读凭据，不将凭据写进URL或手册。原18940是模拟环境入口，不用它判断真机是否接通。

关闭桌面窗口不会停止独立运行的机器人监听、SSH隧道和relay。

## 6. 停止监听与数据保留

前台监听使用`Ctrl+C`；后台监听在机器人执行：

```bash
python3 /home/robo/.local/share/rp1-monitor/20261025-r1/stop_observer.py
```

该工具保留本项目文件和缓存，不停止策略或其他机器人服务。退出请求之后仍需等进程真正结束，再启动下一份监听。

机器人摘要临时保存在`state/pending.sqlite`，上位机确认持久接收后释放对应队列记录。当前128MiB是待传JSON载荷预算，超限会淘汰最早未传摘要并记录丢失；512MiB是剩余空间检查阈值，触发后摘要落盘线程停止，需要处理空间后重启监听。两者都不是目录硬配额。不要删除`state/`、数据库或既有录包来腾出空间。

## 7. 常见情况

| 现象 | 核对方法 |
| --- | --- |
| 提示监听或启动器已运行 | 第4节确认身份和实时状态，避免启动第二份 |
| SSH能登录但电脑收不到数据 | 先检查机器人8891，再检查上位机18971隧道和18972 relay |
| 网页仍显示模拟机器人 | 使用18970真机入口，核对选中设备和来源 |
| 有电池、主机数据，但没有关节数据 | 核对原节点是否发布、ROS Domain/DDS是否一致；不要由监控使能关节 |
| 关节位置有了，但电机温度仍未知 | 还需要独立的电机诊断发布及真实映射；主机热区不能代替电机温度 |
| CPU0仍有整体负载 | 本项目线程排除CPU0，不代表其他程序和内核中断不会使用CPU0 |
| 手册路径在另一台机器人上不存在 | 这是当前已部署机器人的更新手册；新机器需先核对ROS、架构、Python和独立设备档案 |

改动范围见[机器人侧改动清单](ROBOT_CHANGES.md)，桌面连接细节见[上位机桌面读取机器人](DESKTOP_CONNECTION.md)。
