"""A deliberately small MCP server for Telegram workflow alerts.

The server exposes one external-write tool, ``send_alert``. The destination chat
is fixed in process configuration, so a model cannot choose an arbitrary recipient.
Successful deliveries are recorded by caller-supplied event ID to suppress repeats
when a scheduled workflow sees the same source event again.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

BOT_API_ROOT = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_PENDING_LEASE_SECONDS = 300
MAX_MESSAGE_LENGTH = 4096
MAX_EVENT_ID_LENGTH = 256
MAX_RESPONSE_BYTES = 1_000_000
_BOT_TOKEN = re.compile(r"^[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,}$")
_CHAT_ID = re.compile(r"^-?[1-9][0-9]{0,19}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

EXTERNAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

mcp = FastMCP(
    "telegram-alert-mcp",
    instructions=(
        "Use send_alert only after the workflow has verified a real alert condition. "
        "Every call sends data to one preconfigured Telegram chat. Reuse the stable "
        "source event ID on later cycles so duplicate alerts are suppressed."
    ),
)


class TelegramAlertError(RuntimeError):
    """A safe error suitable for returning through MCP."""


def _macos_system_ssl_context() -> ssl.SSLContext | None:
    """Build a verified TLS context from macOS system trust stores.

    Some managed macOS networks install a trusted inspection certificate in the
    system keychain. Python distributions do not always consult that keychain,
    even though macOS applications do. This fallback is used only after the
    normal verified connection fails with a certificate-verification error.
    """
    if sys.platform != "darwin":
        return None
    certificate_stores = (
        "/Library/Keychains/System.keychain",
        "/System/Library/Keychains/SystemRootCertificates.keychain",
    )
    certificates: list[bytes] = []
    try:
        for store in certificate_stores:
            result = subprocess.run(
                ["security", "find-certificate", "-a", "-p", store],
                check=True,
                capture_output=True,
                timeout=10,
            )
            if result.stdout:
                certificates.append(result.stdout)
        if not certificates:
            return None
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=b"".join(certificates).decode("ascii"))
        return context
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ssl.SSLError):
        return None


def _parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise TelegramAlertError(f"{field} must be true or false.")


def _require_bot_token(value: str) -> str:
    if not _BOT_TOKEN.fullmatch(value):
        raise TelegramAlertError("TELEGRAM_BOT_TOKEN is missing or invalid.")
    return value


def _require_chat_id(value: str) -> str:
    if not _CHAT_ID.fullmatch(value):
        raise TelegramAlertError("TELEGRAM_CHAT_ID must be a numeric Telegram chat ID.")
    return value


def _require_message(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TelegramAlertError("message must not be empty.")
    if len(value) > MAX_MESSAGE_LENGTH:
        raise TelegramAlertError(f"message must be at most {MAX_MESSAGE_LENGTH} characters.")
    if "\x00" in value:
        raise TelegramAlertError("message contains a forbidden NUL character.")
    return value


def _require_event_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_EVENT_ID_LENGTH
        or _CONTROL.search(value)
    ):
        raise TelegramAlertError(
            f"event_id must be 1-{MAX_EVENT_ID_LENGTH} printable characters."
        )
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: str
    state_path: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    protect_content: bool = True

    @classmethod
    def from_env(cls, *, require_chat_id: bool = True) -> "Settings":
        bot_token = _require_bot_token(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        raw_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if require_chat_id and not raw_chat_id:
            raise TelegramAlertError(
                "TELEGRAM_CHAT_ID is not configured. Open the bot, press Start, "
                "then use List recent Telegram chats and Select alert chat."
            )
        chat_id = _require_chat_id(raw_chat_id) if raw_chat_id else ""
        raw_timeout = os.environ.get(
            "TELEGRAM_ALERT_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)
        )
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise TelegramAlertError(
                "TELEGRAM_ALERT_TIMEOUT_SECONDS must be a number."
            ) from exc
        if not 1 <= timeout <= 60:
            raise TelegramAlertError(
                "TELEGRAM_ALERT_TIMEOUT_SECONDS must be between 1 and 60."
            )
        default_path = Path.home() / ".local" / "state" / "telegram-alert-mcp" / "alerts.sqlite3"
        state_path = Path(
            os.environ.get("TELEGRAM_ALERT_STATE_PATH", str(default_path))
        ).expanduser()
        if state_path.exists() and state_path.is_dir():
            raise TelegramAlertError("TELEGRAM_ALERT_STATE_PATH must be a file path.")
        protect_content = _parse_bool(
            os.environ.get("TELEGRAM_ALERT_PROTECT_CONTENT", "true"),
            "TELEGRAM_ALERT_PROTECT_CONTENT",
        )
        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
            state_path=state_path,
            timeout_seconds=timeout,
            protect_content=protect_content,
        )


@dataclass(frozen=True)
class Reservation:
    outcome: str
    telegram_message_id: int | None = None
    delivered_at: str | None = None


class DeliveryStore:
    """SQLite-backed duplicate suppression for recurring workflows."""

    def __init__(self, path: Path, lease_seconds: int = DEFAULT_PENDING_LEASE_SECONDS):
        self.path = path
        self.lease = timedelta(seconds=lease_seconds)

    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT PRIMARY KEY,
                    message_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered')),
                    reserved_at TEXT NOT NULL,
                    delivered_at TEXT,
                    telegram_message_id INTEGER
                )
                """
            )
            return connection
        except (OSError, sqlite3.Error) as exc:
            raise TelegramAlertError("Could not open the local alert delivery state.") from exc

    def reserve(self, event_id: str, message_sha256: str) -> Reservation:
        now = datetime.now(UTC)
        now_text = now.isoformat()
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT * FROM deliveries WHERE event_id = ?", (event_id,)
                    ).fetchone()
                    if row is None:
                        connection.execute(
                            """INSERT INTO deliveries
                               (event_id, message_sha256, status, reserved_at)
                               VALUES (?, ?, 'pending', ?)""",
                            (event_id, message_sha256, now_text),
                        )
                        return Reservation("reserved")
                    if row["message_sha256"] != message_sha256:
                        raise TelegramAlertError(
                            "event_id was already used with different alert text."
                        )
                    if row["status"] == "delivered":
                        return Reservation(
                            "duplicate",
                            telegram_message_id=row["telegram_message_id"],
                            delivered_at=row["delivered_at"],
                        )
                    reserved_at = datetime.fromisoformat(row["reserved_at"])
                    if now - reserved_at < self.lease:
                        return Reservation("in_progress")
                    connection.execute(
                        "UPDATE deliveries SET reserved_at = ? WHERE event_id = ?",
                        (now_text, event_id),
                    )
                    return Reservation("reserved")
        except TelegramAlertError:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise TelegramAlertError("Could not update the local alert delivery state.") from exc

    def mark_delivered(
        self, event_id: str, message_sha256: str, telegram_message_id: int
    ) -> str:
        delivered_at = datetime.now(UTC).isoformat()
        try:
            with closing(self._connect()) as connection:
                with connection:
                    updated = connection.execute(
                        """UPDATE deliveries
                           SET status = 'delivered', delivered_at = ?, telegram_message_id = ?
                           WHERE event_id = ? AND message_sha256 = ? AND status = 'pending'""",
                        (delivered_at, telegram_message_id, event_id, message_sha256),
                    ).rowcount
                    if updated != 1:
                        raise TelegramAlertError(
                            "Could not finalize the alert delivery record."
                        )
                    return delivered_at
        except TelegramAlertError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise TelegramAlertError("Could not update the local alert delivery state.") from exc

    def release(self, event_id: str, message_sha256: str) -> None:
        """Allow the next workflow cycle to retry a confirmed failed request."""
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        """DELETE FROM deliveries
                           WHERE event_id = ? AND message_sha256 = ? AND status = 'pending'""",
                        (event_id, message_sha256),
                    )
        except (OSError, sqlite3.Error):
            # Preserve the original Telegram error. A pending lease expires automatically.
            return


