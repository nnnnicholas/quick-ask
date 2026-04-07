# Quick Ask

`quick-ask` is a native macOS floating chat app that uses local CLIs (Claude, Codex/ChatGPT, Gemini, Ollama) instead of API key onboarding.

It is built for fast, short interactions:

- always-on-top panel
- keyboard-first flow
- queue + steer while a turn is still running
- encrypted optional history
- model switching inside one thread

![Quick Ask screenshot](assets/quick-ask-sample.png)

## Current Product Behavior

- Global toggle: `Cmd+\`
- History window: `Cmd+Shift+\`
- Settings window: `Cmd+,`
- New chat: `Cmd+N`
- New additional panel: `Cmd+Shift+N`
- Steer current draft ahead of queue: `Cmd+Enter`
- Model cycle: `Cmd+[ / Cmd+]` or `Ctrl+Tab / Ctrl+Shift+Tab`
- Cancel active generation: `Esc`
- Close focused Quick Ask window: `Cmd+W`
- Paste image attachments into composer: `Cmd+V`

Chat behavior:

- Replies stream inline in the panel.
- If a reply is active, new sends are queued.
- Each queued prompt has per-item `Steer` and cancel actions.
- Hiding the panel does not stop in-flight generation.
- The app does not auto-clear conversation history on a timer.

History window behavior:

- First row is auto-selected.
- Navigation supports Down/Up arrows and `j/k`.
- `Enter` reopens the selected thread.
- Delete is available from history UI.

Model switch behavior:

- Switching model does not interrupt the in-flight turn.
- Next turn uses the newly selected model.
- A divider event is added in-thread:
  `Changed model: <Old> -> <New>`

## Providers And Models

Quick Ask only shows models that are currently available and enabled.

- `codex` provider models are filtered by live Codex app-server `model/list` availability.
- `claude` and `gemini` appear when their CLIs are present and logged in.
- `ollama` models are pulled live from Ollama (`/api/tags`), chat-only filtered.

Default built-in cloud model options:

- `Codex 5.3` (Codex app-server runtime)
- `ChatGPT 5.4 Instant`
- `ChatGPT 5.4 Medium`
- `Claude Opus 4.6`
- `Claude Sonnet 4.6`
- `Gemini 3 Flash`
- `Gemini Flash Lite`

Model ordering:

- Provider groups are ordered: `codex`, `claude`, `gemini`, `ollama`.
- Within each provider group, ordering is dynamically weighted by usage in saved Quick Ask history, with a heavy recency bias (last 72 hours weighted most).

To add local models, install them in Ollama (for example `ollama pull <model>`).

## Coding Scope (Codex/Gemini)

When a coding model is selected (`codex` or `gemini`), the model menu shows a `scope` section:

- `Full Access` (default)
- `<path>…` restricted directory mode (defaults to `~/Downloads`, can be changed via folder picker)

Scope is persisted per app usage and saved with transcript metadata.

Execution policy:

- Codex Full Access uses dangerous full filesystem access mode.
- Codex restricted uses workspace-write mode rooted at selected scope.
- Gemini includes either home directory (full access) or selected restricted directory in its allowed directories.

## Security And History

History is optional. When enabled:

- chats are encrypted before saving
- encrypted session files are stored on disk
- encryption key is stored in macOS Keychain under service `local-chat-transcript-key`
- no plaintext transcript files are written during normal save flow

Archive location:

- default save root is `~/Library/Application Support/Quick Ask/sessions`
- if Dropbox is detected, Quick Ask prefers a Dropbox `Quick Ask/sessions` path
- settings can choose a custom archive folder
- env override: `QUICK_ASK_SAVE_DIR`

Disable history:

- settings toggle, or
- env flag: `QUICK_ASK_DISABLE_HISTORY=1`

## Build And Install

From repo root:

```zsh
./build-quick-ask
```

Build script behavior:

- compiles `QuickAskApp.swift` into `~/Applications/Quick Ask.app`
- copies backend files into app resources
- installs/updates LaunchAgent `app.quickask.mac` (RunAtLoad + KeepAlive)

Build without (re)bootstrapping LaunchAgent:

```zsh
QUICK_ASK_SKIP_LAUNCH_AGENT=1 ./build-quick-ask
```

## Setup Requirements

- macOS
- Python 3
- Xcode command line tools (`xcrun`, `swiftc`)
- optional CLIs depending on providers you want:
  - `claude`
  - `codex`
  - `gemini`
  - `ollama`
- `openssl` + Keychain access for encrypted history

Typical provider login commands:

- `claude auth login --claudeai`
- `codex login --device-auth`
- `gemini`

## Development And Testing

UI harness tests:

```zsh
python3 tests/test_quick_ask_ui.py -v
```

Backend and integration suites:

```zsh
python3 tests/test_backend_env.py -v
python3 tests/test_quick_ask_backend_images.py -v
python3 tests/test_codex_app_server_backend.py -v
python3 tests/test_fresh_install.py -v
python3 tests/test_quick_ask_shared.py -v
```

Notes:

- UI tests run app in a test harness mode and do not invoke paid provider inference.
- They validate layout, keyboard behavior, queueing, history, model selection, scope controls, and failure/retry states.

## Repo

- Repo: `quick-ask`
- App name: `Quick Ask`
- License: Apache-2.0 with Commons Clause
