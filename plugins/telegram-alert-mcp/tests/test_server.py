import io
import json
import ssl
import urllib.error
from pathlib import Path

import pytest

from telegram_alert_mcp import server


class FakeResponse:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def settings(tmp_path: Path) -> server.Settings:
    return server.Settings(
        bot_token="12345:test_token_not_real_xxxxx",
        chat_id="-1001234567890",
        state_path=tmp_path / "alerts.sqlite3",
    )


def test_client_posts_plain_text_to_fixed_chat(tmp_path):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse({"ok": True, "result": {"message_id": 42}})

    client = server.TelegramBotClient(settings(tmp_path), urlopen=fake_urlopen)
    assert client.send_message("🚨 Test") == 42
    assert captured["url"].endswith("/sendMessage")
    assert captured["payload"] == {
        "chat_id": "-1001234567890",
        "text": "🚨 Test",
        "protect_content": True,
        "link_preview_options": {"is_disabled": True},
    }
    assert "parse_mode" not in captured["payload"]


def test_discovery_returns_bot_and_chats_without_message_text(tmp_path):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if request.full_url.endswith("/getMe"):
            return FakeResponse({"ok": True, "result": {"id": 44, "username": "demo_bot"}})
        return FakeResponse(
            {
                "ok": True,
                "result": [
                    {"message": {"text": "private content", "chat": {"id": 101, "type": "private"}}},
                    {"message": {"text": "group content", "chat": {"id": -202, "type": "group"}}},
                    {"message": {"text": "repeat", "chat": {"id": 101, "type": "private"}}},
                ],
            }
        )

    client = server.TelegramBotClient(settings(tmp_path), urlopen=fake_urlopen)
    assert client.get_me() == {"id": 44, "username": "demo_bot"}
    assert client.recent_chats(20) == [
        {"chat_id": "101", "type": "private"},
        {"chat_id": "-202", "type": "group"},
    ]
    assert any(url.endswith("/getMe") for url in calls)
    assert any("/getUpdates?limit=20" in url for url in calls)


def test_client_retries_certificate_error_with_macos_system_trust(monkeypatch, tmp_path):
    calls = []
    trusted_context = object()

    def fake_urlopen(request, timeout, **kwargs):
        calls.append(kwargs.get("context"))
        if "context" not in kwargs:
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "untrusted"))
        return FakeResponse({"ok": True, "result": {"message_id": 43}})

    monkeypatch.setattr(server, "_macos_system_ssl_context", lambda: trusted_context)
    client = server.TelegramBotClient(settings(tmp_path), urlopen=fake_urlopen)
    assert client.send_message("Retry") == 43
    assert calls == [None, trusted_context]


def test_send_alert_suppresses_same_event_on_later_cycle(tmp_path):
    calls = []

    class FakeClient:
        def send_message(self, message):
            calls.append(message)
            return 77

    configured = settings(tmp_path)
    first = server._send_alert(
        "Приостановление лицензии",
        "gmail:<message-1>:rule-v1",
        settings=configured,
        client=FakeClient(),
    )
    second = server._send_alert(
        "Приостановление лицензии",
        "gmail:<message-1>:rule-v1",
        settings=configured,
        client=FakeClient(),
    )
    assert first["status"] == "sent"
    assert second["status"] == "duplicate_suppressed"
    assert calls == ["Приостановление лицензии"]


def test_same_event_id_with_changed_text_is_refused(tmp_path):
    class FakeClient:
        def send_message(self, message):
            return 77

    configured = settings(tmp_path)
    server._send_alert("First", "event-1", settings=configured, client=FakeClient())
    with pytest.raises(server.TelegramAlertError, match="different alert text"):
        server._send_alert("Changed", "event-1", settings=configured, client=FakeClient())


def test_failed_request_is_released_for_next_cycle(tmp_path):
    calls = []

    class FailingClient:
        def send_message(self, message):
            calls.append("failed")
            raise server.TelegramAlertError("network failed")

    class WorkingClient:
        def send_message(self, message):
            calls.append("sent")
            return 88

    configured = settings(tmp_path)
    with pytest.raises(server.TelegramAlertError, match="network failed"):
        server._send_alert("Alert", "event-2", settings=configured, client=FailingClient())
    result = server._send_alert(
        "Alert", "event-2", settings=configured, client=WorkingClient()
    )
    assert result["status"] == "sent"
    assert calls == ["failed", "sent"]


def test_http_error_does_not_leak_bot_token(tmp_path):
    configured = settings(tmp_path)

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(
                json.dumps(
                    {"ok": False, "parameters": {"retry_after": 9}}
                ).encode()
            ),
        )

    client = server.TelegramBotClient(configured, urlopen=fake_urlopen)
    with pytest.raises(server.TelegramAlertError) as caught:
        client.send_message("Alert")
    assert configured.bot_token not in str(caught.value)
    assert "retry after 9 seconds" in str(caught.value)


@pytest.mark.parametrize("message", ["", "   ", "x" * 4097, "bad\x00text"])
def test_invalid_messages_are_refused(message):
    with pytest.raises(server.TelegramAlertError):
        server._require_message(message)


@pytest.mark.parametrize("event_id", ["", "x" * 257, "bad\nvalue"])
def test_invalid_event_ids_are_refused(event_id):
    with pytest.raises(server.TelegramAlertError):
        server._require_event_id(event_id)


def test_settings_require_numeric_fixed_chat(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "TELEGRAM_BOT_TOKEN", "12345:test_token_not_real_xxxxx"
    )
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@public_channel")
    monkeypatch.setenv("TELEGRAM_ALERT_STATE_PATH", str(tmp_path / "state.sqlite3"))
    with pytest.raises(server.TelegramAlertError, match="numeric Telegram chat ID"):
        server.Settings.from_env()


def test_settings_allow_token_only_for_read_only_discovery(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:test_token_not_real_xxxxx")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_ALERT_STATE_PATH", str(tmp_path / "state.sqlite3"))
    assert server.Settings.from_env(require_chat_id=False).chat_id == ""


def test_select_alert_chat_requires_discovered_id_and_saves_on_macos(monkeypatch):
    class Discovery:
        def recent_chats(self, limit):
            assert limit == 100
            return [{"chat_id": "101", "type": "private"}]

    saved = {}
    monkeypatch.setattr(server, "_discovery_client", lambda: Discovery())
    monkeypatch.setattr(server, "_store_chat_id_on_macos", lambda value: saved.setdefault("chat_id", value))
    result = server.select_alert_chat("101")
    assert result["status"] == "saved"
    assert saved == {"chat_id": "101"}
    assert server.os.environ["TELEGRAM_CHAT_ID"] == "101"


def test_select_alert_chat_refuses_arbitrary_destination(monkeypatch):
    class Discovery:
        def recent_chats(self, limit):
            return [{"chat_id": "101", "type": "private"}]

    monkeypatch.setattr(server, "_discovery_client", lambda: Discovery())
    with pytest.raises(server.TelegramAlertError, match="not found in recent"):
        server.select_alert_chat("202")


def test_setup_tools_have_expected_write_boundaries():
    tools = {tool.name: tool for tool in server.mcp._tool_manager._tools.values()}
    assert set(tools) == {
        "check_bot_connection",
        "list_recent_telegram_chats",
        "select_alert_chat",
        "send_setup_test",
        "send_alert",
    }
    for name in {"check_bot_connection", "list_recent_telegram_chats"}:
        assert tools[name].annotations.readOnlyHint is True
    for name in {"select_alert_chat", "send_setup_test", "send_alert"}:
        annotations = tools[name].annotations
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is False
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is True