UrlOpen = Callable[..., Any]


class TelegramBotClient:
    def __init__(self, settings: Settings, urlopen: UrlOpen = urllib.request.urlopen):
        self.settings = settings
        self.urlopen = urlopen

    def _read_response(self, request: urllib.request.Request) -> bytes:
        try:
            try:
                with self.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, ssl.SSLCertVerificationError):
                    raise
                context = _macos_system_ssl_context()
                if context is None:
                    raise
                with self.urlopen(
                    request,
                    timeout=self.settings.timeout_seconds,
                    context=context,
                ) as response:
                    body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise TelegramAlertError("Telegram returned an oversized response.")
            return body
        except urllib.error.HTTPError as exc:
            retry_after: int | None = None
            try:
                parsed_error = json.loads(exc.read(MAX_RESPONSE_BYTES).decode("utf-8"))
                raw_retry = parsed_error.get("parameters", {}).get("retry_after")
                if isinstance(raw_retry, int):
                    retry_after = raw_retry
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            suffix = f"; retry after {retry_after} seconds" if retry_after else ""
            raise TelegramAlertError(
                f"Telegram Bot API rejected the request (HTTP {exc.code}{suffix})."
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TelegramAlertError("Could not reach Telegram Bot API.") from None

    def _get_json(self, method: str, query: str = "") -> dict[str, Any]:
        request = urllib.request.Request(
            f"{BOT_API_ROOT}/bot{self.settings.bot_token}/{method}{query}",
            headers={"User-Agent": "telegram-alert-mcp/0.2.1"},
            method="GET",
        )
        body = self._read_response(request)
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramAlertError("Telegram returned an invalid response.") from None
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            raise TelegramAlertError("Telegram Bot API did not accept the request.")
        return parsed

    def get_me(self) -> dict[str, Any]:
        result = self._get_json("getMe").get("result")
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise TelegramAlertError("Telegram returned invalid bot details.")
        return result

    def recent_chats(self, limit: int) -> list[dict[str, Any]]:
        result = self._get_json("getUpdates", f"?limit={limit}").get("result")
        if not isinstance(result, list):
            raise TelegramAlertError("Telegram returned invalid updates.")
        chats: list[dict[str, Any]] = []
        seen: set[int] = set()
        for update in result:
            if not isinstance(update, dict):
                continue
            chat: dict[str, Any] | None = None
            for value in update.values():
                if isinstance(value, dict) and isinstance(value.get("chat"), dict):
                    chat = value["chat"]
                    break
            if chat is None:
                continue
            chat_id = chat.get("id")
            chat_type = chat.get("type")
            if not isinstance(chat_id, int) or chat_id in seen or not isinstance(chat_type, str):
                continue
            seen.add(chat_id)
            chats.append({"chat_id": str(chat_id), "type": chat_type})
        return chats

    def send_message(self, message: str) -> int:
        payload = {
            "chat_id": self.settings.chat_id,
            "text": message,
            "protect_content": self.settings.protect_content,
            "link_preview_options": {"is_disabled": True},
        }
        request = urllib.request.Request(
            f"{BOT_API_ROOT}/bot{self.settings.bot_token}/sendMessage",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "telegram-alert-mcp/0.2.1",
            },
            method="POST",
        )
        try:
            body = self._read_response(request)
        except TelegramAlertError as exc:
            raise TelegramAlertError(str(exc).replace("request", "alert")) from None

        try:
            parsed = json.loads(body.decode("utf-8"))
            if parsed.get("ok") is not True:
                raise TelegramAlertError("Telegram Bot API did not accept the alert.")
            message_id = parsed["result"]["message_id"]
            if not isinstance(message_id, int):
                raise TypeError
            return message_id
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if isinstance(exc, TelegramAlertError):
                raise
            raise TelegramAlertError("Telegram returned an invalid response.") from None


