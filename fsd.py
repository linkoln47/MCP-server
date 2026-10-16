"""
Feature-Sliced Design view of the Anikoro frontend: which slice a module belongs
to, what a slice exposes, who it depends on and who depends on it.
"""
import json
import os
from pathlib import Path

from js_outline import decl_kinds_summary, format_export, is_js_file, outline_js_file

# Mirrors `paths` in front/tsconfig.json and the vite alias: "@/x" is front/src/x.
FRONT_SRC_REL = "front/src"
ALIAS_PREFIX = "@/"
# Highest layer first: a module may import only from layers below its own.
FSD_LAYERS = ["app", "pages", "widgets", "features", "entities", "shared"]
# Layers built from segments rather than isolated slices, so their parts may import each other.
UNSLICED_LAYERS = {"app", "shared"}
RESOLVE_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]
# Modules directly in front/src (App.jsx, main.jsx) compose the app like the app layer.
ROOT_UNIT = "(root)"
# The flags below mirror the boundaries policies in front/eslint.config.js; keep them in step.
# Layers allowed to call the HTTP boundary directly.
SHARED_API_UNIT = "shared/api"
SHARED_API_CONSUMERS = {"features", "shared", ROOT_UNIT}
# The provisional upward edge: these layers read the title-language and playback
# contexts from app until plan 4.2 moves them to app/providers.
APP_CONTEXT_CONSUMERS = {"pages", "widgets", "entities"}


def front_src(project_root: Path) -> Path:
    return (project_root / FRONT_SRC_REL).resolve()

def unit_of(src: Path, path: Path) -> str:
    """
    The FSD unit a file belongs to: "features/listEdit", "shared/api", "app".
    """
    parts = path.relative_to(src).parts
    if len(parts) == 1:
        return ROOT_UNIT

    layer = parts[0]
    if layer in FSD_LAYERS and layer != "app" and len(parts) >= 3:
        return f"{layer}/{parts[1]}"
    return layer

def unit_layer(unit: str) -> str:
    return unit.split("/", 1)[0]

def layer_rank(unit: str) -> int:
    if unit == ROOT_UNIT:
        return 0
    layer = unit_layer(unit)
    # Anything outside the layer list (global styles) sits with shared, at the bottom.
    return FSD_LAYERS.index(layer) if layer in FSD_LAYERS else len(FSD_LAYERS) - 1

def is_unsliced(unit: str) -> bool:
    layer = unit_layer(unit)
    return unit == ROOT_UNIT or layer in UNSLICED_LAYERS or layer not in FSD_LAYERS

def unit_dir(src: Path, unit: str) -> Path:
    return src if unit == ROOT_UNIT else src / unit

def public_api_file(src: Path, unit: str) -> Path | None:
    if is_unsliced(unit):
        return None
    for ext in RESOLVE_EXTS:
        candidate = unit_dir(src, unit) / f"index{ext}"
        if candidate.is_file():
            return candidate
    return None

def resolve_module(src: Path, importer: Path, source: str) -> Path | None:
    """
    Resolve an internal import to the file the bundler would load, or None for a
    package import. An internal path that matches no file resolves to itself.
    """
    if source.startswith(ALIAS_PREFIX):
        base = src / source[len(ALIAS_PREFIX):]
    elif source.startswith("."):
        base = importer.parent / source
    else:
        return None

    base = Path(os.path.normpath(base))
    if base.is_file():
        return base
    for ext in RESOLVE_EXTS:
        if base.with_name(base.name + ext).is_file():
            return base.with_name(base.name + ext)
    for ext in RESOLVE_EXTS:
        if (base / f"index{ext}").is_file():
            return base / f"index{ext}"
    return base

def package_name(source: str) -> str:
    parts = source.split("/")
    return "/".join(parts[:2]) if source.startswith("@") else parts[0]

def entry_point(src: Path, from_unit: str, to_unit: str, target: Path) -> str | None:
    """
    Name the entry point an import of another unit goes through, or None when it
    reaches past them into the slice's internals.
    """
    try:
        rel = target.relative_to(unit_dir(src, to_unit))
    except ValueError:
        return None

    if len(rel.parts) == 1 and rel.stem == "index":
        return "index"
    # An entity has a second entry point: leaf components under ui/ are addressed directly.
    if unit_layer(to_unit) == "entities" and rel.parts[0] == "ui":
        return "ui"
    # A named cross-import, @x/<consumer>, declares one sibling edge in the slice that owns it.
    if (
        rel.parts[0] == "@x"
        and unit_layer(from_unit) == unit_layer(to_unit)
        and rel.stem == from_unit.split("/", 1)[-1]
    ):
        return "@x"
    return None

