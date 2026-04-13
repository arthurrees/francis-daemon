r"""
Screen capture + OCR worker. Runs on the laptop.

Every CAPTURE_INTERVAL_SECONDS:
1. Captures the primary monitor
2. OCRs it via the built-in Windows.Media.Ocr API (no external Tesseract install)
3. Appends the extracted text + timestamp to a daily NDJSON log
4. Optionally saves the raw image, with retention pruning

All output goes to %LOCALAPPDATA%\daemon\screenshots\ (outside OneDrive).
The OCR text is not yet wired into Francis retrieval — that happens in step 4
when we build the FastAPI backend. For now this is a standalone capture loop.

Run: python screenshot_worker.py
"""

import os
import sys
from pathlib import Path

# Stdout redirect for pythonw / autostart mode (see api.py for full comment).
if sys.stdout is None:
    _log_dir = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "daemon" / "logs"
    )
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = open(
        _log_dir / (Path(__file__).stem + ".log"),
        "a",
        encoding="utf-8",
        buffering=1,
    )
    sys.stdout = _log_file
    sys.stderr = _log_file

import datetime as dt
import json
import time

import mss
import requests
import winocr
from PIL import Image

import config


# ============================================================
# Overlay-visibility check
# ============================================================
# The overlay (overlay.py) refreshes a lock file every 30 s while a window
# is shown. When the lock is fresh (mtime within 60 s), we skip the capture
# cycle — otherwise the OCR grabs the chat window itself, ingests Francis's
# prior answer back into the screenshots table, and the next /ask sees its
# own output as "recent activity" → the model riffs on it and repeats.
# A stale lock (no refresh for >60 s) is treated as "overlay gone" so a
# crashed overlay process doesn't permanently block captures.

OVERLAY_VISIBLE_LOCK = config.DATA_DIR / "overlay_visible.lock"
OVERLAY_LOCK_FRESH_SECONDS = 60


def is_overlay_visible() -> bool:
    try:
        if not OVERLAY_VISIBLE_LOCK.exists():
            return False
        age = time.time() - OVERLAY_VISIBLE_LOCK.stat().st_mtime
        return age < OVERLAY_LOCK_FRESH_SECONDS
    except OSError:
        return False


# ============================================================
# Capture
# ============================================================

def capture_primary_monitor() -> Image.Image:
    """Grab the primary monitor as a PIL Image."""
    with mss.mss() as sct:
        # sct.monitors[0] is "all monitors combined"; [1] is the primary.
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        return Image.frombytes("RGB", shot.size, shot.rgb)


# ============================================================
# OCR (Windows built-in)
# ============================================================

def ocr_image(img: Image.Image) -> str:
    """Run Windows OCR on a PIL image. Returns the extracted text (may be empty)."""
    result = winocr.recognize_pil_sync(img, "en-US")
    # winocr's sync result is a dict with a 'text' key.
    if isinstance(result, dict):
        return result.get("text", "") or ""
    return getattr(result, "text", "") or ""


# ============================================================
# Storage
# ============================================================

def append_log(text: str, ts: dt.datetime) -> Path:
    """Append an OCR result to today's NDJSON log. Returns the log file path."""
    config.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.SCREENSHOTS_DIR / f"{ts.strftime('%Y-%m-%d')}.ndjson"
    entry = {"ts": ts.isoformat(timespec="seconds"), "text": text}
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path


def save_image(img: Image.Image, ts: dt.datetime) -> Path:
    config.SCREENSHOT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    name = ts.strftime("%Y-%m-%dT%H%M%S") + ".png"
    path = config.SCREENSHOT_IMAGES_DIR / name
    img.save(path, "PNG", optimize=True)
    return path


