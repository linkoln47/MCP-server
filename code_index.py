from contextlib import closing
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
SQL_DIR = SERVER_DIR / "sql"
CHUNKER_DIR = SERVER_DIR / "chunker"
# Bump when schema.sql changes: the index is a cache, so a stale one is rebuilt, not migrated.
SCHEMA_VERSION = 2

@lru_cache
def sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")

def db_path_for(project_root: Path) -> Path:
    """
    One index per project root, so pointing the server at another repository
    never mixes the two projects' chunks.
    """
    digest = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return SERVER_DIR / f".repo_index-{digest}.sqlite3"

def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.executescript(sql("reset_schema.sql"))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    conn.executescript(sql("schema.sql"))

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def run_chunker(project_root: Path, *args: str):
    proc = subprocess.run(
        ["go", "run", ".", "-root", str(project_root), *args],
        cwd=CHUNKER_DIR,
        text=True,
        capture_output=True,
        check=False,
    )

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gochunker failed")

    return json.loads(proc.stdout)

def run_go_chunker_output(project_root: Path) -> dict:
    return run_chunker(project_root, "-mode", "index")

def run_go_chunker(project_root: Path) -> list[dict]:
    data = run_go_chunker_output(project_root)
    return data.get("files") or []

def outline_go_files(project_root: Path, paths: list[Path]) -> list[dict]:
    """
    Parse Go files with go/parser and return their top-level declarations.
    """
    if not paths:
        return []

    return run_chunker(project_root.resolve(), "-mode", "outline", *(str(path) for path in paths))

def upsert_file(conn, rel_path, language, is_test, hash_, size) -> int:
    row = conn.execute(
        sql("upsert_file.sql"),
        (rel_path, language, int(is_test), hash_, size, now_utc()),
    ).fetchone()

    return int(row["id"])

def index_project(
    project_root: Path,
    db_path: Path | None = None,
    force: bool = False,
) -> dict:
    project_root = project_root.resolve()
    db_path = db_path or db_path_for(project_root)

    parsed_files = run_go_chunker(project_root)
    seen_paths: set[str] = set()

    stats = {
        "files_seen": 0,
        "test_files_seen": 0,
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
                if item.get("is_test"):
                    stats["test_files_seen"] += 1

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
                    item.get("is_test", False),
                    hash_,
                    size,
                )

                conn.execute(sql("delete_chunks_by_file.sql"), (file_id,))

                # The chunker encodes an empty Go slice as null.
                chunks = item.get("chunks") or []
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

def graph_root_rel_path(project_root: Path, root_path: str) -> str:
    clean_path = root_path.strip().strip("/")
    if not clean_path or clean_path == ".":
        return ""

    parts = Path(clean_path).parts
    if parts and parts[0] == project_root.name:
        clean_path = str(Path(*parts[1:])) if len(parts) > 1 else ""

    target = (project_root / clean_path).resolve()

    try:
        target.relative_to(project_root)
    except ValueError:
        raise ValueError("root_path escapes project root")

    if not target.exists():
        raise ValueError(f"root_path does not exist: {root_path}")
    if not target.is_dir():
        raise ValueError(f"root_path is not a directory: {root_path}")

    rel = target.relative_to(project_root).as_posix()
    return "" if rel == "." else rel

def normalize_go_package(item: dict) -> dict:
    imports: list[dict] = []
    # The chunker encodes an empty Go slice as null.
    for raw_import in item.get("imports") or []:
        if isinstance(raw_import, str):
            import_path = raw_import
            package_name = ""
        else:
            import_path = str(raw_import.get("import_path", ""))
            package_name = str(raw_import.get("package_name", ""))

        if import_path:
            imports.append(
                {
                    "import_path": import_path,
                    "package_name": package_name,
                }
            )

    imports.sort(key=lambda value: value["import_path"])

    return {
        "rel_dir": str(item.get("rel_dir", "")),
        "import_path": str(item.get("import_path", "")),
        "package_name": str(item.get("package_name", "")),
        "module_path": str(item.get("module_path", "")),
        "imports": imports,
    }

def package_in_graph_root(package: dict, graph_root: str) -> bool:
    if not graph_root:
        return True

    rel_dir = package.get("rel_dir", "")
    return rel_dir == graph_root or rel_dir.startswith(f"{graph_root}/")