def edge_flags(from_unit: str, to_unit: str, via: str | None) -> list[str]:
    if from_unit == to_unit:
        return []

    flags: list[str] = []
    from_layer = unit_layer(from_unit) if from_unit != ROOT_UNIT else ROOT_UNIT
    from_rank, to_rank = layer_rank(from_unit), layer_rank(to_unit)

    if to_rank < from_rank:
        if to_unit == "app" and from_layer in APP_CONTEXT_CONSUMERS:
            flags.append("provisional: app context")
        else:
            flags.append("upward")
    elif to_rank == from_rank and not is_unsliced(from_unit) and not is_unsliced(to_unit) and via not in {"@x", "ui"}:
        flags.append("cross-slice")

    if to_unit == SHARED_API_UNIT and from_layer not in SHARED_API_CONSUMERS:
        flags.append("shared-api")

    return flags

def is_violation(flag: str) -> bool:
    return not flag.startswith("provisional")

def js_files_under(directory: Path, is_ignored) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file() and is_js_file(path) and not is_ignored(path))

def collect_imports(project_root: Path, files: list[Path]) -> tuple[list[dict], list[dict]]:
    """
    Every import of the given files, split into edges between frontend modules
    and imports of packages.
    """
    src = front_src(project_root)
    edges: list[dict] = []
    packages: list[dict] = []

    for importer in files:
        outline = outline_js_file(importer)
        from_unit = unit_of(src, importer)

        for item in outline["imports"]:
            target = resolve_module(src, importer, item["source"])
            if target is None:
                packages.append({"importer": importer, "package": package_name(item["source"])})
                continue

            try:
                to_unit = unit_of(src, target)
            except ValueError:
                to_unit = "(outside front/src)"

            crosses = to_unit != from_unit
            via = entry_point(src, from_unit, to_unit, target) if crosses else None
            edges.append(
                {
                    "importer": importer,
                    "line": item["line"],
                    "source": item["source"],
                    "names": item["names"],
                    "type_only": item["type_only"],
                    "target": target,
                    "from_unit": from_unit,
                    "to_unit": to_unit,
                    "via": via,
                    # A slice with a public API must be entered through one of its entry points.
                    "deep": crosses and via is None and public_api_file(src, to_unit) is not None,
                    "flags": edge_flags(from_unit, to_unit, via),
                }
            )

    return edges, packages

def file_summary(outline: dict) -> str:
    parts: list[str] = []
    for kind, items in decl_kinds_summary(outline["decls"]):
        # Plain values are noise unless the module exports them.
        if kind in {"const", "let", "var"}:
            items = [item for item in items if item["exported"]]
            if not items:
                continue
        parts.append(f"{kind}: {', '.join(item['name'] for item in items)}")

    reexports = [item for item in outline["exports"] if item["source"] is not None]
    if reexports:
        parts.append(f"re-exports: {len(reexports)}")
    if outline["has_error"]:
        parts.append("parse error: outline may be partial")

    return "; ".join(parts) or "no top-level declarations"

def edge_marks(edge: dict) -> str:
    marks = list(edge["flags"])
    if edge["via"] == "@x":
        marks.append(f"via {edge['source']}")
    if edge["deep"]:
        marks.append(f"deep: {edge['source']}")
    if edge["type_only"]:
        marks.append("type only")
    return f" [{'; '.join(marks)}]" if marks else ""

