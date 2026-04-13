"""
Daemon FastAPI backend.

Endpoints:
- GET  /health  → status + DB stats + model info
- POST /ask     → semantic search over the vault + LLM answer

Run on the PC where Ollama and the embedding DB live:
    cd daemon
    python api.py

Or directly with uvicorn for hot reload during dev:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import sys
from pathlib import Path

# When launched under pythonw.exe (autostart, no console), sys.stdout is None
# and any print() / uvicorn log will crash. Redirect to a per-script log file
# BEFORE any other imports run. No-op when running interactively under python.
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
import re
import sqlite3
import time
from typing import Optional

import requests
import sqlite_vec
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config


# ============================================================
# DB (read-side; the embedding worker is the writer)
# ============================================================

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")  # play nicely with the worker
    # Screenshots table (owned by api.py — the screenshot worker POSTs here
    # via /ingest_screenshot). IF NOT EXISTS = idempotent across restarts.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS screenshots (
            id   INTEGER PRIMARY KEY,
            ts   REAL UNIQUE NOT NULL,
            text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_screenshots_ts ON screenshots(ts);
        """
    )
    conn.commit()
    return conn


db = open_db()


# ============================================================
# Embedding & retrieval
# ============================================================

def embed(text: str) -> list[float]:
    # Retry on transient connection failures. The primary case this protects
    # against is the boot-time race: Ollama and the daemon API both start at
    # user logon, and if the first /ask request arrives before Ollama has
    # finished warming up, the bare request would ConnectionError. 3 attempts
    # with a 2-second backoff covers normal warmup without masking real bugs
    # (which would surface as HTTP 4xx/5xx, not a connection error).
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(
                f"{config.OLLAMA_HOST}/api/embeddings",
                json={"model": config.EMBEDDING_MODEL, "prompt": text},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()["embedding"]
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as e:
            last_err = e
            if attempt < 2:
                print(
                    f"embed() retry {attempt + 1}/2 — Ollama not reachable: {e}",
                    file=sys.stderr,
                )
                time.sleep(2)
    assert last_err is not None
    raise last_err


def retrieve(query: str, k: int) -> list[dict]:
    """Two-pass semantic search: human notes first, then knowledge graph.

    The vault now has ~1,500 auto-generated graphify notes in
    90_Francis/knowledge_graph/ alongside ~25 human-curated notes.
    Naive top-k drowns out the human notes. Fix: pull a wider set,
    then re-rank so human-curated notes appear before auto-generated ones.
    """
    vec = embed(query)
    # Pull more candidates than requested so we can re-rank
    fetch_k = k * 4
    rows = db.execute(
        """
        SELECT
            notes.path,
            chunks.heading,
            chunks.text,
            matches.distance
        FROM (
            SELECT rowid, distance
            FROM chunk_embeddings
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
        ) AS matches
        JOIN chunks ON chunks.id = matches.rowid
        JOIN notes ON notes.id = chunks.note_id
        ORDER BY matches.distance
        """,
        (json.dumps(vec), fetch_k),
    ).fetchall()

    all_results = [
        {"path": p, "heading": h, "text": t, "distance": d}
        for p, h, t, d in rows
    ]

    # Split into human-curated vs auto-generated
    KG_PREFIX = "90_Francis" + os.sep + "knowledge_graph"
    KG_PREFIX_FWD = "90_Francis/knowledge_graph"
    human = [r for r in all_results
             if not r["path"].startswith(KG_PREFIX)
             and not r["path"].startswith(KG_PREFIX_FWD)]
    auto = [r for r in all_results
            if r["path"].startswith(KG_PREFIX)
            or r["path"].startswith(KG_PREFIX_FWD)]

    # Take human notes first (up to k), fill remainder with auto
    result = human[:k]
    remaining = k - len(result)
    if remaining > 0:
        result.extend(auto[:remaining])

    return result


# ============================================================
# LLM (Ollama)
# ============================================================

SYSTEM_PROMPT = """You are Francis, Arthur's personal AI assistant. Answer \
whatever he asks — coding, math, writing, planning, explaining concepts, \
general knowledge, anything.

Three context blocks are attached to every question:

- CURRENT SCREEN: live OCR of what Arthur is looking at right now.
- VAULT CONTEXT: chunks retrieved from his Obsidian vault.
- RECENT ACTIVITY: background screenshots from the last 30 min (only \
present when there's no fresh CURRENT SCREEN).

CRITICAL RULES for using CURRENT SCREEN:

1. If Arthur's question is short or refers to "this" / "the question" / \
"the answer" / "help me with this" / "what's the answer" / similar, the \
content of CURRENT SCREEN IS what he's asking about. Read it carefully \
and answer the question(s) you find there. Do NOT ask him to re-paste \
or re-state the question — it's already in front of you.

2. If CURRENT SCREEN contains a quiz, exam, problem set, or any list of \
questions, identify them and answer each one directly. Number your \
answers to match the questions if they're numbered.

3. If CURRENT SCREEN is messy OCR (which is normal — it picks up sidebars, \
nav, ads), focus on the largest coherent block of text and ignore the \
chrome.

4. Only ask for clarification if the screen genuinely doesn't contain \
the question (e.g. it's a blank page, a video player, or pure imagery).

For VAULT CONTEXT, cite the note when you use it (e.g. "from \
20_Projects/daemon/overview.md"). For general knowledge questions \
(math, code, definitions), answer from your own knowledge — don't \
force-fit the context.

Be concise. Arthur is busy."""


def get_recent_screenshots(minutes: int = 30, max_entries: int = 5) -> list[tuple[float, str]]:
    """Fetch the most recent screenshot OCR entries."""
    cutoff = time.time() - minutes * 60
    rows = db.execute(
        "SELECT ts, text FROM screenshots WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
        (cutoff, max_entries),
    ).fetchall()
    return list(reversed(rows))  # chronological order


def build_prompt(
    question: str,
    sources: list[dict],
    screen_text: Optional[str] = None,
) -> str:
    if sources:
        chunks = []
        for s in sources:
            header = f"[Note: {s['path']}"
            if s.get("heading"):
                header += f" — {s['heading']}"
            header += "]"
            chunks.append(f"{header}\n{s['text']}")
        context = "\n\n".join(chunks)
    else:
        context = "(no relevant notes found in the vault)"

    # CURRENT SCREEN: the freshly captured OCR (passed in from the overlay).
    # This is what the user is looking at *right now*. Goes in its own block
    # so the model can resolve "this question" / "this code" / "this" etc.
    # without having to guess which RECENT ACTIVITY entry is meant.
    has_current_screen = bool(screen_text and screen_text.strip())
    if has_current_screen:
        # Preserve line structure — quizzes, code, lists, and Q-and-A
        # layouts all depend on linebreaks to be parseable. The previous
        # `" ".join(text.split())` smushed everything into one paragraph,
        # which left the model unable to tell where one question ended
        # and the next began. We collapse horizontal whitespace per line
        # but keep the newlines.
        cleaned_lines = [
            " ".join(line.split())
            for line in screen_text.splitlines()
        ]
        normalized = "\n".join(line for line in cleaned_lines if line)
        # Bumped from 4 K to 8 K — dense pages (multi-question quizzes,
        # textbook pages, full IDE windows) routinely exceed 4 K chars.
        # 8 K ≈ 2 K tokens, comfortable inside our num_ctx of 8192.
        current_block = normalized[:8000]
    else:
        current_block = "(no fresh capture available)"

    # RECENT ACTIVITY: background captures over the last 30 min. Only
    # included when there is NO fresh CURRENT SCREEN — when the user has
    # just captured a screen explicitly, recent background captures are
    # almost always either redundant with it or stale chat-window output
    # that would feed Francis's prior answers back into the new prompt
    # (the source of the "repeats itself" failure mode). Skipping the
    # block entirely also tightens the prompt, leaving more headroom in
    # num_ctx for vault chunks that are actually relevant.
    if has_current_screen:
        activity_section = ""
    else:
        recent = get_recent_screenshots()
        if recent:
            activity_parts = []
            for ts, text in recent:
                stamp = dt.datetime.fromtimestamp(ts).strftime("%H:%M")
                snippet = " ".join(text.split())[:600]
                activity_parts.append(f"[{stamp}] {snippet}")
            activity_block = "\n\n".join(activity_parts)
        else:
            activity_block = "(no recent screenshots)"
        activity_section = (
            f"RECENT ACTIVITY (background captures, last 30 min):\n"
            f"{activity_block}\n\n"
        )

    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"CURRENT SCREEN (live OCR from the moment Arthur pressed the hotkey):\n"
        f"{current_block}\n\n"
        f"VAULT CONTEXT:\n{context}\n\n"
        f"{activity_section}"
        f"QUESTION: {question}\n\n"
        f"ANSWER:"
    )


