#!/usr/bin/env python3
"""Backend bridge for the Quick Ask macOS panel."""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import functools
import http.client
import json
import os
import pathlib
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import quick_ask_shared as shared


QUICK_SYSTEM_PROMPT = (
    "You are a quick-question assistant in a tiny floating Mac panel. "
    "Answer clearly, naturally, and directly. "
    "Be concise by default unless the user explicitly asks for depth. "
    "Do not use preambles, bullet spam, or stage directions unless requested."
)
SAFE_CWD = pathlib.Path.home() / ".local/state/quick-ask/claude-scratch"
CODEX_APP_SERVER_STATE_DIR = pathlib.Path.home() / ".local/state/quick-ask"
CODEX_APP_SERVER_STATUS_PATH = CODEX_APP_SERVER_STATE_DIR / "codex-app-server-status.json"
DEFAULT_CODEX_APP_SERVER_CWD = pathlib.Path.home() / "Downloads"
DEFAULT_GEMINI_CWD = pathlib.Path.home() / "Downloads"
BLOCKED_PROVIDER_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "PERPLEXITY_API_KEY",
    "TOGETHER_API_KEY",
    "FIREWORKS_API_KEY",
}

CLAUDE_MODELS: list[dict[str, Any]] = [
    {
        "id": "claude::claude-opus-4-6",
        "provider": "claude",
        "model": "claude-opus-4-6",
        "label": "Claude Opus 4.6",
        "short_label": "Opus 4.6",
        "hint": "default, strongest prose",
        "default": True,
    },
    {
        "id": "claude::claude-sonnet-4-6",
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
        "short_label": "Sonnet 4.6",
        "hint": "faster remote Claude",
        "default": False,
    },
]

CODEX_MODELS: list[dict[str, Any]] = [
    {
        "id": "codex::gpt-5.3-app-server",
        "provider": "codex",
        "model": "gpt-5.3-codex",
        "label": "Codex 5.3",
        "short_label": "Codex 5.3",
        "hint": "Codex app server",
        "default": False,
        "effort": "medium",
        "runtime": "app_server",
    },
    {
        "id": "codex::gpt-5.4-instant",
        "provider": "codex",
        "model": "gpt-5.4",
        "label": "ChatGPT 5.4 Instant",
        "short_label": "ChatGPT 5.4 Instant",
        "hint": None,
        "default": False,
        "effort": "low",
        "runtime": "cli",
    },
    {
        "id": "codex::gpt-5.4-medium",
        "provider": "codex",
        "model": "gpt-5.4",
        "label": "ChatGPT 5.4 Medium",
        "short_label": "ChatGPT 5.4 Medium",
        "hint": None,
        "default": False,
        "effort": "medium",
        "runtime": "cli",
    },
]

GEMINI_MODELS: list[dict[str, Any]] = [
    {
        "id": "gemini::gemini-3-flash-preview",
        "provider": "gemini",
        "model": "gemini-3-flash-preview",
        "label": "Gemini 3 Flash",
        "short_label": "Gemini 3 Flash",
        "hint": None,
        "default": False,
    },
    {
        "id": "gemini::gemini-2.5-flash-lite",
        "provider": "gemini",
        "model": "gemini-2.5-flash-lite",
        "label": "Gemini Flash Lite",
        "short_label": "Gemini Flash Lite",
        "hint": None,
        "default": False,
    },
]

FRIENDLY_OLLAMA_NAMES = {
    "eva-qwen2.5:14b-q8": "EVA Q8",
    "type32/eva-qwen-2.5-14b:latest": "EVA 14B",
    "qwen2.5:32b": "Qwen 2.5 32B",
    "qwen2.5:14b": "Qwen 2.5 14B",
    "magnum-v4:12b-q8": "Magnum V4 Q8",
    "LESSTHANSUPER/MAGNUM_V4-Mistral_Small:12b_Q4_K_S": "Magnum Small",
    "hermes3:8b": "Hermes 3 8B",
    "qwen3:30b": "Qwen 3 30B",
    "richardyoung/qwen3-14b-abliterated:Q4_K_M": "Qwen 3 Ablit",
}

IMAGE_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/tiff": ".tiff",
}

HistoryMessage = dict[str, Any]


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def internet_reachable(timeout: float = 1.0) -> bool:
    targets = [
        ("1.1.1.1", 443),
        ("8.8.8.8", 53),
    ]
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quick Ask backend bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("models", help="List available models for the panel.")
    subparsers.add_parser("providers", help="List provider availability and login status.")
    storage_parser = subparsers.add_parser("storage", help="Inspect encrypted history storage readiness.")
    storage_parser.add_argument("--ensure-key", action="store_true", help="Create the Keychain transcript key if missing.")

    history_parser = subparsers.add_parser("history", help="List encrypted Quick Ask sessions.")
    history_parser.add_argument("--limit", type=int, default=100, help="Maximum number of sessions to return.")

    load_parser = subparsers.add_parser("load", help="Load an encrypted Quick Ask session.")
    load_parser.add_argument("--session-id", required=True, help="Session identifier to load.")

    delete_parser = subparsers.add_parser("delete", help="Delete an encrypted Quick Ask session.")
    delete_parser.add_argument("--session-id", required=True, help="Session identifier to delete.")

    chat_parser = subparsers.add_parser("chat", help="Stream a chat reply.")
    chat_parser.add_argument("--model-id", required=True, help="Combined provider/model identifier.")

    save_parser = subparsers.add_parser("save", help="Persist an encrypted transcript.")
    save_parser.add_argument("--session-id", required=True, help="Stable session identifier.")
    save_parser.add_argument("--created-at", required=True, help="Session creation timestamp.")
    save_parser.add_argument("--model-id", required=True, help="Combined provider/model identifier.")
    return parser.parse_args()


def friendly_ollama_name(model: str) -> str:
    return FRIENDLY_OLLAMA_NAMES.get(model, model)


def list_available_models() -> list[dict[str, Any]]:
    statuses = provider_statuses()
    enabled_providers = {
        status["id"]
        for status in statuses
        if status.get("available") and status.get("logged_in")
    }

    models: list[dict[str, Any]] = []
    if "codex" in enabled_providers:
        models.extend(codex_models_for_system())
    if "claude" in enabled_providers:
        models.extend(dict(model) for model in CLAUDE_MODELS)
    if "gemini" in enabled_providers:
        models.extend(dict(model) for model in GEMINI_MODELS)
    try:
        endpoint = shared.resolve_ollama_endpoint(ensure_local=True, prefer_env=False)
        records = shared.list_model_records(endpoint["base_url"])
    except Exception:
        return models

    for record in records:
        name = str(record.get("name") or "").strip()
        if not name:
            continue
        label = friendly_ollama_name(name)
        models.append(
            {
                "id": f"ollama::{name}",
                "provider": "ollama",
                "model": name,
                "label": label,
                "short_label": label,
                "hint": None,
                "endpoint": endpoint.get("label", endpoint["base_url"]),
                "default": False,
            }
        )
    return sort_models_by_usage(models)


