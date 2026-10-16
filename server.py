import json
import os
import re

from code_index import (
    get_chunk as index_get_chunk,
    index_project as build_code_index,
    outline_go_files,
    package_dependency_graph as build_package_dependency_graph,
    search_index as index_search,
)

from pathlib import Path
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("repo-search", instructions="A tool to search and map repository files.")

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(os.environ.get("REPO_SEARCH_ROOT", DEFAULT_PROJECT_ROOT)).expanduser().resolve()

IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    # Anikoro's dev.sh runtime state: built binaries, logs, pidfiles.
    ".dev",
}


REPO_MAP_MAX_LINES = 500

SEARCH_CODE_EXTS = {
    ".go", ".js", ".jsx", ".ts", ".tsx", ".py", ".md", ".sql", ".yaml", ".yml",
    ".css", ".scss", ".html", ".json", ".toml", ".sh", ".conf", ".nix", ".mod",
}
SEARCH_CODE_NAMES = {"Dockerfile", "Makefile", ".env.example", ".gitignore", ".dockerignore"}
# Generated or huge files that would only drown real matches.
SEARCH_CODE_SKIP_NAMES = {"package-lock.json"}
SEARCH_CODE_MAX_BYTES = 1_000_000


def is_secret_file(path: Path) -> bool:
    """
    Files that hold credentials and must never be read, whatever the allowlist says.
    """
    name = path.name.lower()
    if name == ".env.example":
        return False
    return (
        name == ".env"
        or name.startswith(".env.")
        or path.suffix.lower() in {".env", ".pem", ".key", ".p12", ".pfx"}
        or "secret" in name
        or "credential" in name
    )

def is_searchable_text(path: Path) -> bool:
    if is_secret_file(path) or path.name in SEARCH_CODE_SKIP_NAMES:
        return False
    if path.suffix not in SEARCH_CODE_EXTS and path.name not in SEARCH_CODE_NAMES:
        return False
    try:
        return path.stat().st_size <= SEARCH_CODE_MAX_BYTES
    except OSError:
        return False


def is_ignored(path: Path) -> bool:
    return any(part in IGNORE_DIRS for part in path.parts)

def format_limited(results: list[str], limit: int) -> str:
    """
    Join up to limit results; callers collect one extra item so a cut-off
    output says so instead of looking complete.
    """
    if len(results) <= limit:
        return "\n".join(results)

    return "\n".join(results[:limit] + [f"... truncated at limit={limit}; raise limit or narrow the query"])

def format_entrypoint(path: Path, kind: str, reason: str) -> str:
    return "\n".join(
        [
            f"path: {path}",
            f"kind: {kind}",
            f"reason: {reason}",
        ]
    )

FRONTEND_EXTS = (".tsx", ".ts", ".jsx", ".js")

def is_within_root(path: Path) -> bool:
    try:
        path.relative_to(PROJECT_ROOT)
    except ValueError:
        return False
    return True

def find_frontend_entrypoints() -> list[tuple[Path, str]]:
    """
    Find frontend main files the way Vite does: from the module scripts in each
    index.html. Falls back to src/main.* when no index.html names one.
    """
    found: dict[Path, str] = {}

    for html in sorted(PROJECT_ROOT.rglob("index.html")):
        if is_ignored(html) or not html.is_file():
            continue

        try:
            text = html.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for tag in re.findall(r"<script\b[^>]*>", text, re.IGNORECASE):
            if not re.search(r"""\btype\s*=\s*["']module["']""", tag, re.IGNORECASE):
                continue

            src = re.search(r"""\bsrc\s*=\s*["']([^"']+)["']""", tag, re.IGNORECASE)
            if not src or "://" in src.group(1):
                continue

            # Vite resolves "/src/main.jsx" against the directory holding index.html.
            target = (html.parent / src.group(1).lstrip("/")).resolve()
            if target.is_file() and is_within_root(target) and not is_ignored(target):
                reason = f"Frontend entrypoint: module script in {html.relative_to(PROJECT_ROOT)}"
                found.setdefault(target, reason)

    if not found:
        for pattern in ("src/main.*", "*/src/main.*"):
            for path in sorted(PROJECT_ROOT.glob(pattern)):
                if path.suffix in FRONTEND_EXTS and path.is_file() and not is_ignored(path):
                    found.setdefault(path, "Frontend entrypoint: src/main.* by convention")

    return list(found.items())

