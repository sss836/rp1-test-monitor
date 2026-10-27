# 从GitHub启动RP1监控：SSH地址与密码连接

2026-10-27。面向Ubuntu电脑和已有ROS 2的RP1机器人。监控只读数据，不发布动作、不使能电机、不启停推理。机器人通过网线连接电脑；运行期间无需机器人联网或配置中央凭据。

## 电脑：三行启动

```bash
git clone https://github.com/sss836/rp1-test-monitor.git
cd rp1-test-monitor
./start-monitor.sh
```

首次在本项目`.venv/`准备依赖，需要电脑能访问Python包源，并已安装Python 3.10+及venv组件。若缺少ensurepip而系统已有pip，脚本只向项目虚拟环境安装pip；两者都没有时按电脑维护流程补齐。脚本不会使用sudo或apt，后续依赖齐全时直接启动。

上位机会打开设备设置，默认选择“SSH地址与密码（电脑后台接入）”：

1. SSH地址填写`ssh robo@192.168.137.250`，也接受`robo@192.168.137.250`。
2. 输入机器人密码。密码不写入命令行、环境变量、文件、日志或浏览器存储。
3. 首次连接显示主机SHA256指纹。与设备负责人或已有记录核对后勾选确认，再点击连接；确认前不会发送密码。后续指纹改变会拒绝连接，不覆盖旧信任记录。
4. 程序核对机器人已有8891监听的身份和来源，创建电脑relay，再接收实时数据。QT仅在收到匹配身份的预览后显示连接成功。
5. 切换到整机健康、部件、趋势或核心进程查看。硬件自检仍需手动选择单项，不自动运行插件。

本次认证后不保留密码，网络断开需要重新输入密码连接，不会无休止自动尝试登录。关闭Qt只关闭显示，独立电脑后台及relay继续运行。电脑重启后重新运行启动命令。

## 同一套数据的网页

```bash
./start-monitor.sh web
```

