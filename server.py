import json
import os
import re

from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("repo-search", "A tool to search and map repository files.")

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
}


def is_ignored(path: Path) -> bool:
    return any(part in IGNORE_DIRS for part in path.parts)

def format_entrypoint(path: Path, kind: str, reason: str) -> str:
    return "\n".join(
        [
            f"path: {path}",
            f"kind: {kind}",
            f"reason: {reason}",
        ]
    )

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


def extract_go_package_name(text: str) -> str | None:
    match = re.search(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_]*)\b", text, re.MULTILINE)
    if not match:
        return None
    return match.group(1)


def extract_go_imports(text: str) -> list[str]:
    imports: list[str] = []

    # import "fmt"
    for match in re.finditer(r'^\s*import\s+"([^"]+)"', text, re.MULTILINE):
        imports.append(match.group(1))

    # import (
    #   "fmt"
    #   alias "pkg/path"
    #   _ "pkg/path"
    # )
    block_match = re.search(r"^\s*import\s*\((.*?)\)", text, re.MULTILINE | re.DOTALL)
    if block_match:
        block = block_match.group(1)

        for line in block.splitlines():
            line = line.strip()

            if not line or line.startswith("//"):
                continue

            match = re.search(r'"([^"]+)"', line)
            if match:
                imports.append(match.group(1))

    return sorted(set(imports))


def extract_go_types(text: str) -> list[str]:
    results: list[str] = []

    # type User struct { ... }
    for match in re.finditer(
        r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+struct\b",
        text,
        re.MULTILINE,
    ):
        results.append(f"{match.group(1)} struct")

    # type Repository interface { ... }
    for match in re.finditer(
        r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+interface\b",
        text,
        re.MULTILINE,
    ):
        results.append(f"{match.group(1)} interface")

    # type UserID int / type Handler = ...
    for match in re.finditer(
        r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s*=)?\s+([A-Za-z_][A-Za-z0-9_./\[\]*]*)",
        text,
        re.MULTILINE,
    ):
        name = match.group(1)
        rhs = match.group(2)

        # чтобы не дублировать struct/interface
        if rhs not in {"struct", "interface"}:
            item = f"{name} type"
            if item not in results:
                results.append(item)

    return sorted(set(results))


def extract_go_functions(text: str) -> list[str]:
    results: list[str] = []

    # func NewService(...)
    for match in re.finditer(
        r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        text,
        re.MULTILINE,
    ):
        results.append(match.group(1))

    return sorted(set(results))


def extract_go_methods(text: str) -> list[str]:
    results: list[str] = []

    # func (s *Service) Login(...)
    # func (h Handler) ServeHTTP(...)
    pattern = (
        r"^\s*func\s*"
        r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s+(\*?\s*[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\("
    )

    for match in re.finditer(pattern, text, re.MULTILINE):
        receiver_type = match.group(2).replace(" ", "")
        method_name = match.group(3)
        results.append(f"({receiver_type}).{method_name}")

    return sorted(set(results))


def extract_go_consts_vars(text: str) -> tuple[list[str], list[str]]:
    consts: list[str] = []
    vars_: list[str] = []

    for match in re.finditer(
        r"^\s*const\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        text,
        re.MULTILINE,
    ):
        consts.append(match.group(1))

    for match in re.finditer(
        r"^\s*var\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        text,
        re.MULTILINE,
    ):
        vars_.append(match.group(1))

    # const ( A = ... )
    const_block = re.search(r"^\s*const\s*\((.*?)\)", text, re.MULTILINE | re.DOTALL)
    if const_block:
        for line in const_block.group(1).splitlines():
            line = line.strip()
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\b", line)
            if match:
                consts.append(match.group(1))

    # var ( A = ... )
    var_block = re.search(r"^\s*var\s*\((.*?)\)", text, re.MULTILINE | re.DOTALL)
    if var_block:
        for line in var_block.group(1).splitlines():
            line = line.strip()
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\b", line)
            if match:
                vars_.append(match.group(1))

    return sorted(set(consts)), sorted(set(vars_))


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
    frontend_candidates = [
        "frontend/src/main.jsx",
        "frontend/src/main.js",
        "frontend/src/main.tsx",
        "frontend/src/main.ts",
        "src/main.jsx",
        "src/main.js",
        "src/main.tsx",
        "src/main.ts",
    ]

    for candidate in frontend_candidates:
        path = PROJECT_ROOT / candidate

        if path.exists() and path.is_file() and not is_ignored(path):
            rel = path.relative_to(PROJECT_ROOT)
            reason = "Frontend entrypoint: React/Vite main file"
            results.append(format_entrypoint(rel, path.suffix.lstrip("."), reason))

        if len(results) >= limit:
            return "\n\n".join(results)

    # 3. React App files as secondary hints
    app_candidates = [
        "frontend/src/App.jsx",
        "frontend/src/App.js",
        "frontend/src/App.tsx",
        "frontend/src/App.ts",
        "src/App.jsx",
        "src/App.js",
        "src/App.tsx",
        "src/App.ts",
    ]

    for candidate in app_candidates:
        path = PROJECT_ROOT / candidate

        if path.exists() and path.is_file() and not is_ignored(path):
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

    package_names: set[str] = set()
    all_imports: set[str] = set()
    file_summaries: list[str] = []

    all_types: set[str] = set()
    all_functions: set[str] = set()
    all_methods: set[str] = set()
    all_consts: set[str] = set()
    all_vars: set[str] = set()

    for path in go_files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as err:
            file_summaries.append(f"- {path.name}: unreadable ({err})")
            continue

        package_name = extract_go_package_name(text)
        if package_name:
            package_names.add(package_name)

        imports = extract_go_imports(text)
        types = extract_go_types(text)
        functions = extract_go_functions(text)
        methods = extract_go_methods(text)
        consts, vars_ = extract_go_consts_vars(text)

        all_imports.update(imports)
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

if __name__ == "__main__":
    mcp.run()
