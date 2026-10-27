# 已安装 RoboPi TUI 后，怎样部署监控并连接上位机

整理日期：2026-10-26。本文是操作说明，本次编写文档没有连接机器人、上传文件或启停程序。整体设计见 [项目技术架构](TECHNICAL_ARCHITECTURE.md)。

先区分两种情况：

- **当前这台 `RP1-ROBOPI2`**：监控代码和TUI适配已经同步，按第一部分重新连接即可，不需要再次安装或上传。
- **另一台只有TUI、没有本项目采集端的机器人**：先按第二部分准备和上传，再接入第一部分的电脑链路。实际用户名、ROS、TUI路径和设备身份要重新核对。

## 一、当前这台机器人：从上电到上位机显示

### 先理解要启动的四样东西

```text
机器人采集端8891 → 网线SSH隧道18971 → 电脑relay18972 → 中央服务18970
                                             ↓              ↓
                                           Qt上位机        网页
```

TUI负责按需自检，不会自动向上位机发送关节数据。Qt负责显示，不会替你启动机器人采集端。relay是电脑上独立的接收与上传程序；图中端口属于不同进程，不能互换。

| 名称 | 在哪里运行 | 本次用途 |
| --- | --- | --- |
| SSH登录终端 | 电脑连接机器人 | 在机器人执行查看/启动本项目监控的命令 |
| `agent` | 机器人 | 订阅现有ROS、读取主机/BMS、汇总缓存 |
| SSH隧道 | 电脑后台 | 把机器人8891安全转接到电脑18971 |
| `relay` | 电脑 | 从18971接收，缓存后向18970上传 |
| `server` | 当前也在电脑 | 提供中央API、网页和数据库查询 |
| Qt | 电脑桌面 | 连接18972显示状态和硬件自检 |

以下命令不包含密码。SSH需要认证时在终端交互输入，或使用已配置的密钥。首次连接应向设备负责人核对主机指纹后接受；指纹变化时先核实原因。

### 第1步：电脑用网线连接并登录机器人

**执行位置：电脑终端。**

```bash
ssh robo@192.168.137.250
```

含义：登录机器人Linux，不会启动运控或使能电机。提示符变成`robo@robopi2`后，接下来该终端中的命令就在机器人上执行。

先查看监控端口有没有监听：

```bash
ss -lntp 'sport = :8891'
```

只有表头表示未发现监听；看到`127.0.0.1:8891`表示端口已占用，后面应通过doctor核对是否为正确采集端。不要因端口被占用就结束不明进程。

### 第2步：需要时启动机器人采集端

**执行位置：刚登录的机器人终端。已有正确采集端运行时跳过。**

```bash
cd /home/robo/.local/share/rp1-monitor/20261025-r1 && \
env -u PYTHONPATH bash /home/robo/.local/share/rp1-monitor/20261025-r1/start_observer.sh
```

含义：先进入本项目独立目录，再用`env -u PYTHONPATH`为本次启动清除可能指向旧源码的导入路径，避免错加载临时测试版本；不修改系统环境文件。随后启动本项目自己的监控，不是启动原运控。此启动器已经包含：

| 已配置内容 | 含义 |
| --- | --- |
| CPU1、2、3、7及`nice=10` | 在Python/ROS初始化前排除CPU0，后续线程和自有日志子进程继承 |
| `/opt/ros/jazzy/setup.bash`及原Fast DDS配置 | 让监听节点使用当前机器人真实ROS通信环境 |
| 独立`.venv`、`profile.json` | 使用本项目Python和该台机器人档案 |
| `--source ros2 --robot-id RP1-ROBOPI2` | 明确真实来源与身份，不自动退回模拟数据 |
| `state/`、128MiB摘要预算、512MiB磁盘保留 | 缓存待转交摘要；不是内存或整个目录上限 |
| `--robopi-tui-executor .../plugin_executor.py` | 复用已经安装的TUI，不安装/升级TUI |
| 锁、PID/启动时间和端口检查 | 防止重复启动本项目监听 |

这是**前台运行**：首次核对时保留该终端，日志在终端显示；`Ctrl+C`请求本次监控退出。需要继续执行电脑命令时，另开电脑终端。

确认前台读取正常后，如需关闭SSH窗口仍保持监控，先结束这次前台实例，再在机器人终端手动运行：

