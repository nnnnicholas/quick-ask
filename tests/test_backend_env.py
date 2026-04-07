#!/usr/bin/env python3
"""Backend environment regression tests for Quick Ask."""

from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import datetime as dt

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import quick_ask_backend as backend


class ProviderRuntimeEnvTests(unittest.TestCase):
    def test_provider_runtime_env_includes_resolved_command_dirs(self) -> None:
        with mock.patch.object(backend, "subscription_only_env", return_value={}):
            with mock.patch.object(backend, "login_shell_path_entries", return_value=("/Users/test/.nvm/versions/node/v24/bin",)):
                with mock.patch.object(backend, "command_path") as command_path:
                    command_path.side_effect = lambda name: {
                        "gemini": "/opt/homebrew/bin/gemini",
                        "node": "/Users/test/.nvm/versions/node/v24/bin/node",
                    }.get(name)

                    env = backend.provider_runtime_env("gemini", "node")
                    path_entries = env["PATH"].split(":")

                    self.assertIn("/opt/homebrew/bin", path_entries)
                    self.assertIn("/Users/test/.nvm/versions/node/v24/bin", path_entries)

    def test_handle_models_reports_network_online_flag(self) -> None:
        with mock.patch.object(backend, "list_available_models", return_value=[]):
            with mock.patch.object(backend, "internet_reachable", return_value=False):
                with mock.patch.object(backend, "emit") as emit:
                    result = backend.handle_models()

        self.assertEqual(result, 0)
        emit.assert_called_once_with(
            {
                "type": "models",
                "models": [],
                "network_online": False,
            }
        )

    def test_model_usage_scores_heavily_weight_recent_72_hours(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        recent = (now - dt.timedelta(hours=4)).isoformat().replace("+00:00", "Z")
        old = (now - dt.timedelta(days=10)).isoformat().replace("+00:00", "Z")

        with tempfile.TemporaryDirectory(prefix="quick-ask-model-usage-") as temp_dir:
            directory = Path(temp_dir)
            first = directory / "first.enc.json"
            second = directory / "second.enc.json"
            first.write_text("{}")
            second.write_text("{}")

            payloads = {
                first: {
                    "source": "quick-ask",
                    "model_id": "codex::gpt-5.3-app-server",
                    "saved_at": recent,
                },
                second: {
                    "source": "quick-ask",
                    "model_id": "codex::gpt-5.3-app-server",
                    "saved_at": old,
                },
            }

            with mock.patch.object(backend, "history_disabled", return_value=False):
                with mock.patch.object(backend.shared, "default_save_dir", return_value=directory):
                    with mock.patch.object(
                        backend.shared,
                        "load_payload_from_path",
                        side_effect=lambda path: payloads[path],
                    ):
                        scores = backend.model_usage_scores()

        # 6.0 recent weight + 1.0 old weight + 0.5 most-recent bonus.
        self.assertAlmostEqual(scores["codex::gpt-5.3-app-server"], 7.5)

    def test_sort_models_by_usage_keeps_provider_priority_then_usage(self) -> None:
        models = [
            {"id": "claude::claude-opus-4-6", "provider": "claude"},
            {"id": "codex::gpt-5.4-instant", "provider": "codex"},
            {"id": "codex::gpt-5.3-app-server", "provider": "codex"},
            {"id": "gemini::gemini-3-flash-preview", "provider": "gemini"},
        ]
        usage = {
            "codex::gpt-5.3-app-server": 9.0,
            "codex::gpt-5.4-instant": 1.0,
            "gemini::gemini-3-flash-preview": 100.0,
        }
        with mock.patch.object(backend, "model_usage_scores", return_value=usage):
            sorted_models = backend.sort_models_by_usage(models)

        ids = [model["id"] for model in sorted_models]
        self.assertEqual(
            ids,
            [
                "codex::gpt-5.3-app-server",
                "codex::gpt-5.4-instant",
                "claude::claude-opus-4-6",
                "gemini::gemini-3-flash-preview",
            ],
        )

    def test_codex_models_for_system_filters_to_available_app_server_models(self) -> None:
        with mock.patch.object(backend, "codex_app_server_available_model_ids", return_value={"gpt-5.3-codex"}):
            filtered = backend.codex_models_for_system()
        ids = [item["id"] for item in filtered]
        self.assertEqual(ids, ["codex::gpt-5.3-app-server"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
