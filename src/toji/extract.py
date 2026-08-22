"""Tree-sitter based extraction: source bytes -> symbols + edges.

Extraction is structural and heuristic by design:
- call edges store the *written* callee text (e.g. `obj.method`, `helper`);
  resolution against indexed symbols happens at query time (see graph.py).
- docstrings are captured for Python only (comments are not part of TS/JS
  docstring conventions at this granularity).
"""

from __future__ import annotations

import re
import textwrap
from importlib.resources import files

from tree_sitter import Node, Query, QueryCursor

from .languages import get_language, parse_tree
from .models import EDGE_CALLS, EDGE_CONTAINS, EDGE_IMPORTS, EDGE_INHERITS, Edge, Symbol

_QUERY_CACHE: dict[str, Query] = {}
_QUERY_FILES = {
    "python": "python.scm",
    "javascript": "js.scm",
    "typescript": "tsjs.scm",
    "tsx": "tsjs.scm",
}


def _load_query(lang: str) -> Query:
    if lang not in _QUERY_CACHE:
        raw = files("toji.queries").joinpath(_QUERY_FILES[lang]).read_text()
        _QUERY_CACHE[lang] = Query(get_language(lang), raw)
    return _QUERY_CACHE[lang]


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _clean_docstring(raw: str) -> str:
    s = raw.strip()
    for q in ('"""', "'''"):
        if s.startswith(q) and s.endswith(q) and len(s) >= 2 * len(q):
            s = s[len(q):-len(q)]
            break
    return textwrap.dedent(s).strip()


def extract(lang: str, src: bytes, file_id: int, relpath: str) -> tuple[list[Symbol], list[Edge]]:
    """Return (symbols, edges) for one file. Never raises for bad source."""
    if lang == "python":
        return _extract_python(src, file_id, relpath)
    return _extract_tsjs(lang, src, file_id, relpath)


def _line_count(src: bytes) -> int:
    if not src:
        return 0
    return src.count(b"\n") + (0 if src.endswith(b"\n") else 1)


def _module_symbol(file_id: int, relpath: str, src: bytes, docstring: str | None) -> Symbol:
    return Symbol(
        file_id=file_id,
        kind="module",
        name=relpath,
        qualname=relpath,
        docstring=docstring,
        line_start=1,
        line_end=_line_count(src),
    )


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def _py_qualname(node: Node, name: str) -> str:
    parts = [name]
    cur = node.parent
    while cur is not None and cur.type != "module":
        if cur.type in ("class_definition", "function_definition"):
            n = cur.child_by_field_name("name")
            if n is not None:
                parts.insert(0, n.text.decode("utf-8", "replace"))
        cur = cur.parent
    return ".".join(parts)


def _py_signature(kind: str, name: str, params: str, supers: str) -> str:
    if kind == "class":
        return f"class {name}({supers})" if supers else f"class {name}"
    return f"def {name}{params}"


def _py_decorators(node: Node) -> list[Node]:
    out: list[Node] = []
    cur = node.prev_named_sibling
    while cur is not None and cur.type == "decorator":
        out.append(cur)
        cur = cur.prev_named_sibling
    out.reverse()
    return out