```bash
cd /home/robo/.local/share/rp1-monitor/20261025-r1 && \
nohup env -u PYTHONPATH bash /home/robo/.local/share/rp1-monitor/20261025-r1/start_observer.sh \
  >> /home/robo/.local/share/rp1-monitor/20261025-r1/collector.log 2>&1 < /dev/null &
```

`nohup`让进程脱离SSH会话；`>>`追加本项目日志；`2>&1`把错误也写入该日志；末尾`&`放到后台。**这不等于开机自启。** 机器人重启后需要再次手动启动。已经运行的实例不会因再次执行而增加第二份监听。

### 第3步：电脑建立独立SSH隧道

**执行位置：另一个电脑终端。** 先查看本机18971是否已监听：

```bash
ss -lntp 'sport = :18971'
```

若已有本项目正确隧道，跳到下方doctor验证；若空闲，再执行：

```bash
ssh -fN -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18971:127.0.0.1:8891 robo@192.168.137.250
```

这条命令的含义：

| 参数 | 含义 |
| --- | --- |
| `-L 127.0.0.1:18971:127.0.0.1:8891` | 电脑访问18971时，SSH把请求转到机器人自己的8891 |
| `-N` | 只转发，不执行机器人命令 |
| `-f` | 认证成功后把隧道放到电脑后台 |
| `StrictHostKeyChecking=yes` | 只连接已核对主机身份；第1步完成首次指纹确认 |
| `ExitOnForwardFailure=yes` | 转发端口无法建立就报错退出，避免误以为已连接 |
| `ServerAlive...` | 检测失联；隧道失效后仍需人工重建，不会无限自动重启 |

然后在电脑验证采集端身份：

```bash
cd /home/shn/Desktop/rp1-app
.venv/bin/python -m rp1_monitor doctor --url http://127.0.0.1:18971
```

应看到`snapshot.upload.stage`为`robot_buffer`、`snapshot.robot_id`为`RP1-ROBOPI2`、`snapshot.source_kind`为`ros2`，对应机器人采集端。这只查询，不执行TUI插件。端口通了但身份不符时停止接入并核对隧道，不继续上传。

### 第4步：确认中央服务；未启动时再启动

**执行位置：电脑终端。**

```bash
curl --fail --silent --show-error http://127.0.0.1:18970/health
```

返回`status: ok`说明中央HTTP进程可访问；这不是数据库写入或机器人在线的证明。正常时跳过下面启动命令。先通过18970的页面和后续上传状态核对完整链路。

若中央未启动，且原PostgreSQL、私有配置仍完好，在电脑新终端执行已有配置对应的命令：

```bash
cd /home/shn/Desktop/rp1-app
.venv/bin/python -m rp1_monitor server \
  --devices /home/shn/Desktop/rp1-app/runtime/real-robopi2/device-config.json \
  --database-url-file /home/shn/Desktop/rp1-app/runtime/real-robopi2/credentials/api-database-url.txt \
  --no-initialize \
  --viewer-token-file /home/shn/Desktop/rp1-app/runtime/real-robopi2/credentials/viewer-token.txt \
  --admin-config /home/shn/Desktop/rp1-app/runtime/real-robopi2/management/admin.json \
  --storage-mounts /home/shn/Desktop/rp1-app/runtime/real-robopi2/storage-mounts.json \
  --port 18970
```

含义：读取电脑现有私有文件，提供中央API和网页。`--no-initialize`保留当前数据库，禁止应用启动时自动建表。此命令前台运行，保留该终端；文件不存在或数据库不可用时先修复对应部署，不重新生成凭据、删库或初始化覆盖。

已有Docker数据库继续使用原容器和数据卷；本文不新建容器或运行SQL迁移。数据库历史部署见 [管理与PostgreSQL](MANAGEMENT_AND_POSTGRES.md)。

### 第5步：确认电脑relay；未启动时再启动

**执行位置：电脑终端。**

```bash
cd /home/shn/Desktop/rp1-app
.venv/bin/python -m rp1_monitor doctor --url http://127.0.0.1:18972
```

若`snapshot.upload.stage`为`upper_computer`，且`snapshot.robot_id`为`RP1-ROBOPI2`、`snapshot.source_kind`为`ros2`，说明relay已经运行，不再启动一份。若未监听，先检查端口空闲，再在电脑新终端运行：

