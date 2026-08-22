"""Core data models shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass

# extension -> tree-sitter language name
LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}

# symbol kinds emitted by extractors
KINDS = ("module", "class", "function", "method", "interface", "type", "enum", "variable")

# edge kinds
EDGE_CALLS = "calls"
EDGE_CONTAINS = "contains"
EDGE_IMPORTS = "imports"
EDGE_INHERITS = "inherits"


@dataclass(slots=True)
class Symbol:
    id: int = 0
    file_id: int = 0
    kind: str = ""
    name: str = ""
    qualname: str = ""
    signature: str = ""
    docstring: str | None = None
    line_start: int = 0
    line_end: int = 0


@dataclass(slots=True)
class Edge:
    id: int = 0
    src_sym: int = 0
    kind: str = ""
    dst_name: str = ""
    line: int = 0
