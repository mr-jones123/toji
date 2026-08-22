"""tree-sitter language loading and parsing (API-version tolerant)."""

from __future__ import annotations

import importlib

from tree_sitter import Language

_LOADERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("tree_sitter_python", ("language",)),
    "javascript": ("tree_sitter_javascript", ("language",)),
    "typescript": ("tree_sitter_typescript", ("language_typescript",)),
    "tsx": ("tree_sitter_typescript", ("language_tsx",)),
}

_cache: dict[str, Language] = {}


def get_language(name: str) -> Language:
    if name in _cache:
        return _cache[name]
    pkg, attrs = _LOADERS[name]
    mod = importlib.import_module(pkg)
    for attr in attrs:
        fn = getattr(mod, attr, None)
        if fn is None:
            continue
        try:
            lang = Language(fn())
        except Exception:
            continue
        _cache[name] = lang
        return lang
    raise RuntimeError(f"no usable language export in {pkg} (wanted {attrs})")


def parse_tree(name: str, src: bytes):
    """Parse source bytes, returning the tree's root node."""
    from tree_sitter import Parser

    parser = Parser(get_language(name))
    return parser.parse(src).root_node
