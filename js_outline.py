"""
Syntactic outline of JavaScript/TypeScript modules with tree-sitter: imports,
exports and top-level declarations, classified the way a React codebase reads.
"""
import re
from functools import lru_cache
from pathlib import Path

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

JS_EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
TEST_FILE_RE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
HOOK_RE = re.compile(r"^use[A-Z0-9]")
COMPONENT_WRAPPERS = {"memo", "forwardRef", "lazy"}
JSX_NODES = {"jsx_element", "jsx_self_closing_element", "jsx_fragment"}
FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
FUNCTION_DECLS = {"function_declaration", "generator_function_declaration"}
CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
TYPE_DECLS = {"type_alias_declaration": "type", "interface_declaration": "interface", "enum_declaration": "enum"}

# Order in which declaration kinds are listed in an outline.
DECL_KIND_ORDER = ["component", "hook", "function", "class", "type", "interface", "enum", "const", "let", "var"]


def is_js_file(path: Path) -> bool:
    return path.suffix.lower() in JS_EXTS

def is_js_test_file(path: Path) -> bool:
    return bool(TEST_FILE_RE.search(path.name))

@lru_cache
def parser_for(suffix: str) -> Parser:
    if suffix == ".ts":
        language = tree_sitter_typescript.language_typescript()
    elif suffix == ".tsx":
        language = tree_sitter_typescript.language_tsx()
    else:
        # The JavaScript grammar parses JSX as well.
        language = tree_sitter_javascript.language()
    return Parser(Language(language))

def node_text(node: Node) -> str:
    return node.text.decode("utf-8")

def node_line(node: Node) -> int:
    return node.start_point[0] + 1

def string_value(node: Node | None) -> str:
    if node is None:
        return ""
    return node_text(node)[1:-1]

def contains_jsx(node: Node) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in JSX_NODES:
            return True
        stack.extend(current.children)
    return False

def classify_function(name: str, node: Node) -> str:
    if HOOK_RE.match(name):
        return "hook"
    if name[:1].isupper() and contains_jsx(node):
        return "component"
    return "function"

def classify_value(name: str, value: Node | None, keyword: str) -> str:
    if value is None:
        return keyword
    if value.type in FUNCTION_VALUES:
        return classify_function(name, value)
    if value.type == "call_expression" and name[:1].isupper():
        callee = node_text(value.child_by_field_name("function") or value).split(".")[-1]
        if callee in COMPONENT_WRAPPERS:
            return "component"
    return keyword

def pattern_names(node: Node) -> list[Node]:
    """
    Identifiers bound by a destructuring pattern such as const { a, b: c } = x.
    """
    if node.type in {"identifier", "shorthand_property_identifier_pattern"}:
        return [node]
    if node.type == "pair_pattern":
        value = node.child_by_field_name("value")
        return pattern_names(value) if value is not None else []

    names: list[Node] = []
    for child in node.named_children:
        names.extend(pattern_names(child))
    return names

def declarations_from(node: Node, exported: bool) -> list[dict]:
    def decl(kind: str, name_node: Node) -> dict:
        return {"kind": kind, "name": node_text(name_node), "line": node_line(name_node), "exported": exported}

    name_node = node.child_by_field_name("name")

    if node.type in FUNCTION_DECLS and name_node is not None:
        return [decl(classify_function(node_text(name_node), node), name_node)]

    if node.type in CLASS_DECLS and name_node is not None:
        return [decl("class", name_node)]

    if node.type in TYPE_DECLS and name_node is not None:
        return [decl(TYPE_DECLS[node.type], name_node)]

    if node.type in {"lexical_declaration", "variable_declaration"}:
        keyword = "var" if node.type == "variable_declaration" else node_text(node.children[0])
        result: list[dict] = []

        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            target = declarator.child_by_field_name("name")
            if target is None:
                continue
            if target.type == "identifier":
                kind = classify_value(node_text(target), declarator.child_by_field_name("value"), keyword)
                result.append(decl(kind, target))
            else:
                result.extend(decl(keyword, name) for name in pattern_names(target))

        return result

    return []

def parse_import(node: Node) -> dict:
    names: list[str] = []
    clause = next((child for child in node.named_children if child.type == "import_clause"), None)

    for part in clause.named_children if clause is not None else []:
        if part.type == "identifier":
            names.append(f"default as {node_text(part)}")
        elif part.type == "namespace_import":
            names.append(node_text(part))
        elif part.type == "named_imports":
            for spec in part.named_children:
                if spec.type == "import_specifier":
                    names.append(specifier_label(spec))

    return {
        "source": string_value(node.child_by_field_name("source")),
        "names": names,
        "line": node_line(node),
        # TS "import type { X }" erases at build time and adds no runtime edge.
        "type_only": any(child.type == "type" for child in node.children),
    }