该命令复用或启动本机后台，并尝试打开[连接页面](http://127.0.0.1:18980/connect)。浏览器未自动打开时手动访问该地址，操作同上。连接成功后点“打开整机状态”，可进入电池、关节、主机负载、历史和诊断子页面。

Qt连接页也提供“打开快捷连接网页”。只有一份电脑后台和一份当前relay，Qt和网页不各自创建一套机器人接收器。网页“断开电脑连接”关闭本后台的隧道和relay，保留数据，机器人监听仍运行。

快捷模式自动设置Qt诊断中央地址和本机只读凭据。要调用AI，还需在“连接与模型设置”选择厂商、实际模型ID、API Key，测试后勾选用于诊断；未配置时只运行本地规则分析。机器人不运行大模型。

## 机器人：已有监听时无需安装

当前`RP1-ROBOPI2`已有独立监控安装。先在机器人终端手动启动原入口即可：

```bash
cd /home/robo/.local/share/rp1-monitor/20261025-r1 && \
env -u PYTHONPATH bash /home/robo/.local/share/rp1-monitor/20261025-r1/start_observer.sh
```

前台试运行可看日志，`Ctrl+C`停止本项目监听。已有实例运行时不再启动。该入口保留当前CPU1、2、3、7、nice10、ROS环境、锁及PID保护，不用GitHub新脚本覆盖它。

**当前电脑已有18972真机relay时，继续使用原模式：**

```bash
.venv/bin/python -m rp1_monitor desktop --url http://127.0.0.1:18972
```

原网页仍在`http://127.0.0.1:18970/`，使用原只读凭据。新快捷连接若检测到同一机器人的18972/8892 relay，会拒绝再启动第二份。迁移前应先确认旧队列已上传，按原记录停止旧电脑接收器并保留数据；不停止机器人运控。其他电脑上的重复接收器无法仅靠本机检测，现场仍须保证一台机器人只有一个持久接收relay。

## 新机器人：只有RoboPi TUI、没有本项目监听

下面步骤是供维护者审阅后手动执行的首次安装方法。本轮没有在机器人执行这些脚本。

在机器人有GitHub/包源访问条件时：

```bash
git clone https://github.com/sss836/rp1-test-monitor.git
cd rp1-test-monitor
bash scripts/install-robot.sh
bash "$HOME/.local/share/rp1-monitor/github/start-observer.sh"
```

安装脚本默认适配当前同类现场的`robo`用户、ROS 2 Jazzy、Python 3.12和设备ID`RP1-ROBOPI2`。**不同机器人必须设置自己的唯一ID和实际ROS路径**，例如：

```bash
RP1_ROBOT_ID=RP1-NEW-01 RP1_ROS_SETUP=/opt/ros/jazzy/setup.bash \
  bash scripts/install-robot.sh
```

安装前在终端加载已有运控确认过的ROS/DDS环境。当前现场使用Fast DDS原配置`/home/robo/roboparty_deploy/assets/rt_fastdds_profile.xml`；需要时在安装前明确设置相应环境，不能把示例强行套到其他机器人。解释器必须能导入现有ROS消息包，不能复制电脑的x86虚拟环境到ARM64。

TUI执行器可在安装前以`RP1_ROBOPI_TUI_EXECUTOR`指定。启动时未指定则在已知安装布局中查找；多份版本不自动挑选，缺少TUI只影响插件自检，不会自动安装TUI或升级驱动。当前已审查的上游源码版本为`706df3d`，不同插件版本须重新核对。

机器人无外网时，在匹配ARM64/Python/系统的准备环境制作`rp1-monitor[agent]`离线wheel集合，经网线复制Git仓库内容及wheel目录，再执行：

```bash
RP1_WHEELHOUSE=/实际离线wheel目录 bash scripts/install-robot.sh
```

脚本只从本地目录安装；缺包时失败，不自动接外网。准备阶段的Git/Python包源访问不是运行期间的网络要求。

### 首次安装具体写哪些文件

监控安装及运行内容都在普通用户的新目录`~/.local/share/rp1-monitor/github/`；Git克隆源码另存在执行clone时的位置：

| 文件/目录 | 用途 |
| --- | --- |
| `.venv/` | 独立Python及agent依赖；读取已有ROS包，不改系统Python |
| `.tmp/`、`.pip-cache/` | 本次安装的临时构建和下载缓存，不使用原运控目录 |
| `profile.json` | 设备身份、版本、待确认电机映射与进程名单；应由维护者按实填写 |
| `observer-config.json` | ROS路径、已确认DDS环境、可选TUI执行器；无中央地址或凭据 |
| `start-observer.sh` | CPU限制、锁和端口检查后的手动启动入口 |
| `observer.lock` | 启动后防止本安装重复运行 |
| `state/` | 启动后生成的摘要、诊断与TUI报告缓存 |

目标目录已存在时安装器拒绝覆盖，不自动更新旧安装。默认不启动、不安装自启，不修改`roboparty_deploy`、策略、权重、驱动、BMS服务、CAN、网络或系统时钟。新启动器在Python/ROS初始化前限制CPU1、2、3、7并排除CPU0；不同CPU布局必须先审阅，不能直接扩大集合。`nice=10`不是CPU使用率上限。

进程名单、电机真实映射和温度来源不能从GitHub生成。默认空映射保持未知；安装监听不保证电机温度/故障已接通，不会把主机板温当电机温度。

## 本机后台和存储

快捷模式专用目录是仓库`runtime/station/`，权限0700，凭据文件0600。包含自己的SQLite历史库、每机器人relay待传队列、页面只读凭据、设备上传凭据、主机公钥记录和进程日志；**不保存SSH密码**。自动生成的凭据只在电脑使用，不上传机器人或GitHub。

它是一个独立本机工作站，不连接现有生产PostgreSQL，不迁移或删除旧数据，也不安装Docker。正式中央部署继续使用原`server/relay`配置和显式SQL迁移；数据库分区、自动月度清理当前仍未实施。

原始秒级摘要与关键投影同时保存，不能把“关键曲线”理解为已经自动删掉原始摘要。机器人摘要缓存有限，电脑relay满时拒收而不确认；缓存容量不是整个目录上限。长期运行需观测磁盘与推理工况CPU/内存，首次短时接通不代表整机验收。

## 开发验证

```bash
.venv/bin/python -m pip install -e '.[dev,desktop,server,station]'
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest -q
```

SSH集成测试仅在电脑回环运行临时SSH服务器和明确标注`simulated`的采集端，验证指纹确认、错误密码、纯端口转发、relay入库及24关节预览，检查没有远程shell/exec请求、没有落盘密码。它不连接真实机器人，不能写成真机联调通过。

## 故障入口

| 提示/现象 | 处理 |
| --- | --- |
| 首次主机指纹 | 核对后确认；指纹变化先查设备身份，不删除旧记录绕过 |
| SSH用户名/密码错误 | 核对账号和密码登录权限；不自动重复认证 |
| SSH已通但未读到采集端 | 机器人先手动启动监听；核对默认8891和实际端口 |
| 检测到已有relay | 继续连接原relay，或审阅完成迁移后再切换 |
| 后台端口被其他服务占用 | 不终止未知进程；使用CLI的`--station-port`指定空闲端口 |
| CPU/BMS有值、关节未知 | 检查原话题/DDS/观察线程；不要由监控使能或重启推理 |
| 电机温度未知 | 需要真实诊断生产者与确认映射，TUI板温不能替代 |

详细设计见[技术架构](TECHNICAL_ARCHITECTURE.md)，既有机器人部署及原链路恢复见[部署与通信](DEPLOY_AFTER_ROBOPI_TUI.md)。
