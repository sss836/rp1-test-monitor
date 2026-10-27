"""Provider wire compatibility and credential boundaries, without paid API calls."""

import json

import httpx
import pytest
from pydantic import ValidationError

from rp1_monitor.assistant import ReadOnlyAssistant
from rp1_monitor.model_providers import AISettings, ModelConnection, ModelError, ModelRegistry
from rp1_monitor.monitor_service import MonitorService
from rp1_monitor.operations_api import AssistantQuery


@pytest.mark.parametrize(
    "provider,host,model,thinking",
    [
        ("deepseek", "api.deepseek.com", "deepseek-flash", {"thinking": {"type": "disabled"}}),
        ("qwen", "dashscope.aliyuncs.com", "qwen-plus", {"enable_thinking": False}),
        ("qwen-intl", "dashscope-intl.aliyuncs.com", "qwen-plus", {"enable_thinking": False}),
    ],
)
def test_chat_completion_tools_round_trip_and_citations(client, provider, host, model, thinking):
    sent = []
    secret = "fake-request-key-not-real"

    def mock(request):
        packet = json.loads(request.content)
        sent.append(packet)
        assert request.url.host == host and request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"] == "Bearer " + secret
        assert packet["model"] == model and packet["stream"] is False
        assert all(packet[key] == value for key, value in thinking.items())
        assert "api_key" not in packet and "strict" not in packet["tools"][0]["function"]
        if len(sent) == 1:
            assert packet["tool_choice"] == "required"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": "provider-native-field",
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_current_observation",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        assert packet["messages"][-2]["reasoning_content"] == "provider-native-field"
        assert (
            packet["messages"][-1]["role"] == "tool"
            and packet["messages"][-1]["tool_call_id"] == "c1"
        )
        assert "E1" in json.loads(packet["messages"][-1]["content"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "[E1] 观测缺失，原因待核对。"},
                    }
                ]
            },
        )

    assistant = ReadOnlyAssistant(
        MonitorService(client.app.state.store), AISettings(), httpx.MockTransport(mock)
    )
    answer = assistant.ask(
        "RP1-TEST",
        AssistantQuery(
            question="请核对", connection=ModelConnection(provider=provider, api_key=secret)
        ),
    )
    assert answer["mode"] == "ai" and answer["provider"] == provider
    assert set(answer["evidence"]) == {"E1"} and len(sent) == 2
    assert secret not in json.dumps(answer, ensure_ascii=False)


def test_registry_credentials_are_scoped_and_request_key_wins(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-server-deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-server-qwen")
    monkeypatch.delenv("DASHSCOPE_INTL_API_KEY", raising=False)
    registry = ModelRegistry(AISettings(api_key="fake-legacy-only"))
    assert registry.resolve(ModelConnection(provider="deepseek")).api_key == "fake-server-deepseek"
    assert registry.resolve(ModelConnection(provider="qwen")).api_key == "fake-server-qwen"
    assert (
        registry.resolve(ModelConnection(provider="deepseek", api_key="fake-user")).api_key
        == "fake-user"
    )
    with pytest.raises(ModelError, match="API Key"):
        registry.resolve(ModelConnection(provider="qwen-intl"))
    assert "fake-" not in json.dumps(registry.catalog())
    assert "fake-user" not in repr(ModelConnection(api_key="fake-user"))
    assert "fake-legacy" not in repr(registry.legacy)


def test_operator_owned_pi_style_custom_provider(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "local": {
                        "baseUrl": "http://127.0.0.1:11434/v1",
                        "api": "openai-completions",
                        "apiKeyEnv": "LOCAL_MODEL_KEY",
                        "requiresKey": False,
                        "models": [{"id": "local-qwen", "name": "Local Qwen"}],
                        "compat": {
                            "supportsToolChoice": False,
                            "maxTokensField": "max_completion_tokens",
                        },
                    }
                }
            }
        )
    )
    monkeypatch.delenv("LOCAL_MODEL_KEY", raising=False)
    registry = ModelRegistry(AISettings(), path)
    resolved = registry.resolve(ModelConnection(provider="local"))
    assert resolved.endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    assert resolved.api_key == "" and resolved.model == "local-qwen"
    with pytest.raises(ValidationError):
        ModelConnection(provider="local", baseUrl="http://evil.invalid")
    with pytest.raises(ModelError, match="未登记"):
        registry.resolve(ModelConnection(provider="unregistered", api_key="fake-key"))
    path.write_text(
        json.dumps({"providers": {"unsafe": {"baseUrl": "http://remote.invalid/v1", "models": []}}})
    )
    with pytest.raises(ModelError, match="HTTPS"):
        ModelRegistry(AISettings(), path)


