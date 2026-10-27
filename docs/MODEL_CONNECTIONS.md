# 诊断助手的多模型连接

v0.4.0的模型请求仍由中央服务执行，经上位机访问中央；机器人不连接模型或中央服务。网线部署见 [WIRED_RELAY.md](WIRED_RELAY.md)。

诊断助手采用 pi 的 Provider / Model / API 分层思路，在现有 Python 后端实现注册表、Key 解析和协议适配。没有引入 Node 版 pi SDK，也没有复制完整 Agent 框架。现有只读工具、机器人范围、时间范围和证据引用校验继续共用。

## 在界面中使用

桌面端：进入「诊断助手」→「连接与模型设置」。网页端：打开诊断对话，在「当前上下文」中点击同名按钮。

1. 选择 DeepSeek、千问北京、千问新加坡、OpenAI，或服务端登记的自定义服务。
2. 填写平台实际支持的模型 ID。预设名称可以编辑；模型可用性以账户、地域及服务端为准。
3. 填入对应厂商与地域的 API Key，也可留空使用该服务在中央后端的环境变量。
4. 点击「测试连接」。测试发出一次短模型请求，检查鉴权、协议和 Function Calling，不发送机器人观测；厂商可能按其规则计费。
5. 勾选「用于诊断对话」，再回到聊天窗口发送问题。此时才发送问题、最近对话和所选证据。

Key 只存在当前窗口内，不写本地存储、数据库、证据包或聊天历史。网页刷新/关闭、桌面退出后需重新填写。切换厂商会清空输入的 Key；模型分析默认关闭。远程传递 Key 须使用 HTTPS，本机回环预览允许 HTTP。中央服务只读凭据与模型 API Key 是两个独立字段。

网页模型设置（本机历史材料，不随源码发布）

## 内置服务与协议

| 服务 ID | 默认模型示例 | 协议 | 服务端 Key 环境变量 |
|---|---|---|---|
| `deepseek` | `deepseek-flash` | `openai-completions` | `DEEPSEEK_API_KEY` |
| `qwen` | `qwen-plus` | `openai-completions` | `DASHSCOPE_API_KEY` |
| `qwen-intl` | `qwen-plus` | `openai-completions` | `DASHSCOPE_INTL_API_KEY` |
| `openai` | 手动填写模型 ID | `openai-responses` | `OPENAI_API_KEY` |
| `server` | 服务端默认 | `RP1_AI_API` 指定 | `RP1_AI_API_KEY` 或所选默认服务的 Key |

DeepSeek 使用其官方 Chat API。千问预设使用仍受支持的 DashScope 北京/新加坡共享域名；生产用业务空间专属域名可通过自定义服务覆盖对应条目。Key 必须与地域、计费方案和模型权限匹配。

为了兼容有界的强制工具查询，DeepSeek 预设显式关闭 thinking，千问预设发送 `enable_thinking=false`；本版不提供推理过程展示。兼容协议不等于所有模型都支持工具调用，以连接测试和实际问答为准。

## 服务端配置

无界面默认选择可用环境变量设置，例如：

```bash
export RP1_AI_ENABLED=1
export RP1_AI_PROVIDER=deepseek
export RP1_AI_MODEL=deepseek-flash
# 在服务环境或秘密管理器中注入 DEEPSEEK_API_KEY，再启动中央服务。
```

千问使用 `RP1_AI_PROVIDER=qwen`，并提供 `DASHSCOPE_API_KEY`。网页或桌面输入的 Key 优先于对应服务端变量；显式切换厂商不会回退使用其他厂商的 Key。

原来的 `RP1_AI_ENDPOINT`、`RP1_AI_MODEL`、`RP1_AI_API_KEY` 和 `RP1_AI_ENABLED` 继续可用。默认协议为 `openai-responses`；完整 Chat Completions 地址须配 `RP1_AI_API=openai-completions`。

自定义企业网关或本地模型可复制 [配置模板](../deploy/models.example.json)，并将 `RP1_AI_MODELS_FILE` 指向该文件，再重启中央服务。

```json
{
  "providers": {
    "local": {
      "name": "本地 Qwen",
      "baseUrl": "http://127.0.0.1:11434/v1",
      "api": "openai-completions",
      "requiresKey": false,
      "models": [{"id": "your-local-model", "name": "本地模型"}]
    }
  }
}
```

`baseUrl` 不含 `/chat/completions` 或 `/responses` 后缀。自定义远程地址须使用 HTTPS；本地服务不需要 Key 时可设置 `requiresKey=false`。使用 `apiKeyEnv` 引用密钥变量，配置文件不接受明文 `apiKey`。普通客户端只能选择服务端登记的地址。

支持的 `compat` 为 `supportsStrictMode`、`supportsToolChoice`、`maxTokensField`（`max_tokens` 或 `max_completion_tokens`）及 `thinking`（`deepseek` / `qwen`）。不支持 Function Calling 的模型不能承担诊断 Agent 的证据查询。

## 接口与验证

- `GET /api/v1/assistant/models`：服务目录和连接元信息，不返回 Key。
- `POST /api/v1/assistant/test`：请求为 `{provider, model, api_key}`，验证连接和工具调用。
- 原有 `POST /api/v1/robot-monitors/{id}/assistant` 新增可选 `connection` 对象，字段同上。

全部沿用中央只读认证；鉴权错误、额度/限流、超时、格式不兼容会给出可读提示。验证失败不会返回包含 Key 的原始输入或厂商响应体。

自动测试使用 Mock 模型检验两种协议的工具往返、原生工具消息保留、引用校验、Key 隔离和失败处理。没有提供真实厂商 Key，因此尚未完成真实账户的线上联通与计费验证；也没有实机连接。

## 参考

- [用户提供的 pi-agent 教程](https://github.com/buchidonggua/dg-ai-notes/tree/main/pi-agent)
- [pi-ai 官方 Provider 与协议设计](https://github.com/earendil-works/pi/tree/main/packages/ai)
- [DeepSeek 官方 Chat API](https://api-docs.deepseek.com/api/create-chat-completion/)
- [百炼官方 Base URL 与地域说明](https://help.aliyun.com/zh/model-studio/base-url)
- [千问 OpenAI 兼容协议](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions/)