def _message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _send_alert(
    message: str,
    event_id: str,
    *,
    settings: Settings | None = None,
    store: DeliveryStore | None = None,
    client: TelegramBotClient | None = None,
) -> dict[str, Any]:
    message = _require_message(message)
    event_id = _require_event_id(event_id)
    settings = settings or Settings.from_env()
    digest = _message_digest(message)
    store = store or DeliveryStore(settings.state_path)
    reservation = store.reserve(event_id, digest)
    if reservation.outcome == "duplicate":
        return {
            "status": "duplicate_suppressed",
            "event_id": event_id,
            "telegram_message_id": reservation.telegram_message_id,
            "delivered_at": reservation.delivered_at,
        }
    if reservation.outcome == "in_progress":
        return {"status": "delivery_in_progress", "event_id": event_id}

    client = client or TelegramBotClient(settings)
    try:
        telegram_message_id = client.send_message(message)
    except Exception:
        store.release(event_id, digest)
        raise
    delivered_at = store.mark_delivered(event_id, digest, telegram_message_id)
    return {
        "status": "sent",
        "event_id": event_id,
        "telegram_message_id": telegram_message_id,
        "delivered_at": delivered_at,
    }


def _discovery_client() -> TelegramBotClient:
    """Create a client that needs only the bot token, not a selected chat."""
    return TelegramBotClient(Settings.from_env(require_chat_id=False))


