# RP1_HARNESS v0.3：四步功能交付

> 版本说明：本文保留对应旧版设计/验证范围。v0.4.0已改为机器人经网线上位机relay上传，当前启动参数、凭据位置和退出行为以 [WIRED_RELAY.md](WIRED_RELAY.md) 为准；旧测试记录不构成新拓扑验收。

验证日期：2026-10-21。用户确认目前没有实机连接，本轮范围为软件实现与回放验证。

## 功能入口

原生 Ubuntu 上位机新增侧栏「诊断助手」；旧监控窗口新增「设备与诊断」页；中央网页底部新增「设备能力与诊断」。三个入口共用设备能力、健康规则和诊断证据契约。

| 步骤 | 已实现 | 验证边界 |
|---|---|---|
| 电机健康只读发布 | LRO/RP 驱动反馈校验、同帧缓存、运控低频 diagnostics 发布补丁；采集端订阅、身份映射、序号和时效校验 | C++ 并发/解码测试、两个驱动编译语法检查、ROS 消息辅助代码检查、端到端合成帧回放；未部署真机，未完整构建 ONNX 运控 |
| 设备档案、能力、自检 | 版本化档案、身份唯一性校验、能力声明与观测分开、缺失原因、物理映射状态、版本一致性、ROS 图/QoS、时钟、缓存与磁盘检查 | 软件测试与两台模拟预览；未核实实际机器版本、BMS 协议及物理映射 |
| 诊断记录和导出 | 中央数据库及采集端 SQLite 持久记录；所选历史窗口、版本、来源、质量、自检、SHA-256；ZIP 包可校验 | 服务重启后仍可检索；临时数据库及模拟进程验证 |
| 只读 AI 查询 | Responses API 工具循环；当前观测、选定历史、前一等长窗口和全部已接入电机温度摘要比较；固定机器人范围、白名单、证据引用、并发限制 | 模拟模型回调验证；本轮无模型 API 凭据，未调用付费模型。未配置时明确返回本地规则诊断 |

## 运行

在本项目使用匹配环境的 Python 安装 `.[server,agent,desktop,dev]`。本机测试使用 `.venv`；ROS Humble 机器人应使用其兼容的 Python/ROS 环境，不能将本机 Python 3.12 的虚拟环境直接复制过去。

```bash
# 无硬件演示
.venv/bin/python -m rp1_monitor demo --runtime runtime/demo-v030 --robots 2
.venv/bin/python -m rp1_monitor desktop --url http://127.0.0.1:8891

# 生成空档案，再依据实际配置编辑。不会自动探测或初始化硬件。
.venv/bin/python -m rp1_monitor profile-init --robot-id RP1-001 --output runtime/RP1-001.json
```

档案字段包括硬件、运控、驱动版本，BMS 类型，诊断 topic，和 motors 列表。单条电机示例（仅说明格式，不是 RP1 实机参数）：

```json
{"joint_name":"joint_0","bus":"can0","motor_id":1,"protocol":"lro"}
```

不要把示例当作实际映射。只有现场核对后才能将 `physical_mapping_verified` 设为 true。缺少档案映射时不会接收电机诊断数值。BMS 入口仍由实际启动参数决定；档案不创建 BMS 或电机连接。

在原有 agent 命令后加 `--profile runtime/RP1-001.json`。中央 `devices.json` 的每个设备条目可选增加 `profile` 对象，供登记配置与采集上报对比。令牌仍独立保存，不得放入 profile。

```bash
# 本机或 SSH 隧道连接采集服务
.venv/bin/python -m rp1_monitor doctor --url http://127.0.0.1:8891
.venv/bin/python -m rp1_monitor diagnostics --url http://127.0.0.1:8891 --output runtime/diagnostics.zip

# 中央只读查询，指标与时间范围由调用者明确选择
.venv/bin/python -m rp1_monitor ask --server http://127.0.0.1:8890 --robot-id DEMO-RP1-01 --question '解释温度缺失原因' --metric joint.0.motor_temperature --local-rules
```

诊断不会启动或重启服务、打开第二个控制器、修改系统配置或清理原始数据。归档仅包含结构化监测证据，不收集任意文件、完整系统日志或设备凭据。导出前仍应按组织要求审阅其中的机器人标识和遥测数据。

## AI 配置