def parse_iso_datetime(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def model_usage_scores() -> dict[str, float]:
    if history_disabled():
        return {}

    now = dt.datetime.now(dt.timezone.utc)
    base_dir = shared.default_save_dir()
    scores: dict[str, float] = {}
    most_recent_model_id = ""
    most_recent_ts: dt.datetime | None = None

    for path in base_dir.glob("*.enc.json"):
        try:
            payload = shared.load_payload_from_path(path)
        except Exception:
            continue
        if str(payload.get("source") or "").strip() not in {"quick-ask", "quick_ask"}:
            continue

        model_id = str(payload.get("model_id") or "").strip()
        if not model_id:
            continue

        when = parse_iso_datetime(str(payload.get("saved_at") or payload.get("created_at") or ""))
        if when is None:
            when = now
        age_hours = max(0.0, (now - when).total_seconds() / 3600.0)
        # Heavily weight the last 72h so ranking reacts quickly.
        weight = 6.0 if age_hours <= 72 else 1.0
        scores[model_id] = scores.get(model_id, 0.0) + weight

        if most_recent_ts is None or when > most_recent_ts:
            most_recent_ts = when
            most_recent_model_id = model_id

    if most_recent_model_id:
        # Small tie-break bonus for the most recently used model.
        scores[most_recent_model_id] = scores.get(most_recent_model_id, 0.0) + 0.5
    return scores


def sort_models_by_usage(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not models:
        return models

    provider_rank = {"codex": 0, "claude": 1, "gemini": 2, "ollama": 3}
    usage = model_usage_scores()
    indexed = list(enumerate(models))
    indexed.sort(
        key=lambda item: (
            provider_rank.get(str(item[1].get("provider") or ""), 99),
            -usage.get(str(item[1].get("id") or ""), 0.0),
            item[0],
        )
    )
    return [model for _index, model in indexed]


def codex_app_server_available_model_ids(timeout_seconds: float = 3.0) -> set[str]:
    codex_path = command_path("codex")
    if not codex_path:
        return set()

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [codex_path, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=provider_runtime_env("codex"),
        )
        if proc.stdin is None or proc.stdout is None:
            return set()

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-02-01",
                "clientInfo": {"name": "quick-ask-model-probe", "version": "1.0"},
                "capabilities": {},
            },
        }
        model_list = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "model/list",
            "params": {"includeHidden": False},
        }
        proc.stdin.write(json.dumps(initialize) + "\n")
        proc.stdin.write(json.dumps(model_list) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.15)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != 2:
                continue
            result = payload.get("result")
            if not isinstance(result, dict):
                return set()
            records = result.get("data")
            if not isinstance(records, list):
                return set()
            ids: set[str] = set()
            for record in records:
                if not isinstance(record, dict):
                    continue
                model_id = str(record.get("model") or record.get("id") or "").strip()
                if model_id:
                    ids.add(model_id)
            return ids
    except Exception:
        return set()
    finally:
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=0.5)

    return set()


def codex_models_for_system() -> list[dict[str, Any]]:
    available = codex_app_server_available_model_ids()
    if not available:
        return []

    filtered: list[dict[str, Any]] = []
    for option in CODEX_MODELS:
        model_name = str(option.get("model") or "").strip()
        if model_name and model_name not in available:
            continue
        filtered.append(dict(option))

    return filtered


