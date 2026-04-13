"""
Vault embedding worker — keeps a sqlite-vec embedding index in sync with the
Obsidian vault.

Design:
- On startup: full scan of the vault. For each .md file, hash it and compare
  to what's in the DB. Re-embed only if it changed (idempotent restarts).
- Then: watch the vault with watchdog and incrementally re-embed on changes.
- Embeddings come from Ollama (nomic-embed-text) running locally on the PC.
- Storage: SQLite + sqlite-vec virtual table for vector search.

Run on the PC where Ollama lives:
    cd daemon
    python embedding_worker.py
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

import hashlib
import json
import re
import sqlite3
import time

import requests
import sqlite_vec
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import config


# ============================================================
# Database
# ============================================================

def open_db() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: full_scan runs on the main thread, but watchdog
    # callbacks fire on a worker thread. Watchdog serializes its callbacks and
    # the main thread is idle after the scan, so there's no concurrent access —
    # we just need Python to stop guarding.
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # WAL mode lets the API process read while the worker writes without
    # "database is locked" errors. The pragma persists in the DB file, so
    # setting it here once is enough — but we set it on every connection
    # anyway because it's idempotent and cheap.
    conn.execute("PRAGMA journal_mode=WAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id           INTEGER PRIMARY KEY,
            path         TEXT UNIQUE NOT NULL,   -- vault-relative path
            file_hash    TEXT NOT NULL,           -- sha256 of file bytes
            last_indexed REAL NOT NULL            -- unix timestamp
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id           INTEGER PRIMARY KEY,
            note_id      INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            chunk_index  INTEGER NOT NULL,
            heading      TEXT,                    -- nullable: chunks before any heading
            text         TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_note ON chunks(note_id);
        """
    )
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_embeddings USING vec0(
            embedding float[{config.EMBEDDING_DIM}]
        )
        """
    )
    conn.commit()


# ============================================================
# Vault scanning
# ============================================================

def vault_files() -> list[Path]:
    """All .md files in the vault, excluding skip dirs."""
    files = []
    for p in config.VAULT_PATH.rglob("*.md"):
        rel_parts = p.relative_to(config.VAULT_PATH).parts
        if any(part in config.VAULT_SKIP_DIRS for part in rel_parts):
            continue
        files.append(p)
    return files


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ============================================================
# Markdown chunking
# ============================================================

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def chunk_markdown(text: str) -> list[tuple[str | None, str]]:
    """
    Split markdown into (heading, text) chunks.
    Strategy: split on heading lines. If a section is too long, sub-split by
    blank-line-separated paragraphs, packing paragraphs until MAX_CHUNK_CHARS.
    Sections without a preceding heading get heading=None.
    """
    if not text.strip():
        return []

    sections: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if current_lines:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, "\n".join(current_lines).strip()))

    sections = [(h, t) for h, t in sections if t]

    chunks: list[tuple[str | None, str]] = []
    for heading, body in sections:
        if len(body) <= config.MAX_CHUNK_CHARS:
            chunks.append((heading, body))
            continue
        # Sub-split long sections by paragraph
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        buf = ""
        for para in paras:
            if buf and len(buf) + len(para) + 2 > config.MAX_CHUNK_CHARS:
                chunks.append((heading, buf.strip()))
                buf = para
            else:
                buf = (buf + "\n\n" + para) if buf else para
        if buf:
            chunks.append((heading, buf.strip()))

    return chunks


# ============================================================
# Embedding via Ollama
# ============================================================

def embed(text: str) -> list[float]:
    r = requests.post(
        f"{config.OLLAMA_HOST}/api/embeddings",
        json={"model": config.EMBEDDING_MODEL, "prompt": text},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["embedding"]


# ============================================================
# Indexing
# ============================================================

def index_file(conn: sqlite3.Connection, path: Path) -> str:
    """Embed a single file. Returns 'new' | 'updated' | 'unchanged' | 'skipped'."""
    rel = str(path.relative_to(config.VAULT_PATH))
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        print(f"  ! skipped {rel}: {e}")
        return "skipped"

    file_hash = hash_file(path)

    row = conn.execute(
        "SELECT id, file_hash FROM notes WHERE path = ?", (rel,)
    ).fetchone()
    if row and row[1] == file_hash:
        return "unchanged"

    chunks = chunk_markdown(text)
    if not chunks:
        return "skipped"

    if row:
        note_id = row[0]
        conn.execute(
            "DELETE FROM chunk_embeddings WHERE rowid IN "
            "(SELECT id FROM chunks WHERE note_id = ?)",
            (note_id,),
        )
        conn.execute("DELETE FROM chunks WHERE note_id = ?", (note_id,))
        conn.execute(
            "UPDATE notes SET file_hash = ?, last_indexed = ? WHERE id = ?",
            (file_hash, time.time(), note_id),
        )
        result = "updated"
    else:
        cur = conn.execute(
            "INSERT INTO notes (path, file_hash, last_indexed) VALUES (?, ?, ?)",
            (rel, file_hash, time.time()),
        )
        note_id = cur.lastrowid
        result = "new"

    for i, (heading, chunk_text) in enumerate(chunks):
        embed_input = f"{heading}\n\n{chunk_text}" if heading else chunk_text
        vector = embed(embed_input)
        cur = conn.execute(
            "INSERT INTO chunks (note_id, chunk_index, heading, text) "
            "VALUES (?, ?, ?, ?)",
            (note_id, i, heading, chunk_text),
        )
        chunk_id = cur.lastrowid
        conn.execute(
            "INSERT INTO chunk_embeddings (rowid, embedding) VALUES (?, ?)",
            (chunk_id, json.dumps(vector)),
        )

    conn.commit()
    return result


def remove_file(conn: sqlite3.Connection, path: Path) -> None:
    try:
        rel = str(path.relative_to(config.VAULT_PATH))
    except ValueError:
        return
    row = conn.execute("SELECT id FROM notes WHERE path = ?", (rel,)).fetchone()
    if not row:
        return
    note_id = row[0]
    conn.execute(
        "DELETE FROM chunk_embeddings WHERE rowid IN "
        "(SELECT id FROM chunks WHERE note_id = ?)",
        (note_id,),
    )
    conn.execute("DELETE FROM chunks WHERE note_id = ?", (note_id,))
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    print(f"  - removed {rel}")


def full_scan(conn: sqlite3.Connection) -> None:
    print(f"Full scan of {config.VAULT_PATH}")
    files = vault_files()
    print(f"  found {len(files)} markdown files")

    seen: set[str] = set()
    counts = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    for f in files:
        rel = str(f.relative_to(config.VAULT_PATH))
        seen.add(rel)
        result = index_file(conn, f)
        counts[result] += 1
        if result in ("new", "updated"):
            print(f"  {result}: {rel}")

    db_paths = {row[0] for row in conn.execute("SELECT path FROM notes").fetchall()}
    deleted = db_paths - seen
    for rel in deleted:
        remove_file(conn, config.VAULT_PATH / rel)

    print(f"Scan complete: {counts}, removed {len(deleted)}")


# ============================================================
# File watcher
# ============================================================

class VaultEventHandler(FileSystemEventHandler):
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _is_relevant(self, path_str: str) -> bool:
        p = Path(path_str)
        if p.suffix != ".md":
            return False
        try:
            rel_parts = p.relative_to(config.VAULT_PATH).parts
        except ValueError:
            return False
        if any(part in config.VAULT_SKIP_DIRS for part in rel_parts):
            return False
        return True

    def on_created(self, event):
        if event.is_directory or not self._is_relevant(event.src_path):
            return
        try:
            result = index_file(self.conn, Path(event.src_path))
            print(f"  + created [{result}]: {Path(event.src_path).name}")
        except Exception as e:
            print(f"  ! error on create {Path(event.src_path).name}: {e}")

    def on_modified(self, event):
        if event.is_directory or not self._is_relevant(event.src_path):
            return
        try:
            result = index_file(self.conn, Path(event.src_path))
            if result != "unchanged":
                print(f"  ~ modified [{result}]: {Path(event.src_path).name}")
        except Exception as e:
            print(f"  ! error on modify {Path(event.src_path).name}: {e}")

    def on_deleted(self, event):
        if event.is_directory or not self._is_relevant(event.src_path):
            return
        print(f"  - deleted: {Path(event.src_path).name}")
        try:
            remove_file(self.conn, Path(event.src_path))
        except Exception as e:
            print(f"  ! error: {e}")


# ============================================================
# Main
# ============================================================

def main():
    if not config.VAULT_PATH.exists():
        print(f"Vault path does not exist: {config.VAULT_PATH}")
        sys.exit(1)

    conn = open_db()
    full_scan(conn)

    print(f"\nWatching {config.VAULT_PATH} for changes (Ctrl+C to stop)...")
    observer = Observer()
    observer.schedule(VaultEventHandler(conn), str(config.VAULT_PATH), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        observer.stop()
    observer.join()
    conn.close()


if __name__ == "__main__":
    main()
