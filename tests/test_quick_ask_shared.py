#!/usr/bin/env python3
"""Shared helper regression tests for Quick Ask."""

from __future__ import annotations

import base64
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import quick_ask_shared as shared


class KeychainCommandTests(unittest.TestCase):
    def test_find_master_key_prefers_explicit_keychain_path(self) -> None:
        encoded = base64.b64encode(b"x" * 32).decode("ascii")
        with mock.patch.object(
            shared,
            "user_keychain_candidates",
            return_value=[Path("/Users/test/Library/Keychains/login.keychain-db")],
        ):
            with mock.patch.object(shared.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=encoded,
                    stderr="",
                )

                key = shared.find_master_key()

                self.assertEqual(key, b"x" * 32)
                command = run.call_args.args[0]
                self.assertEqual(command[-1], "/Users/test/Library/Keychains/login.keychain-db")

    def test_store_master_key_prefers_explicit_keychain_path(self) -> None:
        with mock.patch.object(
            shared,
            "user_keychain_candidates",
            return_value=[Path("/Users/test/Library/Keychains/login.keychain-db")],
        ):
            with mock.patch.object(shared.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="",
                    stderr="",
                )

                shared.store_master_key(b"x" * 32)

                command = run.call_args.args[0]
                self.assertEqual(command[0:2], ["security", "add-generic-password"])
                self.assertEqual(command[-1], "/Users/test/Library/Keychains/login.keychain-db")


if __name__ == "__main__":
    unittest.main(verbosity=2)