def command_path(name: str) -> str | None:
    direct = shutil.which(name)
    if direct:
        return direct

    common_paths = {
        "claude": [
            pathlib.Path.home() / ".local/bin/claude",
            pathlib.Path("/opt/homebrew/bin/claude"),
            pathlib.Path("/usr/local/bin/claude"),
        ],
        "codex": [
            pathlib.Path("/opt/homebrew/bin/codex"),
            pathlib.Path.home() / ".local/bin/codex",
            pathlib.Path("/usr/local/bin/codex"),
        ],
        "gemini": [
            pathlib.Path.home() / ".nvm/versions/node/v24.14.0/bin/gemini",
            pathlib.Path.home() / ".local/bin/gemini",
            pathlib.Path("/opt/homebrew/bin/gemini"),
            pathlib.Path("/usr/local/bin/gemini"),
        ],
        "ollama": [
            pathlib.Path("/opt/homebrew/bin/ollama"),
            pathlib.Path("/usr/local/bin/ollama"),
        ],
    }
    for candidate in common_paths.get(name, []):
        if candidate.exists():
            return str(candidate)

    try:
        result = subprocess.run(
            ["/bin/zsh", "-lc", f"command -v {shlex.quote(name)}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        candidate = result.stdout.strip()
        if candidate and pathlib.Path(candidate).exists():
            return candidate
    except Exception:
        pass
    return None


def run_subprocess(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


@functools.lru_cache(maxsize=1)
def login_shell_path_entries() -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["/bin/zsh", "-lc", "printf %s \"$PATH\""],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except Exception:
        return ()
    return tuple(entry for entry in result.stdout.strip().split(":") if entry)


def merged_path_entries(*command_names: str) -> list[str]:
    seen: set[str] = set()
    entries: list[str] = []

    def add(entry: str | pathlib.Path | None) -> None:
        if entry is None:
            return
        text = str(entry).strip()
        if not text or text in seen:
            return
        seen.add(text)
        entries.append(text)

    for entry in os.environ.get("PATH", "").split(":"):
        add(entry)
    for entry in login_shell_path_entries():
        add(entry)

    for name in command_names:
        resolved = command_path(name)
        if resolved:
            add(pathlib.Path(resolved).parent)

    node_path = command_path("node")
    if node_path:
        add(pathlib.Path(node_path).parent)

    common_dirs = [
        pathlib.Path.home() / ".local/bin",
        pathlib.Path("/opt/homebrew/bin"),
        pathlib.Path("/usr/local/bin"),
        pathlib.Path("/usr/bin"),
        pathlib.Path("/bin"),
        pathlib.Path("/usr/sbin"),
        pathlib.Path("/sbin"),
    ]
    for directory in common_dirs:
        if directory.exists():
            add(directory)

    return entries


def provider_runtime_env(*command_names: str) -> dict[str, str]:
    env = subscription_only_env()
    env["PATH"] = ":".join(merged_path_entries(*command_names))
    return env


def last_json_line(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return payload

    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def provider_statuses() -> list[dict[str, Any]]:
    return [
        claude_provider_status(),
        codex_provider_status(),
        gemini_provider_status(),
        ollama_provider_status(),
    ]


def claude_provider_status() -> dict[str, Any]:
    claude = command_path("claude") or str(pathlib.Path.home() / ".local/bin/claude")
    if not pathlib.Path(claude).exists():
        return {
            "id": "claude",
            "label": "Claude",
            "available": False,
            "logged_in": False,
            "detail": "Claude CLI is not installed.",
            "setup_command": "claude auth login --claudeai",
        }

    try:
        result = run_subprocess(
            [claude, "auth", "status", "--json"],
            env=provider_runtime_env("claude"),
            timeout=10.0,
        )
    except Exception as exc:
        return {
            "id": "claude",
            "label": "Claude",
            "available": True,
            "logged_in": False,
            "detail": f"Could not read Claude login status: {exc}",
            "setup_command": "claude auth login --claudeai",
        }

    payload = last_json_line(result.stdout)
    logged_in = bool(payload and payload.get("loggedIn"))
    detail = "Ready via Claude CLI login." if logged_in else "Claude CLI is installed but not logged in."
    return {
        "id": "claude",
        "label": "Claude",
        "available": True,
        "logged_in": logged_in,
        "detail": detail,
        "setup_command": "claude auth login --claudeai",
    }


def codex_provider_status() -> dict[str, Any]:
    codex = command_path("codex")
    if not codex:
        return {
            "id": "codex",
            "label": "ChatGPT",
            "available": False,
            "logged_in": False,
            "detail": "Codex CLI is not installed.",
            "setup_command": "codex login --device-auth",
        }

    try:
        result = run_subprocess([codex, "login", "status"], env=provider_runtime_env("codex"), timeout=10.0)
    except Exception as exc:
        return {
            "id": "codex",
            "label": "ChatGPT",
            "available": True,
            "logged_in": False,
            "detail": f"Could not read Codex login status: {exc}",
            "setup_command": "codex login --device-auth",
        }

    combined = "\n".join(part for part in [result.stdout, result.stderr] if part).lower()
    logged_in = result.returncode == 0 and "logged in" in combined
    detail = "Ready via Codex CLI login." if logged_in else "Codex CLI is installed but not logged in."
    return {
        "id": "codex",
        "label": "ChatGPT",
        "available": True,
        "logged_in": logged_in,
        "detail": detail,
        "setup_command": "codex login --device-auth",
    }


def gemini_credentials_path() -> pathlib.Path:
    return pathlib.Path.home() / ".gemini" / "oauth_creds.json"


def gemini_provider_status() -> dict[str, Any]:
    gemini = command_path("gemini")
    if not gemini:
        return {
            "id": "gemini",
            "label": "Gemini",
            "available": False,
            "logged_in": False,
            "detail": "Gemini CLI is not installed.",
            "setup_command": "gemini",
        }

    creds_path = gemini_credentials_path()
    logged_in = creds_path.exists() and creds_path.stat().st_size > 0
    detail = (
        "Ready via Gemini CLI cached credentials."
        if logged_in
        else "Gemini CLI is installed but no cached CLI login was detected."
    )
    return {
        "id": "gemini",
        "label": "Gemini",
        "available": True,
        "logged_in": logged_in,
        "detail": detail,
        "setup_command": "gemini",
    }


def ollama_provider_status() -> dict[str, Any]:
    ollama = command_path("ollama")
    if not ollama:
        return {
            "id": "ollama",
            "label": "Ollama",
            "available": False,
            "logged_in": False,
            "detail": "Ollama is not installed.",
            "setup_command": None,
        }

    try:
        endpoint = shared.resolve_ollama_endpoint(ensure_local=True, prefer_env=False)
        available = shared.endpoint_is_available(endpoint["base_url"])
        detail = endpoint.get("label", endpoint["base_url"]) if available else "Ollama is installed but unavailable."
    except Exception as exc:
        available = False
        detail = f"Ollama is installed but unavailable: {exc}"

    return {
        "id": "ollama",
        "label": "Ollama",
        "available": available,
        "logged_in": available,
        "detail": detail,
        "setup_command": None,
    }


def message_attachments(message: HistoryMessage) -> list[dict[str, str]]:
    raw = message.get("attachments")
    if not isinstance(raw, list):
        return []

    cleaned: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "image").strip() or "image"
        mime_type = str(item.get("mimeType") or item.get("mime_type") or "image/png").strip() or "image/png"
        data_base64 = str(item.get("dataBase64") or item.get("data_base64") or "").strip()
        if not data_base64:
            continue
        cleaned.append(
            {
                "filename": filename,
                "mime_type": mime_type,
                "data_base64": data_base64,
            }
        )
    return cleaned


def history_contains_attachments(history: list[HistoryMessage]) -> bool:
    return any(message_attachments(message) for message in history)


def attachment_count_label(count: int) -> str:
    return "1 image" if count == 1 else f"{count} images"


def attachment_prompt_text(attachments: list[dict[str, str]], start_index: int) -> tuple[str, int]:
    labels: list[str] = []
    next_index = start_index
    for attachment in attachments:
        filename = attachment.get("filename") or f"image-{next_index}"
        labels.append(f"image #{next_index} ({filename})")
        next_index += 1
    if not labels:
        return "", start_index
    if len(labels) == 1:
        return f"Attached {labels[0]}.", next_index
    return f"Attached images: {', '.join(labels)}.", next_index


def build_prompt(
    history: list[HistoryMessage],
    attachment_reference_groups: list[list[str]] | None = None,
) -> str:
    if not history:
        return "The user has not said anything yet."

    lines: list[str] = [
        "Continue this conversation naturally.",
        "",
        "Conversation:",
    ]
    image_index = 1
    for index, message in enumerate(history):
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        attachments = message_attachments(message)
        if role not in {"user", "assistant"} or (not content and not attachments):
            continue
        prefix = "User" if role == "user" else "Assistant"
        if attachments:
            attachment_text, image_index = attachment_prompt_text(attachments, image_index)
            if attachment_text:
                lines.append(f"{prefix}: {attachment_text}")
            if attachment_reference_groups is not None and index < len(attachment_reference_groups):
                for reference in attachment_reference_groups[index]:
                    lines.append(f"{prefix}: Local image path: {reference}")
        if content:
            lines.append(f"{prefix}: {content}")
    lines.extend(["", "Assistant:"])
    return "\n".join(lines)


def build_remote_cli_prompt(
    history: list[HistoryMessage],
    attachment_reference_groups: list[list[str]] | None = None,
) -> str:
    return (
        f"System:\n{QUICK_SYSTEM_PROMPT}\n\n"
        f"{build_prompt(history, attachment_reference_groups=attachment_reference_groups)}"
    )


def build_gemini_prompt(history: list[HistoryMessage], attachment_reference_groups: list[list[str]] | None = None) -> str:
    if not history:
        return f"System:\n{QUICK_SYSTEM_PROMPT}\n\nThe user has not said anything yet."

    lines: list[str] = [
        f"System:\n{QUICK_SYSTEM_PROMPT}",
        "",
        "Continue this conversation naturally.",
        "",
        "Conversation:",
    ]
    image_index = 1
    for index, message in enumerate(history):
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        attachments = message_attachments(message)
        if role not in {"user", "assistant"} or (not content and not attachments):
            continue
        prefix = "User" if role == "user" else "Assistant"
        if attachments:
            attachment_text, next_index = attachment_prompt_text(attachments, image_index)
            if attachment_text:
                lines.append(f"{prefix}: {attachment_text}")
            if attachment_reference_groups is not None and index < len(attachment_reference_groups):
                for reference in attachment_reference_groups[index]:
                    lines.append(reference)
            image_index = next_index
        if content:
            lines.append(f"{prefix}: {content}")
    lines.extend(["", "Assistant:"])
    return "\n".join(lines)


def compact_preview(text: str, limit: int = 140) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def history_disabled() -> bool:
    return os.environ.get("QUICK_ASK_DISABLE_HISTORY", "").strip() == "1"


def session_preview(messages: list[HistoryMessage]) -> str:
    for message in reversed(messages):
        role = str(message.get("role") or "").strip().lower()
        if role == "system":
            continue
        content = str(message.get("content") or "").strip()
        if content:
            return compact_preview(content)
        attachments = message_attachments(message)
        if attachments:
            return attachment_count_label(len(attachments))
    return ""


def subscription_only_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in BLOCKED_PROVIDER_ENV_KEYS:
        env.pop(key, None)
    return env


def materialize_attachment_file_groups(history: list[HistoryMessage], target_dir: pathlib.Path) -> list[list[pathlib.Path]]:
    groups: list[list[pathlib.Path]] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    counter = 1
    for message in history:
        paths: list[pathlib.Path] = []
        for attachment in message_attachments(message):
            try:
                data = base64.b64decode(attachment["data_base64"], validate=True)
            except Exception:
                continue
            if not data:
                continue
            stem = safe_attachment_stem(attachment.get("filename") or "", f"image-{counter}")
            suffix = attachment_file_suffix(attachment)
            path = target_dir / f"{counter:03d}-{stem}{suffix}"
            path.write_bytes(data)
            paths.append(path)
            counter += 1
        groups.append(paths)
    return groups


def claude_shell_invocation(
    model: str,
    history: list[HistoryMessage],
    attachment_dir: pathlib.Path | None = None,
) -> tuple[list[str], pathlib.Path]:
    attachment_reference_groups: list[list[str]] | None = None
    allow_read = history_contains_attachments(history) and attachment_dir is not None
    if allow_read:
        attachment_reference_groups = [
            [str(path) for path in group]
            for group in materialize_attachment_file_groups(history, attachment_dir)
        ]
    prompt = build_prompt(history, attachment_reference_groups=attachment_reference_groups)
    claude_path = pathlib.Path(command_path("claude") or pathlib.Path.home() / ".local/bin/claude")
    SAFE_CWD.mkdir(parents=True, exist_ok=True)
    argv = [
        str(claude_path),
        "-p",
        prompt,
        "--model",
        model,
        "--effort",
        "low",
        "--no-session-persistence",
        "--permission-mode",
        "default" if allow_read else "dontAsk",
        "--no-chrome",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--disable-slash-commands",
        "--setting-sources",
        "user",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--strict-mcp-config",
        "--system-prompt",
        QUICK_SYSTEM_PROMPT,
    ]
    if allow_read:
        argv.extend(["--allowedTools", "Read"])
    else:
        argv.extend(["--tools", ""])
    return argv, SAFE_CWD


def stream_claude(model: str, history: list[HistoryMessage]) -> int:
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    proc: subprocess.Popen[str] | None = None
    SAFE_CWD.mkdir(parents=True, exist_ok=True)
    try:
        attachment_dir = None
        if history_contains_attachments(history):
            temp_dir = tempfile.TemporaryDirectory(prefix="quick-ask-claude-images-", dir=SAFE_CWD)
            attachment_dir = pathlib.Path(temp_dir.name)

        command, safe_cwd = claude_shell_invocation(model, history, attachment_dir=attachment_dir)
        proc = subprocess.Popen(
            command,
            cwd=str(safe_cwd),
            env=provider_runtime_env("claude"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        assert proc.stdout is not None
        streamed_any = False
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            if payload.get("type") == "stream_event":
                event = payload.get("event") or {}
                delta = event.get("delta") or {}
                text = delta.get("text")
                if isinstance(text, str) and text:
                    emit({"type": "chunk", "text": text})
                    streamed_any = True
                continue

            if payload.get("type") == "assistant":
                continue

            if payload.get("type") == "result":
                if payload.get("is_error"):
                    emit({"type": "error", "message": str(payload.get("result") or "Claude request failed.")})
                    return 1
                if not streamed_any:
                    result_text = str(payload.get("result") or "")
                    if result_text:
                        emit({"type": "chunk", "text": result_text})
                emit({"type": "done"})
                return 0
    except KeyboardInterrupt:
        if proc is not None:
            proc.kill()
        raise
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    stderr = ""
    if proc is not None and proc.stderr is not None:
        stderr = proc.stderr.read().strip()
    if proc is None:
        emit({"type": "error", "message": "Claude could not start."})
        return 1
    proc.wait()
    if proc.returncode != 0:
        emit({"type": "error", "message": stderr or f"Claude exited with status {proc.returncode}."})
        return proc.returncode
    emit({"type": "done"})
    return 0


def codex_app_server_runtime(model_id: str) -> str:
    option = codex_model_option(model_id)
    runtime = str(option.get("runtime") or "").strip() if option else ""
    return runtime or "cli"


def latest_user_turn(history: list[HistoryMessage]) -> HistoryMessage | None:
    for message in reversed(history):
        if str(message.get("role") or "").strip().lower() == "user":
            return message
    return None


def write_codex_app_server_status(payload: dict[str, Any]) -> None:
    CODEX_APP_SERVER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_APP_SERVER_STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def clear_codex_app_server_status() -> None:
    with contextlib.suppress(FileNotFoundError):
        CODEX_APP_SERVER_STATUS_PATH.unlink()


def codex_jsonrpc_request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }


def codex_send_jsonrpc(stdin: Any, payload: dict[str, Any]) -> None:
    stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
    stdin.flush()


def codex_app_server_start(
    model: str,
    session_id: str,
    cwd: pathlib.Path,
) -> subprocess.Popen[str]:
    codex_path = command_path("codex")
    if not codex_path:
        raise RuntimeError("Codex CLI is not installed.")

    process = subprocess.Popen(
        [codex_path, "app-server", "--listen", "stdio://"],
        cwd=str(cwd),
        env=provider_runtime_env("codex"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    write_codex_app_server_status(
        {
            "pid": process.pid,
            "model": model,
            "session_id": session_id,
            "cwd": str(cwd),
            "status": "starting",
        }
    )
    return process


def codex_extract_turn_error(notification: dict[str, Any]) -> str | None:
    params = notification.get("params")
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if not isinstance(turn, dict):
        return None
    error = turn.get("error")
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "").strip()
    return message or None


def codex_thread_id_from_result(response: dict[str, Any]) -> str | None:
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return None
    thread_id = str(thread.get("id") or "").strip()
    return thread_id or None


def codex_try_resume_thread(
    stdin: Any,
    request_id: int,
    thread_id: str,
) -> int:
    codex_send_jsonrpc(
        stdin,
        codex_jsonrpc_request(
            request_id,
            "thread/resume",
            {
                "threadId": thread_id,
            },
        ),
    )
    return request_id + 1


def codex_start_thread(
    stdin: Any,
    request_id: int,
    model: str,
    cwd: pathlib.Path,
    scope_mode: str,
) -> int:
    sandbox_mode = "danger-full-access" if scope_mode == "full_access" else "workspace-write"
    codex_send_jsonrpc(
        stdin,
        codex_jsonrpc_request(
            request_id,
            "thread/start",
            {
                "approvalPolicy": "never",
                "sandbox": sandbox_mode,
                "cwd": str(cwd),
                "model": model,
            },
        ),
    )
    return request_id + 1


def codex_start_turn(
    stdin: Any,
    request_id: int,
    thread_id: str,
    model: str,
    user_input: list[dict[str, str]],
    cwd: pathlib.Path,
    effort: str | None,
    scope_mode: str,
) -> int:
    sandbox_policy: dict[str, Any]
    if scope_mode == "restricted":
        sandbox_policy = {
            "type": "workspaceWrite",
            "writableRoots": [str(cwd)],
            "networkAccess": True,
        }
    else:
        sandbox_policy = {"type": "dangerFullAccess"}

    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": user_input,
        "model": model,
        "cwd": str(cwd),
        "approvalPolicy": "never",
        "sandboxPolicy": sandbox_policy,
    }
    if effort:
        params["effort"] = effort
    codex_send_jsonrpc(
        stdin,
        codex_jsonrpc_request(
            request_id,
            "turn/start",
            params,
        ),
    )
    return request_id + 1


def codex_build_turn_input(
    history: list[HistoryMessage],
    attachment_dir: pathlib.Path | None = None,
) -> list[dict[str, str]]:
    latest_user = latest_user_turn(history)
    if latest_user is None:
        return [{"type": "text", "text": "Continue."}]

    content = str(latest_user.get("content") or "").strip()
    payload: list[dict[str, str]] = []
    if content:
        payload.append({"type": "text", "text": content})

    if attachment_dir is not None:
        groups = materialize_attachment_file_groups([latest_user], attachment_dir)
        for group in groups:
            for path in group:
                payload.append({"type": "localImage", "path": str(path)})

    if payload:
        return payload

    # Fallback for attachment-only turns when files could not be materialized.
    return [{"type": "text", "text": "The user sent an image. Please analyze it."}]


def stream_codex_app_server(model_id: str, history: list[HistoryMessage], context: dict[str, Any]) -> int:
    option = codex_model_option(model_id)
    if option is None:
        emit({"type": "error", "message": f"Unknown Codex model: {model_id}"})
        return 1

    model = str(option.get("model") or "").strip() or "gpt-5.3-codex"
    effort = str(option.get("effort") or "").strip() or None
    session_id = str(context.get("session_id") or "").strip()
    requested_thread_id = str(context.get("codex_thread_id") or "").strip()

    scope_mode, scoped_cwd = scope_from_context(context)
    cwd = scoped_cwd if scoped_cwd.exists() else (pathlib.Path.home() / "Downloads")
    if not cwd.exists():
        cwd = pathlib.Path.home()

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    proc: subprocess.Popen[str] | None = None
    try:
        attachment_dir = None
        if history_contains_attachments(history):
            temp_dir = tempfile.TemporaryDirectory(prefix="quick-ask-codex-app-server-images-", dir=SAFE_CWD)
            attachment_dir = pathlib.Path(temp_dir.name)

        proc = codex_app_server_start(model, session_id=session_id, cwd=cwd)
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("Codex app server could not start stdio transport.")

        request_id = 1
        pending: dict[int, str] = {}
        thread_id = requested_thread_id or ""
        current_turn_id = ""
        streamed_any = False
        saw_turn_terminal = False
        emitted_error = False
        initialized = False
        started_turn = False

        pending[request_id] = "initialize"
        codex_send_jsonrpc(
            proc.stdin,
            codex_jsonrpc_request(
                request_id,
                "initialize",
                {
                    "clientInfo": {
                        "name": "quick-ask",
                        "version": "1.0",
                    },
                    "capabilities": {},
                },
            ),
        )
        request_id += 1

        if thread_id:
            pending[request_id] = "thread/resume"
            request_id = codex_try_resume_thread(proc.stdin, request_id, thread_id)
        else:
            pending[request_id] = "thread/start"
            request_id = codex_start_thread(proc.stdin, request_id, model, cwd, scope_mode)

        while True:
            raw_line = proc.stdout.readline()
            if not raw_line:
                break
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "id" in payload:
                response_id = payload.get("id")
                if not isinstance(response_id, int):
                    continue
                response_kind = pending.pop(response_id, "")
                if payload.get("error"):
                    if response_kind == "thread/resume":
                        thread_id = ""
                        pending[request_id] = "thread/start"
                        request_id = codex_start_thread(proc.stdin, request_id, model, cwd, scope_mode)
                        continue
                    message = str((payload.get("error") or {}).get("message") or "Codex app server request failed.").strip()
                    emit({"type": "error", "message": message})
                    emitted_error = True
                    break

                if response_kind == "initialize":
                    initialized = True
                    continue
                if response_kind in {"thread/start", "thread/resume"}:
                    maybe_thread_id = codex_thread_id_from_result(payload)
                    if maybe_thread_id:
                        thread_id = maybe_thread_id
                        emit({"type": "meta", "codex_thread_id": thread_id, "codex_pid": proc.pid})
                        write_codex_app_server_status(
                            {
                                "pid": proc.pid,
                                "model": model,
                                "session_id": session_id,
                                "thread_id": thread_id,
                                "cwd": str(cwd),
                                "status": "running",
                            }
                        )
                    if initialized and thread_id and not started_turn:
                        pending[request_id] = "turn/start"
                        request_id = codex_start_turn(
                            proc.stdin,
                            request_id,
                            thread_id,
                            model,
                            codex_build_turn_input(history, attachment_dir=attachment_dir),
                            cwd,
                            effort,
                            scope_mode,
                        )
                        started_turn = True
                    continue
                if response_kind == "turn/start":
                    result = payload.get("result")
                    if isinstance(result, dict):
                        turn = result.get("turn")
                        if isinstance(turn, dict):
                            current_turn_id = str(turn.get("id") or "").strip()
                    continue
                continue

            method = str(payload.get("method") or "").strip()
            params = payload.get("params")
            if not isinstance(params, dict):
                continue

            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    emit({"type": "chunk", "text": delta})
                    streamed_any = True
                continue

            if method == "error":
                details = params.get("error")
                if isinstance(details, dict):
                    message = str(details.get("message") or "").strip()
                else:
                    message = str(params.get("message") or "").strip()
                if message:
                    emit({"type": "error", "message": message})
                    emitted_error = True
                continue

            if method == "turn/completed":
                completed_turn = params.get("turn")
                if isinstance(completed_turn, dict):
                    status = str(completed_turn.get("status") or "").strip()
                    if status == "failed":
                        if not emitted_error:
                            message = codex_extract_turn_error(payload) or "Codex app server turn failed."
                            emit({"type": "error", "message": message})
                            emitted_error = True
                    else:
                        emit({"type": "done"})
                else:
                    emit({"type": "done"})
                saw_turn_terminal = True
                break

        if not saw_turn_terminal and not emitted_error:
            if proc.poll() is not None and proc.returncode not in {None, 0}:
                stderr = proc.stderr.read().strip() if proc.stderr is not None else ""
                emit({"type": "error", "message": stderr or f"Codex app server exited with status {proc.returncode}."})
                return int(proc.returncode or 1)
            if not streamed_any:
                emit({"type": "error", "message": "Codex app server did not return a reply."})
                return 1
            emit({"type": "done"})
            return 0

        return 1 if emitted_error else 0
    except KeyboardInterrupt:
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.kill()
        raise
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        return 1
    finally:
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2.0)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
        clear_codex_app_server_status()
        if temp_dir is not None:
            temp_dir.cleanup()


def codex_model_option(model_id: str) -> dict[str, Any] | None:
    return next((option for option in CODEX_MODELS if str(option.get("id")) == model_id), None)


def scope_from_context(context: dict[str, Any]) -> tuple[str, pathlib.Path]:
    requested_mode = str(context.get("scope_mode") or "").strip().lower()
    requested_path = str(context.get("scope_path") or "").strip()

    downloads = pathlib.Path.home() / "Downloads"
    fallback = downloads if downloads.exists() else pathlib.Path.home()

    if requested_mode == "restricted":
        if requested_path:
            candidate = pathlib.Path(os.path.expanduser(requested_path)).resolve()
            if candidate.exists() and candidate.is_dir():
                return "restricted", candidate
        return "restricted", fallback

    return "full_access", fallback


def attachment_file_suffix(attachment: dict[str, str]) -> str:
    filename = attachment.get("filename") or ""
    suffix = pathlib.Path(filename).suffix
    if suffix:
        return suffix
    return IMAGE_MIME_EXTENSIONS.get(attachment.get("mime_type") or "", ".png")


def safe_attachment_stem(filename: str, fallback: str) -> str:
    stem = pathlib.Path(filename).stem.strip() or fallback
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-")
    return sanitized or fallback


def materialize_attachment_files(history: list[HistoryMessage], target_dir: pathlib.Path) -> list[pathlib.Path]:
    return [path for group in materialize_attachment_file_groups(history, target_dir) for path in group]


def codex_shell_invocation(
    model_id: str,
    history: list[HistoryMessage],
    scope_mode: str,
    scope_cwd: pathlib.Path,
    attachment_dir: pathlib.Path | None = None,
) -> tuple[list[str], pathlib.Path]:
    prompt = build_remote_cli_prompt(history)
    codex_path = command_path("codex")
    if not codex_path:
        raise RuntimeError("Codex CLI is not installed.")
    option = codex_model_option(model_id)
    model = str(option.get("model")) if option is not None else model_id
    effort = str(option.get("effort")) if option is not None and option.get("effort") else ""
    scope_cwd.mkdir(parents=True, exist_ok=True)
    sandbox_mode = "danger-full-access" if scope_mode == "full_access" else "workspace-write"
    argv = [
        codex_path,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(scope_cwd),
        "-s",
        sandbox_mode,
        "-m",
        model,
        prompt,
    ]
    if effort:
        argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
    if attachment_dir is not None:
        for path in materialize_attachment_files(history, attachment_dir):
            argv.extend(["-i", str(path)])
    return argv, scope_cwd


def stream_codex(model_id: str, history: list[HistoryMessage], context: dict[str, Any]) -> int:
    if codex_app_server_runtime(model_id) == "app_server":
        return stream_codex_app_server(model_id, history, context)

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    proc: subprocess.Popen[str] | None = None
    scope_mode, scope_cwd = scope_from_context(context)
    SAFE_CWD.mkdir(parents=True, exist_ok=True)
    try:
        attachment_dir = None
        if history_contains_attachments(history):
            temp_dir = tempfile.TemporaryDirectory(prefix="quick-ask-codex-images-", dir=SAFE_CWD)
            attachment_dir = pathlib.Path(temp_dir.name)

        command, safe_cwd = codex_shell_invocation(
            model_id,
            history,
            scope_mode=scope_mode,
            scope_cwd=scope_cwd,
            attachment_dir=attachment_dir,
        )
        proc = subprocess.Popen(
            command,
            cwd=str(safe_cwd),
            env=provider_runtime_env("codex"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        assert proc.stdout is not None
        streamed_any = False
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            item = payload.get("item") if payload.get("type") == "item.completed" else None
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
                if text:
                    emit({"type": "chunk", "text": text})
                    streamed_any = True
                continue

            if payload.get("type") == "turn.completed":
                emit({"type": "done"})
                return 0
    except KeyboardInterrupt:
        if proc is not None:
            proc.kill()
        raise
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    stderr = ""
    if proc is not None and proc.stderr is not None:
        stderr = proc.stderr.read().strip()
    if proc is None:
        emit({"type": "error", "message": "Codex could not start."})
        return 1
    proc.wait()
    if proc.returncode != 0:
        emit({"type": "error", "message": stderr or f"Codex exited with status {proc.returncode}."})
        return proc.returncode
    if not streamed_any:
        emit({"type": "error", "message": "Codex did not return a reply."})
        return 1
    emit({"type": "done"})
    return 0


def gemini_shell_invocation(
    model: str,
    history: list[HistoryMessage],
    scope_mode: str,
    scope_cwd: pathlib.Path,
    attachment_dir: pathlib.Path | None = None,
) -> tuple[list[str], pathlib.Path]:
    gemini_cwd = scope_cwd if scope_cwd.exists() else DEFAULT_GEMINI_CWD
    if not gemini_cwd.exists():
        gemini_cwd = pathlib.Path.home()

    attachment_reference_groups: list[list[str]] | None = None
    if attachment_dir is not None and history_contains_attachments(history):
        attachment_reference_groups = []
        for group in materialize_attachment_file_groups(history, attachment_dir):
            references: list[str] = []
            for path in group:
                try:
                    relative = path.relative_to(gemini_cwd)
                except ValueError:
                    relative = path.resolve()
                references.append(f"@{relative}")
            attachment_reference_groups.append(references)
    prompt = build_gemini_prompt(history, attachment_reference_groups=attachment_reference_groups)
    gemini_path = command_path("gemini")
    if not gemini_path:
        raise RuntimeError("Gemini CLI is not installed.")
    SAFE_CWD.mkdir(parents=True, exist_ok=True)
    argv = [
        gemini_path,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--approval-mode",
        "yolo",
        "--include-directories",
        str(pathlib.Path.home() if scope_mode == "full_access" else gemini_cwd),
    ]
    if model:
        argv.extend(["--model", model])
    return argv, gemini_cwd


def stream_gemini(model: str, history: list[HistoryMessage], context: dict[str, Any]) -> int:
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    proc: subprocess.Popen[str] | None = None
    scope_mode, scope_cwd = scope_from_context(context)
    SAFE_CWD.mkdir(parents=True, exist_ok=True)
    try:
        attachment_dir = None
        if history_contains_attachments(history):
            temp_dir = tempfile.TemporaryDirectory(prefix="quick-ask-gemini-images-", dir=SAFE_CWD)
            attachment_dir = pathlib.Path(temp_dir.name)

        command, safe_cwd = gemini_shell_invocation(
            model,
            history,
            scope_mode=scope_mode,
            scope_cwd=scope_cwd,
            attachment_dir=attachment_dir,
        )
        proc = subprocess.Popen(
            command,
            cwd=str(safe_cwd),
            env=provider_runtime_env("gemini", "node"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        assert proc.stdout is not None
        streamed_any = False
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            if payload.get("type") == "message" and payload.get("role") == "assistant":
                text = str(payload.get("content") or "")
                if text:
                    emit({"type": "chunk", "text": text})
                    streamed_any = True
                continue

            if payload.get("type") == "result":
                if payload.get("status") != "success":
                    error_payload = payload.get("error")
                    error_message = None
                    if isinstance(error_payload, dict):
                        error_message = error_payload.get("message")
                    if not isinstance(error_message, str) or not error_message.strip():
                        error_message = "Gemini request failed."
                    normalized = error_message.lower()
                    if "mime_type" in normalized and "audio/mpeg" in normalized and "function_response.parts" in normalized:
                        error_message = (
                            "Gemini could not process tool output for this audio/video task in this mode. "
                            "Try Codex 5.3 for local transcription, or convert the file to text first."
                        )
                    emit({"type": "error", "message": error_message})
                    return 1
                emit({"type": "done"})
                return 0
    except KeyboardInterrupt:
        if proc is not None:
            proc.kill()
        raise
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    stderr = ""
    if proc is not None and proc.stderr is not None:
        stderr = proc.stderr.read().strip()
    if proc is None:
        emit({"type": "error", "message": "Gemini could not start."})
        return 1
    proc.wait()
    if proc.returncode != 0:
        emit({"type": "error", "message": stderr or f"Gemini exited with status {proc.returncode}."})
        return proc.returncode
    if not streamed_any:
        emit({"type": "error", "message": "Gemini did not return a reply."})
        return 1
    emit({"type": "done"})
    return 0


def ollama_messages_from_history(history: list[HistoryMessage]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": QUICK_SYSTEM_PROMPT}]
    for message in history:
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        attachments = [attachment["data_base64"] for attachment in message_attachments(message)]
        if role not in {"user", "assistant"} or (not content and not attachments):
            continue
        payload: dict[str, Any] = {"role": role, "content": content}
        if attachments and role == "user":
            payload["images"] = attachments
        messages.append(payload)
    return messages


def open_ollama_connection(base_url: str) -> http.client.HTTPConnection:
    conn, _scheme = shared.connection_for_base_url(base_url, timeout=600)
    return conn


def stream_ollama_once(endpoint: dict[str, str], model: str, history: list[HistoryMessage]) -> tuple[int, bool]:
    body = json.dumps(
        {
            "model": model,
            "messages": ollama_messages_from_history(history),
            "think": False,
            "stream": True,
            "options": {
                "temperature": 0.7,
                "num_ctx": 8192,
            },
            "keep_alive": "30m",
        }
    )

    conn = open_ollama_connection(endpoint["base_url"])
    streamed_any = False
    try:
        conn.request(
            "POST",
            "/api/chat",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        if response.status != 200:
            text = response.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama error {response.status}: {text}")

        while True:
            line = response.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            text = str((payload.get("message") or {}).get("content") or "")
            if text:
                emit({"type": "chunk", "text": text})
                streamed_any = True
            if payload.get("done"):
                emit({"type": "done"})
                return 0, streamed_any
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    emit({"type": "done"})
    return 0, streamed_any


def stream_ollama(model: str, history: list[HistoryMessage]) -> int:
    endpoint = shared.resolve_ollama_endpoint(ensure_local=True, prefer_env=False)
    try:
        code, streamed_any = stream_ollama_once(endpoint, model, history)
        return code
    except Exception as exc:
        if endpoint.get("kind") == "remote":
            fallback = shared.resolve_ollama_endpoint(ensure_local=True, prefer_env=False)
            if fallback.get("kind") != endpoint.get("kind"):
                try:
                    code, streamed_any = stream_ollama_once(fallback, model, history)
                    return code
                except Exception as fallback_exc:
                    emit({"type": "error", "message": str(fallback_exc)})
                    return 1
        emit({"type": "error", "message": str(exc)})
        return 1


def read_chat_request_from_stdin() -> tuple[list[HistoryMessage], dict[str, Any]]:
    raw = sys.stdin.read().strip()
    if not raw:
        return [], {}
    payload = json.loads(raw)
    context: dict[str, Any] = {}
    if isinstance(payload, dict):
        history = payload.get("history", payload)
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            context["session_id"] = session_id
        codex_thread_id = str(payload.get("codex_thread_id") or "").strip()
        if codex_thread_id:
            context["codex_thread_id"] = codex_thread_id
        scope_mode = str(payload.get("scope_mode") or "").strip()
        if scope_mode:
            context["scope_mode"] = scope_mode
        scope_path = str(payload.get("scope_path") or "").strip()
        if scope_path:
            context["scope_path"] = scope_path
    else:
        history = payload
    if not isinstance(history, list):
        raise RuntimeError("Expected a history array on stdin.")
    cleaned: list[HistoryMessage] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        attachments = message_attachments(item)
        if role not in {"user", "assistant"} or (not content and not attachments):
            continue
        payload: HistoryMessage = {"role": role, "content": content}
        if attachments:
            payload["attachments"] = attachments
        cleaned.append(payload)
    return cleaned, context


def read_history_from_stdin() -> list[HistoryMessage]:
    history, _context = read_chat_request_from_stdin()
    return history


def handle_models() -> int:
    emit(
        {
            "type": "models",
            "models": list_available_models(),
            "network_online": internet_reachable(),
        }
    )
    return 0


def handle_providers() -> int:
    emit({"type": "providers", "providers": provider_statuses()})
    return 0


def handle_history(limit: int) -> int:
    if history_disabled():
        emit({"type": "history", "sessions": []})
        return 0
    base_dir = shared.default_save_dir()
    sessions: list[dict[str, Any]] = []
    for path in sorted(base_dir.glob("*.enc.json"), reverse=True):
        try:
            payload = shared.load_payload_from_path(path)
        except Exception:
            continue

        if str(payload.get("source") or "").strip() not in {"quick-ask", "quick_ask"}:
            continue

        messages = payload.get("messages") or []
        if not isinstance(messages, list):
            messages = []

        cleaned_messages = [
            message
            for message in messages
            if isinstance(message, dict)
            and str(message.get("role") or "").strip().lower() in {"user", "assistant"}
        ]
        session_id = str(payload.get("session_id") or path.name.removesuffix(".enc.json") or "")
        endpoint = payload.get("endpoint")
        endpoint_label = str(endpoint.get("label") or "") if isinstance(endpoint, dict) else ""
        sessions.append(
            {
                "session_id": session_id,
                "created_at": str(payload.get("created_at") or ""),
                "saved_at": str(payload.get("saved_at") or ""),
                "model": str(payload.get("model") or ""),
                "model_id": str(payload.get("model_id") or ""),
                "endpoint_label": endpoint_label,
                "message_count": len(cleaned_messages),
                "preview": session_preview(cleaned_messages),
            }
        )

    sessions.sort(key=lambda item: (item.get("saved_at") or item.get("created_at") or "", item.get("session_id") or ""), reverse=True)
    emit({"type": "history", "sessions": sessions[: max(0, limit)]})
    return 0


def handle_load(session_id: str) -> int:
    if history_disabled():
        emit({"type": "error", "message": "History is disabled."})
        return 1
    base_dir = shared.default_save_dir()
    path = shared.resolve_session_path(base_dir, session_id)
    payload = shared.load_payload_from_path(path)
    emit({"type": "session", "session": payload})
    return 0


def handle_delete(session_id: str) -> int:
    if history_disabled():
        emit({"type": "error", "message": "History is disabled."})
        return 1
    base_dir = shared.default_save_dir()
    path = shared.delete_session(base_dir, session_id)
    emit({"type": "deleted", "session_id": session_id, "path": str(path)})
    return 0


def handle_chat(model_id: str) -> int:
    history, context = read_chat_request_from_stdin()
    if "::" not in model_id:
        emit({"type": "error", "message": f"Malformed model id: {model_id}"})
        return 1

    provider, model = model_id.split("::", 1)
    if provider == "claude":
        return stream_claude(model, history)
    if provider == "codex":
        return stream_codex(model_id, history, context)
    if provider == "gemini":
        return stream_gemini(model, history, context)
    if provider == "ollama":
        return stream_ollama(model, history)

    emit({"type": "error", "message": f"Unknown provider: {provider}"})
    return 1


def transcript_model_label(model_id: str) -> str:
    provider, model = model_id.split("::", 1)
    if provider == "claude":
        for option in CLAUDE_MODELS:
            if option["id"] == model_id:
                return str(option["label"])
        return model
    if provider == "codex":
        for option in CODEX_MODELS:
            if option["id"] == model_id:
                return str(option["label"])
        return model
    if provider == "gemini":
        for option in GEMINI_MODELS:
            if option["id"] == model_id:
                return str(option["label"])
        return model
    return friendly_ollama_name(model)


def transcript_endpoint(model_id: str) -> dict[str, str]:
    provider, model = model_id.split("::", 1)
    if provider == "claude":
        return {
            "kind": "remote",
            "label": "claude-cli-login",
            "base_url": "claude://login",
        }
    if provider == "codex":
        option = codex_model_option(model_id)
        if option and str(option.get("runtime") or "") == "app_server":
            return {
                "kind": "remote",
                "label": "codex-app-server",
                "base_url": "codex://app-server",
            }
        return {
            "kind": "remote",
            "label": "codex-cli-login",
            "base_url": "codex://login",
        }
    if provider == "gemini":
        return {
            "kind": "remote",
            "label": "gemini-cli-login",
            "base_url": "gemini://login",
        }

    try:
        endpoint = shared.resolve_ollama_endpoint(ensure_local=True, prefer_env=False)
    except Exception:
        return {
            "kind": "local",
            "label": "ollama",
            "base_url": "ollama://unknown",
        }
    return endpoint


def handle_save(session_id: str, created_at: str, model_id: str) -> int:
    if history_disabled():
        emit({"type": "saved", "path": ""})
        return 0
    history, context = read_chat_request_from_stdin()
    messages = [{"role": "system", "content": QUICK_SYSTEM_PROMPT}, *history]
    payload = {
        "session_id": session_id,
        "created_at": created_at,
        "model": transcript_model_label(model_id),
        "model_id": model_id,
        "num_ctx": 0,
        "endpoint": transcript_endpoint(model_id),
        "source": "quick-ask",
        "messages": messages,
    }
    codex_thread_id = str(context.get("codex_thread_id") or "").strip()
    if codex_thread_id and model_id.startswith("codex::"):
        payload["codex_thread_id"] = codex_thread_id
    scope_mode = str(context.get("scope_mode") or "").strip().lower()
    if scope_mode in {"full_access", "restricted"}:
        payload["scope_mode"] = scope_mode
    scope_path = str(context.get("scope_path") or "").strip()
    if scope_path:
        payload["scope_path"] = scope_path
    store = shared.SessionStore(shared.default_save_dir(), session_id=session_id)
    store.save(payload)
    emit({"type": "saved", "path": str(store.path)})
    return 0


def handle_storage(ensure_key: bool) -> int:
    try:
        if ensure_key:
            shared.get_or_create_master_key()
        else:
            existing = shared.find_master_key()
            if existing is None:
                emit(
                    {
                        "type": "storage",
                        "history_ready": False,
                        "keychain_ready": False,
                        "detail": "Transcript encryption key is not in Keychain yet.",
                        "path": str(shared.default_save_dir()),
                    }
                )
                return 0
        emit(
            {
                "type": "storage",
                "history_ready": True,
                "keychain_ready": True,
                "detail": f"Transcript encryption key is ready in Keychain under {shared.KEYCHAIN_SERVICE}.",
                "path": str(shared.default_save_dir()),
            }
        )
        return 0
    except Exception as exc:
        emit(
            {
                "type": "storage",
                "history_ready": False,
                "keychain_ready": False,
                "detail": f"Could not prepare encrypted history: {exc}",
                "path": str(shared.default_save_dir()),
            }
        )
        return 1


def main() -> int:
    args = parse_args()
    if args.command == "models":
        return handle_models()
    if args.command == "providers":
        return handle_providers()
    if args.command == "storage":
        return handle_storage(args.ensure_key)
    if args.command == "history":
        return handle_history(args.limit)
    if args.command == "load":
        return handle_load(args.session_id)
    if args.command == "delete":
        return handle_delete(args.session_id)
    if args.command == "chat":
        return handle_chat(args.model_id)
    if args.command == "save":
        return handle_save(args.session_id, args.created_at, args.model_id)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
