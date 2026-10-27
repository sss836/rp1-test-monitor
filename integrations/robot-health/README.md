# RP1 电机健康只读发布补丁

如果只需在终端查看一台LRO电机的温度，可使用独立单文件工具[`read_temperature.cpp`](read_temperature.cpp)，按[简单说明](../../docs/SIMPLE_MOTOR_TEMPERATURE.md)上传、编译、运行。该工具不应用下述补丁、不修改运控；当前仅通过本地软件自检，尚未真机运行。

本目录包含可应用的驱动/运控补丁、同帧快照与诊断消息代码、C++ 测试和版本校验器。补丁读取实际运控进程持有的电机对象；不增加硬件控制实例，不发送查询/使能/清故障帧。

## 适配基线

- roboparty_motors：`45eac3ff3dc860e3bd688c1e62a3c5368166849a`
- roboparty_inference：`fbf32e9227d9dbb4cefe3718916c37a3d980bc64`

`manifest.json` 记录每个被修改文件的原始 SHA-256。用户的版本未知时不能盲套补丁。检查器先验证两个目录及所有补丁，再执行应用步骤。建议在独立源码副本应用；本次只在 `/tmp` 副本验证，未改原始运控目录。

```bash
python3 integrations/robot-health/apply_patches.py --motors /path/to/roboparty_motors --inference /path/to/roboparty_inference
# 上述检查通过后，在用于构建的源码副本应用：
python3 integrations/robot-health/apply_patches.py --motors /path/to/roboparty_motors --inference /path/to/roboparty_inference --apply
```

补丁不执行构建、部署或服务启停。按原工程流程先重新构建并安装 motors，再构建 inference。新增 ROS 依赖为 `diagnostic_msgs`。完整 inference 构建还需要原工程的 ONNX Runtime 等依赖，本次环境没有 ONNX 头文件，未声称完成整套运控构建。

## 实现与消息

1. LRO 只接受匹配 CAN ID、长度有效且反馈类型为 1 的帧；RP 只接受匹配响应 ID、长度至少 19 且类型为 6 的帧。拒绝错误帧、RTR 和扩展 ID。
2. 电机温度、MOS 温度、完整原始故障码、序号、主机单调时间以一份缓存更新。RP 16 位故障码不经过旧的 8 位 getter；负温度保持有符号数。
3. 健康缓存写入使用 try-lock；与读取竞争时跳过本次健康快照，不阻塞反馈回调。序号表示已保存的有效健康快照，不是总线无损帧计数。
4. 既有 `RobotInterface` 按实际 `motor2urdf_` 映射构造 `joint_N`，保留总线和电机 ID。这里是软件配置映射，现场物理左右部位仍需核对。
5. 运控节点以独立 500ms ROS timer 发布 `/rp1/diagnostics` (`diagnostic_msgs/DiagnosticArray`)；高频控制循环不做网络或数据库操作。
6. 没有有效反馈或反馈超过 5 秒时，诊断级别为 STALE，各字段 valid=false。序号和真实反馈年龄原样保留；重复发布不构成新反馈。

每个 DiagnosticStatus 包含：`schema_version=1`、`joint_name`、`bus`、`motor_id`、`protocol`、`producer_epoch`、`valid`、`feedback_sequence`、`feedback_age_ms`，以及 motor_temperature / mos_temperature / fault_code 各自的 valid 和数值。

时间表示驱动完成有效帧解析后的主机时间，不是电机硬件采样时间。ROS header 使用发布节点时间；订阅端结合传输年龄和反馈年龄判断时效。ROS 使用仿真时钟时必须保持发布和采集时间体系一致；回放脚本使用显式合成时间，不把旧录包时间冒充当前数据。

采集端必须加载设备档案，匹配总线＋ID＋协议＋关节名；未映射或重复/乱序反馈不会入库。物理映射是否完成现场核对另行展示。ROS 图中的同名发布者并不是设备身份认证，本实现仍依赖受控的 ROS 网络环境。

## 软件验证

```bash
g++ -std=c++17 -O2 -pthread -I integrations/robot-health/include integrations/robot-health/tests/snapshot_test.cpp -o /tmp/rp1-health-test
/tmp/rp1-health-test
.venv/bin/python scripts/check_health_replay.py
```

回放从 C++ 合成 CAN 帧解码，经实际 ROS 诊断消息结构到 Python 接收、中央数据库和诊断证据包。结果见 `docs/health-replay-verification.json`。不会创建 ROS 控制节点或打开 CAN 设备。

补丁及 C++ 文件沿用目标代码的 GPL-3.0 标识；上游原版权标识保留在补丁上下文中。
