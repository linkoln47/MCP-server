from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("repo-search", "A tool to search and map repository files.")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
}


def is_ignored(path: Path) -> bool:
    return any(part in IGNORE_DIRS for part in path.parts)


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

    return "\n".join(lines[:500])


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

        if len(results) >= limit:
            break

    if not results:
        return f"No files found for query: {query}"

    return "\n".join(results)

@mcp.tool()
def search_code(query: str, limit: int = 20) -> str:
    """
    Search text inside source files.
    Use this before reading files directly.
    """
    exts = {".go", ".js", ".jsx", ".ts", ".tsx", ".md", ".sql", ".yaml", ".yml"}
    query_lower = query.lower()
    results: list[str] = []

    for path in PROJECT_ROOT.rglob("*"):
        if is_ignored(path) or not path.is_file():
            continue

        if path.suffix not in exts:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        lines = text.splitlines()

        for i, line in enumerate(lines, start=1):
            if query_lower in line.lower():
                rel = path.relative_to(PROJECT_ROOT)
                results.append(f"{rel}:{i}: {line.strip()}")

                if len(results) >= limit:
                    return "\n".join(results)

    if not results:
        return f"No code matches found for query: {query}"

    return "\n".join(results)


if __name__ == "__main__":
    mcp.run()