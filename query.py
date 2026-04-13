"""
Semantic search test for the daemon vault embeddings.

Usage:
    python query.py "your question here"
    python query.py "your question here" --k 3

Run on the PC where the embedding worker has been running (same DB, same Ollama).
"""

import argparse
import json
import sqlite3
import sys

import requests
import sqlite_vec

import config


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def embed(text: str) -> list[float]:
    r = requests.post(
        f"{config.OLLAMA_HOST}/api/embeddings",
        json={"model": config.EMBEDDING_MODEL, "prompt": text},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def search(conn: sqlite3.Connection, query: str, k: int):
    """Top-k semantic search. Returns list of (path, heading, text, distance)."""
    vec = embed(query)
    return conn.execute(
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
        (json.dumps(vec), k),
    ).fetchall()


def main():
    parser = argparse.ArgumentParser(description="Semantic search over the vault index.")
    parser.add_argument("query", help="search query (in quotes)")
    parser.add_argument("--k", type=int, default=5, help="top-k results (default 5)")
    args = parser.parse_args()

    conn = open_db()

    # Sanity: how much is in the DB?
    n_notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"DB: {n_chunks} chunks across {n_notes} notes")
    print()

    if n_chunks == 0:
        print("No embeddings in the DB. Run embedding_worker.py first to index your vault.")
        sys.exit(1)

    print(f"Query: {args.query!r}")
    print(f"Top {args.k} matches:")
    print("-" * 70)

    rows = search(conn, args.query, args.k)
    if not rows:
        print("(no matches)")
        return

    for i, (path, heading, text, distance) in enumerate(rows, 1):
        print(f"\n[{i}] distance={distance:.4f}  {path}")
        if heading:
            print(f"    heading: {heading}")
        # Single-line snippet for compact output
        snippet = " ".join(text.split())[:280]
        if len(text) > 280:
            snippet += "..."
        print(f"    {snippet}")

    print()
    conn.close()


if __name__ == "__main__":
    main()
