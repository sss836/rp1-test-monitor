# v0.4.0 网线上位机链路验证

验证日期：2026-10-21。当前链路为机器人 agent→网线上位机 relay→中央服务。以下为软件、临时 SQLite、合成/模拟设备和回环 HTTP 验证，未连接 RP1、未配置真实网卡或 SSH 服务，不构成实机验收。

## 本次执行

| 项目 | 结果 | 范围 |
|---|---|---|
| 全量 pytest | 177 passed、1 skipped，9.86秒 | 跳过未配置临时 PostgreSQL 的专用用例；1条现有 TestClient 依赖弃用警告 |
| 最终 Qt 状态栏回归 | 9 passed，3.97秒 | 分段连接状态、队列计数、断连隐藏、窗口生命周期 |
| Ruff / format | 通过，74个Python文件格式通过 | 静态检查 |
| Bash语法 | 两个启动脚本通过 | 机器人脚本无中央参数；电脑脚本负责上传 |
| 五组 agent+relay 实际进程 | 通过，27.83秒 | 五个模拟机器人、五个独立上位机relay、中央SQLite、Qt进程 |
| 中央服务中断 | 电脑积压4窗口，恢复后补传 | 机器人与本地预览不依赖中央在线 |
| 模拟网线中断 | 通过 | 本机TCP代理阻断，机器人保持运行并积累摘要；失联隐藏当前健康值；重连入库序号连续无缺口 |
| Qt关闭 | 正常退出，随后中央采集时间继续推进 | 电脑relay和独立传输通道仍运行 |
| 诊断实际进程 | 通过 | 本机Qt、doctor、聊天规则模式、ZIP哈希、中央重启后记录可检索 |
| demo命令 | 两台模拟设备，运行5秒，正常退出 | 新命令创建独立采集与relay，不把凭据传给agent |
| wheel | 构建及内容检查通过 | 包含relay、采集代码及网页资产，不含运行数据库和凭据；不是原生安装包 |

机器可读结果与本轮关键源码 SHA-256 见 wired-relay-verification.json（本机历史材料，不随源码发布）。此前 v0.1～v0.3 的验证文件保留原样，不当作本轮拓扑验证。

## 关键行为回归

- 机器人命令拒绝原 `--backend` 参数；不要求中央令牌。
- 电脑请求机器人时不携带中央 Authorization。
- 源端峰值、最后值、采集时间、stream/sequence、来源和质量字段，经两跳入库保持一致。
- 电脑 SQLite FULL 提交后才确认源端窗口；磁盘落盘失败时源端不删除摘要。
- 哈希不一致和跨机器人确认被拒绝；确认响应丢失可以幂等重试。
- 电脑队列重启后可独立上传已接收历史；backfill不会冒充当前状态。
- 旧积压晚于实时窗口到达电脑时，按源stream/sequence选择最新实时窗口，不依赖电脑落盘顺序。
- 电脑队列满时保留已接收数据并停止新的接收确认；缓存目录不可改绑机器人或来源。
- 中央断网不隐藏仍有效的本地预览；机器人连接中断会隐藏数值并在能力、自检中标为过期。
- 上位机环境检查明确标为电脑环境，不冒充机器人主板。

## 复现

```bash
env -u PYTHONPATH -u PYTHONHOME -u RP1_MONITOR_TEST_DATABASE_URL \
  PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
bash -n deploy/start-agent.sh deploy/start-relay.sh
.venv/bin/python scripts/check_e2e.py --report /tmp/rp1-wired-e2e.json
.venv/bin/python scripts/check_operations_e2e.py \
  --output /tmp/rp1-wired-operations.json --screenshot /tmp/rp1-wired-diagnostics.png
```

TestClient和进程测试需要允许本机线程通信与回环端口的环境。本次未重新执行PostgreSQL容器矩阵；未实测网线/SSH、真实ROS/BMS、实机CPU/延迟或长期稳定性。有限缓存也不保证任意长断连后无损。现场启动及迁移方法见 [WIRED_RELAY.md](WIRED_RELAY.md)。