def explain_frontend_dir(project_root: Path, directory: Path, include_tests: bool, is_ignored) -> str:
    project_root = project_root.resolve()
    src = front_src(project_root)
    rel_dir = directory.relative_to(project_root).as_posix()

    all_files = js_files_under(directory, is_ignored)
    test_files = [path for path in all_files if outline_js_file(path)["is_test"]]
    only_tests = bool(all_files) and len(test_files) == len(all_files)
    # Hiding the tests of a test-only directory would leave nothing to explain.
    hidden_tests = [] if include_tests or only_tests else test_files
    files = [path for path in all_files if path not in hidden_tests]

    try:
        units = sorted({unit_of(src, path) for path in files})
    except ValueError:
        return f"Error: {rel_dir} is not inside {FRONT_SRC_REL}"

    lines = [f"Path: {rel_dir}"]
    if directory == src:
        lines.append("FSD: the whole frontend source")
    elif len(units) == 1 and unit_dir(src, units[0]) == directory:
        unit = units[0]
        kind = "segment" if is_unsliced(unit) else "slice"
        lines.append(f"FSD: layer {unit_layer(unit)}, {kind} {unit}")
    else:
        lines.append(f"FSD units: {', '.join(units)}")
    lines.append("")

    if only_tests and not include_tests:
        lines.extend(["Note: this directory holds only test files, so they are shown.", ""])

    index = next((directory / f"index{ext}" for ext in (".ts", ".tsx", ".js", ".jsx") if (directory / f"index{ext}").is_file()), None)
    if index is not None:
        exports = outline_js_file(index)["exports"]
        lines.append(f"Public API ({index.name}):")
        if exports:
            lines.extend(format_export(item) for item in exports)
        else:
            lines.append("- (exports nothing)")
        lines.append("")

    lines.append("Files:")
    for path in files:
        outline = outline_js_file(path)
        mark = " (test)" if outline["is_test"] else ""
        lines.append(f"- {path.relative_to(directory).as_posix()}{mark}: {file_summary(outline)}")
    lines.append("")

    if hidden_tests:
        lines.append(f"Test files hidden ({len(hidden_tests)}): " + ", ".join(path.relative_to(directory).as_posix() for path in hidden_tests))
        lines.append(f"Call explain_package(\"{rel_dir}\", include_tests=true) to include them.")
        lines.append("")

    edges, packages = collect_imports(project_root, files)

    outgoing: dict[str, list[dict]] = {}
    for edge in edges:
        if not edge["target"].is_relative_to(directory):
            outgoing.setdefault(edge["to_unit"], []).append(edge)

    if outgoing:
        lines.append("Depends on:")
        for unit in sorted(outgoing, key=lambda value: (layer_rank(value), value)):
            names = sorted({name for edge in outgoing[unit] for name in edge["names"]})
            marks = sorted({edge_marks(edge) for edge in outgoing[unit]} - {""})
            lines.append(f"- {unit}: {', '.join(names) or '(side effect)'}{''.join(marks)}")
        lines.append("")

    external = sorted({item["package"] for item in packages})
    if external:
        lines.append(f"Packages: {', '.join(external)}")
        lines.append("")

    # Dependents come from scanning the whole frontend, with the same test policy.
    every_file = js_files_under(src, is_ignored)
    if not include_tests:
        every_file = [path for path in every_file if not outline_js_file(path)["is_test"]]
    candidates = [path for path in every_file if not path.is_relative_to(directory)]
    incoming: dict[str, list[dict]] = {}
    for edge in collect_imports(project_root, candidates)[0]:
        if edge["target"].is_relative_to(directory):
            incoming.setdefault(edge["from_unit"], []).append(edge)

    if incoming:
        lines.append("Used by:")
        for unit in sorted(incoming, key=lambda value: (layer_rank(value), value)):
            marks = sorted({edge_marks(edge) for edge in incoming[unit]} - {""})
            files_text = ", ".join(sorted({edge["importer"].relative_to(src).as_posix() for edge in incoming[unit]}))
            lines.append(f"- {unit} ({files_text}){''.join(marks)}")
        lines.append("")
    else:
        lines.extend(["Used by: nothing in the frontend imports this directory", ""])

    return "\n".join(lines).strip()