def find_frontend_app_files(main_files: list[Path]) -> list[Path]:
    apps: list[Path] = []

    for main_file in main_files:
        for ext in FRONTEND_EXTS:
            path = main_file.with_name(f"App{ext}")
            if path.is_file() and path not in apps:
                apps.append(path)

    return apps

def safe_package_dir(package_path: str) -> Path:
    """
    Convert a user-provided package path into a safe absolute directory path
    inside PROJECT_ROOT.
    """
    clean_path = package_path.strip().strip("/")
    parts = Path(clean_path).parts

    if parts and parts[0] == PROJECT_ROOT.name:
        clean_path = str(Path(*parts[1:]))

    target = (PROJECT_ROOT / clean_path).resolve()

    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        raise ValueError("package_path escapes PROJECT_ROOT")

    if not target.exists():
        raise ValueError(f"package path does not exist: {package_path}")

    if not target.is_dir():
        raise ValueError(f"package path is not a directory: {package_path}")

    return target

def find_go_package_dirs(root: Path, limit: int = 10) -> list[Path]:
    package_dirs: list[Path] = []
    seen: set[Path] = set()

    for path in sorted(root.rglob("*.go")):
        if is_ignored(path) or not path.is_file() or path.name.endswith("_generated.go"):
            continue

        if path.parent in seen:
            continue

        seen.add(path.parent)
        package_dirs.append(path.parent)

        if len(package_dirs) >= limit:
            break

    return package_dirs

GO_TYPE_KINDS = ("struct", "interface", "type")

def go_decl_label(decl: dict) -> str:
    if decl["kind"] == "method":
        return f"({decl['receiver']}).{decl['name']}"
    return decl["name"]

def safe_file_path(path: str) -> Path:
    clean_path = path.strip().strip("/")
    target = (PROJECT_ROOT / clean_path).resolve()

    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError:
        raise ValueError("path escapes PROJECT_ROOT")

    if not target.exists():
        raise ValueError(f"file path does not exist: {path}")

    if not target.is_file():
        raise ValueError(f"path is not a file: {path}")

    if is_ignored(target):
        raise ValueError(f"path is ignored: {path}")

    return target

def graph_node_label(node: dict) -> str:
    return str(node.get("label") or node.get("id") or "")

def format_package_dependency_graph_text(graph: dict) -> str:
    nodes_by_id = {
        node["id"]: node
        for node in graph.get("nodes", [])
    }
    edges_by_source: dict[str, list[dict]] = {}
    for edge in graph.get("edges", []):
        edges_by_source.setdefault(edge["from"], []).append(edge)

    lines = [
        f"root: {graph.get('root_path', '.')}",
        f"packages: {graph.get('packages', 0)}",
        f"nodes: {len(graph.get('nodes', []))}",
        f"edges: {len(graph.get('edges', []))}",
    ]

    source_ids = sorted(
        graph.get("sources", []),
        key=lambda node_id: graph_node_label(nodes_by_id.get(node_id, {"id": node_id})),
    )
    if not source_ids:
        lines.extend(["", "No Go packages found."])
        return "\n".join(lines)

    for source_id in source_ids:
        source_node = nodes_by_id.get(source_id, {"id": source_id})
        lines.extend(["", graph_node_label(source_node)])

        edges = sorted(
            edges_by_source.get(source_id, []),
            key=lambda edge: graph_node_label(nodes_by_id.get(edge["to"], {"id": edge["to"]})),
        )
        if not edges:
            lines.append("  -> (no package imports after filters)")
            continue

        for edge in edges:
            target_node = nodes_by_id.get(edge["to"], {"id": edge["to"]})
            suffix = "" if edge.get("kind") == "local" else f" [{edge.get('kind')}]"
            lines.append(f"  -> {graph_node_label(target_node)}{suffix}")

    return "\n".join(lines)