```bash
cd /home/shn/Desktop/rp1-app
.venv/bin/python -m rp1_monitor relay \
  --source ros2 --robot-id RP1-ROBOPI2 \
  --collector http://127.0.0.1:18971 \
  --backend http://127.0.0.1:18970 \
  --token-file /home/shn/Desktop/rp1-app/runtime/real-robopi2/credentials/device-token.txt \
  --state-dir /home/shn/Desktop/rp1-app/runtime/real-robopi2/relay \
  --cache-mib 256 --reserve-mib 512 --port 18972
```

含义：`collector`是电脑上的SSH隧道入口，`backend`是中央API，`token-file`是绑定当前机器人的中央上传凭据，`state-dir`保存电脑待传缓存。**这些中央地址和凭据只在电脑，不上传机器人。**

先保持前台，观察错误和上传状态。关闭此终端会停止前台relay；要持续后台运行需由独立后台会话或后续审阅的服务托管，不能依赖Qt窗口。不要为同一机器人、端口和状态目录启动多个relay。

### 第6步：打开上位机和网页

**Qt，电脑终端执行：**

```bash
cd /home/shn/Desktop/rp1-app
.venv/bin/rp1-monitor desktop --url http://127.0.0.1:18972
```

在设置中选择“上位机后台relay”，点击“连接监控”。地址是18972，不是机器人IP或中央18970。先核对设备`RP1-ROBOPI2`、来源`ros2`、连接新鲜度与中央上传状态。

若使用“诊断助手”，还需在它的“连接与模型设置”中将**诊断中央服务地址**填写为`http://127.0.0.1:18970`，提供现有页面只读凭据。此请求直接访问中央，不经relay代理；与Qt实时连接的18972分开配置。仅查看实时健康和运行本机硬件自检不需要模型Key。

**网页，在电脑浏览器打开：**

