# 核心进程监控与软件验证

已接入机器人采集端、上位机 relay、Qt 部件页、网页核心进程表、历史曲线、诊断证据和只读 Agent。无实机时可直接使用 `demo` 查看模拟运控与模拟驱动，每分钟第30～36秒模拟驱动缺失，恢复后模拟重启数增加。

机器人采集端、relay、中央和Qt应使用包含本轮改动的同一构建；旧版严格协议模型可能拒绝新增进程字段。升级先在模拟环境验证，不自动修改生产部署。

## 机器人侧如何配置

沿用设备档案 `--profile`，新增可选 `processes` 名单，最多16项。未配置时明确显示未配置，不扫描并猜测运控进程。示例见 [process-profile.example.json](../deploy/process-profile.example.json)，其中 `REPLACE_*` 必须替换，示例不代表真实 RP1 进程名。

```json
{
  "robot_id": "RP1-001",
  "processes": [
    {"key": "motion", "label": "运控", "comm": "实际comm"}
  ]
}
```

现场人员提供实际 PID 后，可只读查看 `/proc/实际PID/comm`，填写其完整内容；Linux comm 最多15字节（不含结尾空字节），长程序名可能被截断。采集器读取主进程 comm，按精确名称匹配。多个同名实例显示“同名冲突”，不会任选一个或合并资源；Python等同名进程需在拿到真实部署结构后适配。采集结果只覆盖当前可见PID命名空间，容器与 hidepid 权限需现场核对。

`key` 是历史指标标识，如 `process.motion.rss_mib`；选择另一种被监测程序时使用新的 key，保留档案和原始窗口以追溯历史含义。进程档案不包含命令行、环境变量、账号或 Key。机器人仍不配置中央地址和上传凭据。

## 指标含义与开销控制

| 项目 | 含义 |
|---|---|
| 存活 | 主进程存在且非僵尸/退出态；保留 Linux 原始状态码，T/t 等暂停态可见；不能证明运控功能正常 |
| CPU | 相邻两次有效采样的 utime+stime 差值，单核100%，多线程可超过100%；首次、实例变化及采样失败后的首帧为空 |
| RSS | `/proc/PID/stat` 常驻页 × 页大小，单位 MiB，内核近似值，包含共享页；不读取开销更大的 smaps |
| 线程数 | 该进程当前 num_threads |
| 观察到的重启 | 同一名单项的 PID+start_ticks 发生变化；首次采样不计，本采集会话从0开始，可能漏掉两次采样之间的快速重启 |
| 观测时间与年龄 | 原始观测时间保留；按单调时钟累计年龄，relay及中央继续增加传输/等待年龄；6秒后显示未知，链路失联时提前失效 |

默认每2秒遍历可见PID的 comm，只为名单匹配项读取 stat。不创建系统命令子进程，不读 argv、environ、maps，不发送信号、不启停服务。不新增 psutil 等依赖。资源算法依据 [Linux 内核 /proc 文档](https://docs.kernel.org/filesystems/proc.html)。

只保存每个名单项上一轮计数、实例身份及累计变化；机器人内存不保存进程历史列表。当前值、每秒摘要和原始观测质量经既有两级缓存传输；采样间隔内没有新样本的摘要窗口保留 `count=0`，不会把上次RSS冒充新样本。进程退出、重启CPU预热或读失败会立即清空对应当前资源值，历史曲线保留缺口。

存活、重启数保留最后离散值；CPU、RSS、线程数保留窗口最后值/最小值/最大值/样本数。暂停、僵尸、未发现、同名冲突、权限不足与过期均可从状态及原始观测说明区分。

## 复验入口

```bash
# 假 /proc 边界、中央历史、Agent证据、Qt断线状态
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -q
# 本机创建10个自有测试进程，测只读轮询并验证退出/重启
.venv/bin/python scripts/check_process_monitor.py --output /tmp/process-cost.json
# 5台模拟采集端 → relay → 中央；实际TCP、断线、补传、Qt关闭
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/check_e2e.py --report /tmp/process-e2e.json
```

以上均为软件验证，无 RP1 实机连接。只对测试脚本自己创建的子进程模拟退出/重启，不操作机器人进程。完整采集服务RSS、ROS负载和对控制周期的影响仍需实机测量。

进程采集继续保留原始摘要。后续中央功能已增加关键曲线白名单、容量日账、小时压缩归档和30天到期预览，见 [中央存储与告警验证](CENTRAL_MONITORING_VERIFICATION.md)。原始与关键层同时留存，不自动清理或宣称已节省磁盘；进程采集本身的结果见 [软件验证记录](PROCESS_VERIFICATION.md)。
