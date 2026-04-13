"""
Daemon Alt+Space overlay.

Lives on the laptop. Runs in the system tray. Press Alt+Space (configurable
in config.py) to summon a floating dark window where you can ask Francis a
question. Esc to dismiss.

Requires Windows for the global hotkey (uses Win32 RegisterHotKey via ctypes).

Run: python overlay.py
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

import ctypes
import datetime as dt
import json
import threading
import time
from ctypes import wintypes

import mss
import requests
import winocr
from PIL import Image
from PyQt6.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import config


# ============================================================
# Win32 global hotkey
# ============================================================

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class GlobalHotkeyThread(threading.Thread):
    """
    Registers a Win32 global hotkey and runs a Windows message pump on this
    thread. Calls `callback()` (from this thread) when the hotkey fires.

    RegisterHotKey is a system-level mechanism — it properly suppresses the
    original key behavior (e.g. Alt+Space won't open the title bar menu).
    """

    def __init__(self, modifiers: int, vk: int, callback):
        super().__init__(daemon=True)
        self.modifiers = modifiers
        self.vk = vk
        self.callback = callback
        self.thread_id: int | None = None

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.modifiers, self.vk):
            print(
                "ERROR: failed to register global hotkey "
                f"(mods={self.modifiers:#x}, vk={self.vk:#x}). "
                "Another app may already own it.",
                file=sys.stderr,
            )
            return

        msg = wintypes.MSG()
        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):  # WM_QUIT or error
                    break
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception as e:
                        print(f"hotkey callback error: {e}", file=sys.stderr)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self.thread_id is not None:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


# ============================================================
# Bridge: hotkey thread → Qt main thread (via signal)
# ============================================================

class HotkeyBridge(QObject):
    triggered = pyqtSignal()


# ============================================================
# Custom widgets — Esc-aware input + draggable background
# ============================================================

class QueryInput(QLineEdit):
    """QLineEdit that emits `escaped` when the user presses Esc.

    QLineEdit eats arrow keys and a few others by default, and Qt's
    shortcut routing for top-level Esc on a frameless window is flaky.
    Catching Esc directly here is the most reliable fix.
    """

    escaped = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class DraggableContainer(QWidget):
    """The dark rounded background. Click + drag anywhere on it (other than
    the input/output widgets) moves the whole overlay window.

    A frameless window has no title bar to drag, so we wire up dragging
    manually on this child container.
    """

    def __init__(self, parent):
        super().__init__(parent)
        # WA_StyledBackground is REQUIRED for QSS background-color/border to
        # actually paint on a QWidget subclass. Without it, the stylesheet
        # silently no-ops, the container looks transparent, AND mouse
        # hit-testing falls through to the parent — which is exactly the bug
        # we hit on the first run.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._drag_offset = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.window().move(
                event.globalPosition().toPoint() - self._drag_offset
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


# ============================================================
# Streaming POST worker — reads NDJSON line-by-line from /ask/stream
# ============================================================

class StreamingHttpWorker(QThread):
    sources_received = pyqtSignal(list)
    chunk_received = pyqtSignal(str)
    finished_streaming = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, url: str, payload: dict, timeout: int = 180):
        super().__init__()
        self.url = url
        self.payload = payload
        self.timeout = timeout

    def run(self):
        try:
            r = requests.post(
                self.url,
                json=self.payload,
                timeout=self.timeout,
                stream=True,
            )
            r.raise_for_status()
            for raw_line in r.iter_lines():
                if not raw_line:
                    continue
                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if "sources" in msg:
                    self.sources_received.emit(msg["sources"])
                elif "chunk" in msg:
                    self.chunk_received.emit(msg["chunk"])
                elif msg.get("done"):
                    break
            self.finished_streaming.emit()
        except Exception as e:
            self.failed.emit(str(e))


# ============================================================
# Fresh screen capture + OCR (laptop-side, same path as screenshot_worker)
# ============================================================
# Split into two phases on purpose:
#   capture_screen() — fast (~30-50 ms), grabs pixels only
#   ocr_image()      — slow (~500-1500 ms), runs Windows OCR
# The Alt+Shift+C window needs the capture to happen BEFORE its own window
# is shown on screen (or the OCR would "see" the green capture box). The
# caller grabs pixels synchronously on the main thread first, then starts
# a worker to do the slow OCR in parallel with the user typing.

def capture_screen() -> tuple[dt.datetime, Image.Image]:
    """Grab the primary monitor as a PIL image. Fast enough to run on the
    UI thread — a brief ~50 ms stall is imperceptible and avoids any race
    between showing the capture window and snapping the screen behind it.
    """
    ts = dt.datetime.now()
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # [0] is "all monitors combined"
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    return ts, img


def ocr_image(img: Image.Image) -> str:
    """Run Windows OCR on a PIL image. Slow — always call from a worker."""
    result = winocr.recognize_pil_sync(img, "en-US")
    if isinstance(result, dict):
        return result.get("text", "") or ""
    return getattr(result, "text", "") or ""


def capture_and_ocr() -> tuple[dt.datetime, str]:
    """Grab + OCR the primary monitor in one call. Used by the silent
    summarize worker, which has no window-ordering concern."""
    ts, img = capture_screen()
    return ts, ocr_image(img)


def push_ingest(ts: dt.datetime, text: str) -> None:
    """Best-effort push of a capture into the PC's screenshots table.

    Silent on failure — same philosophy as screenshot_worker.bootstrap.
    The capture still ends up in the vault file (for Alt+Shift+C) or in the
    summarize output (for Alt+Shift+Space) regardless.
    """
    if not text.strip():
        return
    try:
        requests.post(
            f"{config.API_URL}/ingest_screenshot",
            json={"ts": ts.timestamp(), "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"ingest push failed: {e}", file=sys.stderr)


# ============================================================
# Silent summarize worker: capture → ingest → /summarize
# ============================================================

class SilentSummarizeWorker(QThread):
    """Fires a fresh capture, pushes it to the PC, then calls /summarize.

    The fresh capture ensures /summarize sees the moment the hotkey was
    pressed, not whatever the 7.5-min background worker captured last.
    """

    progress = pyqtSignal(str)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def run(self):
        try:
            self.progress.emit("capturing...")
            ts, text = capture_and_ocr()
            push_ingest(ts, text)

            self.progress.emit("summarizing...")
            r = requests.post(
                f"{config.API_URL}/summarize",
                json={"minutes": 60},
                timeout=180,
            )
            r.raise_for_status()
            self.succeeded.emit(r.json())
        except Exception as e:
            self.failed.emit(str(e))


# ============================================================
# Capture worker: OCR a pre-captured image + ingest, returns text
# ============================================================

class CaptureOcrWorker(QThread):
    """OCRs a PIL image that the caller captured *before* showing its
    window (so the capture window itself isn't in the OCR input). Pushes
    the result to /ingest_screenshot and emits the text back so the
    capture window can assemble the combined vault file."""

    succeeded = pyqtSignal(float, str)  # (epoch_ts, text)
    failed = pyqtSignal(str)

    def __init__(self, ts: dt.datetime, img: Image.Image):
        super().__init__()
        self.ts = ts
        self.img = img

    def run(self):
        try:
            text = ocr_image(self.img)
            push_ingest(self.ts, text)
            self.succeeded.emit(self.ts.timestamp(), text)
        except Exception as e:
            self.failed.emit(str(e))


# ============================================================
# Persistent overlay state (last position, etc.)
# ============================================================

OVERLAY_STATE_FILE = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "daemon"
    / "overlay_state.json"
)

# Visibility lock — written by overlay when a window is shown, polled by the
# screenshot worker before each capture. The worker skips its capture cycle
# when the file's mtime is fresh (< 60 s old), preventing the background
# screenshot loop from OCRing the chat window itself and feeding Francis's
# own prior answers back into the next /ask. The overlay refreshes the file
# every 30 s while visible (via a QTimer) so a crashed overlay process
# auto-recovers within 60 s — captures resume on their own, no manual cleanup.
OVERLAY_VISIBLE_LOCK = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    / "daemon"
    / "overlay_visible.lock"
)


def mark_overlay_visible() -> None:
    try:
        OVERLAY_VISIBLE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        OVERLAY_VISIBLE_LOCK.write_text(str(time.time()))
    except OSError:
        pass


def load_overlay_state() -> dict:
    try:
        if OVERLAY_STATE_FILE.exists():
            return json.loads(OVERLAY_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_overlay_state(updates: dict) -> None:
    """Merge `updates` into the on-disk state file.

    Both OverlayWindow and CaptureWindow persist position here, under
    different keys — we must merge, not clobber, or one window saving will
    wipe the other's remembered position.
    """
    try:
        OVERLAY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = load_overlay_state()
        state.update(updates)
        OVERLAY_STATE_FILE.write_text(
            json.dumps(state), encoding="utf-8"
        )
    except Exception:
        pass


# ============================================================
# The overlay window
# ============================================================

class OverlayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.worker: QThread | None = None
        self.stream_worker: StreamingHttpWorker | None = None
        self.ocr_worker: CaptureOcrWorker | None = None
        self.tray: QSystemTrayIcon | None = None  # set by main()
        self._answer_buffer: str = ""
        self._sources_buffer: list = []
        # Fresh-OCR-on-summon state. Same pattern as CaptureWindow:
        # capture pixels sync before show, OCR on a worker, ingest happens
        # inside the worker. Submits that arrive before OCR completes are
        # parked in _pending_question and fired from _on_capture_done.
        # _fresh_screen_text is sent to /ask as `screen_text` so the API can
        # put it in its own CURRENT SCREEN block — that's how the model
        # resolves "this question" / "what's on my screen" deictically.
        self._ocr_ready: bool = True
        self._pending_question: str | None = None
        self._fresh_screen_text: str = ""
        # Refreshes OVERLAY_VISIBLE_LOCK every 30 s while shown so the
        # background screenshot worker keeps skipping its cycle.
        self._visibility_timer = QTimer(self)
        self._visibility_timer.setInterval(30 * 1000)
        self._visibility_timer.timeout.connect(mark_overlay_visible)
        self._build_ui()

    def _build_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # no taskbar entry
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(560, 380)

        # Inner container holds the rounded dark background and the layout.
        # DraggableContainer adds click-and-drag from the empty padding.
        root = DraggableContainer(self)
        root.setObjectName("root")
        root.setGeometry(0, 0, self.width(), self.height())

        # Sleek minimal dark — soft near-black panel, hairline border, large
        # rounded corners, generous padding, system sans for a non-terminal
        # feel. One subtle accent (#7a7aff) used only for the streaming caret
        # / focus signal; everything else is grayscale.
        root.setStyleSheet(
            """
            QWidget#root {
                background-color: #161616;
                border: 1px solid #2a2a2a;
                border-radius: 14px;
            }
            QLineEdit, QTextEdit, QLabel {
                font-family: "Segoe UI Variable Display", "Segoe UI",
                             "Inter", -apple-system, BlinkMacSystemFont,
                             "Helvetica Neue", sans-serif;
            }
            QLineEdit#query {
                background-color: transparent;
                color: #f0f0f0;
                border: none;
                padding: 0;
                font-size: 17px;
                selection-background-color: #2e3a5c;
                selection-color: #f0f0f0;
            }
            QTextEdit#output {
                background-color: transparent;
                color: #c8c8c8;
                border: none;
                font-size: 13px;
                selection-background-color: #2e3a5c;
                selection-color: #f0f0f0;
            }
            QLabel#status {
                color: #6a6a6a;
                font-size: 10px;
                padding: 0;
            }
            """
        )

        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # Input
        self.input = QueryInput()
        self.input.setObjectName("query")
        self.input.setPlaceholderText("ask francis")
        self.input.returnPressed.connect(self._on_submit)
        self.input.escaped.connect(self.hide)
        layout.addWidget(self.input)

        # Status (subtle, single line)
        self.status = QLabel("")
        self.status.setObjectName("status")
        layout.addWidget(self.status)

        # Output area
        self.output = QTextEdit()
        self.output.setObjectName("output")
        self.output.setReadOnly(True)
        layout.addWidget(self.output, stretch=1)

        # Esc dismisses regardless of which child currently has focus.
        esc = QShortcut(QKeySequence("Escape"), self)
        esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        esc.activated.connect(self.hide)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    # ---- Show / hide with position memory ----

    def toggle(self):
        # Alt+Space behaves like Spotlight — same key opens AND dismisses.
        # Esc still works, but the hotkey-toggle is muscle-memory friendly.
        if self.isVisible():
            self.hide()
        else:
            self.show_centered()

    def show_centered(self):
        # Just show the window. OCR fires per-submit, not per-summon, so a
        # user who summons and then sits for 30 seconds doesn't get stale
        # screen context — every Enter triggers a fresh capture.
        self._pending_question = None
        self._ocr_ready = True
        self._fresh_screen_text = ""

        state = load_overlay_state()
        if "ask_x" in state and "ask_y" in state:
            self.move(state["ask_x"], state["ask_y"])
        elif "x" in state and "y" in state:
            # Backwards compat with the pre-namespaced key format.
            self.move(state["x"], state["y"])
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            x = screen.center().x() - self.width() // 2
            y = screen.center().y() - self.height() // 2 - 80
            self.move(x, y)

        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        self.input.selectAll()
        self.status.setText("")

    def showEvent(self, event):
        mark_overlay_visible()
        self._visibility_timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        # Stop refreshing the visibility lock; mtime ages past 60 s within
        # a minute, so background captures resume on their own.
        self._visibility_timer.stop()
        # Persist position so the next summon comes back to the same spot.
        pos = self.pos()
        save_overlay_state({"ask_x": pos.x(), "ask_y": pos.y()})
        super().hideEvent(event)

    # ---- /ask (streaming) ----

    def _on_submit(self):
        question = self.input.text().strip()
        if not question:
            return
        if self.stream_worker and self.stream_worker.isRunning():
            return
        if self.worker and self.worker.isRunning():
            return

        self.output.clear()
        self.input.clear()
        self._answer_buffer = ""
        self._sources_buffer = []
        self._fresh_screen_text = ""
        self._pending_question = question
        self._ocr_ready = False
        self.status.setText("reading screen...")

        # Capture the screen with the overlay out of frame. setWindowOpacity
        # + WA_TranslucentBackground proved unreliable on Windows DWM — the
        # window still made it into mss.grab(). Moving the window outside
        # the primary monitor's coordinate range is bulletproof: mss grabs
        # monitors[1] (primary), and a window at (-32000, -32000) is not in
        # that region. No hide/show, no focus loss, no z-order shuffle.
        saved_pos = self.pos()
        self.move(-32000, -32000)
        QApplication.processEvents()
        try:
            ts, img = capture_screen()
        except Exception as e:
            print(f"per-submit screen capture failed: {e}", file=sys.stderr)
            ts, img = dt.datetime.now(), None
        self.move(saved_pos)
        QApplication.processEvents()
        self.input.setFocus()

        if img is None:
            # Couldn't capture — fire /ask anyway, just without screen_text.
            self._ocr_ready = True
            self._pending_question = None
            self._fire_ask(question)
            return

        # Replace any in-flight OCR worker from a previous submit. The old
        # one is left to finish its push_ingest in the background; we just
        # disconnect our handlers so its succeeded signal doesn't fire a
        # stale /ask after we've moved on.
        if self.ocr_worker and self.ocr_worker.isRunning():
            try:
                self.ocr_worker.succeeded.disconnect(self._on_capture_done)
                self.ocr_worker.failed.disconnect(self._on_capture_failed)
            except TypeError:
                pass
        self.ocr_worker = CaptureOcrWorker(ts, img)
        self.ocr_worker.succeeded.connect(self._on_capture_done)
        self.ocr_worker.failed.connect(self._on_capture_failed)
        self.ocr_worker.start()

    def _fire_ask(self, question: str):
        self.status.setText("thinking...")
        payload = {"question": question, "k": 4}
        if self._fresh_screen_text.strip():
            payload["screen_text"] = self._fresh_screen_text
        self.stream_worker = StreamingHttpWorker(
            url=f"{config.API_URL}/ask/stream",
            payload=payload,
        )
        self.stream_worker.sources_received.connect(self._on_sources)
        self.stream_worker.chunk_received.connect(self._on_chunk)
        self.stream_worker.finished_streaming.connect(self._on_stream_done)
        self.stream_worker.failed.connect(self._on_error)
        self.stream_worker.start()

    # ---- Fresh-OCR callbacks (the worker also pushed to /ingest_screenshot
    # before emitting, so the fresh capture is already in the DB by now) ----

    def _on_capture_done(self, _ts: float, text: str):
        self._fresh_screen_text = text
        self._ocr_ready = True
        chars = len(text.strip())
        if self._pending_question:
            q = self._pending_question
            self._pending_question = None
            self._fire_ask(q)
        elif chars:
            self.status.setText(f"screen ready — {chars} chars OCR'd")
        else:
            self.status.setText("screen read — (no text found)")

    def _on_capture_failed(self, msg: str):
        # Don't block /ask on OCR failure — answer without fresh screen
        # context. Background worker captures still cover recent activity.
        print(f"ask-overlay OCR failed: {msg}", file=sys.stderr)
        self._ocr_ready = True
        self._fresh_screen_text = ""
        if self._pending_question:
            q = self._pending_question
            self._pending_question = None
            self._fire_ask(q)
        else:
            self.status.setText(f"OCR failed: {msg[:80]}")

    def _on_sources(self, sources: list):
        self._sources_buffer = sources
        self.status.setText("streaming...")

    def _on_chunk(self, text: str):
        self._answer_buffer += text
        # During streaming, render as plain text so we can append cheaply.
        # Switch to markdown rendering when streaming completes.
        self.output.setPlainText(self._answer_buffer)
        # Scroll to bottom so the user sees new tokens as they arrive.
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.output.setTextCursor(cursor)

    def _on_stream_done(self):
        self.status.setText("")
        # Final render: markdown for the answer + sources + OCR preview.
        text = self._answer_buffer.strip() or "*(no response)*"
        if self._sources_buffer:
            text += "\n\n---\n\n**sources**\n"
            for s in self._sources_buffer:
                line = f"- `{s.get('path', '?')}`"
                heading = s.get("heading")
                if heading:
                    line += f" — {heading}"
                text += line + "\n"
        # OCR preview: shows what Francis actually received in CURRENT SCREEN.
        # Acts as a sanity check — if Francis's answer feels off, scroll
        # down here to see whether the OCR captured the question or just
        # picked up nav chrome / placeholder text.
        ocr = self._fresh_screen_text.strip()
        if ocr:
            preview = ocr if len(ocr) <= 700 else ocr[:700] + "..."
            text += (
                f"\n\n---\n\n**screen OCR ({len(ocr)} chars)**\n\n"
                f"```\n{preview}\n```"
            )
        self.output.setMarkdown(text)
        # Scroll back to the top so the user reads the answer from the start.
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.output.setTextCursor(cursor)

    # ---- /summarize (silent — triggered by hotkey, no window) ----
    # Flow: fresh capture + OCR → push to /ingest_screenshot → call
    # /summarize. The fresh capture guarantees the summary reflects the
    # moment the hotkey was pressed, not whatever the 7.5-min background
    # worker happened to have captured last.

    def silent_summarize(self):
        # Don't pile up requests if one is already running.
        if (self.worker and self.worker.isRunning()) or (
            self.stream_worker and self.stream_worker.isRunning()
        ):
            self._notify("Daemon", "already busy with a request", warning=False)
            return

        self._notify("Francis", "capturing + summarizing...", warning=False)
        self.worker = SilentSummarizeWorker()
        self.worker.succeeded.connect(self._on_silent_summarize_result)
        self.worker.failed.connect(self._on_silent_summarize_error)
        self.worker.start()

    def _on_silent_summarize_result(self, payload: dict):
        note_path = payload.get("note_path", "?")
        inputs = payload.get("inputs", 0)
        self._notify(
            "Francis",
            f"wrote summary ({inputs} captures): {note_path}",
            warning=False,
        )

    def _on_silent_summarize_error(self, msg: str):
        self._notify("Francis", f"summary failed: {msg}", warning=True)

    def _notify(self, title: str, message: str, warning: bool = False):
        if self.tray is None:
            return
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.tray.showMessage(title, message, icon, 4000)

    # ---- shared error handler ----

    def _on_error(self, msg: str):
        self.status.setText("error")
        self.output.setMarkdown(
            f"**request failed**\n\n```\n{msg}\n```\n\n"
            f"check that the API is running on the PC and reachable:\n\n"
            f"```\ncurl {config.API_URL}/health\n```"
        )


# ============================================================
# Capture window — Alt+Shift+C: user paragraph + fresh OCR → vault file
# ============================================================

class CaptureTextEdit(QTextEdit):
    """QTextEdit variant: Ctrl+Enter submits, Esc dismisses, plain Enter
    still inserts a newline so the user can write multi-paragraph notes.
    """

    submitted = pyqtSignal()
    escaped = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.submitted.emit()
                event.accept()
                return
        super().keyPressEvent(event)


class CaptureWindow(QWidget):
    """Floating overlay for Alt+Shift+C.

    On summon: fires a fresh screen capture + OCR on a worker thread,
    shows a multi-line input where the user types their own annotation.
    On submit (Ctrl+Enter): writes a single markdown file to
    90_Francis/captures/YYYY-MM-DD/HH-MM-SS.md containing both the user's
    note and the OCR text, as one file. Also pushes the OCR into the
    screenshots table so /summarize sees it too.
    """

    def __init__(self):
        super().__init__()
        self.ocr_worker: CaptureOcrWorker | None = None
        self.tray: QSystemTrayIcon | None = None
        self._ocr_text: str = ""
        self._ocr_ts: float = 0.0
        self._ocr_ready: bool = False
        # Same visibility-lock refresher as OverlayWindow — capture window
        # also occludes the screen and must not be OCR'd by the background.
        self._visibility_timer = QTimer(self)
        self._visibility_timer.setInterval(30 * 1000)
        self._visibility_timer.timeout.connect(mark_overlay_visible)
        self._build_ui()

    def _build_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(560, 320)

        root = DraggableContainer(self)
        root.setObjectName("root")
        root.setGeometry(0, 0, self.width(), self.height())

        # Matches OverlayWindow — soft near-black panel, hairline border,
        # 14px corners, system sans, grayscale with one subtle blue accent.
        root.setStyleSheet(
            """
            QWidget#root {
                background-color: #161616;
                border: 1px solid #2a2a2a;
                border-radius: 14px;
            }
            QTextEdit, QLabel {
                font-family: "Segoe UI Variable Display", "Segoe UI",
                             "Inter", -apple-system, BlinkMacSystemFont,
                             "Helvetica Neue", sans-serif;
            }
            QLabel#prompt {
                color: #6a6a6a;
                font-size: 11px;
                font-weight: 600;
                letter-spacing: 0.5px;
                padding: 0;
            }
            QTextEdit#note {
                background-color: transparent;
                color: #f0f0f0;
                border: none;
                padding: 0;
                font-size: 15px;
                selection-background-color: #2e3a5c;
                selection-color: #f0f0f0;
            }
            QLabel#status {
                color: #6a6a6a;
                font-size: 10px;
                padding: 0;
            }
            QLabel#hint {
                color: #6a6a6a;
                font-size: 10px;
                padding: 0;
            }
            """
        )

        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        self.prompt_label = QLabel("CAPTURE")
        self.prompt_label.setObjectName("prompt")
        layout.addWidget(self.prompt_label)

        self.note_input = CaptureTextEdit()
        self.note_input.setObjectName("note")
        self.note_input.setPlaceholderText(
            "what's happening? — what you're thinking, deciding, working on..."
        )
        self.note_input.submitted.connect(self._on_submit)
        self.note_input.escaped.connect(self.hide)
        layout.addWidget(self.note_input, stretch=1)

        status_row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("status")
        status_row.addWidget(self.status, stretch=1)

        self.hint = QLabel("Ctrl+Enter to save  ·  Esc to cancel")
        self.hint.setObjectName("hint")
        status_row.addWidget(self.hint)
        layout.addLayout(status_row)

        esc = QShortcut(QKeySequence("Escape"), self)
        esc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        esc.activated.connect(self.hide)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    # ---- summon / position memory ----

    def summon(self):
        # Reset per-summon state
        self._ocr_text = ""
        self._ocr_ts = 0.0
        self._ocr_ready = False
        self.note_input.clear()
        self.status.setText("capturing screen...")

        # STEP 1: Grab pixels synchronously BEFORE the window is shown.
        # This is the whole point — if we showed the window first, the OCR
        # would include the green capture box and the user's placeholder
        # text, not the actual screen content they're annotating. mss.grab()
        # on a single monitor is ~30-50 ms, imperceptible to the user.
        try:
            ts, img = capture_screen()
        except Exception as e:
            print(f"screen capture failed: {e}", file=sys.stderr)
            ts, img = dt.datetime.now(), None

        # STEP 2: Now show the window.
        state = load_overlay_state()
        if "capture_x" in state and "capture_y" in state:
            self.move(state["capture_x"], state["capture_y"])
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            x = screen.center().x() - self.width() // 2
            y = screen.center().y() - self.height() // 2 - 80
            self.move(x, y)

        self.show()
        self.raise_()
        self.activateWindow()
        self.note_input.setFocus()

        # STEP 3: Fire the OCR worker with the already-captured image.
        # OCR runs in parallel with the user typing; by the time they hit
        # Ctrl+Enter, _ocr_ready is almost always already true.
        if img is None:
            self._on_capture_failed("screen capture returned no image")
            return
        self.ocr_worker = CaptureOcrWorker(ts, img)
        self.ocr_worker.succeeded.connect(self._on_capture_done)
        self.ocr_worker.failed.connect(self._on_capture_failed)
        self.ocr_worker.start()

    def showEvent(self, event):
        mark_overlay_visible()
        self._visibility_timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._visibility_timer.stop()
        pos = self.pos()
        save_overlay_state({"capture_x": pos.x(), "capture_y": pos.y()})
        super().hideEvent(event)

    # ---- capture worker callbacks ----

    def _on_capture_done(self, ts: float, text: str):
        self._ocr_ts = ts
        self._ocr_text = text
        self._ocr_ready = True
        chars = len(text.strip())
        if chars:
            self.status.setText(f"captured — {chars} chars of screen context")
        else:
            self.status.setText("captured — (no OCR text extracted)")

    def _on_capture_failed(self, msg: str):
        # Allow submit anyway; we'll just save a note with no OCR block.
        self._ocr_ready = True
        self.status.setText(f"capture failed — will save note without OCR")
        print(f"capture failed: {msg}", file=sys.stderr)

    # ---- submit → hide window, then write file (wait for OCR if needed) ----

    def _on_submit(self):
        note_text = self.note_input.toPlainText().strip()
        if not note_text:
            return

        # Hide the window FIRST so it feels instant — the user is done with
        # the UI the moment they hit Ctrl+Enter.
        self.note_input.clear()
        self.hide()

        if self._ocr_ready:
            # Fast path: OCR already came back, write inline.
            self._save_capture(note_text, self._ocr_text)
            return

        # Slow path: OCR worker is still running. Rebind its signals to a
        # one-shot handler that writes the file with THIS note text. We
        # disconnect the window's own handlers first so a subsequent
        # summon()'s new worker can't race with this one via shared state.
        worker = self.ocr_worker
        if worker is None:
            # Shouldn't happen (summon() always starts a worker), but be
            # safe — save with no OCR rather than losing the note.
            self._save_capture(note_text, "")
            return

        try:
            worker.succeeded.disconnect(self._on_capture_done)
            worker.failed.disconnect(self._on_capture_failed)
        except TypeError:
            pass  # already disconnected

        def on_late_done(_ts: float, ocr_text: str):
            self._save_capture(note_text, ocr_text)

        def on_late_fail(msg: str):
            print(f"deferred capture OCR failed: {msg}", file=sys.stderr)
            self._save_capture(note_text, "")

        worker.succeeded.connect(on_late_done)
        worker.failed.connect(on_late_fail)

    def _save_capture(self, note_text: str, ocr_text: str) -> None:
        """Write the combined user-note + OCR file and send a tray notification."""
        try:
            rel_path = self._write_capture_file(note_text, ocr_text)
            self._notify("Francis", f"saved capture: {rel_path}", warning=False)
        except Exception as e:
            self._notify("Francis", f"capture save failed: {e}", warning=True)
            print(f"capture save failed: {e}", file=sys.stderr)

    def _write_capture_file(self, note_text: str, ocr_text: str) -> str:
        """Write the combined user-note + OCR file into the vault.

        Path: 90_Francis/captures/YYYY-MM-DD/HH-MM-SS.md. Seconds are in the
        filename (not just HH-MM like /summarize) because a user could fire
        Alt+Shift+C twice in the same minute.
        """
        now = dt.datetime.now()
        date_dir = (
            config.VAULT_PATH / "90_Francis" / "captures" / now.strftime("%Y-%m-%d")
        )
        date_dir.mkdir(parents=True, exist_ok=True)
        filename = now.strftime("%H-%M-%S") + ".md"
        file_path = date_dir / filename

        ocr_body = ocr_text.strip()
        has_ocr = bool(ocr_body)

        content = (
            f"---\n"
            f"type: user_capture\n"
            f"author: user\n"
            f"created_at: {now.isoformat(timespec='seconds')}\n"
            f"has_ocr: {str(has_ocr).lower()}\n"
            f"---\n\n"
            f"# Note\n\n"
            f"{note_text}\n"
        )
        if has_ocr:
            content += f"\n## Screen context\n\n{ocr_body}\n"

        file_path.write_text(content, encoding="utf-8")
        try:
            return str(file_path.relative_to(config.VAULT_PATH))
        except ValueError:
            return str(file_path)

    # ---- tray notification helper ----

    def _notify(self, title: str, message: str, warning: bool = False):
        if self.tray is None:
            return
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.tray.showMessage(title, message, icon, 4000)


# ============================================================
# Tray icon (programmatically generated, no asset file needed)
# ============================================================

def make_tray_icon() -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#5a5aff"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 24, 24)
    p.end()
    return QIcon(pixmap)


# ============================================================
# Main
# ============================================================

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # closing the window only hides it

    overlay = OverlayWindow()
    capture = CaptureWindow()

    # ---- Tray icon and menu ----
    tray = QSystemTrayIcon(make_tray_icon())
    tray.setToolTip(
        f"Daemon — Francis  ({config.HOTKEY_LABEL} ask, "
        f"{config.HOTKEY_SILENT_LABEL} summary, "
        f"{config.HOTKEY_CAPTURE_LABEL} capture)"
    )

    menu = QMenu()

    show_action = QAction(f"Open Francis ({config.HOTKEY_LABEL})")
    show_action.triggered.connect(overlay.show_centered)
    menu.addAction(show_action)

    capture_action = QAction(f"Capture moment ({config.HOTKEY_CAPTURE_LABEL})")
    capture_action.triggered.connect(capture.summon)
    menu.addAction(capture_action)

    silent_action = QAction(
        f"Summarize last hour silently ({config.HOTKEY_SILENT_LABEL})"
    )
    silent_action.triggered.connect(overlay.silent_summarize)
    menu.addAction(silent_action)

    menu.addSeparator()

    quit_action = QAction("Quit")
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: overlay.show_centered()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick
        else None
    )
    tray.show()

    # Make the tray accessible to both windows so they can show balloon
    # notifications (silent_summarize, capture save confirmations).
    overlay.tray = tray
    capture.tray = tray

    # ---- Hotkey 1: toggle the overlay (open if hidden, close if shown) ----
    open_bridge = HotkeyBridge()
    open_bridge.triggered.connect(overlay.toggle)

    open_hotkey = GlobalHotkeyThread(
        modifiers=config.HOTKEY_MODIFIERS,
        vk=config.HOTKEY_VK,
        callback=open_bridge.triggered.emit,
    )
    open_hotkey.start()

    # ---- Hotkey 2: silent summarize (with fresh capture) ----
    silent_bridge = HotkeyBridge()
    silent_bridge.triggered.connect(overlay.silent_summarize)

    silent_hotkey = GlobalHotkeyThread(
        modifiers=config.HOTKEY_SILENT_MODIFIERS,
        vk=config.HOTKEY_SILENT_VK,
        callback=silent_bridge.triggered.emit,
    )
    silent_hotkey.start()

    # ---- Hotkey 3: capture moment (user annotation + fresh OCR) ----
    capture_bridge = HotkeyBridge()
    capture_bridge.triggered.connect(capture.summon)

    capture_hotkey = GlobalHotkeyThread(
        modifiers=config.HOTKEY_CAPTURE_MODIFIERS,
        vk=config.HOTKEY_CAPTURE_VK,
        callback=capture_bridge.triggered.emit,
    )
    capture_hotkey.start()

    print(f"Daemon overlay running.")
    print(f"  {config.HOTKEY_LABEL:<16} -> open Francis")
    print(f"  {config.HOTKEY_SILENT_LABEL:<16} -> silent summarize (captures first)")
    print(f"  {config.HOTKEY_CAPTURE_LABEL:<16} -> capture moment (OCR + your note)")
    print(f"  API URL          : {config.API_URL}")
    print(f"  Right-click the tray icon for menu / quit.")

    try:
        sys.exit(app.exec())
    finally:
        open_hotkey.stop()
        silent_hotkey.stop()
        capture_hotkey.stop()


if __name__ == "__main__":
    main()