- [真机整机状态](http://127.0.0.1:18970/robots/RP1-ROBOPI2/overview)
- [电池](http://127.0.0.1:18970/robots/RP1-ROBOPI2/battery)
- [关节](http://127.0.0.1:18970/robots/RP1-ROBOPI2/joints)
- [主机负载](http://127.0.0.1:18970/robots/RP1-ROBOPI2/host)
- [诊断与最近TUI报告](http://127.0.0.1:18970/robots/RP1-ROBOPI2/diagnostics)

网页使用现有只读页面凭据，不用设备上传凭据或SSH密码。18940是原模拟环境；不要把DEMO设备的温度当成真机。网页必须通过HTTP服务访问，直接打开`index.html`文件不会建立正确数据链路。

### 怎样确认确实接通

| 检查点 | 正常表现 | 不能因此推断 |
| --- | --- | --- |
| 隧道doctor | `snapshot.upload.stage=robot_buffer`、身份和来源匹配 | 不能证明全部硬件数据已提供 |
| relay doctor / Qt | `snapshot.upload.stage=upper_computer`、预览在更新 | 不能只看“进程在线”就判健康 |
| 主机页 | 8核观测、内存、板温、开机时长更新 | 板温不代表电机温度 |
| 电池页 | 已提供的SOC/电压/电流等按来源更新 | 不存在的温度/保护位不能补零 |
| 关节页 | 原发布者有反馈时显示实际24关节数据 | 推理运行不保证温度诊断存在 |
| 上传状态/网页 | 中央确认入库、真实机器人历史更新 | 仅`/health`成功不代表落库成功 |

缺失关节数据时先看原话题是否发布、ROS观察线程是否报错、DDS环境是否一致，不由监控启动或使能电机。当前电机/MOS温度和故障源仍未接通，热像保持未知是正确行为。

### 怎样使用TUI检查

Qt左侧“硬件自检” → “读取状态/历史报告” → 选择一个项目 → 查看说明 → “运行所选单项”。刷新只读取，执行才调用原TUI及必要前置项；两次发起至少间隔10秒。

当前14项及限制见 [硬件自检说明](HARDWARE_CHECKS.md)。网页只读展示最近报告，不能触发检查。本次真机同步实际验证了RP034、RP029两项；其余项目没有因出现在列表中就被自动验收。

### 怎样停止、重启后要做什么

**机器人终端，停止本项目采集端：**

```bash
python3 /home/robo/.local/share/rp1-monitor/20261025-r1/stop_observer.py
```

它核对PID、启动时间和解释器，只请求本项目监听退出；保留缓存，不停止原运控/BMS/推理。前台试运行也可用`Ctrl+C`。

关闭Qt只关闭窗口。电脑前台relay/server可在各自终端`Ctrl+C`停止；不要用按名称批量杀Python进程的命令。后台进程需要先核对各自身份再结束。

机器人重启后：手动启动采集端 → 重建SSH隧道 → 核对relay身份/恢复上传 → 打开Qt。电脑重启后还需恢复原数据库、中央服务和relay。本项目当前未安装开机自启。以前按要求只`stop`过的原`rp-server.service`没有被disable/mask，重启后它仍可能自行尝试启动；本监控不依赖它，不把启动它作为部署步骤。

## 二、只有TUI的新机器人：代码怎样准备和上传

这一部分是**首次部署流程**，没有在本轮执行，也不要用于覆盖当前`20261025-r1`安装。新机依赖必须在匹配环境准备，不能仅复制上位机目录就声称部署完成。

### 1. 先确认四件事

| 信息 | 为什么需要 |
| --- | --- |
| CPU架构、ROS发行版、对应Python ABI | ARM64机器人不能用电脑x86_64的虚拟环境或二进制wheel；当前现场为Jazzy/Python3.12 |
| 实际运控工作空间与DDS配置 | 采集节点必须与已有发布者处于一致ROS环境；不要把通用Humble示例覆盖到Jazzy |
| TUI实际执行器路径、插件版本与依赖 | TUI可能源码安装，`dpkg`查不到不代表没有安装 |
| 新设备ID、已有BMS日志读权限、进程名单 | 身份用于缓存和中央绑定；权限不足时显示缺失，不另开硬件端口 |

例如在**机器人终端**只定位TUI入口：

```bash
command -v robopi-tui
readlink -f /usr/local/bin/robopi-tui
```

当前已验证路径为`.../robopi-tui-source/706df3d/loader/plugin_executor.py`；Debian布局也可能是`/opt/roboparty/lib/robopi-tui/loader/plugin_executor.py`。入口位置与版本变化需要重新核对，不能直接更新插件白名单声明或重装TUI来跳过检查。

### 2. 在电脑生成当前项目安装包

**执行位置：电脑项目目录。** 下方目录名是新机首次交付示例，已有同名交付时换新名字，保留旧包和清单。

```bash
cd /home/shn/Desktop/rp1-app
mkdir -p runtime
mkdir runtime/agent-first-install && \
mkdir runtime/agent-first-install/wheels && \
.venv/bin/python -m pip wheel --no-deps --no-build-isolation --no-index \
  --wheel-dir runtime/agent-first-install/wheels .
```

含义：从**当前源码**生成本项目wheel；`--no-deps`不在电脑上下载机器人依赖，`--no-index`禁止网络下载构建依赖，构建工具须事先在电脑虚拟环境中具备。包包含共用代码和网页资源，但按角色启动；不会把电脑运行凭据或`runtime/`打进包。

然后在**匹配目标ARM64、Python和系统的准备环境**按`pyproject.toml`补齐`agent`依赖wheel，放进同一`wheels/`。ROS的`rclpy`、`sensor_msgs`、`diagnostic_msgs`复用机器人已安装版本，不用pip另装ROS。不要在普通x86电脑直接下载一批默认wheel后当作ARM离线依赖；具体wheel集合由目标环境决定。

TUI已有依赖继续使用现场安装，不随本项目安装器升级。先在匹配的隔离环境检查安装/导入，再对交付文件生成SHA256：

```bash
cd /home/shn/Desktop/rp1-app/runtime/agent-first-install
sha256sum wheels/*.whl > SHA256SUMS
```

旧`rp1-monitor-agent-source-20261026-imu1.tar.gz`仅46个模块，缺本次TUI接入；不要用它批量覆盖当前48模块的部署。旧统一源码包也不等于带齐ARM依赖的一键安装器。

### 3. 上传到独立新目录

以下以同类RP1、用户`robo`、地址`192.168.137.250`为**命令格式示例**；仅对尚未部署本项目的新机使用，按实际新机修改目标。

**执行位置：电脑终端。**

```bash
ssh robo@192.168.137.250 'mkdir -p /home/robo/.local/share/rp1-monitor' && \
ssh robo@192.168.137.250 'mkdir -m 700 /home/robo/.local/share/rp1-monitor/first-install' && \
scp -r /home/shn/Desktop/rp1-app/runtime/agent-first-install/wheels \
  /home/shn/Desktop/rp1-app/runtime/agent-first-install/SHA256SUMS \
  robo@192.168.137.250:/home/robo/.local/share/rp1-monitor/first-install/
```

前两条创建本项目父目录及本次独立目录，后者已存在会报错。`&&`表示前一步成功才继续；目录创建失败就不会执行`scp`。`scp`只复制安装包和校验单，不安装、不启动、不改原运控。该目标目录须为空且专用于此次安装；已有安装应走后面的更新流程。

**切换到机器人终端，校验后创建独立环境：**

```bash
cd /home/robo/.local/share/rp1-monitor/first-install && \
sha256sum --check SHA256SUMS && \
python3 -m venv --system-site-packages .venv && \
.venv/bin/python -m pip install --no-index --find-links wheels 'rp1-monitor[agent]==0.4.0'
```

这组命令用`&&`串联，校验失败就不会创建环境，环境创建失败也不会安装。这里的`python3`必须是匹配现有ROS的解释器。`.venv`把本项目安装文件放在独立目录；`--system-site-packages`允许读取已有系统包，避免复制ROS或改写系统Python。`--no-index`只用本地wheel，缺依赖则失败，不要求机器人上外网，也不自动安装系统软件。

### 4. 加载现有ROS并检查导入

**执行位置：机器人终端。以下为已核对的同类Jazzy现场示例。**

```bash
source /opt/ros/jazzy/setup.bash
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMW_FASTRTPS_USE_QOS_FROM_XML=1
export FASTRTPS_DEFAULT_PROFILES_FILE=/home/robo/roboparty_deploy/assets/rt_fastdds_profile.xml
env -u PYTHONPATH taskset --cpu-list 1-3,7 .venv/bin/python -B -c \
  'import rclpy, sensor_msgs.msg, diagnostic_msgs.msg, rp1_monitor; print("基础导入成功")'
```

`source`和`export`只设置当前终端环境，不修改系统ROS配置。DDS取值应与该台机器人现有运控一致；不要擅自设置`ROS_DOMAIN_ID=0`或替换已有DDS文件。CPU集合也须按实际现场确认，始终排除CPU0；当前机器人确认的是1、2、3、7。

导入成功仅证明依赖存在，不证明话题、插件或硬件已通过。若已有工作空间需要额外`source`，使用原团队确认的工作空间环境，不在部署时重编译运控。

### 5. 创建设备档案并首次前台启动

**执行位置：机器人终端，继续使用上述独立目录。** 下面`RP1-NEW-01`是待替换的**新机器人编号**，不要冒用已有机器人ID。

```bash
.venv/bin/python -B -m rp1_monitor profile-init \
  --robot-id RP1-NEW-01 --output profile.json
```

它只生成监控档案，不探测硬件。按真实信息填写版本、BMS来源和核心进程`comm`；尚未确认的电机映射留空，`physical_mapping_verified`保持`false`。24/28自由度和界面部位映射还需在中央管理工作台配置，不能用模拟映射代替。

确认8891空闲后，前台启动示例：

```bash
env -u PYTHONPATH taskset --cpu-list 1-3,7 nice -n 10 .venv/bin/python -B -m rp1_monitor agent \
  --source ros2 --robot-id RP1-NEW-01 --profile profile.json \
  --state-dir state --cache-mib 128 --reserve-mib 512 --port 8891 \
  --robopi-tui-executor /实际已核对的安装位置/loader/plugin_executor.py
```

执行前替换最后的占位路径。此命令不包含中央地址、Token或模型Key；不加`--rp-server-url`，默认跟随已有BMS日志。普通账号不能读日志时先向维护者核对授权，不能为此另启BMS服务或用root运行整个监听。

新机此处仅首次前台验证，用`Ctrl+C`结束。本命令没有当前正式启动器的锁/PID保护；不要重复运行、直接`nohup`或自动重启。稳定部署时需为新机单独准备已核对的启动器、锁和PID记录；不能原样复制当前机器人启动器，因为其中绑定了目录、ID和现有`deployment.json`。

### 6. 电脑登记、relay和界面

1. 在已有中央`/admin`登记**新设备ID、真实来源、24/28配置**，取得该设备专用上传凭据；保存在电脑私有目录，仅本人可读，不复制机器人。
2. 为新机器人分配电脑独立隧道端口、relay端口及缓存目录。一个机器人一份绑定relay，不能复用`RP1-ROBOPI2`的凭据或缓存。
3. 按第一部分建立SSH转发，先doctor核对新ID，再启动对应relay，Qt连接该relay。
4. 中央服务已部署时沿用当前数据库和只读身份；新中央实例另按 [数据库设计](CENTRAL_DATABASE_DESIGN.md) 和 [管理部署](MANAGEMENT_AND_POSTGRES.md) 审阅SQL后部署，不由agent初始化。
5. 核对主机/BMS、已有关节与IMU、断网补传和单项TUI报告；记录真实发布频率、CPU/RSS和控制周期差异。没有完成的项目保持待验收。

另一台电脑首次安装时，使用独立虚拟环境，按角色安装：Qt选`desktop`，relay选`agent`，中央选`server`。机器人只装`agent`；无需Qt、PostgreSQL服务器或模型依赖。当前电脑已有`.venv`，第一部分不需要重装。

## 三、已经部署的机器人，后续更新究竟改哪里

当前机器人已完成的TUI同步范围如下，**不是本轮再次执行的操作**：

| 机器人独立监控中的位置 | 操作 | 含义 |
| --- | --- | --- |
| `.venv/lib/python3.12/site-packages/rp1_monitor/robopi_tui.py` | 新增 | 原TUI适配、14项白名单、执行边界 |
| 同目录`board_diagnostics.py` | 新增 | 单项任务、报告缓存和历史 |
| 同目录`agent.py` | 替换 | 初始化/清理自检组件 |
| 同目录`cli.py` | 替换 | 接收执行器路径参数 |
| 同目录`local_operations.py` | 替换 | 注册本机检查接口 |
| `start_observer.sh` | 追加一个参数 | 指向现场已安装的TUI执行器；保留CPU、ROS、锁和原配置 |

备份在`/home/robo/.local/share/rp1-monitor/20261025-r1/updates/20261026-tui-r2/before/`。本项目正常维护自己的日志、PID、部署身份记录、摘要和报告缓存，不改原运控文件。完整过程见 [真机同步记录](ROBOPI_TUI_SYNC.md)。

以后更新应遵循这个顺序：

1. 在电脑完成代码与软件回放验证，列出此次机器人**逐文件差异**及启动参数变化。
2. 只上传到新的`updates/本次版本/stage/`；核对SHA256、解释器、原文件和监控进程身份。
3. 在原文件备份完成、改动范围明确后，用`stop_observer.py`只停止本项目监听；保留原档案、缓存和启动保护。
4. 应用已审阅的文件；若依赖变化另行列明，不整目录覆盖、不顺带升级TUI或系统包。
5. 用原启动器启动一次，核对全部线程CPU集合、数据来源、新鲜度、上传及原有服务状态。

升级某个已有版本时，上传命令的格式为：

```text
scp 电脑上已审阅的待更新文件 robo@机器人IP:监控目录/updates/本次版本/stage/
```

`scp`只是传文件，不等于已经应用更新。正式替换前需有具体文件清单；不要直接把旧源码包解压覆盖`site-packages`，也不要把通用`deploy/start-agent.sh`替换现场启动器——通用模板没有当前CPU集合、锁和PID保护。

## 四、常见故障按哪一段排查

| 现象 | 先检查 |
| --- | --- |
| SSH不通 | 网线、电脑/机器人实际IP、机器人是否开机、SSH认证；不调整CAN |
| 18971访问失败 | 机器人8891是否监听、隧道是否存活、端口是否冲突 |
| doctor身份错误 | 隧道目标或relay绑定；不带错身份继续上传 |
| 主机/BMS更新但关节不更新 | ROS观察线程日志、已有话题和DDS一致性；不是直接重启推理 |
| Qt正常但网页不更新 | relay上传状态、设备凭据/禁用状态、中央/数据库；不是Qt必须一直开着 |
| 硬件自检按钮禁用 | 板端新模块、执行器参数、元数据/依赖/权限和冷却状态 |
| 插件错误/超时 | 查看该项原始证据；不自动循环重试或执行主动修复 |
| 电机温度未知 | 当前无有效诊断来源/确认映射；TUI板温不能补成电机温度 |

整个接入流程不要求修改运控策略、权重、`robot.yaml`、CAN参数、驱动或BMS实现，也不要求启用关节。只读监控仍有CPU、内存和磁盘开销，长期推理工况应单独比较控制周期和资源使用；连接成功不能替代这项验收。
