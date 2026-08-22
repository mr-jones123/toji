"""End-to-end tests: index, incremental reindex, graph queries, blast radius, CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from toji.cli import main
from toji.graph import Graph
from toji.indexer import index
from toji.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "demo"
N_FILES = 7  # pyapp 5 + tsapp 2; ignored/unsupported/node_modules skipped


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    dst = tmp_path / "repo"
    shutil.copytree(FIXTURE, dst)
    return dst


def index_repo(repo: Path, db: Path, **kw):
    return index(repo, db, **kw)


def open_graph(db: Path) -> Graph:
    return Graph(Store(db))


def qualnames(graph: Graph) -> set[str]:
    return {s.qualname for s in graph.symbols}


# ---------------------------------------------------------------------------
# indexing
# ---------------------------------------------------------------------------

def test_index_extracts_symbols(repo: Path, tmp_path: Path):
    db = tmp_path / "g.db"
    report = index_repo(repo, db)

    assert report.indexed == N_FILES
    assert report.failed == []

    g = open_graph(db)
    q = qualnames(g)
    for expected in (
        "pyapp/util.py", "pyapp/worker.py", "pyapp/main.py", "pyapp/cli.py",
        "helper", "run", "main", "entry",
        "Pipeline", "Pipeline.execute",
        "App", "App.run", "Child",
        "Config",
        "tsapp/util.ts", "tsapp/index.ts",
    ):
        assert expected in q, f"missing {expected}"

    py_helper = [s for s in g.symbols if s.qualname == "helper" and g.path_of.get(s.file_id) == "pyapp/util.py"][0]
    assert py_helper.kind == "function"
    assert py_helper.docstring == "Adds a and b."
    assert py_helper.signature == "def helper(a, b=1)"
    assert py_helper.line_start == 4

    # gitignored + unsupported + node_modules + env/venv never indexed
    paths = {g.path_of.get(s.file_id) for s in g.symbols}
    assert "ignored.py" not in paths
    assert "skipped.md" not in paths
    assert "node_modules/dep/index.ts" not in paths
    assert "venv/lib/site.py" not in paths
    assert "env/lib/site.py" not in paths
    assert ".env.local" not in paths
    assert "site_helper" not in qualnames(g)
    assert "env_helper" not in qualnames(g)

    # index report edge count matches what is actually stored (post-dedupe)
    g2 = open_graph(db)
    assert report.edges == len(g2.edges)


def test_reindex_incremental(repo: Path, tmp_path: Path):
    db = tmp_path / "g.db"
    r1 = index_repo(repo, db)
    r2 = index_repo(repo, db)
    assert r2.unchanged == N_FILES
    assert r2.indexed == 0

    # modify a file -> only it re-parses
    worker = repo / "pyapp" / "worker.py"
    worker.write_text(worker.read_text() + "\n\ndef extra():\n    return 7\n")
    r3 = index_repo(repo, db)
    assert r3.changed == 1
    g = open_graph(db)
    assert "extra" in qualnames(g)

    # delete a file -> symbols vanish; stale call edges keep their text but
    # resolution is best-effort (suffix fallback may land elsewhere)
    worker.unlink()
    r4 = index_repo(repo, db)
    assert r4.removed == 1
    g = open_graph(db)
    assert "run" not in qualnames(g)
    resolved, unresolved = g.calls_of("Pipeline.execute")
    names = {h.qualname for h in resolved} | {n for n, _ in unresolved}
    raw = {e.dst_name for e in g.edges if e.kind == "calls"}
    assert "run" in raw  # the written callee is still recorded as evidence
    assert names  # resolution is best-effort (suffix may land on App.run)


def test_force_rebuild(repo: Path, tmp_path: Path):
    db = tmp_path / "g.db"
    index_repo(repo, db)
    worker = repo / "pyapp" / "worker.py"
    worker.write_text(worker.read_text() + "\n\ndef extra():\n    return 7\n")
    r = index_repo(repo, db, force=True)
    assert r.indexed == N_FILES
    g = open_graph(db)
    assert "extra" in qualnames(g)


# ---------------------------------------------------------------------------
# graph queries
# ---------------------------------------------------------------------------

@pytest.fixture()
def graph(repo: Path, tmp_path: Path) -> Graph:
    db = tmp_path / "g.db"
    index_repo(repo, db)
    return open_graph(db)


def test_call_edges(graph: Graph):
    resolved, unresolved = graph.calls_of("App.run")
    assert {h.qualname for h in resolved} == {"helper"}
    assert unresolved == []

    resolved, unresolved = graph.calls_of("Pipeline.execute")
    assert {h.qualname for h in resolved} == {"run"}

    resolved, unresolved = graph.calls_of("main")
    assert unresolved == []  # p.execute now resolves via tail-component match
    assert {h.qualname for h in resolved} == {"Pipeline", "run", "Pipeline.execute"}


def test_callers(graph: Graph):
    res = graph.callers("run")
    callers = {(h.qualname, h.path) for h in res.hits}
    assert callers == {("main", "pyapp/main.py"), ("Pipeline.execute", "pyapp/main.py")}


def test_inherits_edge(graph: Graph):
    inherits = [(graph.by_id[e.src_sym].qualname, e.dst_name) for e in graph.edges if e.kind == "inherits"]
    assert ("Child", "App") in inherits


def test_blast_radius(graph: Graph):
    res = graph.blast("run")
    by_hop: dict[int, set[str]] = {}
    for h in res.hits:
        by_hop.setdefault(h.hop, set()).add(h.qualname)
    assert by_hop[1] == {"main", "Pipeline.execute"}
    assert by_hop[2] == {"entry", "Pipeline"}  # entry calls main; Pipeline contains execute


def test_blast_depth_limit(graph: Graph):
    res = graph.blast("run", depth=1)
    assert all(h.hop <= 1 for h in res.hits)
    assert {h.qualname for h in res.hits} == {"main", "Pipeline.execute"}


def test_blast_forward(graph: Graph):
    # forward adds callees (helper); reverse-only shows callers
    res = graph.blast("run", depth=1, forward=True)
    assert {h.qualname for h in res.hits} == {"main", "Pipeline.execute", "helper"}
    res = graph.blast("run", depth=1)
    assert {h.qualname for h in res.hits} == {"main", "Pipeline.execute"}


def test_blast_merges_same_name_definitions(graph: Graph):
    # both helpers share qualname "helper" across files -> blast merges both
    # definitions and reports callers of all of them
    res = graph.blast("helper")
    assert res.definitions == 2
    by_path = {(h.qualname, h.path) for h in res.hits}
    assert ("run", "pyapp/worker.py") in by_path      # py helper's caller
    assert ("App.run", "tsapp/index.ts") in by_path   # ts helper's caller


def test_blast_contains_class(graph: Graph):
    # changing a method affects its class
    res = graph.blast("Pipeline.execute", depth=1)
    assert "Pipeline" in {h.qualname for h in res.hits}


def test_deps(graph: Graph):
    imports, imported_by, _ = graph.deps("pyapp/worker.py")
    assert ("pyapp.util.helper", "pyapp/util.py") in imports
    assert any(i == "pyapp/main.py" for i, _, _ in imported_by)

    imports, imported_by, _ = graph.deps("tsapp/index.ts")
    assert any(dst == "./util" and resolved == "tsapp/util.ts" for dst, resolved in imports)


def test_reindex_hash_unchanged(repo: Path, tmp_path: Path):
    """git-checkout scenario: mtimes rewritten, content identical -> no re-extract."""
    import os

    db = tmp_path / "g.db"
    index_repo(repo, db)
    for p in repo.rglob("*"):
        if p.is_file() and p.suffix in (".py", ".ts", ".tsx", ".js"):
            os.utime(p, None)
    r = index_repo(repo, db)
    assert r.changed == 0
    assert r.new == 0
    assert r.unchanged == N_FILES


def test_decorator_call_attached_once():
    from toji.extract import extract

    syms, edges = extract("python", b'@app.route("/x")\ndef handler():\n    pass\n', 1, "t.py")
    call_edges = [(syms[e.src_sym].qualname, e.dst_name) for e in edges if e.kind == "calls"]
    assert call_edges == [("handler", "app.route")]


def test_aliased_import_no_duplicate_edge():
    from toji.extract import extract

    syms, edges = extract("python", b"import a.b as c\n", 1, "t.py")
    imports = [e.dst_name for e in edges if e.kind == "imports"]
    assert imports == ["a.b"]


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def run_cli(argv: list[str], capsys) -> tuple[int, str]:
    try:
        rc = main(argv)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def test_cli_index_and_queries(repo: Path, tmp_path: Path, capsys):
    db = tmp_path / "g.db"
    rc, out = run_cli(["index", str(repo), "--db", str(db)], capsys)
    assert rc == 0
    assert f"{N_FILES} file(s)" in out

    rc, out = run_cli(["find", "helper", "--db", str(db)], capsys)
    assert rc == 0
    assert "helper" in out

    rc, out = run_cli(["map", "pyapp/worker.py", "--db", str(db)], capsys)
    assert rc == 0
    assert "def run()" in out

    rc, out = run_cli(["callers", "run", "--db", str(db)], capsys)
    assert rc == 0
    assert "main.py" in out and "Pipeline.execute" in out

    rc, out = run_cli(["read", "run", "--db", str(db)], capsys)
    assert rc == 0
    assert "def run" in out

    rc, out = run_cli(["blast", "run", "--db", str(db)], capsys)
    assert rc == 0
    assert "blast radius of run" in out
    assert "Pipeline.execute" in out


def test_cli_json(repo: Path, tmp_path: Path, capsys):
    import json as j

    db = tmp_path / "g.db"
    run_cli(["index", str(repo), "--db", str(db)], capsys)
    rc, out = run_cli(["blast", "run", "--db", str(db), "--json"], capsys)
    assert rc == 0
    data = j.loads(out)
    assert data["root"] == "run"
    assert all("path" in h and "line" in h and "hop" in h for h in data["hits"])

    rc, out = run_cli(["find", "helper", "--db", str(db), "--json"], capsys)
    data = j.loads(out)
    assert len(data) == 2


def test_cli_no_index(tmp_path: Path, capsys):
    rc, out = run_cli(["find", "x", "--db", str(tmp_path / "missing.db")], capsys)
    assert rc == 1
    assert "no index" in out


def test_cli_ambiguous_errors(repo: Path, tmp_path: Path, capsys):
    db = tmp_path / "g.db"
    run_cli(["index", str(repo), "--db", str(db)], capsys)
    rc, out = run_cli(["read", "helper", "--db", str(db)], capsys)
    assert rc == 1
    assert "ambiguous" in out


def test_stats(repo: Path, tmp_path: Path, capsys):
    db = tmp_path / "g.db"
    run_cli(["index", str(repo), "--db", str(db)], capsys)
    rc, out = run_cli(["stats", "--db", str(db)], capsys)
    assert rc == 0
    assert "files" in out and str(N_FILES) in out
    assert "python" in out and "typescript" in out
