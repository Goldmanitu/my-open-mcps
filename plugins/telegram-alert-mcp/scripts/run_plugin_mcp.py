#!/usr/bin/env python3
"""Launch the bundled MCP server from a Codex plugin installation."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LOCAL_VENV_PYTHON = PLUGIN_ROOT / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python"
)
SHARED_VENV_PYTHON = (
    Path.home()
    / ".local"
    / "share"
    / "telegram-alert-mcp"
    / "venv"
    / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
)


def _find_runtime() -> Path | None:
    configured = os.environ.get("TELEGRAM_ALERT_MCP_PYTHON", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend([LOCAL_VENV_PYTHON, SHARED_VENV_PYTHON])
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _read_macos_keychain(account: str) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "telegram-alert-mcp",
                "-a",
                account,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.rstrip("\r\n")


def _load_configuration() -> None:
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        token = _read_macos_keychain("bot-token")
        if token:
            os.environ["TELEGRAM_BOT_TOKEN"] = token
    if not os.environ.get("TELEGRAM_CHAT_ID"):
        chat_id = _read_macos_keychain("chat-id")
        if chat_id:
            os.environ["TELEGRAM_CHAT_ID"] = chat_id


def main() -> None:
    if importlib.util.find_spec("mcp") is None:
        runtime = _find_runtime()
        current_executable = Path(os.path.abspath(sys.executable))
        if runtime is not None and Path(os.path.abspath(runtime)) != current_executable:
            os.execv(str(runtime), [str(runtime), str(Path(__file__).resolve())])
        print(
            "telegram-alert-mcp requires the MCP Python SDK. "
            "Follow the public plugin setup in README.md.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    _load_configuration()
    sys.path.insert(0, str(PLUGIN_ROOT))
    from telegram_alert_mcp.server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