def mermaid_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

def format_package_dependency_graph_mermaid(graph: dict) -> str:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return 'flowchart LR\n  empty["No Go packages found"]'

    node_names = {
        node["id"]: f"N{i}"
        for i, node in enumerate(nodes)
    }

    lines = ["flowchart LR"]
    for node in nodes:
        label = graph_node_label(node)
        if node.get("kind") != "local":
            label = f"{label} [{node.get('kind')}]"
        lines.append(f'  {node_names[node["id"]]}["{mermaid_escape(label)}"]')

    for edge in edges:
        source = node_names.get(edge["from"])
        target = node_names.get(edge["to"])
        if source and target:
            lines.append(f"  {source} --> {target}")

    return "\n".join(lines)

##=====Tools=====##

@mcp.tool()
def repo_map(max_depth: int = 3) -> str:
    """
    Return a compact map of the repository structure.
    Use this before scanning repository files directly.
    """
    lines: list[str] = []

    root_depth = len(PROJECT_ROOT.parts)

    for path in sorted(PROJECT_ROOT.rglob("*")):
        if is_ignored(path):
            continue

        rel = path.relative_to(PROJECT_ROOT)
        depth = len(path.parts) - root_depth

        if depth > max_depth:
            continue

        prefix = "  " * (depth - 1)

        if path.is_dir():
            lines.append(f"{prefix}{rel.name}/")
        else:
            lines.append(f"{prefix}{rel.name}")

    if len(lines) > REPO_MAP_MAX_LINES:
        hidden = len(lines) - REPO_MAP_MAX_LINES
        lines = lines[:REPO_MAP_MAX_LINES]
        lines.append(f"... truncated: {hidden} more entries; lower max_depth or use search_files")

    return "\n".join(lines)

@mcp.tool()
def search_files(query: str, limit: int = 20) -> str:
    """
    Search file paths in the repository by substring.
    """
    query_lower = query.lower()
    results: list[str] = []

    for path in PROJECT_ROOT.rglob("*"):
        if is_ignored(path) or not path.is_file():
            continue

        rel = str(path.relative_to(PROJECT_ROOT))

        if query_lower in rel.lower():
            results.append(rel)

        # One result past the limit tells us the output is truncated.
        if len(results) > limit:
            break

    if not results:
        return f"No files found for query: {query}"

    return format_limited(results, limit)

@mcp.tool()
def search_code(query: str, limit: int = 20) -> str:
    """
    Search text inside source files.
    Use this before reading files directly.
    """
    query_lower = query.lower()
    results: list[str] = []

    for path in PROJECT_ROOT.rglob("*"):
        if is_ignored(path) or not path.is_file() or not is_searchable_text(path):
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        lines = text.splitlines()

        for i, line in enumerate(lines, start=1):
            if query_lower in line.lower():
                rel = path.relative_to(PROJECT_ROOT)
                results.append(f"{rel}:{i}: {line.strip()}")

                # One match past the limit tells us the output is truncated.
                if len(results) > limit:
                    return format_limited(results, limit)

    if not results:
        return f"No code matches found for query: {query}"

    return format_limited(results, limit)