# Ollama generation options shared by /ask and /summarize.
# - num_ctx 8192: Ollama's default is 2048 — silently truncates our prompts
#   (system + 6 vault chunks + 4 KB CURRENT SCREEN + RECENT ACTIVITY easily
#   exceeds 2 K tokens). Llama 3.1 supports 128 K so 8 K is plenty of room.
# - repeat_penalty 1.15: nudge above the 1.1 default to suppress the
#   "regurgitate the same answer" failure mode when context overlaps.
# - num_predict 512: cap response length. Francis answers are short by design.
# - stop: prevent the model from continuing into a fake follow-up turn.
LLM_OPTIONS = {
    "num_ctx": 8192,
    "repeat_penalty": 1.15,
    "num_predict": 512,
    "stop": ["\n\nQUESTION:", "\n\nANSWER:"],
}


def call_llm(prompt: str) -> str:
    r = requests.post(
        f"{config.OLLAMA_HOST}/api/generate",
        json={
            "model": config.LLM_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": LLM_OPTIONS,
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["response"].strip()


# ============================================================
# FastAPI app
# ============================================================

app = FastAPI(title="Daemon API")


class AskRequest(BaseModel):
    question: str
    k: int = 6
    # Fresh OCR text from the moment the user pressed the hotkey. Distinct
    # from the background screenshot stream — when present, this is "the
    # screen the user is asking about right now" and gets its own labeled
    # block in the prompt so the model doesn't have to guess which of the
    # last 5 captures is the deictic referent for "this question" / "this".
    screen_text: Optional[str] = None


class Source(BaseModel):
    path: str
    heading: Optional[str] = None
    distance: float
    snippet: str


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]


@app.get("/health")
def health():
    n_notes = db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    n_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_screenshots = db.execute("SELECT COUNT(*) FROM screenshots").fetchone()[0]
    return {
        "status": "ok",
        "vault_notes": n_notes,
        "vault_chunks": n_chunks,
        "screenshots": n_screenshots,
        "llm_model": config.LLM_MODEL,
        "embedding_model": config.EMBEDDING_MODEL,
    }


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    sources = retrieve(req.question, req.k)
    prompt = build_prompt(req.question, sources, req.screen_text)
    answer = call_llm(prompt)

    return AskResponse(
        answer=answer,
        sources=[
            Source(
                path=s["path"],
                heading=s["heading"],
                distance=s["distance"],
                snippet=" ".join(s["text"].split())[:280],
            )
            for s in sources
        ],
    )


# ============================================================
# Streaming /ask
# ============================================================
# Returns NDJSON: one JSON object per line. The first line carries the
# retrieved sources; subsequent lines carry response chunks; the final
# line is {"done": true}. The overlay reads this incrementally so it can
# render tokens as Llama produces them.

def stream_llm(prompt: str):
    """Yield response chunks from Ollama as they're generated."""
    r = requests.post(
        f"{config.OLLAMA_HOST}/api/generate",
        json={
            "model": config.LLM_MODEL,
            "prompt": prompt,
            "stream": True,
            "options": LLM_OPTIONS,
        },
        stream=True,
        timeout=180,
    )
    r.raise_for_status()
    for line in r.iter_lines():
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        chunk = obj.get("response", "")
        if chunk:
            yield chunk
        if obj.get("done"):
            break


@app.post("/ask/stream")
def ask_stream(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is required")

    sources = retrieve(req.question, req.k)
    prompt = build_prompt(req.question, sources, req.screen_text)

    def generate():
        # Send sources first so the overlay can show them while the answer
        # is still streaming.
        sources_payload = {
            "sources": [
                {
                    "path": s["path"],
                    "heading": s["heading"],
                    "distance": s["distance"],
                    "snippet": " ".join(s["text"].split())[:280],
                }
                for s in sources
            ]
        }
        yield json.dumps(sources_payload) + "\n"

        for chunk in stream_llm(prompt):
            yield json.dumps({"chunk": chunk}) + "\n"

        yield json.dumps({"done": True}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ============================================================
# Screenshot ingestion
# ============================================================

class IngestScreenshotRequest(BaseModel):
    ts: float
    text: str


@app.post("/ingest_screenshot")
def ingest_screenshot(req: IngestScreenshotRequest):
    db.execute(
        "INSERT OR IGNORE INTO screenshots (ts, text) VALUES (?, ?)",
        (req.ts, req.text),
    )
    db.commit()
    return {"status": "ok"}


# ============================================================
# Session summarization
# ============================================================

# Regex-based sensitivity scrub. Not bulletproof — catches the obvious
# things in OCR text (card-like digit runs, SSNs, labeled credentials,
# bearer tokens) before the text reaches Llama. A determined attacker
# OR an unlucky screen capture could still leak secrets; this is defense
# in depth, not a guarantee.
SECRET_PATTERNS = [
    (re.compile(r"\b\d{13,19}\b"), "[REDACTED_DIGITS]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[-_]?key|auth)[=:]\s*\S+"
        ),
        "[REDACTED_CRED]",
    ),
    (re.compile(r"(?i)bearer\s+[a-z0-9._\-=]+"), "[REDACTED_BEARER]"),
]


def scrub(text: str) -> str:
    for pat, rep in SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


SUMMARIZE_INSTRUCTION = (
    "You are Francis, Arthur's personal AI assistant. "
    "Below is a series of OCR-extracted text fragments from screenshots taken "
    "every ~5 minutes of Arthur's computer screen over the last {minutes} "
    "minutes.\n\n"
    "Your task: write a short, honest summary of what Arthur appears to have "
    "been doing. Focus on:\n"
    "- The main topic(s) or task(s)\n"
    "- Any specific files, documents, apps, or websites he interacted with\n"
    "- Transitions between activities (if any)\n\n"
    "Rules:\n"
    "- Write in past tense (\"Arthur was working on...\").\n"
    "- Be concise — 3 to 6 bullet points total.\n"
    "- If the OCR is too sparse or garbled to tell what was happening, say so "
    "honestly. Do NOT invent activities.\n"
    "- Do NOT include the raw OCR fragments in your output.\n"
    "- Ignore any [REDACTED_*] tokens — they were scrubbed intentionally."
)


def build_summarize_prompt(entries: list[tuple[float, str]], minutes: int) -> str:
    fragments = []
    for ts, text in entries:
        if not text.strip():
            continue
        stamp = dt.datetime.fromtimestamp(ts).strftime("%H:%M")
        snippet = " ".join(text.split())[:800]
        fragments.append(f"[{stamp}]\n{snippet}")
    frag_block = "\n\n".join(fragments) if fragments else "(no OCR data)"
    return (
        SUMMARIZE_INSTRUCTION.format(minutes=minutes)
        + "\n\nOCR FRAGMENTS:\n"
        + frag_block
        + "\n\nSUMMARY:"
    )


def write_session_note(
    summary: str,
    start_ts: float,
    end_ts: float,
    source_count: int,
    minutes: int,
) -> Path:
    now = dt.datetime.now()
    start = dt.datetime.fromtimestamp(start_ts)
    end = dt.datetime.fromtimestamp(end_ts)

    date_dir = (
        config.VAULT_PATH / "90_Francis" / "sessions" / now.strftime("%Y-%m-%d")
    )
    date_dir.mkdir(parents=True, exist_ok=True)

    filename = now.strftime("%H-%M") + ".md"
    note_path = date_dir / filename

    content = (
        f"---\n"
        f"type: session_summary\n"
        f"author: francis\n"
        f"generated_at: {now.isoformat(timespec='seconds')}\n"
        f"start: {start.isoformat(timespec='seconds')}\n"
        f"end: {end.isoformat(timespec='seconds')}\n"
        f"window_minutes: {minutes}\n"
        f"source_count: {source_count}\n"
        f"model: {config.LLM_MODEL}\n"
        f"---\n\n"
        f"# Session summary ({start.strftime('%H:%M')}"
        f"–{end.strftime('%H:%M')})\n\n"
        f"{summary}\n"
    )
    note_path.write_text(content, encoding="utf-8")
    return note_path


class SummarizeRequest(BaseModel):
    minutes: int = 60


class SummarizeResponse(BaseModel):
    summary: str
    note_path: str
    inputs: int


@app.post("/summarize", response_model=SummarizeResponse)
def summarize(req: SummarizeRequest):
    if req.minutes < 1 or req.minutes > 24 * 60:
        raise HTTPException(
            status_code=400, detail="minutes must be between 1 and 1440"
        )

    cutoff = time.time() - req.minutes * 60
    rows = db.execute(
        "SELECT ts, text FROM screenshots WHERE ts >= ? ORDER BY ts",
        (cutoff,),
    ).fetchall()

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no screenshots ingested in the last {req.minutes} minutes — "
                "is the screenshot worker running and pushing to "
                "/ingest_screenshot?"
            ),
        )

    entries = [(ts, scrub(text)) for ts, text in rows]
    prompt = build_summarize_prompt(entries, req.minutes)
    summary = call_llm(prompt)

    note_path = write_session_note(
        summary=summary,
        start_ts=entries[0][0],
        end_ts=entries[-1][0],
        source_count=len(entries),
        minutes=req.minutes,
    )

    return SummarizeResponse(
        summary=summary,
        note_path=str(note_path.relative_to(config.VAULT_PATH)),
        inputs=len(entries),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host=config.API_HOST, port=config.API_PORT, reload=False)
