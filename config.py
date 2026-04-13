"""Daemon configuration — paths, models, settings."""

import os
from pathlib import Path

# ----- Vault -----
# Path to the Obsidian vault. Override with the DAEMON_VAULT_PATH environment
# variable when the vault lives elsewhere (typical setup: inside a cloud-sync
# folder so both machines see the same files, with "Always keep on this device"
# enabled on the PC so the embedding worker reads real files, not placeholders).
VAULT_PATH = Path(
    os.environ.get("DAEMON_VAULT_PATH", str(Path(__file__).parent / "vault"))
)

# Folders inside the vault that are NOT text content and should be skipped.
VAULT_SKIP_DIRS = {".obsidian", "_attachments", "Templates"}

# ----- Database -----
# Stored OUTSIDE OneDrive on purpose. SQLite + cloud sync is a known bad
# combination — journal and WAL files cause sync conflicts and corruption.
# Each machine has its own DB; only the PC's matters because that's where the
# embedding worker runs.
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "daemon"
DB_PATH = DATA_DIR / "daemon.db"

# ----- Ollama (embeddings + LLM) -----
# Worker runs on the same machine as Ollama, so localhost is correct here.
OLLAMA_HOST = "http://localhost:11434"
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 768  # nomic-embed-text output dimension
LLM_MODEL = "llama3.1:8b"  # primary chat model

# ----- API -----
API_HOST = "0.0.0.0"  # bind all interfaces so Tailscale can reach it
API_PORT = 8000

# ----- API client (laptop-side, used by the overlay) -----
# Set DAEMON_API_URL to the PC's reachable address (typically a Tailscale IP
# like http://100.x.x.x:8000). Defaults to localhost so the API is callable
# when both the server and client run on the same machine.
# Requires Windows Firewall on the PC to allow inbound TCP on API_PORT —
# run once on PC (admin shell):
#   netsh advfirewall firewall add rule name="Daemon API" dir=in action=allow protocol=TCP localport=8000
API_URL = os.environ.get("DAEMON_API_URL", f"http://localhost:{API_PORT}")

# ----- Overlay hotkeys -----
# Win32 RegisterHotKey constants.
# Modifier flags (combine with bitwise OR):
#   MOD_ALT     = 0x0001
#   MOD_CONTROL = 0x0002
#   MOD_SHIFT   = 0x0004
#   MOD_WIN     = 0x0008
# Common virtual-keys:
#   VK_SPACE = 0x20  |  VK_F1..VK_F12 = 0x70..0x7B

# Primary hotkey: summons the overlay window. Default = Alt+Space.
HOTKEY_MODIFIERS = 0x0001  # MOD_ALT
HOTKEY_VK = 0x20           # VK_SPACE
HOTKEY_LABEL = "Alt+Space"

# Secondary hotkey: silently triggers /summarize (no window opens).
# As of the capture feature, this now fires a fresh screen capture + OCR
# BEFORE calling /summarize, so the summary always reflects the moment you
# pressed the key — not whatever was captured up to 7.5 min ago.
# Default = Alt+Shift+Space.
HOTKEY_SILENT_MODIFIERS = 0x0001 | 0x0004  # MOD_ALT | MOD_SHIFT
HOTKEY_SILENT_VK = 0x20                    # VK_SPACE
HOTKEY_SILENT_LABEL = "Alt+Shift+Space"

# Tertiary hotkey: opens the manual capture overlay. Fires a fresh screen
# capture + OCR, then shows a window where you type your own annotation.
# Both the user note and the OCR are saved as a single markdown file in
# the vault at 90_Francis/captures/YYYY-MM-DD/HH-MM-SS.md.
# Default = Alt+Shift+C.
HOTKEY_CAPTURE_MODIFIERS = 0x0001 | 0x0004  # MOD_ALT | MOD_SHIFT
HOTKEY_CAPTURE_VK = 0x43                    # VK_C
HOTKEY_CAPTURE_LABEL = "Alt+Shift+C"

# ----- Chunking -----
# Soft cap per chunk in characters. ~4 chars/token, so 1500 ≈ 375 tokens —
# well inside nomic-embed-text's 8192-token context with room for headings.
MAX_CHUNK_CHARS = 1500

# ----- Screenshot worker (laptop-side) -----
# Outputs go in %LOCALAPPDATA%\daemon\screenshots\ — outside OneDrive on purpose,
# same reason as the DB. Per-machine activity data, no cross-machine sync.
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
SCREENSHOT_IMAGES_DIR = SCREENSHOTS_DIR / "images"

# How often to capture. 7.5 min = 450s. Gives ~96 captures over a 12-hour
# workday — granular enough to reflect activity, light on disk and battery.
CAPTURE_INTERVAL_SECONDS = 450

# Whether to keep the raw PNG screenshot in addition to the OCR text.
# Disabled by default — OCR text alone is enough for retrieval/summarization,
# and skipping the PNG save reduces disk I/O and storage. Set to True
# temporarily if you need to debug OCR misses against a visual reference.
SAVE_RAW_IMAGES = False

# Days to keep raw images before pruning. Text logs are kept forever (small).
IMAGE_RETENTION_DAYS = 7