@mcp.tool()
def find_entrypoints(limit: int = 50) -> str:
    """
    Find likely application entrypoints in the repository.

    Detects:
    - Go main packages and main functions
    - Go cmd/*/main.go files
    - React/Vite frontend main files
    - package.json scripts
    """
    results: list[str] = []

    # 1. Go entrypoints
    for path in PROJECT_ROOT.rglob("*.go"):
        if is_ignored(path) or not path.is_file():
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        rel = path.relative_to(PROJECT_ROOT)

        is_package_main = "package main" in text
        has_func_main = "func main(" in text

        # Самый сильный сигнал: package main + func main
        if is_package_main and has_func_main:
            reason = "Go executable entrypoint: package main with func main"
            results.append(format_entrypoint(rel, "go", reason))

        # Частый Go convention: cmd/<name>/main.go
        elif "cmd" in rel.parts and path.name == "main.go":
            reason = "Likely Go command entrypoint: cmd/*/main.go"
            results.append(format_entrypoint(rel, "go", reason))

        if len(results) >= limit:
            return "\n\n".join(results)

    # 2. Frontend/Vite/React entrypoints
    frontend_entrypoints = find_frontend_entrypoints()
    for path, reason in frontend_entrypoints:
        rel = path.relative_to(PROJECT_ROOT)
        results.append(format_entrypoint(rel, path.suffix.lstrip("."), reason))

        if len(results) >= limit:
            return "\n\n".join(results)

    # 3. React App files next to each main file, as secondary hints
    for path in find_frontend_app_files([path for path, _ in frontend_entrypoints]):
        rel = path.relative_to(PROJECT_ROOT)
        reason = "Frontend root component: likely used by main.*"
        results.append(format_entrypoint(rel, path.suffix.lstrip("."), reason))

        if len(results) >= limit:
            return "\n\n".join(results)

    # 4. package.json scripts
    for path in PROJECT_ROOT.rglob("package.json"):
        if is_ignored(path) or not path.is_file():
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue

        scripts = data.get("scripts", {})
        if not isinstance(scripts, dict) or not scripts:
            continue

        useful_scripts = {
            key: value
            for key, value in scripts.items()
            if key in {"dev", "start", "build", "preview", "serve"}
        }

        if useful_scripts:
            rel = path.relative_to(PROJECT_ROOT)
            scripts_text = "\n".join(
                f"  - {name}: {command}"
                for name, command in useful_scripts.items()
            )

            results.append(
                "\n".join(
                    [
                        f"path: {rel}",
                        "kind: package_json",
                        "reason: Node/Vite/npm scripts",
                        "scripts:",
                        scripts_text,
                    ]
                )
            )

        if len(results) >= limit:
            return "\n\n".join(results)

    if not results:
        return "No likely entrypoints found."

    return "\n\n".join(results)

@mcp.tool()
def get_file_outline(path: str) -> str:
    """
    Return a compact outline for a single Go source file.

    path must be relative to PROJECT_ROOT, for example:
    back/internal/domain/auth.go
    """
    try:
        file_path = safe_file_path(path)
    except ValueError as err:
        return f"Error: {err}"

    if file_path.suffix != ".go":
        return f"Unsupported file type: {path}. get_file_outline currently supports Go files only."

    try:
        outline = outline_go_files(PROJECT_ROOT, [file_path])[0]
    except Exception as err:
        return f"Error: {err}"

    decls = outline["decls"]
    imports = sorted(set(outline["imports"]))
    # Declarations arrive in source order, so the line numbers read top to bottom.
    sections = [
        ("types", [d for d in decls if d["kind"] in GO_TYPE_KINDS]),
        ("functions", [d for d in decls if d["kind"] in ("func", "method")]),
        ("constants", [d for d in decls if d["kind"] == "const"]),
        ("variables", [d for d in decls if d["kind"] == "var"]),
    ]

    lines: list[str] = [
        f"file: {outline['path']}",
        f"package {outline['package_name'] or 'unknown'}",
        "",
    ]

    if outline.get("error"):
        lines.extend([f"parse error: {outline['error']}", ""])

    if imports:
        lines.append("imports:")
        lines.extend(f"- {item}" for item in imports)
        lines.append("")

    for title, items in sections:
        if items:
            lines.append(f"{title}:")
            lines.extend(f"- {go_decl_label(d)}:{d['line']}" for d in items)
            lines.append("")

    if not imports and not decls:
        lines.append("No imports or declarations detected.")

    return "\n".join(lines).strip()

