# 中央服务部署与已有大屏接入

本轮提供可运行的新工程和接入模板；没有安装机器人systemd服务、没有修改生产数据库或 `10.43.17.214:8443`。

## 路径一：独立中央服务

先在现有PostgreSQL集群中准备**专用监控数据库与专用角色**。不要给监控连接正式测试库的写权限。机器人不持有数据库密码、中央地址或上传令牌。中央写入由上位机relay使用每机器人独立令牌完成，详见 [网线链路](WIRED_RELAY.md)。

管理员审阅 `sql/001_monitor_tables.sql`，在新监控数据库执行一次。它只有三张monitor表，无正式测试或MTBF表。后续结构变化需要显式迁移；本版没有自动迁移/回滚管理器。

```bash
python -m pip install 'rp1_monitor-0.4.0-py3-none-any.whl[server]'
rp1-monitor provision --directory runtime/provision-rp1-001 \
  --robot-id RP1-001 --name 'RP1 001' --source ros2
# 在私有环境文件设置RP1_MONITOR_DATABASE_URL，不写到命令行/版本库
rp1-monitor server --devices runtime/provision-rp1-001/devices.json \
  --viewer-token-file runtime/provision-rp1-001/viewer-token.txt \
  --no-initialize --host 127.0.0.1 --port 8890
```

DSN形式：`postgresql+psycopg://<监控角色>:<密码>@<数据库主机>/<专用监控库>`。示例中尖括号必须替换，密码不要粘贴进共享命令或提交文件。

HTTPS由现有反向代理终止，模板见 `deploy/nginx-standalone.conf.example`。最终访问 `/monitor/`。上位机relay的backend设置到同一个 `/monitor` 前缀。此模式使用独立只读令牌，**不会自动复用原平台登录**。

## 路径二：已有FastAPI平台同源挂载

已有 `sss836/rp1_test_shn` 参考快照为 `f44a50e76bc8c74c4f6c34885eb0974790cc173e`。本轮只读参考它，没有改动其源码或提交PR。部署前需以现场最新分支再核对。

本项目 `integration.mount_monitoring` 可把整套只读页面/API挂到 `/monitor`。`examples/platform_mount.py` 给出对应平台的调用代码。需要：

1. 先在专用监控库人工建表，给平台进程设置监控专用DSN和设备配置路径。
2. 既有FastAPI初始化并注册异常处理后调用 `monitor_child = install(app)`。
3. 复用 `get_request_actor` 检查人的登录会话，再检查 `principal_kind == HUMAN`。如果平台还有额外模块权限要求，在human_read依赖中加入，不要绕开。
4. 既有lifespan退出处加 `monitor_child.state.store.engine.dispose()`，因为挂载子应用不会自动运行自己的lifespan。
5. 原React导航增加“整机监控”链接或同源iframe `/monitor/`。页面内部使用相对地址，Cookie需覆盖该路径。

新路径：

| 路径 | 方法 | 认证 | 用途 |
|---|---|---|---|
| `/monitor/api/v1/robot-telemetry/batches` | POST | 本项目每设备写令牌 | 连续遥测入库 |
| `/monitor/api/v1/robot-monitors` | GET | 人的平台会话 | 大屏当前状态 |
| `/monitor/api/v1/robot-monitors/{id}/telemetry` | GET | 人的平台会话 | 有界摘要曲线 |
| `/monitor/`、`/monitor/assets/*` | GET | 页面资产无数据；API另认证 | 监控页面 |

上位机使用的设备令牌不会传给原平台 `get_request_actor`；只有新的写入路由识别它。人的读路径仍由原平台依赖验证。**不要放宽原Bearer规则，不要恢复旧ingestion路由，不要借用正式执行表保存连续监控。**

本项目对挂载路径、Cookie读依赖、写入隔离及原ingestion不被注册做了隔离测试。尚未在实际平台的完整依赖、数据库角色、代理及CSP配置下联调，这一步仍需真实部署验证。

如果代理对API有统一鉴权，应只针对新的POST路径明确配置设备认证转交，并保持原路径规则；不能为了方便对整站放行Bearer。

## 机器人与上位机后台服务模板

`deploy/agent.env.example`、`deploy/start-agent.sh`、`deploy/rp1-monitor-agent.service` 是模板。现场核对用户、ROS/Python路径、工作空间、DDS、CA、设备ID、磁盘目录后才能安装。

使用与当前运控相同的ROS环境，但独立进程运行。不要自动重启/停止运控。建议先手动启动完成静止联调，再安装后台服务。使用专用状态目录，磁盘空间不足会显式报错，先解决空间问题再重启监控。

机器人模板没有中央地址/凭据。上位机使用 `deploy/relay.env.example`、`start-relay.sh`、`rp1-monitor-relay.service`，SSH隧道须独立维护；模板不自动安装。

日志由systemd/journald保留；机器人缓存丢弃只涉及本工程的未传摘要。电脑relay缓存不淘汰已接收的待传窗口，满时停止接收确认并提示。它不清理现有PCAP或rosbag。

## 数据与运维边界

- 默认每秒摘要；中央不存完整源消息，原始故障复盘仍依靠现有录包工具。
- 队列1GiB有效载荷预算和256MiB磁盘保留值是暂定，可调整，不是物理硬配额。
- 30天容量估算没有附带自动清理。真实保留策略、备份与历史查询性能需确定后实现。
- 设备配置是全量清单，启动时未列出设备会停用。多worker使用相同完整配置；本版推荐单API进程先验证。
- 写令牌撤销/轮换通过更新配置并重启服务；只读页面刷新后需重新提供凭据，凭据不持久化到浏览器。
- 当前无限制请求频率的管理中心、多租户RLS和在线注册没有加入首版；现有网关按实际部署限制访问和速率。
- 长范围查询在数据库内聚合，返回点数有界但扫描成本随时间范围增长。网页10分钟约2秒、1小时约10秒、24小时约60秒刷新；尚未完成30天规模压测。
