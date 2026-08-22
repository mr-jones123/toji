"""In-memory graph over the store + blast-radius BFS.

Call edges store the *written* callee text; resolution against indexed
symbols happens here at load time (exact qualname -> unique suffix -> unique
bare name). Unresolved callees are counted and surfaced, never guessed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .models import EDGE_CALLS, EDGE_CONTAINS, EDGE_INHERITS, EDGE_IMPORTS
from .store import Store


@dataclass(slots=True)
class Hit:
    hop: int
    qualname: str
    kind: str
    path: str
    line: int
    via: str


@dataclass(slots=True)
class BlastResult:
    root: str
    found: bool = False
    candidates: list = field(default_factory=list)  # unused; kept for compat
    hits: list[Hit] = field(default_factory=list)
    truncated: bool = False
    unresolved: int = 0
    max_hop: int = 0
    definitions: int = 1


class Graph:
    def __init__(self, store: Store, with_edges: bool = True):
        files_rows, self.symbols, self.edges = store.load_graph()
        if not with_edges:
            self.edges = []
        self.path_of = {f["id"]: f["path"] for f in files_rows}
        self.by_id = {s.id: s for s in self.symbols}
        self.by_qual: dict[str, list] = {}
        self.by_bare: dict[str, list] = {}
        self.by_stem: dict[str, list] = {}
        self.by_file: dict[int, list] = {}
        for s in self.symbols:
            self.by_qual.setdefault(s.qualname, []).append(s)
            self.by_bare.setdefault(s.name, []).append(s)
            self.by_file.setdefault(s.file_id, []).append(s)
            if s.kind == "module":
                stem = Path(s.name).stem
                self.by_stem.setdefault(stem, []).append(s)
                if s.name.endswith("__init__.py"):
                    self.by_stem.setdefault(Path(s.name).parent.name, []).append(s)

        self._rev_calls: dict[str, list[tuple[str, int]]] = {}
        self._fwd_calls: dict[str, list[tuple[str, int]]] = {}
        self._rev_contains: dict[str, list[str]] = {}
        self._fwd_contains: dict[str, list[str]] = {}
        self._rev_inherits: dict[str, list[str]] = {}
        self.unresolved_calls: int = 0
        # import-scoped names: file_id -> bare name -> symbols reachable via imports
        self.file_names: dict[int, dict[str, list]] = {}
        if not with_edges:
            return

        for e in self.edges:
            if e.kind == EDGE_IMPORTS:
                src = self.by_id[e.src_sym]
                if src.kind == "module":
                    for c in self.resolve_import(e.dst_name, src):
                        for s in self.by_file.get(c.file_id, []):
                            if s.kind == "module":
                                continue
                            bucket = self.file_names.setdefault(src.file_id, {}).setdefault(s.name, [])
                            if all(x.id != s.id for x in bucket):
                                bucket.append(s)
            src = self.by_id[e.src_sym]
            if e.kind == EDGE_CALLS:
                callees = self._resolve_edge(e.dst_name, src.file_id)
                if not callees:
                    self.unresolved_calls += 1
                for c in callees:
                    self._rev_calls.setdefault(c.qualname, []).append((src.qualname, e.line))
                    self._fwd_calls.setdefault(src.qualname, []).append((c.qualname, e.line))
            elif e.kind == EDGE_CONTAINS:
                self._rev_contains.setdefault(e.dst_name, []).append(src.qualname)
                self._fwd_contains.setdefault(src.qualname, []).append(e.dst_name)
            elif e.kind == EDGE_INHERITS:
                for base in self._resolve_edge(e.dst_name, src.file_id):
                    self._rev_inherits.setdefault(base.qualname, []).append(src.qualname)

    def _resolve_edge(self, name: str, file_id: int) -> list:
        """Strict, unique-only resolution for call/inherit edges.

        File-local exact, then import-scoped (what this file actually
        imports), then global unique suffix/bare. Multiple candidates ->
        unresolved ([]): an edge only resolves when toji is confident.
        """
        local = [s for s in self.by_qual.get(name, []) if s.file_id == file_id]
        if len(local) == 1:
            return local
        if len(local) > 1:
            return []
        imported = self.file_names.get(file_id, {}).get(name, [])
        if len(imported) == 1:
            return imported
        if len(imported) > 1:
            return []
        exact = self.by_qual.get(name, [])
        if len(exact) == 1:
            return exact
        if len(exact) > 1:
            return []
        if "." in name:
            tail = name.rsplit(".", 1)[-1]
            cands = self.by_bare.get(tail, [])
            if cands:
                bare = [s for s in cands if s.name == tail and s.kind != "module"]
                return bare if len(bare) == 1 else []
            return []
        cands = self.by_bare.get(name, [])
        suffix = [s for s in cands if s.qualname.endswith("." + name)]
        if len(suffix) == 1:
            return suffix
        bare = [s for s in cands if s.name == name and s.kind != "module"]
        return bare if len(bare) == 1 else []

    def resolve(self, name: str, file_id: int | None = None) -> list:
        """Exact (file-scoped) -> exact (global) -> suffix -> bare name.

        Dotted callee text (obj.method, module.func) resolves by its last
        component against the bare-name index. Backed by dict indexes, so it
        is O(#candidates), never O(#symbols).
        """
        if file_id is not None:
            local = [s for s in self.by_qual.get(name, []) if s.file_id == file_id]
            if local:
                return local
        exact = self.by_qual.get(name, [])
        if exact:
            return exact
        if "." in name:
            # attribute/module-qualified callee text: match the tail component
            tail = name.rsplit(".", 1)[-1]
            cands = self.by_bare.get(tail, [])
            if cands:
                return [s for s in cands if s.name == tail and s.kind != "module"]
            return []
        cands = self.by_bare.get(name, [])
        suffix = [s for s in cands if s.qualname.endswith("." + name)]
        if suffix:
            return suffix
        return [s for s in cands if s.name == name and s.kind != "module"]

    def resolve_import(self, dst: str, importer: object | None = None) -> list:
        """Resolve an import specifier to indexed symbols/files.

        With an importer module, module-scoped resolution wins: relative
        specifiers (./util) and dotted from-imports (pyapp.util.helper) match
        against modules in the importer's directory first. Falls back to
        generic symbol resolution, then unscoped module-stem matching.
        """
        clean = dst.strip("'\"")
        importer_dir = None
        if importer is not None:
            importer_dir = str(Path(self.path_of.get(importer.file_id, "")).parent)

        if importer_dir is not None:
            if clean.startswith(("./", "../")):
                return self._resolve_module(clean, importer_dir)
            if "." in clean:
                mod_part, _, rest = clean.rpartition(".")
                mods = self._resolve_module(mod_part, importer_dir)
                if mods:
                    for m in mods:
                        local = [
                            s for s in self.by_file.get(m.file_id, [])
                            if s.qualname == rest or s.qualname.endswith("." + rest) or s.name == rest
                        ]
                        if local:
                            return local
                    return mods

        syms = self.resolve(clean)
        if syms:
            return syms
        return self._resolve_module(clean, importer_dir)

    def _resolve_module(self, name: str, importer_dir: str | None = None) -> list:
        key = name.split("/")[-1].split(".")[-1]
        if not key:
            return []
        mods = list(self.by_stem.get(key, []))
        if importer_dir is not None:
            scoped = [m for m in mods if str(Path(m.name).parent) == importer_dir]
            if scoped:
                mods = scoped
        return mods

    def _sym(self, qualname: str):
        hits = self.by_qual.get(qualname, [])
        return hits[0] if hits else None

    def _hit(self, hop: int, qualname: str, via: str) -> Hit | None:
        s = self._sym(qualname)
        if s is None:
            return None
        return Hit(hop=hop, qualname=qualname, kind=s.kind, path=self.path_of.get(s.file_id, "?"), line=s.line_start, via=via)

    # -- queries -----------------------------------------------------------

    def callers(self, name: str) -> BlastResult:
        return self.blast(name, depth=1)

    def calls_of(self, name: str) -> tuple[list[Hit], list[tuple[str, int]]]:
        """Forward calls from a symbol: (resolved hits, unresolved (name, line))."""
        syms = self.resolve(name)
        out: list[Hit] = []
        unresolved: list[tuple[str, int]] = []
        for s in syms:
            for e in self.edges:
                if e.src_sym != s.id or e.kind != EDGE_CALLS:
                    continue
                callees = self._resolve_edge(e.dst_name, s.file_id)
                if callees:
                    for c in callees:
                        hit = self._hit(0, c.qualname, "called")
                        if hit:
                            hit.line = e.line
                            out.append(hit)
                else:
                    unresolved.append((e.dst_name, e.line))
        return out, unresolved

    def deps(self, path: str) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]], list[str]]:
        """Import edges for a file: (imports, imported_by, unresolved).

        imports: (dst_name, resolved_file_or_None)
        imported_by: (importer_path, dst_name, line)
        """
        mod = next((s for s in self.symbols if s.kind == "module" and s.name == path), None)
        if mod is None:
            return [], [], []
        imports: list[tuple[str, str]] = []
        for e in self.edges:
            if e.src_sym != mod.id or e.kind != EDGE_IMPORTS:
                continue
            callees = self.resolve_import(e.dst_name, mod)
            files = sorted({self.path_of.get(c.file_id, "?") for c in callees})
            imports.append((e.dst_name, files[0] if len(files) == 1 else (",".join(files) if files else "")))
        imported_by: list[tuple[str, str, str]] = []
        for e in self.edges:
            if e.kind != EDGE_IMPORTS:
                continue
            src = self.by_id[e.src_sym]
            if src.kind != "module":
                continue
            for c in self.resolve_import(e.dst_name, src):
                if self.path_of.get(c.file_id) == path:
                    imported_by.append((self.path_of.get(src.file_id, "?"), e.dst_name, e.line))
        unresolved = [d for d, _ in imports if not _]
        return imports, sorted(set(imported_by)), unresolved

    # -- blast radius -------------------------------------------------------

    def blast(
        self,
        root: str,
        depth: int | None = None,
        max_nodes: int = 200,
        forward: bool = False,
    ) -> BlastResult:
        res = BlastResult(root=root)
        roots = self.resolve(root)
        if not roots:
            return res  # found=False
        res.found = True
        res.definitions = len(roots)

        seen: set[str] = {r.qualname for r in roots}
        queue: deque[tuple[str, int]] = deque((r.qualname, 0) for r in roots)
        hits: list[Hit] = []
        truncated = False
        max_hop = 0

        while queue:
            q, hop = queue.popleft()
            if depth is not None and hop >= depth:
                continue
            frontier: list[tuple[str, str]] = []  # (neighbor_qual, via)
            for caller, line in self._rev_calls.get(q, []):
                frontier.append((caller, f"called by {q} (line {line})"))
            for child in self._rev_inherits.get(q, []):
                frontier.append((child, f"extends {q}"))
            for cls in self._rev_contains.get(q, []):
                frontier.append((cls, "contains"))
            if forward:
                for callee, line in self._fwd_calls.get(q, []):
                    frontier.append((callee, f"calls {q} (line {line})"))
            else:
                # changing a class affects its methods even without --forward
                for meth in self._fwd_contains.get(q, []):
                    frontier.append((meth, "member of"))

            for neighbor, via in frontier:
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                hit = self._hit(hop + 1, neighbor, via)
                if hit:
                    hits.append(hit)
                    max_hop = max(max_hop, hop + 1)
                queue.append((neighbor, hop + 1))
                if len(seen) > max_nodes:
                    truncated = True
                    queue.clear()
                    break

        hits.sort(key=lambda h: (h.hop, h.path, h.line))
        res.hits = hits
        res.truncated = truncated
        res.unresolved = self.unresolved_calls
        res.max_hop = max_hop
        return res