@mcp.tool()
def explain_package(package_path: str) -> str:
    """
    Explain a Go package directory.

    Provide a compact overview of:
    - package name
    - Go files
    - imports
    - types
    - functions
    - methods
    - constants
    - variables

    package_path must be a path relative to PROJECT_ROOT, for example:
    back/internal/domain
    """
    try:
        package_dir = safe_package_dir(package_path)
    except ValueError as err:
        return f"Error: {err}"

    go_files = sorted(
        path
        for path in package_dir.glob("*.go")
        if path.is_file() and not path.name.endswith("_generated.go")
    )

    if not go_files:
        suggestions = find_go_package_dirs(package_dir)

        if suggestions:
            lines = [
                f"No Go files found directly in package path: {package_path}",
                "",
                "Go packages are directories. This path contains subpackages:",
            ]
            lines.extend(
                f"- {path.relative_to(PROJECT_ROOT)}"
                for path in suggestions
            )
            lines.append("")
            lines.append("Call explain_package with one of those package paths.")
            return "\n".join(lines)

        return f"No Go files found in package path: {package_path}"

    try:
        outlines = outline_go_files(PROJECT_ROOT, go_files)
    except Exception as err:
        return f"Error: {err}"

    package_names: set[str] = set()
    all_imports: set[str] = set()
    file_summaries: list[str] = []

    all_types: set[str] = set()
    all_functions: set[str] = set()
    all_methods: set[str] = set()
    all_consts: set[str] = set()
    all_vars: set[str] = set()

    for path, outline in zip(go_files, outlines):
        if outline["package_name"]:
            package_names.add(outline["package_name"])

        decls = outline["decls"]
        types = sorted({f"{d['name']} {d['kind']}" for d in decls if d["kind"] in GO_TYPE_KINDS})
        functions = sorted({d["name"] for d in decls if d["kind"] == "func"})
        methods = sorted({go_decl_label(d) for d in decls if d["kind"] == "method"})
        consts = sorted({d["name"] for d in decls if d["kind"] == "const"})
        vars_ = sorted({d["name"] for d in decls if d["kind"] == "var"})

        all_imports.update(outline["imports"])
        all_types.update(types)
        all_functions.update(functions)
        all_methods.update(methods)
        all_consts.update(consts)
        all_vars.update(vars_)

        parts: list[str] = []

        if types:
            parts.append(f"types: {', '.join(types)}")
        if functions:
            parts.append(f"funcs: {', '.join(functions)}")
        if methods:
            parts.append(f"methods: {', '.join(methods)}")
        if outline.get("error"):
            parts.append(f"parse error: {outline['error']}")

        if not parts:
            parts.append("no top-level symbols detected")

        file_summaries.append(f"- {path.name}: " + "; ".join(parts))

    rel_dir = package_dir.relative_to(PROJECT_ROOT)

    if len(package_names) == 1:
        package_name_text = next(iter(package_names))
    elif len(package_names) > 1:
        package_name_text = ", ".join(sorted(package_names))
    else:
        package_name_text = "unknown"

    lines: list[str] = []

    lines.append(f"Package path: {rel_dir}")
    lines.append(f"Package name: {package_name_text}")
    lines.append("")

    lines.append("Files:")
    lines.extend(file_summaries)
    lines.append("")

    if all_imports:
        lines.append("Imports:")
        for item in sorted(all_imports):
            lines.append(f"- {item}")
        lines.append("")

    if all_types:
        lines.append("Types:")
        for item in sorted(all_types):
            lines.append(f"- {item}")
        lines.append("")

    if all_functions:
        lines.append("Functions:")
        for item in sorted(all_functions):
            lines.append(f"- {item}")
        lines.append("")

    if all_methods:
        lines.append("Methods:")
        for item in sorted(all_methods):
            lines.append(f"- {item}")
        lines.append("")

    if all_consts:
        lines.append("Constants:")
        for item in sorted(all_consts):
            lines.append(f"- {item}")
        lines.append("")

    if all_vars:
        lines.append("Variables:")
        for item in sorted(all_vars):
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).strip()

