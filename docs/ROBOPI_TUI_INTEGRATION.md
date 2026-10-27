# RoboPi TUI 接入与改动说明

初次交付完成本机代码接入与模拟验证，当时没有操作机器人。随后经用户授权，2026-10-26 已在机器人独立测试目录复用现场安装的 TUI，完成 9 项被动检查、报告缓存与 relay 上传验证；测试进程已停止，未覆盖原监控安装或修改运控。详细范围与结果见 [真机软件测试记录](ROBOPI_TUI_ROBOT_TEST.md)。

后续代码扩展至 14 项，并新增独立 [上位机硬件自检页](HARDWARE_CHECKS.md)。用户随后明确授权同步真机，现已更新现有采集端、启动监控，并完成两份真实报告入库及上位机读取验证；逐文件与备份见 [正式同步记录](ROBOPI_TUI_SYNC.md)。原 9 项临时联调与本次两项验证均不代表全部项目硬件验收。

## 使用方法

1. 上位机按原方式连接本机 relay，打开左侧“硬件自检”；“诊断助手”也提供跳转入口。
2. 点击“读取状态 / 历史报告”。GET 不执行插件。
3. 板端安装和配置就绪后，选择项目，点击“运行所选单项”。同时只运行一个任务，两次发起至少间隔 10 秒。
4. 上位机显示执行状态、失败原因及每项最近报告。失败后可刷新状态，不会自动重复执行。
5. 网页打开 `/robots/<机器人ID>/diagnostics`，可刷新中央收到的历史报告、查看逐项结论和原始 JSON；最近诊断记录可下载证据包。

网页凭据保持只读，不能执行板端命令或上传报告。模拟来源显示“模拟数据”；历史报告显示完成时间，不代表当前实时状态。`pass` 仅符合本项判据，`needs_review` 待复核，`skip` 跳过，`error` 检查错误；上游 `testing` 状态保留。

## 复用范围

核对上游：`Roboparty/robopi_TUI@706df3d226c6e2deb3467c0eabce507f309fdc0f`。适配 JSON schema_version=3、插件版本 1.0，通过进程调用已安装的执行器，没有复制上游 GPL 源码。

允许 RP001 板温、RP002 镜像身份、RP003 网口、RP005 软件版本、RP007 CAN 接口、RP008 IMU 命名规则、RP011 USB、RP012 USB 3.0 扩展坞、RP013 USB-CAN、RP020 BMS 配置、RP021 BMS 服务、RP022 BMS 版本、RP029 串口清单、RP034 序列号。RP013 前置 RP012，RP021 前置 RP020，RP022 前置 RP021 和间接依赖 RP020。RP001 的 90°C 判据不覆盖本项目温度限值。RP022 的资源限制与 RP040 不可执行的边界见 [硬件自检说明](HARDWARE_CHECKS.md)。

持续采集保持：关节和 IMU 用 ROS 2，电池用 bms.service 新日志，CPU/内存/进程读 /proc、/sys。旧 --rp-server-url 仅显式兼容，不自动启用或启动服务。本次 TUI 不提供电机温度；RP001 是板温，RP025 是参数刷写，均不能代替此前计划的被动温度读取模块。

## 原监控启用改动

上位机和中央侧改动：relay.py 增加独立报告接收/缓存/上传；backend.py、report_store.py、operations_api.py 增加绑定设备上传、幂等校验和只读查询；desktop_operations.py 增加指定检查与状态界面；web/index.html、operations.js、app.css 增加历史报告及响应式展示，admin.html 同步资源版本；deploy 模板增加可选执行器参数。scripts/check_rp1_hardware.sh 仅将软件版本清单中的旧 robopi-config 改为 robopi-tui，该单独硬件脚本没有接入自动监听。

**下表为组件范围。** 此前隔离测试没有替换原监控；后续授权同步采用最小 5 个 Python 文件及一个启动参数，已执行，具体文件与备份以 [本次同步记录](ROBOPI_TUI_SYNC.md) 为准。

| 本项目隔离目录中的位置 | 作用 |
|---|---|
| rp1_monitor/robopi_tui.py | 新增被动 JSON 适配器 |
| rp1_monitor/board_diagnostics.py | 新增任务及私有报告缓存 |
| agent.py、cli.py、local_operations.py | 接入可选参数和本机接口 |
| 本项目 start_observer.sh | 启动命令追加执行器路径，保留原 Python、ROS、档案和缓存参数 |
| 本项目状态目录 | 新增 board-diagnostics.sqlite，保存报告与游标，不自动删除 |

不修改 roboparty_deploy、策略、驱动、robot.yaml、CAN 配置或 BMS 实现；不安装 TUI 常驻服务或更改开机自启。新增工作线程和子进程排除 CPU0，继承原允许核心集合、不扩大范围，nice 至少为 10；nice 不是 CPU 百分比上限。