def _extract_python(src: bytes, file_id: int, relpath: str) -> tuple[list[Symbol], list[Edge]]:
    root = parse_tree("python", src)
    query = _load_query("python")

    symbols: list[Symbol] = []
    edges: list[Edge] = []
    by_node: dict[int, Symbol] = {}
    sym_to_node: dict[int, Node] = {}
    module_doc: str | None = None
    pending_calls: list[Node] = []
    pending_imports: list[dict] = []

    def add_def(caps: dict, cls: bool) -> None:
        nonlocal module_doc
        node = caps["class" if cls else "func"][0]
        existing = by_node.get(node.id)
        if existing is not None:
            # duplicate pattern match (with/without docstring): upgrade if this
            # one carries the docstring and the earlier one did not
            if caps.get("docstring") and not existing.docstring:
                existing.docstring = _clean_docstring(_text(caps["docstring"][0]))
            return
        name = _text(caps["name"][0])
        qual = _py_qualname(node, name)
        params = _text(caps["params"][0]) if caps.get("params") else ""
        doc = _clean_docstring(_text(caps["docstring"][0])) if caps.get("docstring") else None
        decorators = _py_decorators(node)
        start = decorators[0].start_point.row + 1 if decorators else node.start_point.row + 1
        kind = "class" if cls else (
            "method"
            if node.parent and node.parent.type == "block" and node.parent.parent and node.parent.parent.type == "class_definition"
            else "function"
        )
        sym = Symbol(
            file_id=file_id,
            kind=kind,
            name=name,
            qualname=qual,
            signature=_py_signature(kind, name, params, ""),
            docstring=doc,
            line_start=start,
            line_end=node.end_point.row + 1,
        )
        if cls:
            arglist = node.child_by_field_name("superclasses")
            supers = arglist.text.decode("utf-8", "replace") if arglist is not None else ""
            sym.signature = _py_signature("class", name, "", supers)
        symbols.append(sym)
        by_node[node.id] = sym
        sym_to_node[id(sym)] = node
        # decorator calls attach to this def (e.g. @app.route('/x'))
        for dec in decorators:
            for _, dcaps in QueryCursor(query).matches(dec):
                if dcaps.get("callee"):
                    edges.append(Edge(src_sym=id(sym), kind=EDGE_CALLS, dst_name=_text(dcaps["callee"][0]), line=dec.start_point.row + 1))

    for _, caps in QueryCursor(query).matches(root):
        if caps.get("func"):
            add_def(caps, cls=False)
        elif caps.get("class"):
            add_def(caps, cls=True)
        elif caps.get("mod_doc") and module_doc is None:
            module_doc = _clean_docstring(_text(caps["docstring"][0]))
        elif caps.get("callee"):
            n = caps["callee"][0]
            # decorator calls are attached to the def in add_def; skip here
            if n.parent and n.parent.type == "call" and n.parent.parent and n.parent.parent.type == "decorator":
                continue
            pending_calls.append(n)
        elif caps.get("imp"):
            pending_imports.append(caps["imp"][0])

    module = _module_symbol(file_id, relpath, src, module_doc)
    symbols.insert(0, module)
    by_node[root.id] = module

    def owner_of(node: Node) -> Symbol:
        cur = node.parent
        while cur is not None and cur.type != "module":
            if cur.id in by_node:
                return by_node[cur.id]
            cur = cur.parent
        return module

    for callee in pending_calls:
        call_node = callee.parent
        edges.append(Edge(src_sym=id(owner_of(callee)), kind=EDGE_CALLS, dst_name=_text(callee), line=call_node.start_point.row + 1))

    for node in pending_imports:
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    edges.append(Edge(src_sym=id(module), kind=EDGE_IMPORTS, dst_name=child.text.decode("utf-8", "replace"), line=module.line_start))
                elif child.type == "aliased_import":
                    dn = child.named_children[0] if child.named_children else None
                    if dn is not None:
                        edges.append(Edge(src_sym=id(module), kind=EDGE_IMPORTS, dst_name=dn.text.decode("utf-8", "replace"), line=module.line_start))
        else:  # import_from_statement
            mod = node.child_by_field_name("module_name")
            mod_id = mod.id if mod is not None else None
            mod_name = _text(mod)
            for child in node.named_children:
                if child.id == mod_id:
                    continue
                if child.type == "dotted_name":
                    dst = f"{mod_name}.{_text(child)}"
                elif child.type == "aliased_import":
                    dn = child.named_children[0] if child.named_children else None
                    dst = f"{mod_name}.{_text(dn)}" if dn is not None else ""
                elif child.type == "wildcard_import":
                    dst = f"{mod_name}.*"
                else:
                    continue
                if dst:
                    edges.append(Edge(src_sym=id(module), kind=EDGE_IMPORTS, dst_name=dst, line=module.line_start))

    # contains: class -> methods (not descending into nested classes)
    for sym in symbols:
        if sym.kind != "class":
            continue
        node = sym_to_node.get(id(sym))
        if node is None:
            continue
        for method_node in _class_methods(node):
            edges.append(Edge(src_sym=id(sym), kind=EDGE_CONTAINS, dst_name=_py_qualname(method_node, _text(method_node.child_by_field_name("name"))), line=sym.line_start))

    # inherits: class -> base names
    for sym in symbols:
        if sym.kind != "class":
            continue
        node = sym_to_node.get(id(sym))
        if node is None:
            continue
        arglist = node.child_by_field_name("superclasses")
        if arglist is None:
            continue
        for base in arglist.named_children:
            if base.type in ("identifier", "attribute"):
                edges.append(Edge(src_sym=id(sym), kind=EDGE_INHERITS, dst_name=base.text.decode("utf-8", "replace"), line=sym.line_start))

    return _finalize(symbols, edges)