def prune_old_images() -> int:
    """Delete images older than IMAGE_RETENTION_DAYS. Returns count deleted."""
    if not config.SCREENSHOT_IMAGES_DIR.exists():
        return 0
    cutoff = time.time() - config.IMAGE_RETENTION_DAYS * 86400
    deleted = 0
    for p in config.SCREENSHOT_IMAGES_DIR.glob("*.png"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted


# ============================================================
# API push (best-effort — PC may be off or unreachable)
# ============================================================

def ingest_to_api(ts: dt.datetime, text: str) -> None:
    """Push a single capture to the PC for summarization/retrieval.

    Silent on failure — the local NDJSON is the source of truth. If the PC
    is off or Tailscale is unreachable, we skip and the next startup's
    bootstrap will catch up via INSERT OR IGNORE.
    """
    try:
        requests.post(
            f"{config.API_URL}/ingest_screenshot",
            json={"ts": ts.timestamp(), "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"  ! ingest to PC failed: {e}", flush=True)


def bootstrap_ndjson_to_api() -> None:
    """On startup, push all existing NDJSON entries to the PC's DB.

    The server-side INSERT OR IGNORE makes this idempotent — re-running
    after every startup is safe and won't create duplicates. If the PC is
    unreachable, we silently skip; next startup retries.
    """
    if not config.SCREENSHOTS_DIR.exists():
        return
    total = 0
    pushed = 0
    for log_file in sorted(config.SCREENSHOTS_DIR.glob("*.ndjson")):
        try:
            with log_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = entry.get("text", "")
                    if not text:
                        continue
                    try:
                        ts = dt.datetime.fromisoformat(entry["ts"]).timestamp()
                    except (KeyError, ValueError):
                        continue
                    total += 1
                    try:
                        r = requests.post(
                            f"{config.API_URL}/ingest_screenshot",
                            json={"ts": ts, "text": text},
                            timeout=10,
                        )
                        if r.status_code == 200:
                            pushed += 1
                    except Exception:
                        pass
        except OSError:
            continue
    if total > 0:
        print(
            f"  bootstrap: pushed {pushed}/{total} historical NDJSON "
            f"entries to PC",
            flush=True,
        )


# ============================================================
# One capture cycle
# ============================================================

def capture_once() -> None:
    ts = dt.datetime.now()
    if is_overlay_visible():
        print(
            f"[{ts.strftime('%H:%M:%S')}] skipped — overlay visible",
            flush=True,
        )
        return
    print(f"[{ts.strftime('%H:%M:%S')}] capturing...", flush=True)
    img = capture_primary_monitor()
    text = ocr_image(img)
    log_path = append_log(text, ts)
    if config.SAVE_RAW_IMAGES:
        save_image(img, ts)
    pruned = prune_old_images()
    # Push to PC for summarization/retrieval (best-effort).
    if text.strip():
        ingest_to_api(ts, text)
    line_count = len([ln for ln in text.splitlines() if ln.strip()])
    print(
        f"  → {img.size[0]}x{img.size[1]} px, "
        f"{len(text)} chars / {line_count} text lines, "
        f"logged to {log_path.name}"
        + (f", pruned {pruned} old images" if pruned else ""),
        flush=True,
    )


# ============================================================
# Main loop
# ============================================================

def main():
    print("Screenshot worker starting")
    print(f"  log dir       : {config.SCREENSHOTS_DIR}")
    print(f"  capture every : {config.CAPTURE_INTERVAL_SECONDS}s")
    print(f"  save images   : {config.SAVE_RAW_IMAGES}")
    print(f"  retention     : {config.IMAGE_RETENTION_DAYS} days")
    print(f"  API target    : {config.API_URL}")
    print()

    # Push any accumulated history to the PC first (idempotent via
    # server-side INSERT OR IGNORE).
    bootstrap_ndjson_to_api()

    # Immediate first capture so you see something within seconds.
    capture_once()

    while True:
        try:
            time.sleep(config.CAPTURE_INTERVAL_SECONDS)
            capture_once()
        except KeyboardInterrupt:
            print("\nStopping...")
            break
        except Exception as e:
            print(f"  ! error: {e}", flush=True)
            time.sleep(config.CAPTURE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
