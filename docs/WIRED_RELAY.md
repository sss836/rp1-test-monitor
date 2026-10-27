# v0.4.0 网线采集与上位机转发

RP1 无需 Wi-Fi 或外网，仅通过网线与上位机电脑形成局域网。机器人不连接中央 FastAPI、不访问网页、不保存中央上传凭据。上位机电脑上的独立 relay 接收、持久缓存并上传；Qt 窗口读取 relay，中央网页仍读取中央服务。未对实机安装服务、配置网卡或修改运控。

```text
机器人 agent（127.0.0.1:8891）
    └─网线上的 SSH 隧道→ 上位机 relay（127.0.0.1:8892）→HTTPS→中央数据库/API→网页
                              └─本地 Qt 窗口
```

## 机器人侧

首次接入可先按 [机器人准备清单](ROBOT_PREPARATION.md) 执行只读环境检查，再确定实际配置。

使用匹配实际 ROS 发行版/Python ABI 的环境，提前准备并安装 wheel 和依赖。离线主板运行时不需要在线 pip；不能将电脑的虚拟环境直接复制到不同架构或 Python 版本的机器人。已有 BMS、ROS、物理映射仍按 [ROS_CONNECT.md](ROS_CONNECT.md) 核验。

```bash
rp1-monitor agent --source ros2 --robot-id RP1-001 \
  --profile /etc/rp1-monitor/profile.json \
  --state-dir /var/lib/rp1-monitor --port 8891
```

不再接受 `agent --backend`、`--token-file` 或 `--ca-file`。原机器人服务模板需要改用当前 `deploy/agent.env.example` 与 `start-agent.sh`，不会自动修改已安装服务。旧摘要缓存可以继续读取，原始 stream/sequence 和内容保持不变；缓存目录绑定 robot_id 与来源，拒绝跨机器人或混用 simulated/ros2。

agent 的本机 HTTP/WebSocket 是网线只读传输出口，不是中央服务。它只监听回环地址，由上位机经现有 SSH 连接访问。不会创建第二个电机控制器、修改控制话题或启动 rp-server。

## 上位机电脑

电脑到机器人网线需具备双方可达的地址；到中央服务使用电脑另一条可用网络，具体接口按现场条件配置。本工程不启用网卡共享、NAT、默认路由或自动配置网络。

先通过系统 SSH 核对机器人主机指纹和密钥。隧道独立于 Qt 窗口运行：

```bash
ssh -N -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18911:127.0.0.1:8891 <现场用户>@<机器人网线IP>
```

中央 `provision` 生成的 `devices.json` 留在中央；`device-token.txt` 安全分发到对应机器人使用的上位机电脑，机器人无需该文件。每台机器人令牌仍只能写其绑定 robot_id 和来源，页面只读令牌另行分发。

在另一个终端启动上位机后台服务：

```bash
rp1-monitor relay --source ros2 --robot-id RP1-001 \
  --collector http://127.0.0.1:18911 \
  --backend https://<实际中央地址>/monitor \
  --token-file /etc/rp1-monitor/device-token.txt \
  --state-dir /var/lib/rp1-monitor-relay/RP1-001 --port 8892
rp1-monitor desktop --url http://127.0.0.1:8892
```

中央使用私有 CA 时，给 relay 加 `--ca-file <公司CA文件>`。`--collector` 仅接受本机回环地址，HTTP 通过网线 SSH 隧道；中央凭据仅走 HTTPS，回环模拟例外。collector 请求不携带中央令牌，禁止重定向。每台机器人运行一份 agent 和一份对应 relay，多台机器人各用独立端口和状态目录。

`deploy/relay.env.example`、`start-relay.sh` 和 `rp1-monitor-relay.service` 是电脑后台部署模板。SSH 隧道需另行维护；模板不会安装服务或改变现场系统。Qt 的地址指向 relay，原有可选 SSH 功能仅用于远程读取上位机；不能用随 Qt 退出的隧道承载后台 robot→relay 通道。

## 两级缓存与确认

1. 源消息直接进入机器人 Accumulator，生成约一秒摘要。10Hz 预览不用于重建摘要或峰值。
2. relay 拉取最新窗口与旧积压，交替处理。每个窗口先以 SQLite FULL 同步提交到电脑，再发送原 stream/sequence 与 SHA-256 接收确认。
3. agent 仅对哈希相符的窗口释放待传缓存。确认响应丢失后可以重试，重复接收不会改写内容。
4. relay 使用原窗口身份、来源、采集时间、单位和质量字段上传中央。中央返回明确 receipt 后才删除电脑待传摘要；旧积压按 backfill 入历史，不覆盖当前状态。

机器人缓存延续已有容量预算：超限会丢弃最旧的本工程摘要并累计丢失范围，不清理原始日志或 rosbag。电脑缓存满或磁盘不足时不淘汰已接收窗口、不确认新的机器人窗口；界面提示异常，原始数据不自动清理。两端缓存、磁盘保留值仍是暂定参数，有限缓存不能保证任意长中断后无损。

## 运行边界

| 场景 | 行为 |
|---|---|
| 关闭 Qt 窗口 | 电脑、relay 和独立 SSH 隧道仍运行时继续转发 |
| 电脑到中央断网 | 现场预览继续，电脑缓存增长，恢复后补传 |
| 网线/SSH 中断 | 机器人 agent 继续采集缓存；relay 隐藏过期健康数值，恢复后接收积压 |
| 电脑关机/relay 停止 | 中央无法收到新数据；机器人有限缓存，电脑已接收队列重启后继续补传 |
| 缓存满/磁盘不足 | 显示接收或采集异常；不自动清理原始数据 |

上位机 doctor 的软件、磁盘与 ROS 模块检查仅代表电脑环境，不代表机器人主板；诊断证据保留机器人上报档案与来源。可用 SSH 隧道的 `18911` 单独查询机器人 doctor，或用 `8892` 查询上位机环境。

## 验证范围

单元测试覆盖两级确认、哈希冲突、电脑落盘失败、ACK 响应丢失、电脑队列重启、身份/来源绑定、中央中断和本地失联隐藏。`scripts/check_e2e.py` 使用五组模拟 agent+relay 和本机 TCP 代理模拟网线中断，`check_operations_e2e.py` 检查当前 Qt、诊断持久化与导出。测试不改变真实网卡，不连接机器人，不构成实机网线、SSH 或长期稳定性验收。
