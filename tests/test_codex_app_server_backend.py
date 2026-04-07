#!/usr/bin/env python3
"""Targeted tests for Codex app-server integration in Quick Ask backend."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import quick_ask_backend as backend


class CodexAppServerBackendTests(unittest.TestCase):
    def test_read_chat_request_parses_context_fields(self) -> None:
        payload = {
            "history": [{"role": "user", "content": "hello"}],
            "session_id": "session-123",
            "codex_thread_id": "thread-abc",
            "scope_mode": "restricted",
            "scope_path": "/tmp",
        }

        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            history, context = backend.read_chat_request_from_stdin()

        self.assertEqual(history, [{"role": "user", "content": "hello"}])
        self.assertEqual(context["session_id"], "session-123")
        self.assertEqual(context["codex_thread_id"], "thread-abc")
        self.assertEqual(context["scope_mode"], "restricted")
        self.assertEqual(context["scope_path"], "/tmp")

    def test_stream_codex_dispatches_to_app_server_runtime(self) -> None:
        history = [{"role": "user", "content": "ping"}]
        context = {"session_id": "session-123"}

        with mock.patch.object(backend, "codex_app_server_runtime", return_value="app_server"):
            with mock.patch.object(backend, "stream_codex_app_server", return_value=0) as stream_app_server:
                result = backend.stream_codex("codex::gpt-5.3-app-server", history, context)

        self.assertEqual(result, 0)
        stream_app_server.assert_called_once_with("codex::gpt-5.3-app-server", history, context)

    def test_handle_save_persists_codex_thread_id_for_codex_sessions(self) -> None:
        history = [{"role": "user", "content": "hello"}]
        context = {"codex_thread_id": "thread-abc"}

        fake_store = mock.Mock()
        fake_store.path = Path("/tmp/fake.enc.json")

        with mock.patch.object(backend, "history_disabled", return_value=False):
            with mock.patch.object(backend, "read_chat_request_from_stdin", return_value=(history, context)):
                with mock.patch.object(backend.shared, "SessionStore", return_value=fake_store):
                    with mock.patch.object(backend, "emit"):
                        result = backend.handle_save(
                            "session-123",
                            "2026-04-06T23:59:00Z",
                            "codex::gpt-5.3-app-server",
                        )

        self.assertEqual(result, 0)
        fake_store.save.assert_called_once()
        saved_payload = fake_store.save.call_args.args[0]
        self.assertEqual(saved_payload["codex_thread_id"], "thread-abc")

    def test_codex_start_turn_sets_danger_full_access_policy(self) -> None:
        fake_stdin = io.StringIO()
        with mock.patch.object(backend, "codex_send_jsonrpc") as send_jsonrpc:
            backend.codex_start_turn(
                fake_stdin,
                request_id=7,
                thread_id="thread-xyz",
                model="gpt-5.3-codex",
                user_input=[{"type": "text", "text": "hi"}],
                cwd=Path("/Users/nicholas/Downloads"),
                effort="medium",
                scope_mode="full_access",
            )

        send_jsonrpc.assert_called_once()
        payload = send_jsonrpc.call_args.args[1]
        self.assertEqual(payload["method"], "turn/start")
        params = payload["params"]
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandboxPolicy"], {"type": "dangerFullAccess"})

    def test_codex_start_turn_sets_workspace_write_policy_for_restricted_scope(self) -> None:
        fake_stdin = io.StringIO()
        with mock.patch.object(backend, "codex_send_jsonrpc") as send_jsonrpc:
            backend.codex_start_turn(
                fake_stdin,
                request_id=8,
                thread_id="thread-xyz",
                model="gpt-5.3-codex",
                user_input=[{"type": "text", "text": "hi"}],
                cwd=Path("/tmp"),
                effort="medium",
                scope_mode="restricted",
            )

        params = send_jsonrpc.call_args.args[1]["params"]
        self.assertEqual(
            params["sandboxPolicy"],
            {"type": "workspaceWrite", "writableRoots": ["/tmp"], "networkAccess": True},
        )

    def test_scope_from_context_uses_valid_restricted_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="quick-ask-scope-test-") as temp_dir:
            mode, cwd = backend.scope_from_context(
                {
                    "scope_mode": "restricted",
                    "scope_path": temp_dir,
                }
            )
        self.assertEqual(mode, "restricted")
        self.assertEqual(cwd, Path(temp_dir).resolve())

    def test_scope_from_context_falls_back_when_restricted_path_missing(self) -> None:
        with mock.patch.object(Path, "home", return_value=Path("/Users/example")):
            with mock.patch.object(Path, "exists", autospec=True) as exists:
                exists.side_effect = lambda path_obj: str(path_obj) == "/Users/example/Downloads"
                mode, cwd = backend.scope_from_context(
                    {
                        "scope_mode": "restricted",
                        "scope_path": "/does/not/exist",
                    }
                )
        self.assertEqual(mode, "restricted")
        self.assertEqual(cwd, Path("/Users/example/Downloads"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