@pytest.mark.parametrize("status,expected", [(401, "鉴权"), (403, "鉴权"), (429, "额度")])
def test_probe_errors_never_echo_provider_secrets(status, expected):
    assistant = ReadOnlyAssistant(
        None,
        AISettings(),
        httpx.MockTransport(
            lambda request: httpx.Response(
                status, json={"error": "fake-key-leaked-in-provider-body"}
            )
        ),
    )
    with pytest.raises(ModelError, match=expected) as error:
        assistant.probe(ModelConnection(provider="deepseek", api_key="fake-key"))
    assert "fake-key" not in str(error.value)


@pytest.mark.parametrize("provider", ["deepseek", "qwen", "openai"])
def test_probe_checks_tool_support_without_robot_data(provider):
    def mock(request):
        packet = json.loads(request.content)
        assert "robot_id" not in request.content.decode() and "E1" not in request.content.decode()
        if provider == "openai":
            assert packet["store"] is False
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "function_call",
                            "name": "connection_check",
                            "arguments": "{}",
                            "call_id": "probe",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "probe",
                                    "function": {"name": "connection_check", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ]
            },
        )

    assistant = ReadOnlyAssistant(None, AISettings(), httpx.MockTransport(mock))
    result = assistant.probe(
        ModelConnection(provider=provider, model="configured-model", api_key="fake-key")
    )
    assert result["status"] == "ready" and result["provider"] == provider
    assert "fake-key" not in json.dumps(result)


def test_model_endpoints_require_read_auth_and_hide_invalid_input(client, read_headers):
    assert client.get("/api/v1/assistant/models").status_code == 401
    assert client.post("/api/v1/assistant/test", json={"provider": "deepseek"}).status_code == 401
    result = client.get("/api/v1/assistant/models", headers=read_headers)
    assert result.status_code == 200
    assert {"deepseek", "qwen", "openai"} <= {item["id"] for item in result.json()["items"]}
    secret = "fake-secret-must-not-echo"
    invalid = {"provider": "deepseek", "api_key": secret, "endpoint": "https://bad.invalid"}
    for path, body in [
        ("/api/v1/assistant/test", invalid),
        (
            "/api/v1/robot-monitors/RP1-TEST/assistant",
            {"question": "请核对", "connection": invalid},
        ),
    ]:
        response = client.post(path, headers=read_headers, json=body)
        assert response.status_code == 422 and secret not in response.text


def test_valid_model_probe_and_chat_through_http_router(client):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from rp1_monitor.operations_api import operations_router

    def mock(request):
        packet = json.loads(request.content)
        if packet["tools"][0]["function"]["name"] == "connection_check":
            name = "connection_check"
        elif packet["messages"][-1]["role"] == "tool":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "[E1] 模拟来源，实机待核对。",
                            },
                        }
                    ]
                },
            )
        else:
            name = "get_current_observation"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": name, "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ]
            },
        )

    service = MonitorService(client.app.state.store)
    assistant = ReadOnlyAssistant(service, AISettings(), httpx.MockTransport(mock))
    app = FastAPI()
    app.include_router(
        operations_router(service, Depends(lambda: None), assistant), prefix="/api/v1"
    )
    connection = {"provider": "qwen", "api_key": "fake-request-only-key", "model": "qwen-plus"}
    with TestClient(app) as api:
        probe = api.post("/api/v1/assistant/test", json=connection)
        assert probe.status_code == 200 and probe.json()["status"] == "ready"
        answer = api.post(
            "/api/v1/robot-monitors/RP1-TEST/assistant",
            json={"question": "请核对", "connection": connection},
        )
        assert answer.status_code == 200 and answer.json()["provider"] == "qwen"
        assert "fake-request-only-key" not in answer.text


def test_headless_provider_selection_uses_corresponding_key(monkeypatch):
    monkeypatch.setenv("RP1_AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-headless-key")
    registry = ModelRegistry(AISettings(enabled=True))
    assert registry.resolve().provider == "deepseek"
    assert registry.resolve().api_key == "fake-headless-key"
    assert registry.catalog()["items"][0]["endpoint"] == "https://api.deepseek.com/chat/completions"