def slice_dependency_graph(project_root: Path, root: Path, include_tests: bool, include_external: bool, is_ignored) -> dict:
    project_root = project_root.resolve()
    src = front_src(project_root)

    files = js_files_under(root, is_ignored)
    if not include_tests:
        files = [path for path in files if not outline_js_file(path)["is_test"]]

    edges, packages = collect_imports(project_root, files)

    units: set[str] = {unit_of(src, path) for path in files}
    aggregated: dict[tuple[str, str], dict] = {}
    violations: list[dict] = []
    provisional: list[dict] = []

    for edge in edges:
        if edge["from_unit"] == edge["to_unit"]:
            continue
        units.add(edge["to_unit"])
        key = (edge["from_unit"], edge["to_unit"])
        item = aggregated.setdefault(key, {"from": key[0], "to": key[1], "imports": 0, "deep": 0, "flags": []})
        item["imports"] += 1
        item["deep"] += int(edge["deep"])
        item["flags"] = sorted(set(item["flags"]) | set(edge["flags"]) | ({"via @x"} if edge["via"] == "@x" else set()))

        broken = [flag for flag in edge["flags"] if is_violation(flag)] + (["deep import"] if edge["deep"] else [])
        pending = [flag for flag in edge["flags"] if not is_violation(flag)]
        record = {
            "from": edge["from_unit"],
            "to": edge["to_unit"],
            "at": f"{edge['importer'].relative_to(project_root).as_posix()}:{edge['line']}",
            "source": edge["source"],
        }
        if broken:
            violations.append({**record, "rule": ", ".join(broken)})
        elif pending:
            provisional.append({**record, "rule": ", ".join(pending)})

    if include_external:
        for item in packages:
            from_unit = unit_of(src, item["importer"])
            key = (from_unit, item["package"])
            units.add(item["package"])
            entry = aggregated.setdefault(key, {"from": from_unit, "to": item["package"], "imports": 0, "deep": 0, "flags": ["external"]})
            entry["imports"] += 1

    def sort_key(value: str) -> tuple[int, str]:
        return (layer_rank(value), value)

    return {
        "root_path": root.relative_to(project_root).as_posix(),
        "include_tests": include_tests,
        "units": sorted(units, key=sort_key),
        "edges": sorted(aggregated.values(), key=lambda item: (sort_key(item["from"]), sort_key(item["to"]))),
        "violations": violations,
        "provisional": provisional,
    }

def format_slice_graph_text(graph: dict) -> str:
    lines = [
        f"root: {graph['root_path']}",
        f"units: {len(graph['units'])}",
        f"edges: {len(graph['edges'])}",
        f"rule violations: {len(graph['violations'])}",
        f"provisional edges: {len(graph['provisional'])}",
    ]

    if graph["violations"]:
        lines.extend(["", "Violations (upward / cross-slice / shared-api / deep import):"])
        lines.extend(f"- {item['from']} -> {item['to']} [{item['rule']}] {item['at']} ({item['source']})" for item in graph["violations"])

    if graph["provisional"]:
        # Allowed by eslint.config.js for now; listed so the planned move stays visible.
        lines.extend(["", "Provisional (allowed until plan 4.2):"])
        pairs = sorted({(item["from"], item["to"], item["rule"]) for item in graph["provisional"]})
        lines.extend(f"- {source} -> {target} [{rule}]" for source, target, rule in pairs)

    by_source: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        by_source.setdefault(edge["from"], []).append(edge)

    for unit in graph["units"]:
        if unit not in by_source:
            continue
        lines.extend(["", unit])
        for edge in by_source[unit]:
            marks = list(edge["flags"])
            if edge["deep"]:
                marks.append(f"deep x{edge['deep']}")
            suffix = f" [{', '.join(marks)}]" if marks else ""
            lines.append(f"  -> {edge['to']} ({edge['imports']}){suffix}")

    return "\n".join(lines)

def format_slice_graph_mermaid(graph: dict) -> str:
    if not graph["units"]:
        return 'flowchart LR\n  empty["No frontend modules found"]'

    names = {unit: f"N{i}" for i, unit in enumerate(graph["units"])}
    lines = ["flowchart LR"]
    lines.extend(f'  {names[unit]}["{unit}"]' for unit in graph["units"])
    for edge in graph["edges"]:
        # Rule-breaking edges are dotted so they stand out in the rendered graph.
        broken = any(is_violation(flag) for flag in edge["flags"] if flag not in {"external", "via @x"})
        arrow = "-.->" if broken or edge["deep"] else "-->"
        lines.append(f"  {names[edge['from']]} {arrow}|{edge['imports']}| {names[edge['to']]}")
    return "\n".join(lines)

def format_slice_graph_json(graph: dict) -> str:
    return json.dumps(graph, indent=2, ensure_ascii=False)