def _class_methods(cls_node: Node) -> list[Node]:
    out: list[Node] = []
    stack = [cls_node]
    while stack:
        cur = stack.pop()
        if cur.type == "function_definition" and cur is not cls_node:
            out.append(cur)
            continue
        if cur.type == "class_definition" and cur is not cls_node:
            continue  # do not descend into nested classes
        for child in cur.named_children:
            stack.append(child)
    return out


# ---------------------------------------------------------------------------
# TS / JS
# ---------------------------------------------------------------------------

def _tsjs_qualname(node: Node, name: str) -> str:
    parts = [name]
    cur = node.parent
    while cur is not None:
        if cur.type in ("class_declaration", "abstract_class_declaration"):
            n = cur.child_by_field_name("name")
            if n is not None:
                parts.insert(0, n.text.decode("utf-8", "replace"))
        cur = cur.parent
    return ".".join(parts)


def _extract_tsjs(lang: str, src: bytes, file_id: int, relpath: str) -> tuple[list[Symbol], list[Edge]]:
    root = parse_tree(lang, src)
    query = _load_query(lang)

    symbols: list[Symbol] = []
    edges: list[Edge] = []
    by_node: dict[int, Symbol] = {}
    sym_to_node: dict[int, Node] = {}

    for _, caps in QueryCursor(query).matches(root):
        if caps.get("func") or caps.get("method"):
            node = caps["func"][0] if caps.get("func") else caps["method"][0]
            kind = "function" if caps.get("func") else "method"
            name = _text(caps["name"][0])
            params = _text(caps["params"][0]) if caps.get("params") else "()"
            qual = _tsjs_qualname(node, name)
            if kind == "function":
                sig = f"function {name}{params}"
            else:
                sig = f"{name}{params}"
            sym = Symbol(
                file_id=file_id, kind=kind, name=name, qualname=qual, signature=sig,
                line_start=node.start_point.row + 1, line_end=node.end_point.row + 1,
            )
            symbols.append(sym)
            by_node[node.id] = sym
            sym_to_node[id(sym)] = node
        elif caps.get("varfn"):
            node = caps["varfn"][0]
            name = _text(caps["name"][0])
            params = _text(caps["params"][0]) if caps.get("params") else "()"
            qual = _tsjs_qualname(node, name)
            sym = Symbol(
                file_id=file_id, kind="variable", name=name, qualname=qual,
                signature=f"{name}{params}",
                line_start=node.start_point.row + 1, line_end=node.end_point.row + 1,
            )
            symbols.append(sym)
            by_node[node.id] = sym
            sym_to_node[id(sym)] = node
        elif caps.get("class"):
            node = caps["class"][0]
            name = _text(caps["name"][0])
            qual = _tsjs_qualname(node, name)
            sig = f"class {name}"
            base = caps.get("base")
            if base:
                sig += f" extends {_text(base[0])}"
            sym = Symbol(
                file_id=file_id, kind="class", name=name, qualname=qual, signature=sig,
                line_start=node.start_point.row + 1, line_end=node.end_point.row + 1,
            )
            symbols.append(sym)
            by_node[node.id] = sym
            sym_to_node[id(sym)] = node
            if base:
                edges.append(Edge(src_sym=id(sym), kind=EDGE_INHERITS, dst_name=_text(base[0]), line=sym.line_start))
        elif caps.get("interface"):
            node = caps["interface"][0]
            name = _text(caps["name"][0])
            base = caps.get("base")
            existing = by_node.get(node.id)
            if existing is None:
                sym = Symbol(
                    file_id=file_id, kind="interface", name=name, qualname=name,
                    signature=f"interface {name}",
                    line_start=node.start_point.row + 1, line_end=node.end_point.row + 1,
                )
                symbols.append(sym)
                by_node[node.id] = sym
                sym_to_node[id(sym)] = node
                existing = sym
            if base:
                edges.append(Edge(src_sym=id(existing), kind=EDGE_INHERITS, dst_name=_text(base[0]), line=existing.line_start))
        elif caps.get("type"):
            node = caps["type"][0]
            name = _text(caps["name"][0])
            sym = Symbol(
                file_id=file_id, kind="type", name=name, qualname=name,
                signature=f"type {name}",
                line_start=node.start_point.row + 1, line_end=node.end_point.row + 1,
            )
            symbols.append(sym)
            by_node[node.id] = sym
            sym_to_node[id(sym)] = node
        elif caps.get("enum"):
            node = caps["enum"][0]
            name = _text(caps["name"][0])
            sym = Symbol(
                file_id=file_id, kind="enum", name=name, qualname=name,
                signature=f"enum {name}",
                line_start=node.start_point.row + 1, line_end=node.end_point.row + 1,
            )
            symbols.append(sym)
            by_node[node.id] = sym
            sym_to_node[id(sym)] = node

    module = _module_symbol(file_id, relpath, src, None)
    symbols.insert(0, module)
    by_node[root.id] = module

    # contains: class -> methods
    for sym in symbols:
        if sym.kind != "class":
            continue
        node = sym_to_node.get(id(sym))
        if node is None:
            continue
        for child in node.named_children:
            if child.type == "class_body":
                for member in child.named_children:
                    if member.id in by_node and by_node[member.id].kind == "method":
                        edges.append(Edge(src_sym=id(sym), kind=EDGE_CONTAINS, dst_name=by_node[member.id].qualname, line=sym.line_start))

    # imports -> edges from module symbol
    for _, caps in QueryCursor(query).matches(root):
        if caps.get("imp") and caps.get("src"):
            src_text = _text(caps["src"][0]).strip("'\"")
            edges.append(Edge(src_sym=id(module), kind=EDGE_IMPORTS, dst_name=src_text, line=module.line_start))

    # calls -> attach to nearest enclosing def/class
    for _, caps in QueryCursor(query).matches(root):
        if not caps.get("callee"):
            continue
        call_node = None
        for n in caps["callee"]:
            if n.parent and n.parent.type == "call_expression":
                call_node = n.parent
                break
        if call_node is None:
            continue
        owner = None
        cur = call_node.parent
        while cur is not None and cur.type != "program":
            if cur.id in by_node:
                owner = by_node[cur.id]
                break
            cur = cur.parent
        if owner is None:
            edges.append(Edge(src_sym=id(module), kind=EDGE_CALLS, dst_name=_text(caps["callee"][0]), line=call_node.start_point.row + 1))
        else:
            edges.append(Edge(src_sym=id(owner), kind=EDGE_CALLS, dst_name=_text(caps["callee"][0]), line=call_node.start_point.row + 1))

    return _finalize(symbols, edges)


def _finalize(symbols: list[Symbol], edges: list[Edge]) -> tuple[list[Symbol], list[Edge]]:
    """Remap edge.src_sym from owner object id() to index in the symbol list,
    and drop exact duplicate (src_sym, kind, dst_name) edges."""
    idx = {id(s): i for i, s in enumerate(symbols)}
    seen: set[tuple[int, str, str]] = set()
    out: list[Edge] = []
    for e in edges:
        e.src_sym = idx[e.src_sym]
        key = (e.src_sym, e.kind, e.dst_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return symbols, out