def _store_chat_id_on_macos(chat_id: str) -> None:
    if sys.platform != "darwin":
        raise TelegramAlertError(
            "Automatic chat selection is available on macOS only. "
            "Store TELEGRAM_CHAT_ID in your host's secret manager instead."
        )
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                "telegram-alert-mcp",
                "-a",
                "chat-id",
                "-w",
                chat_id,
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise TelegramAlertError("Could not save the selected chat in macOS Keychain.") from None


@mcp.tool(
    title="Check Telegram bot connection",
    annotations=READ_ONLY,
)
def check_bot_connection() -> dict[str, Any]:
    """Check the configured bot token without reading messages or sending anything."""
    bot = _discovery_client().get_me()
    return {
        "status": "connected",
        "bot_id": bot["id"],
        "username": bot.get("username"),
        "can_join_groups": bool(bot.get("can_join_groups")),
    }


@mcp.tool(
    title="List recent Telegram chats",
    annotations=READ_ONLY,
)
def list_recent_telegram_chats(limit: int = 20) -> dict[str, Any]:
    """List chat IDs from recent bot updates without returning message text.

    First ask the person configuring the bot to open it in Telegram and press
    Start (or send /start). This tool is read-only and returns only chat IDs and
    chat types, so a user can safely identify the intended recipient.
    """
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise TelegramAlertError("limit must be an integer between 1 and 100.")
    chats = _discovery_client().recent_chats(limit)
    return {
        "status": "found" if chats else "none_found",
        "chats": chats,
        "next_step": (
            "Ask the user to send /start to the bot, then call this tool again."
            if not chats
            else "Ask the user which listed chat should receive alerts, then call Select alert chat."
        ),
    }


@mcp.tool(
    title="Select Telegram alert chat",
    annotations=EXTERNAL_WRITE,
)
def select_alert_chat(chat_id: str) -> dict[str, Any]:
    """Save one recently discovered chat as the fixed alert destination on macOS.

    This changes local Keychain configuration. Call it only after the user has
    explicitly confirmed one of the IDs returned by List recent Telegram chats.
    An arbitrary chat ID is refused: it must occur in the bot's recent updates.
    """
    chat_id = _require_chat_id(chat_id)
    candidates = _discovery_client().recent_chats(100)
    selected = next((item for item in candidates if item["chat_id"] == chat_id), None)
    if selected is None:
        raise TelegramAlertError(
            "The supplied chat_id was not found in recent bot updates. "
            "Ask the user to open the bot and send /start, then list chats again."
        )
    _store_chat_id_on_macos(chat_id)
    os.environ["TELEGRAM_CHAT_ID"] = chat_id
    return {
        "status": "saved",
        "chat_id": chat_id,
        "type": selected["type"],
        "message": "This is now the fixed destination for Telegram Alerts.",
    }


@mcp.tool(
    title="Send Telegram setup test",
    annotations=EXTERNAL_WRITE,
)
def send_setup_test() -> dict[str, Any]:
    """Send one safe setup test to the fixed configured chat.

    This is an external message. Call it only after the user explicitly asks for
    a test. Repeated calls use the same event ID and therefore do not create
    duplicate setup messages.
    """
    return _send_alert(
        "Telegram Alerts подключён успешно.",
        "setup:telegram-alert-mcp:v1",
    )


@mcp.tool(
    title="Send Telegram alert",
    annotations=EXTERNAL_WRITE,
)
def send_alert(message: str, event_id: str) -> dict[str, Any]:
    """Send one confirmed alert to the configured Telegram chat.

    This is an external side effect. Call it only after the source data and alert
    condition have been checked. ``event_id`` must be a stable unique identifier
    derived from the source event, such as ``gmail:<message-id>:rule-v1``. Reusing
    that ID with the same text suppresses duplicate alerts in later schedule cycles.
    """
    return _send_alert(message, event_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
