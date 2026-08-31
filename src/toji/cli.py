"""toji CLI: index codebases into graph memory, answer with evidence.

Rendering: rich tables/panels/syntax for humans, plain JSON for machines
(--json bypasses rich entirely). Non-tty output degrades to plain text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .benchmark import run_graphcode, run_traceeval
from .graph import Graph
from .indexer import index as run_index
from .store import Store

console = Console()
err_console = Console(stderr=True)

KIND_STYLE = {
    "module": "dim",
    "class": "yellow",
    "interface": "magenta",
    "function": "green",
    "method": "cyan",
    "type": "blue",
    "enum": "magenta",
    "variable": "white",
}

SYNTAX_LANG = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "tsx": "tsx",
}


def _db_path(args, *, for_index: bool = False) -> Path:
    if getattr(args, "db", None):
        return Path(args.db)
    if for_index:
        return Path(args.target or ".").resolve() / ".toji" / "graph.db"
    return Path(".toji") / "graph.db"


def _open_store(args, *, for_index: bool = False) -> Store:
    db = _db_path(args, for_index=for_index)
    if not db.exists():
        err_console.print(f"no index at {db} — run `toji index [PATH]` first")
        sys.exit(1)
    return Store(db)


def _root(store: Store) -> Path:
    r = store.get_meta("root")
    return Path(r) if r else Path.cwd()


def _json(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def _resolve_or_report(args, store: Store, graph: Graph, name: str) -> list:
    syms = graph.resolve(name)
    if not syms:
        err_console.print(f"`{name}` not found in index")
        sys.exit(1)
    if len(syms) > 1:
        err_console.print(f"`{name}` is ambiguous — candidates:")
        for s in syms:
            loc = f"{graph.path_of.get(s.file_id, '?')}:{s.line_start}"
            err_console.print(f"  {loc}  {s.kind:<8} {s.qualname}")
        sys.exit(1)
    return syms


def _loc(path: str, line: int) -> str:
    return f"{path}:{line}"


def _kind(kind: str) -> str:
    return f"[{KIND_STYLE.get(kind, 'white')}]{kind}[/]"


def _hits_table(title: str, hits) -> Table:
    t = Table(title=title, box=box.SIMPLE, header_style="bold cyan")
    t.add_column("Hop", justify="right", style="dim", no_wrap=True)
    t.add_column("Location", no_wrap=True)
    t.add_column("Kind")
    t.add_column("Symbol", no_wrap=True)
    t.add_column("Via", style="dim")
    for h in hits:
        t.add_row(str(h.hop), _loc(h.path, h.line), _kind(h.kind), h.qualname, h.via)
    return t


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_index(args) -> None:
    target = Path(args.target or ".").resolve()
    db = _db_path(args, for_index=True)
    report = run_index(target, db, force=args.force, jobs=args.jobs)
    if args.json:
        _json({
            "new": report.new, "changed": report.changed, "removed": report.removed,
            "unchanged": report.unchanged, "symbols": report.symbols,
            "edges": report.edges, "failed": report.failed, "elapsed": report.elapsed,
        })
        return
    console.print(
        f"indexed {report.indexed} file(s): {report.new} new, {report.changed} changed, "
        f"{report.removed} removed, {report.unchanged} unchanged",
        style="bold green",
    )
    console.print(f"  {report.symbols} symbols, {report.edges} edges ({report.elapsed:.1f}s)")
    for f in report.failed:
        err_console.print(f"failed to index {f}", style="red")


def cmd_map(args, store: Store, graph: Graph) -> None:
    file_of = {s.file_id: s.name for s in graph.symbols if s.kind == "module"}
    arg = args.target
    if arg is None:
        # bare map: repo skeleton
        counts: dict[str, int] = {}
        for s in graph.symbols:
            if s.kind == "module":
                continue
            key = file_of.get(s.file_id, "?")
            counts[key] = counts.get(key, 0) + 1
        rows = sorted(counts.items())
        if args.json:
            _json([{"path": p, "symbols": c} for p, c in rows[: args.limit]])
            return
        t = Table(title=f"repo skeleton — {len(rows)} files", box=box.SIMPLE, header_style="bold cyan")
        t.add_column("File")
        t.add_column("Symbols", justify="right")
        for p, c in rows[: args.limit]:
            t.add_row(f"[cyan]{p}[/]", str(c))
        console.print(t)
        if len(rows) > args.limit:
            console.print(f"  … {len(rows) - args.limit} more (use --limit)", style="dim")
        return

    fid = next(
        (s.file_id for s in graph.symbols if s.kind == "module" and s.name == arg), None
    )
    if fid is not None:
        syms = sorted((s for s in graph.symbols if s.file_id == fid), key=lambda x: x.line_start)
        if args.json:
            _json([{"kind": s.kind, "qualname": s.qualname, "signature": s.signature,
                    "docstring": s.docstring, "line_start": s.line_start, "line_end": s.line_end}
                   for s in syms if s.kind != "module"])
            return
        t = Table(title=f"{arg}  ({len(syms) - 1} symbols)", box=box.SIMPLE, header_style="bold cyan")
        t.add_column("Line", justify="right", style="dim", no_wrap=True)
        t.add_column("Kind")
        t.add_column("Signature", no_wrap=True)
        t.add_column("Doc")
        for s in syms:
            if s.kind == "module":
                continue
            first = s.docstring.splitlines()[0][:100] if s.docstring else ""
            t.add_row(f"L{s.line_start}", _kind(s.kind), s.signature or s.qualname, first)
        console.print(t)
        return

    resolved = _resolve_or_report(args, store, graph, arg)
    s = resolved[0]
    if args.json:
        _json({"kind": s.kind, "qualname": s.qualname, "signature": s.signature,
               "docstring": s.docstring, "path": graph.path_of.get(s.file_id, "?"),
               "line_start": s.line_start, "line_end": s.line_end})
        return
    body = f"[bold]{s.signature or s.qualname}[/]"
    if s.docstring:
        body += f"\n\n{s.docstring}"
    loc = f"{graph.path_of.get(s.file_id, '?')}:{s.line_start}-{s.line_end}"
    console.print(Panel(body, title=f"[{KIND_STYLE.get(s.kind, 'white')}]{s.qualname}[/]", subtitle=loc))


def cmd_find(args, store: Store, graph: Graph) -> None:
    try:
        pat = re.compile(args.pattern, re.IGNORECASE)
    except re.error as e:
        err_console.print(f"bad pattern: {e}")
        sys.exit(1)
    rows = [s for s in graph.symbols if s.kind != "module" and (pat.search(s.qualname) or pat.search(s.name))]
    rows.sort(key=lambda s: (graph.path_of.get(s.file_id, "?"), s.line_start))
    rows = rows[: args.limit]
    if args.json:
        _json([{"path": graph.path_of.get(s.file_id, "?"), "line": s.line_start,
                "kind": s.kind, "qualname": s.qualname, "signature": s.signature} for s in rows])
        return
    if not rows:
        console.print("no matches")
        return
    t = Table(box=box.SIMPLE, header_style="bold cyan")
    t.add_column("Location", no_wrap=True)
    t.add_column("Kind")
    t.add_column("Qualname", no_wrap=True)
    for s in rows:
        t.add_row(_loc(graph.path_of.get(s.file_id, "?"), s.line_start), _kind(s.kind), s.qualname)
    console.print(t)


def cmd_callers(args, store: Store, graph: Graph) -> None:
    res = graph.callers(args.symbol)
    if not res.found:
        _resolve_or_report(args, store, graph, args.symbol)  # prints not-found, exits
    if args.json:
        _json([{"hop": h.hop, "qualname": h.qualname, "kind": h.kind, "path": h.path, "line": h.line, "via": h.via} for h in res.hits])
        return
    span = f" across {res.definitions} definitions" if res.definitions > 1 else ""
    console.print(f"callers of [bold]{args.symbol}[/] — {len(res.hits)} symbol(s), 1 hop{span}", style="green")
    if res.hits:
        console.print(_hits_table(None, res.hits))


def cmd_calls(args, store: Store, graph: Graph) -> None:
    syms = _resolve_or_report(args, store, graph, args.symbol)
    hits, unresolved = graph.calls_of(args.symbol)
    if args.json:
        _json({
            "resolved": [{"path": h.path, "line": h.line, "kind": h.kind, "qualname": h.qualname} for h in sorted(hits, key=lambda x: (x.path, x.line))],
            "unresolved": [{"name": n, "line": l} for n, l in sorted(unresolved)],
        })
        return
    console.print(f"calls from [bold]{args.symbol}[/] — {len(hits)} resolved, {len(unresolved)} unresolved", style="green")
    if hits:
        console.print(_hits_table(None, sorted(hits, key=lambda x: (x.path, x.line))))
    where = graph.path_of.get(syms[0].file_id, "?")
    for name, line in sorted(unresolved):
        console.print(f"  {_loc(where, line)}  (unresolved: [red]{name}[/])")


def cmd_deps(args, store: Store, graph: Graph) -> None:
    found = any(s.kind == "module" and s.name == args.path for s in graph.symbols)
    if not found:
        err_console.print(f"`{args.path}` not found in index")
        sys.exit(1)
    imports, imported_by, unresolved = graph.deps(args.path)
    if args.json:
        _json({"imports": [{"name": d, "resolved": r} for d, r in sorted(imports)],
               "imported_by": [{"file": i, "via": d} for i, d, _ in sorted(imported_by)]})
        return
    console.print(f"deps of [bold]{args.path}[/]", style="green")
    for dst, resolved in sorted(imports):
        arrow = f" -> [cyan]{resolved}[/]" if resolved else ""
        console.print(f"  imports: [yellow]{dst}[/]{arrow}")
    for importer, dst, _line in sorted(imported_by):
        console.print(f"  imported by: [cyan]{importer}[/] (via [yellow]{dst}[/])")


def cmd_read(args, store: Store, graph: Graph) -> None:
    syms = _resolve_or_report(args, store, graph, args.symbol)
    s = syms[0]
    root = _root(store)
    rel = graph.path_of.get(s.file_id, "")
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        err_console.print(f"cannot read {rel}: {e}")
        sys.exit(1)
    lines = text.splitlines()
    ctx = args.context
    start = max(1, s.line_start - ctx)
    end = min(len(lines), s.line_end + ctx)
    if args.json:
        _json({"path": rel, "line_start": s.line_start, "line_end": s.line_end,
               "lines": [{"n": i + 1, "text": lines[i]} for i in range(start - 1, end)]})
        return
    lang = SYNTAX_LANG.get(next((f["lang"] for f in _files(store) if f["path"] == rel), ""), "text")
    snippet = Syntax(
        text, lang, line_numbers=True, line_range=(start, end),
        highlight_lines={s.line_start}, word_wrap=False,
    )
    console.print(Panel(snippet, title=f"{rel} — {s.qualname}", subtitle=f"{start}-{end}"))


def _files(store: Store) -> list[dict]:
    files, _, _ = store.load_graph()
    return files


def cmd_blast(args, store: Store, graph: Graph) -> None:
    res = graph.blast(args.symbol, depth=args.depth, max_nodes=args.max_nodes, forward=args.forward)
    if not res.found:
        _resolve_or_report(args, store, graph, args.symbol)  # prints not-found, exits
    if args.json:
        _json({"root": args.symbol, "hits": [{"hop": h.hop, "qualname": h.qualname, "kind": h.kind,
                "path": h.path, "line": h.line, "via": h.via} for h in res.hits],
               "truncated": res.truncated, "unresolved": res.unresolved,
               "definitions": res.definitions})
        return
    direction = "forward" if args.forward else "reverse (calls/contains/inherits)"
    span = f", across {res.definitions} definitions" if res.definitions > 1 else ""
    style = "bold red" if len(res.hits) > 0 else "green"
    console.print(
        f"blast radius of [bold]{args.symbol}[/] — {len(res.hits)} symbol(s), "
        f"{res.max_hop} hop(s), {direction}{span}",
        style=style,
    )
    if res.hits:
        console.print(_hits_table(None, res.hits))
    if res.truncated:
        console.print(f"  … truncated at {args.max_nodes} nodes (--max-nodes)", style="dim")


def cmd_stats(args, store: Store, graph: Graph) -> None:
    st = store.stats()
    if args.json:
        _json(st)
        return
    t = Table(title="toji index", box=box.SIMPLE, header_style="bold cyan")
    t.add_column("Metric")
    t.add_column("Count", justify="right")
    t.add_row("files", str(st["files"]))
    for lang, n in sorted(st["files_by_lang"].items()):
        t.add_row(f"  {lang}", str(n))
    t.add_row("symbols", str(st["symbols"]))
    for kind, n in sorted(st["symbols_by_kind"].items()):
        t.add_row(f"  {kind}", str(n))
    t.add_row("edges", str(st["edges"]))
    t.add_row(f"  calls", f"{st['calls']} ({graph.unresolved_calls} unresolved)")
    for kind, n in sorted(st["edges_by_kind"].items()):
        if kind == "calls":
            continue
        t.add_row(f"  {kind}", str(n))
    console.print(t)

def cmd_benchmark(args) -> None:
    if args.suite == "graphcode":
        report = run_graphcode(Path(args.dataset), Path(args.repos), limit=args.limit)
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    else:
        report = run_traceeval(
            Path(args.corpus),
            ids=Path(args.ids) if args.ids else None,
            languages=tuple(args.languages),
            limit=args.limit,
            workers=args.workers,
            output_dir=Path(args.output_dir) if args.output_dir else None,
        )

    if args.json:
        _json(report)
        return
    metrics = report["metrics"]
    console.print(f"[bold]{report['benchmark']}[/]: {report['scored']} scored")
    console.print(
        f"precision {metrics['precision']:.3f}  recall {metrics['recall']:.3f}  F1 {metrics['f1']:.3f}"
    )
    if args.suite == "graphcode":
        console.print(
            f"macro F1 {metrics['macro_f1']:.3f}  exact match {metrics['exact_match']:.3f}  "
            f"skipped {len(report['skipped'])}"
        )
    else:
        for language, summary in report["by_language"].items():
            console.print(f"  {language}: {summary['scored']} programs, F1 {summary['f1']:.3f}")
        for output in report["output_files"]:
            console.print(f"  wrote {output}")



# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="toji",
        description="Codebase graph memory: index structure, answer with evidence.",
    )
    p.add_argument("--version", action="version", version=f"toji {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="path to graph.db (default: <target>/.toji/graph.db)")
    common.add_argument("--json", action="store_true", help="emit JSON instead of text")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("index", parents=[common], help="index or reindex a codebase")
    i.add_argument("target", nargs="?", default=".", help="directory to index (default: .)")
    i.add_argument("--force", action="store_true", help="full rebuild, ignore mtime/size cache")
    i.add_argument("--jobs", type=int, default=None, help="parallel extract workers")
    i.set_defaults(func=cmd_index)

    m = sub.add_parser("map", parents=[common], help="file skeleton or symbol detail")
    m.add_argument("target", nargs="?", default=None, help="file path or symbol name (default: repo skeleton)")
    m.add_argument("--limit", type=int, default=200)
    m.set_defaults(func=cmd_map)

    f = sub.add_parser("find", parents=[common], help="search symbols by name/qualname regex")
    f.add_argument("pattern")
    f.add_argument("--limit", type=int, default=100)
    f.set_defaults(func=cmd_find)

    c = sub.add_parser("calls", parents=[common], help="forward call edges of a symbol")
    c.add_argument("symbol")
    c.set_defaults(func=cmd_calls)

    c2 = sub.add_parser("callers", parents=[common], help="who calls a symbol (1-hop reverse)")
    c2.add_argument("symbol")
    c2.set_defaults(func=cmd_callers)

    d = sub.add_parser("deps", parents=[common], help="import edges for a file")
    d.add_argument("path")
    d.set_defaults(func=cmd_deps)

    r = sub.add_parser("read", parents=[common], help="print a symbol's source lines from disk")
    r.add_argument("symbol")
    r.add_argument("--context", type=int, default=0, help="extra lines around the symbol")
    r.set_defaults(func=cmd_read)

    b = sub.add_parser("blast", parents=[common], help="BFS blast radius: what a change affects")
    b.add_argument("symbol")
    b.add_argument("--depth", type=int, default=None, help="max hops (default: unlimited)")
    b.add_argument("--max-nodes", type=int, default=200, help="BFS node cap")
    b.add_argument("--forward", action="store_true", help="also traverse forward call edges")
    b.set_defaults(func=cmd_blast)

    s = sub.add_parser("stats", parents=[common], help="index statistics")
    s.set_defaults(func=cmd_stats)

    bench = sub.add_parser("benchmark", help="run graph correctness benchmarks")
    suites = bench.add_subparsers(dest="suite", required=True)

    gc = suites.add_parser("graphcode", help="run GraphCode-Bench JSONL")
    gc.add_argument("dataset", help="path to bench100.jsonl or bench500_balanced.jsonl")
    gc.add_argument("repos", help="directory containing benchmark repository checkouts")
    gc.add_argument("--limit", type=int)
    gc.add_argument("--output", help="write the complete JSON report")
    gc.add_argument("--json", action="store_true", help="emit JSON instead of text")
    gc.set_defaults(func=cmd_benchmark)

    te = suites.add_parser("traceeval", help="run TraceEval Python/JavaScript corpus")
    te.add_argument("corpus", help="directory containing language/program/callgraph.json")
    te.add_argument("--ids", help="optional TraceEval train_ids.json or test_ids.json")
    te.add_argument("--languages", nargs="+", choices=("python", "javascript"), default=("python", "javascript"))
    te.add_argument("--limit", type=int)
    te.add_argument("--workers", type=int, default=4)
    te.add_argument("--output-dir", help="write files compatible with TraceEval compute_metrics.py")
    te.add_argument("--json", action="store_true", help="emit JSON instead of text")
    te.set_defaults(func=cmd_benchmark)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = None
    try:
        if args.command in ("index", "benchmark"):
            args.func(args)
            return 0
        store = _open_store(args)
        with_edges = args.command in ("calls", "callers", "deps", "blast", "stats")
        graph = Graph(store, with_edges=with_edges)
        args.func(args, store, graph)
        return 0
    except SystemExit:
        raise
    except KeyboardInterrupt:
        err_console.print("interrupted", style="bold red")
        return 130
    except Exception as e:  # noqa: BLE001 - CLI boundary
        err_console.print(f"error: {e}", style="bold red")
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