@mcp.tool()
def package_dependency_graph(
    root_path: str = "",
    include_external: bool = False,
    include_stdlib: bool = False,
    output: str = "text",
) -> str:
    """
    Return a Go package dependency graph.

    root_path narrows source packages to a repository directory, for example:
    back or back/internal/usecase.

    output supports:
    - text
    - json
    - mermaid
    """
    try:
        graph = build_package_dependency_graph(
            PROJECT_ROOT,
            root_path=root_path,
            include_external=include_external,
            include_stdlib=include_stdlib,
        )
    except Exception as err:
        return f"Error: {err}"

    output = output.strip().lower() or "text"
    if output == "text":
        return format_package_dependency_graph_text(graph)
    if output == "json":
        return json.dumps(graph, indent=2, ensure_ascii=False)
    if output == "mermaid":
        return format_package_dependency_graph_mermaid(graph)

    return "Error: unsupported output. Use one of: text, json, mermaid."

@mcp.tool()
def index_project(force: bool = False) -> str:
    """
    Build or refresh the local SQLite FTS index.
    Run this once at the beginning of work with the project.
    """
    try:
        stats = build_code_index(PROJECT_ROOT, force=force)
    except Exception as err:
        return f"Error: {err}"

    return "\n".join(
        [
            "Index updated.",
            f"files seen: {stats['files_seen']} ({stats['test_files_seen']} test files)",
            f"files indexed: {stats['files_indexed']}",
            f"files skipped: {stats['files_skipped']}",
            f"files deleted: {stats['files_deleted']}",
            f"chunks indexed: {stats['chunks_indexed']}",
        ]
    )


@mcp.tool()
def search_index(query: str, limit: int = 10, include_tests: bool = False) -> str:
    """
    Search indexed Go chunks using SQLite FTS5.
    _test.go files are indexed but skipped unless include_tests is true.
    """
    try:
        rows = index_search(PROJECT_ROOT, query, limit, include_tests)
    except Exception as err:
        return f"Error: {err}"

    if not rows:
        return f"No indexed chunks found for query: {query}"

    blocks = []
    for row in rows:
        blocks.append(
            "\n".join(
                [
                    f"chunk_id: {row['id']}",
                    f"path: {row['path']}:{row['start_line']}-{row['end_line']}" + (" (test)" if row["is_test"] else ""),
                    f"kind: {row['kind']}",
                    f"symbol: {row['symbol'] or ''}",
                    f"package: {row['package_name'] or ''}",
                    f"score: {row['score']:.6f}",
                    f"snippet: {row['snippet']}",
                ]
            )
        )

    return "\n\n".join(blocks)


@mcp.tool()
def get_chunk(chunk_id: int) -> str:
    """
    Return full indexed chunk content by chunk id.
    """
    try:
        row = index_get_chunk(PROJECT_ROOT, chunk_id)
    except Exception as err:
        return f"Error: {err}"

    if not row:
        return f"Chunk not found: {chunk_id}"

    return "\n".join(
        [
            f"chunk_id: {row['id']}",
            f"path: {row['path']}:{row['start_line']}-{row['end_line']}",
            f"kind: {row['kind']}",
            f"symbol: {row['symbol'] or ''}",
            f"package: {row['package_name'] or ''}",
            "",
            row["content"],
        ]
    )


if __name__ == "__main__":
    mcp.run()