核对现场安装后，可在现有 **agent 命令末尾**追加执行器参数（不是一条独立命令）。2026-10-26 现场确认的源码安装路径是：

```bash
--robopi-tui-executor /opt/roboparty/lib/robopi-tui-source/706df3d/loader/plugin_executor.py
```

Debian 包布局可能使用 `/opt/roboparty/lib/robopi-tui/loader/plugin_executor.py`；不能仅凭软件包未登记就判断没有安装。没有参数时关闭；缺少文件时显示异常，不自动安装。通用 deploy/start-agent.sh 支持可选 RP1_ROBOPI_TUI_EXECUTOR 变量，但现有专用 start_observer.sh 不会自动读取通用环境模板，须单独核对。

完整 ARM64 包依赖包括 python3-yaml、dialog、adbd、robopi-addon、roboparty-analyze、roboparty-motors >= 2.3.1-1 等。将来安装前先列出新增/升级/移除的包，避免为了自检连带升级运控驱动。**本次没有安装依赖，也没有修改 pyproject.toml。** 上位机无需安装 ARM64 包或改用 ADB。

## 报告传输与接口

```text
本机上位机 → 网线 SSH 隧道 → 机器人采集端 → 独立线程调用 TUI
  → 本项目报告缓存 → 上位机 relay 缓存并上传中央 → 网页只读查询
```

机器人不配置中央地址或凭据。relay 每 5 秒读取最多两份新报告，持久游标支持断连和重启后继续读取。中央按报告 ID/SHA256 幂等接收，冲突拒绝覆盖；确认匹配后才标记本机已上传。原报告保留、不自动清理。现有遥测独立运行。

执行前核对目录及完整依赖均在白名单、版本匹配、test/passive；禁止 all、query、主动配置、刷写、服务重启、CPU/网络设置。不使用 shell，不接受任意命令或插件参数。--env csharp 是上游外部客户端模式名称，不要求使用 C#，也不作为认证。

每次执行器调用最多 45 秒，stdout/stderr 总计最多 256 KiB。取消/超时时中断本次执行器，由上游清理其插件，必要时终止该执行器；不使用全局 pkill。若换用不遵守上游退出清理行为的执行器，须重新验收其子进程退出行为。

| 接口 | 用途 |
|---|---|
| 本机 GET /api/v1/board-diagnostics | 状态和最后报告，不执行插件 |
| 本机 POST /api/v1/board-diagnostics/runs | robot_id、plugin_id，返回 202；跨源请求拒绝 |
| 采集端 GET /api/v1/board-diagnostics/reports?after=… | 报告分页 |
| 中央 POST /api/v1/robot-telemetry/board-diagnostics | 仅绑定设备凭据上传，事务内复查撤销与来源 |
| 中央 GET /api/v1/robot-monitors/{robot_id}/board-diagnostics | 只读获取最新历史报告 |

复用 monitor_diagnostic_records.payload，不新增 PostgreSQL 表、不修改分区、不清理生产数据；诊断与正式测试、审批、MTBF 隔离。

## 回退与验证

移除 --robopi-tui-executor 及路径，仅重启本项目采集进程，即可关闭新检查。原 ROS、BMS 日志和主机采集继续，报告保留；无需修改原策略或删除缓存。

SQLite 与独立临时 PostgreSQL 全量回归 **554 项通过**，临时容器已清理。包括白名单及依赖、CPU0 排除、超时取消、输出上限、跨源、模拟标识、断连重启补传、幂等冲突、凭据和来源、Qt/网页旧响应隔离。详见 模拟验证记录（本机历史材料，不随源码发布）。随后已确认现场源码安装与这 9 项插件的实际执行权限；没有安装或升级完整包依赖，运动负载开销及长期稳定性仍未验收。

收尾增加磁盘写入失败、缓存报告损坏和仅 CPU0 可用的边界验证，连同传输及 UI 回归 **84 项通过**。原始报告在源端、上位机读取时均检查 SHA256，损坏内容拒绝传输。

初次交付时核对并重启本机 18940 的两台 simulated 演示采集、relay 和中央进程，使现有网页加载新接口；数据库及配置保留。当时未操作 18970 真机后端、真机 relay 或机器人。首次真机临时测试使用独立、需要页面凭据的中央环境，没有混入 18940 演示库。用户后来授权正式同步，已更新真机采集端并重启 18970 真机中央和 18972 relay，复用原数据库、配置及凭据；该轮未操作 18940。重新打开上位机可使用独立硬件自检页。

重现三进程模拟验证（会创建临时本机服务并自动清理，不连接机器人）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/check_robopi_tui.py --screenshot docs/robopi-tui-desktop.png
```