def specifier_label(spec: Node) -> str:
    name = spec.child_by_field_name("name")
    alias = spec.child_by_field_name("alias")
    label = node_text(name) if name is not None else node_text(spec)
    return f"{label} as {node_text(alias)}" if alias is not None else label

def parse_export(node: Node, decls: list[dict]) -> list[dict]:
    line = node_line(node)
    is_default = any(child.type == "default" for child in node.children)
    source_node = node.child_by_field_name("source")
    source = string_value(source_node) if source_node is not None else None
    declaration = node.child_by_field_name("declaration")
    exports: list[dict] = []

    if declaration is not None:
        declared = declarations_from(declaration, exported=True)
        decls.extend(declared)
        for item in declared:
            name = "default" if is_default else item["name"]
            exports.append({"name": name, "local": item["name"], "source": None, "line": line})
        return exports

    if is_default:
        value = node.child_by_field_name("value")
        local = node_text(value) if value is not None and value.type == "identifier" else None
        exports.append({"name": "default", "local": local, "source": None, "line": line})
        return exports

    for child in node.named_children:
        if child.type == "export_clause":
            for spec in child.named_children:
                if spec.type != "export_specifier":
                    continue
                name = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias")
                local = node_text(name) if name is not None else node_text(spec)
                exported = node_text(alias) if alias is not None else local
                exports.append({"name": exported, "local": local, "source": source, "line": line})
        elif child.type == "namespace_export":
            exports.append({"name": node_text(child), "local": None, "source": source, "line": line})

    # export * from "./x" has no clause, only the star.
    if not exports and source is not None:
        exports.append({"name": "*", "local": None, "source": source, "line": line})

    return exports

@lru_cache(maxsize=4096)
def _outline_cached(path_str: str, mtime_ns: int, size: int) -> dict:
    path = Path(path_str)
    tree = parser_for(path.suffix.lower()).parse(path.read_bytes())
    root = tree.root_node

    imports: list[dict] = []
    exports: list[dict] = []
    decls: list[dict] = []

    for node in root.named_children:
        if node.type == "import_statement":
            imports.append(parse_import(node))
        elif node.type == "export_statement":
            exports.extend(parse_export(node, decls))
        else:
            decls.extend(declarations_from(node, exported=False))

    # export { local } marks a declaration made earlier in the file.
    exported_locals = {item["local"] for item in exports if item["source"] is None and item["local"]}
    for item in decls:
        if item["name"] in exported_locals:
            item["exported"] = True

    return {
        "imports": imports,
        "exports": exports,
        "decls": decls,
        "has_error": root.has_error,
    }

def outline_js_file(path: Path) -> dict:
    """
    Outline one module. Results are cached by mtime and size, so scanning the
    whole frontend repeatedly reparses only what changed.
    """
    stat = path.stat()
    outline = _outline_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    return {**outline, "is_test": is_js_test_file(path)}

def decl_kinds_summary(decls: list[dict]) -> list[tuple[str, list[dict]]]:
    grouped: dict[str, list[dict]] = {}
    for item in decls:
        grouped.setdefault(item["kind"], []).append(item)
    return [(kind, grouped[kind]) for kind in DECL_KIND_ORDER if kind in grouped]

def format_import(item: dict) -> str:
    suffix = " (type only)" if item["type_only"] else ""
    if not item["names"]:
        return f"- {item['source']} (side effect){suffix}"
    return f"- {item['source']}: {', '.join(item['names'])}{suffix}"

def format_export(item: dict) -> str:
    if item["source"] is not None:
        local = item["local"]
        label = item["name"] if not local or local == item["name"] else f"{local} as {item['name']}"
        return f"- {label} from {item['source']}"
    if item["name"] == "default" and item["local"]:
        return f"- default ({item['local']})"
    return f"- {item['name']}"

def format_js_outline(rel_path: str, outline: dict) -> str:
    lines = [f"file: {rel_path}", f"module: {Path(rel_path).suffix.lstrip('.')}" + (" (test)" if outline["is_test"] else ""), ""]

    if outline["has_error"]:
        lines.extend(["parse error: syntax errors present; the outline may be partial", ""])

    if outline["imports"]:
        lines.append("imports:")
        lines.extend(format_import(item) for item in outline["imports"])
        lines.append("")

    if outline["exports"]:
        lines.append("exports:")
        lines.extend(format_export(item) for item in outline["exports"])
        lines.append("")

    for kind, items in decl_kinds_summary(outline["decls"]):
        lines.append(f"{kind}s:" if not kind.endswith("s") else f"{kind}es:")
        lines.extend(f"- {item['name']}:{item['line']}" + (" (exported)" if item["exported"] else "") for item in items)
        lines.append("")

    if not outline["imports"] and not outline["exports"] and not outline["decls"]:
        lines.append("No imports, exports, or declarations detected.")

    return "\n".join(lines).strip()
