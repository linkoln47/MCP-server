from contextlib import closing
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"
DB_NAME = ".repo_index.sqlite3"

@lru_cache
def sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")

def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(sql("schema.sql"))

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def run_go_chunker(project_root: Path) -> list[dict]:
    helper_dir = Path(__file__).resolve().parent / "chunker"
    proc = subprocess.run(
        ["go", "run", ".", "-root", str(project_root)],
        cwd=helper_dir,
        text=True,
        capture_output=True,
        check=False,
    )

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gochunker failed")

    data = json.loads(proc.stdout)
    return data.get("files", [])

def upsert_file(conn, rel_path, language, hash_, size) -> int:
    row = conn.execute(
        sql("upsert_file.sql"),
        (rel_path, language, hash_, size, now_utc()),
    ).fetchone()

    return int(row["id"])

def index_project(
    project_root: Path,
    db_path: Path | None = None,
    force: bool = False,
) -> dict:
    project_root = project_root.resolve()
    db_path = db_path or (Path(__file__).resolve().parent / DB_NAME)

    parsed_files = run_go_chunker(project_root)
    seen_paths: set[str] = set()

    stats = {
        "files_seen": 0,
        "files_indexed": 0,
        "files_skipped": 0,
        "files_deleted": 0,
        "chunks_indexed": 0,
    }

    with closing(connect(db_path)) as conn:
        init_db(conn)

        with conn:
            for item in parsed_files:
                rel_path = item["path"]
                abs_path = project_root / rel_path

                if not abs_path.is_file():
                    continue

                seen_paths.add(rel_path)
                stats["files_seen"] += 1

                hash_ = file_hash(abs_path)
                size = abs_path.stat().st_size

                existing = conn.execute(
                    sql("select_file.sql"),
                    (rel_path,),
                ).fetchone()

                if existing and existing["hash"] == hash_ and not force:
                    stats["files_skipped"] += 1
                    continue

                file_id = upsert_file(
                    conn,
                    rel_path,
                    item.get("language", "go"),
                    hash_,
                    size,
                )

                conn.execute(sql("delete_chunks_by_file.sql"), (file_id,))

                chunks = item.get("chunks", [])
                conn.executemany(
                    sql("insert_chunk.sql"),
                    [
                        (
                            file_id,
                            chunk["kind"],
                            chunk.get("symbol"),
                            item.get("package_name"),
                            chunk["start_line"],
                            chunk["end_line"],
                            chunk["content"],
                        )
                        for chunk in chunks
                    ],
                )

                stats["files_indexed"] += 1
                stats["chunks_indexed"] += len(chunks)

            indexed_files = conn.execute(sql("list_indexed_go_files.sql")).fetchall()

            for row in indexed_files:
                rel_path = row["path"]
                abs_path = project_root / rel_path

                if rel_path not in seen_paths and not abs_path.exists():
                    conn.execute(sql("delete_stale_file.sql"), (row["id"],))
                    stats["files_deleted"] += 1

    return stats

def fts_query(query: str) -> str:
    raw_tokens = re.findall(r"[A-Za-z0-9_]+", query)
    if not raw_tokens:
        raise ValueError("empty search query")

    parts: list[str] = []
    last_was_operator = True
    seen_terms: set[str] = set()

    for raw in raw_tokens[:16]:
        lower = raw.lower()

        if lower == "or":
            if not last_was_operator and parts:
                parts.append("OR")
                last_was_operator = True
            continue

        if lower in {"and", "not"}:
            continue

        term = lower
        if term in seen_terms:
            continue

        seen_terms.add(term)

        if not last_was_operator and parts:
            parts.append("AND")

        parts.append(f"{term}*")
        last_was_operator = False

    while parts and parts[-1] == "OR":
        parts.pop()

    if not parts:
        raise ValueError("empty search query")

    return " ".join(parts)

def search_index(query: str, limit: int = 10, db_path: Path | None = None) -> list[dict]:
    db_path = db_path or (Path(__file__).resolve().parent / DB_NAME)

    with closing(connect(db_path)) as conn:
        init_db(conn)

        rows = conn.execute(
            sql("search_index.sql"),
            (fts_query(query), limit),
        ).fetchall()

        return [dict(row) for row in rows]

def get_chunk(chunk_id: int, db_path: Path | None = None) -> dict | None:
    db_path = db_path or (Path(__file__).resolve().parent / DB_NAME)

    with closing(connect(db_path)) as conn:
        init_db(conn)

        row = conn.execute(sql("get_chunk.sql"), (chunk_id,)).fetchone()
        return dict(row) if row else None