def local_package_node_id(package: dict) -> str:
    return package.get("rel_dir") or package.get("import_path")

def is_stdlib_import(import_path: str) -> bool:
    first = import_path.split("/", 1)[0]
    return "." not in first

def package_dependency_graph(
    project_root: Path,
    root_path: str = "",
    include_external: bool = False,
    include_stdlib: bool = False,
) -> dict:
    project_root = project_root.resolve()
    graph_root = graph_root_rel_path(project_root, root_path)
    data = run_go_chunker_output(project_root)

    packages = [
        normalize_go_package(item)
        for item in data.get("packages") or []
        if item.get("import_path")
    ]
    packages_by_import = {
        package["import_path"]: package
        for package in packages
    }
    selected_packages = [
        package
        for package in packages
        if package_in_graph_root(package, graph_root)
    ]

    nodes_by_id: dict[str, dict] = {}
    edges_by_key: dict[tuple[str, str, str], dict] = {}
    source_ids: set[str] = set()

    def add_local_node(package: dict) -> str:
        node_id = local_package_node_id(package)
        nodes_by_id[node_id] = {
            "id": node_id,
            "kind": "local",
            "label": package.get("rel_dir") or package.get("import_path"),
            "rel_dir": package.get("rel_dir"),
            "import_path": package.get("import_path"),
            "package_name": package.get("package_name"),
            "module_path": package.get("module_path"),
        }
        return node_id

    def add_import_node(import_path: str, package_name: str, kind: str) -> str:
        node_id = import_path
        nodes_by_id[node_id] = {
            "id": node_id,
            "kind": kind,
            "label": import_path,
            "rel_dir": "",
            "import_path": import_path,
            "package_name": package_name,
            "module_path": "",
        }
        return node_id

    for package in selected_packages:
        source_id = add_local_node(package)
        source_ids.add(source_id)

        for package_import in package["imports"]:
            import_path = package_import["import_path"]
            target_package = packages_by_import.get(import_path)

            if target_package is not None:
                target_id = add_local_node(target_package)
                edge_kind = "local"
            elif is_stdlib_import(import_path):
                if not include_stdlib:
                    continue
                target_id = add_import_node(
                    import_path,
                    package_import.get("package_name", ""),
                    "stdlib",
                )
                edge_kind = "stdlib"
            else:
                if not include_external:
                    continue
                target_id = add_import_node(
                    import_path,
                    package_import.get("package_name", ""),
                    "external",
                )
                edge_kind = "external"

            edge_key = (source_id, target_id, import_path)
            edges_by_key[edge_key] = {
                "from": source_id,
                "to": target_id,
                "import_path": import_path,
                "kind": edge_kind,
            }

    kind_order = {"local": 0, "external": 1, "stdlib": 2}
    nodes = sorted(
        nodes_by_id.values(),
        key=lambda node: (kind_order.get(node["kind"], 99), node["id"]),
    )
    edges = sorted(
        edges_by_key.values(),
        key=lambda edge: (edge["from"], edge["to"], edge["import_path"]),
    )

    return {
        "root_path": graph_root or ".",
        "include_external": include_external,
        "include_stdlib": include_stdlib,
        "packages": len(selected_packages),
        "nodes": nodes,
        "edges": edges,
        "sources": sorted(source_ids),
    }

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

def search_index(
    project_root: Path,
    query: str,
    limit: int = 10,
    include_tests: bool = False,
    db_path: Path | None = None,
) -> list[dict]:
    db_path = db_path or db_path_for(project_root)

    with closing(connect(db_path)) as conn:
        init_db(conn)

        rows = conn.execute(
            sql("search_index.sql"),
            (fts_query(query), int(include_tests), limit),
        ).fetchall()

        return [dict(row) for row in rows]

def get_chunk(project_root: Path, chunk_id: int, db_path: Path | None = None) -> dict | None:
    db_path = db_path or db_path_for(project_root)

    with closing(connect(db_path)) as conn:
        init_db(conn)

        row = conn.execute(sql("get_chunk.sql"), (chunk_id,)).fetchone()
        return dict(row) if row else None