v0.3.1 已增加多厂商配置、Chat Completions 适配和界面 Key 输入，详见 [多模型连接说明](MODEL_CONNECTIONS.md)。以下环境变量仍兼容。

模型接口按照 [OpenAI 官方 Function calling 文档](https://developers.openai.com/api/docs/guides/function-calling) 的 Responses API 实现，可使用兼容该协议的本地服务。后端环境变量：

- `RP1_AI_ENABLED=1`：启用模型调用。
- `RP1_AI_MODEL`：实际可用模型名，本项目不猜测账户可用性。
- `RP1_AI_ENDPOINT`：默认 `https://api.openai.com/v1/responses`；本地模型允许回环 HTTP。
- `RP1_AI_API_KEY`：服务端默认 Key；也可使用界面中仅本窗口有效的 Key，不写入代码或设备档案。

界面「连接与模型设置」（网页为「模型与诊断记录」）中的「启用服务端模型分析」默认不勾选。勾选后发送当前提问、最近对话和该机器人选定的诊断证据，模型返回的分析带证据引用。模型不能选择其他机器人、提供 SQL、读取文件或执行 Shell。最多两次白名单工具调用，第三轮必须返回回答；工具响应、输出体积和同时查询数量均有限制。`store=false` 仅控制该 Responses 请求的存储选项，不构成对模型提供商整体数据政策的保证。

未配置模型或未勾选时返回 `mode=local_rules` 并明确提示未调用大模型。规则诊断不是 AI 回答。外部模型超时或返回无效工具时显示错误，不用虚构结果补齐。

查询问题中的“昨天”等文字不会覆盖界面指定时间：工具只使用明确的查询范围，并要求模型说明时间不一致。全部电机温度比较使用每段首末有效摘要点之差，不能推断机器人正在跑步或确认发热根因。

## API 与数据

中央新增端点沿用现有人员只读认证：

- `GET /api/v1/robot-monitors/{id}/capabilities`
- `GET /api/v1/robot-monitors/{id}/doctor`
- `POST/GET /api/v1/robot-monitors/{id}/diagnostics`
- `GET /api/v1/robot-monitors/{id}/diagnostics/{report_id}`
- `GET /api/v1/robot-monitors/{id}/diagnostics/{report_id}/bundle`
- `POST /api/v1/robot-monitors/{id}/assistant`

助手请求另可含 `history`：最多 8 条 `user` / `assistant` 消息，每条最多 4000 字。对话只用于理解追问，每次仍重新读取所选机器人的证据；前端在新对话或切换机器人时清空上下文，不持久保存聊天。

诊断请求可含 `metrics`（最多 8 项）、`start`、`end`（带时区、范围最多 24 小时）。诊断记录创建是在监控数据库保存证据，不写机器人。旧库需先审阅并执行 `sql/002_monitor_diagnostic_records.sql`，再用 `--no-initialize` 启动。既有遥测三表不变，不涉及正式测试与 MTBF 表。

采集端本机接口：`GET /api/v1/capabilities`、`GET /api/v1/doctor`、`POST/GET /api/v1/diagnostics`、`GET /api/v1/diagnostics/{report_id}/bundle`。只通过本机回环或 SSH 隧道访问，跨站 Origin 被拒绝。记录保存在 agent 的 `state_dir/diagnostics.sqlite`。

诊断记录不自动删除。没有配置记录保留策略，本地与中央磁盘容量仍需按实际使用规划。摘要中的 collector_status 是窗口生成时的状态，不代表查询当刻的本地队列状态；本地 doctor 会读取当前队列状态。

## 验证与复现

```bash
env -u PYTHONPATH -u PYTHONHOME PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python scripts/check_health_replay.py
.venv/bin/python scripts/check_operations_e2e.py
.venv/bin/python scripts/check_postgres.py
```

API 测试使用本机异步通信；受限沙箱可能阻止 TestClient 线程唤醒，需要允许本机通信的测试环境，不应据此修改产品代码。PostgreSQL 脚本创建随机名称和回环端口的临时容器，只清理自己创建的容器。

现场剩余验证：实际驱动/运控版本及完整构建、BMS 字段有效性、物理关节映射、ROS 时间体系、动作负载下回调开销、发布/采集延迟、长期稳定性。没有完成这些项之前，不把本轮软件验证写成整机健康验收。
