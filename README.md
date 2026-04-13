# Daemon

> An always-on local AI assistant that knows what's on my screen and in my notes. Llama 3.1 8B running on my own hardware. No API keys, no subscriptions, no telemetry.

The product is called **Daemon**. The AI persona inside it is **Francis**.

## What it does

Press **Alt+Space** anywhere on my laptop. A small dark window pops up. Type a question, hit Enter, and Francis answers using two things at the same time:

1. A fresh OCR of whatever is on my screen at the moment of the question.
2. Semantic search over my entire Obsidian vault of 1,800+ notes covering every project, course, person, and goal in my life.

Press Alt+Space again or hit Esc to dismiss. Same hotkey toggles.

Two extra hotkeys:

- **Alt+Shift+Space** silently summarizes the last hour of screen activity into a markdown note in the vault.
- **Alt+Shift+C** opens a capture window that pairs a typed annotation with a fresh OCR of the screen, saved as one markdown file.

## Architecture

Two machines, four processes, all auto-starting at user logon as Windows Scheduled Tasks. Zero manual upkeep.

**Desktop PC (RTX 3070)** is a headless server. The user never sits at it.

| Process | Role |
|---|---|
| `Daemon-API` (`api.py`) | FastAPI on port 8000. Handles `/ask`, `/ask/stream`, `/summarize`, `/ingest_screenshot`, `/health`. Calls Ollama. |
| `Daemon-EmbeddingWorker` (`embedding_worker.py`) | Watches the Obsidian vault folder via `watchdog`. Embeds new and changed notes via `nomic-embed-text` into a sqlite-vec database. |

**Laptop** is the only client.

| Process | Role |
|---|---|
| `Daemon-ScreenshotWorker` (`screenshot_worker.py`) | Captures the primary monitor every 7.5 minutes. Runs OCR locally via the Windows.Media.Ocr API. Pushes only the text to the PC. |
| `Daemon-Overlay` (`overlay.py`) | PyQt6 tray app, frameless overlay window, three global hotkeys via Win32 RegisterHotKey. Talks to the PC over Tailscale. |

**Communication**:

- Laptop ↔ PC over Tailscale (port 8000). Direct peer-to-peer, no port forwarding.
- PC ↔ Ollama on localhost. Llama 3.1 8B for chat, nomic-embed-text for embeddings.
- Vault syncs both ways via OneDrive.
- Both PC services share `%LOCALAPPDATA%\daemon\daemon.db` (SQLite + WAL mode + sqlite-vec).

## How a question flows

1. User presses Alt+Space. Overlay window appears at last position.
2. User types question, hits Enter.
3. Overlay slides itself to `(-32000, -32000)` (off-screen so OCR doesn't capture the chat window itself), takes a screenshot of the primary monitor with `mss`, restores its position, and refocuses the input.
4. A worker thread runs OCR locally via `winocr`, pushes the text to `/ingest_screenshot` on the PC, and emits the text back to the overlay.
5. Overlay calls `/ask/stream` on the PC with the question and screen text in the payload.
6. API embeds the question, runs a two-pass semantic search (hand-curated notes rank above auto-generated knowledge graph nodes), builds a prompt with the screen content plus the most relevant chunks, and streams the answer back token by token over NDJSON.
7. Overlay renders the streaming response with markdown, sources footer, and a captured-OCR preview.

About a second to first token. Three to six seconds for a full short answer.

## Tech Stack

- **Language**: Python 3.12
- **Model runtime**: Ollama running Llama 3.1 8B (chat) and nomic-embed-text (embeddings)
- **Vector search**: SQLite + sqlite-vec
- **Backend**: FastAPI + uvicorn
- **Frontend**: PyQt6
- **Vault watcher**: watchdog
- **Screen capture**: mss
- **OCR**: winocr (Windows.Media.Ocr API, no external Tesseract install)
- **Networking**: Tailscale
- **Knowledge substrate**: Obsidian
- **Vault sync**: OneDrive
- **Autostart**: Windows Scheduled Tasks

## Repo Layout

```
daemon/
├── api.py                  FastAPI backend (PC)
├── embedding_worker.py     Vault embedder (PC)
├── screenshot_worker.py    Screen capture + OCR (laptop)
├── overlay.py              PyQt6 tray app + overlay windows + hotkeys (laptop)
├── query.py                Standalone CLI for ad-hoc semantic search
├── config.py               Paths, model names, hotkey definitions
├── install_autostart.ps1   One-shot Windows Scheduled Task installer
├── requirements.txt        Python dependencies (same on both machines)
├── CLAUDE.md               Detailed engineering log + project memory
└── README.md               This file
```

## A note on running this yourself

This is a personal project built specifically for my hardware (a desktop PC with an RTX 3070 + a Windows laptop on the same Tailscale tailnet). It is not meant to be a deployable tool. Paths, Tailscale IPs, machine hostnames, and OCR engine choice are all baked in for my setup.

If you want to do something similar, the architecture is the interesting part. The code is open and self-explanatory. `CLAUDE.md` has the full engineering log, including the gotchas I hit and the design decisions I made.

## License

MIT. See [LICENSE](LICENSE).

## Author

Arthur Rees · [arthurrees.dev](https://arthurrees.dev)
