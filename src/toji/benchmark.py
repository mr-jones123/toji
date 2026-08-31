"""Adapters for GraphCode-Bench and TraceEval."""

from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .graph import Graph
from .indexer import index
from .models import EDGE_CALLS, Symbol
from .store import Store


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def _score(gold: set, predicted: set) -> dict[str, float | int]:
    return _prf(len(gold & predicted), len(predicted - gold), len(gold - predicted))


def _call_adjacency(graph: Graph) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    forward: dict[int, set[int]] = defaultdict(set)
    reverse: dict[int, set[int]] = defaultdict(set)
    for edge in graph.edges:
        if edge.kind != EDGE_CALLS:
            continue
        source = graph.by_id[edge.src_sym]
        for target in graph._resolve_edge(edge.dst_name, source.file_id):
            forward[source.id].add(target.id)
            reverse[target.id].add(source.id)
    return forward, reverse


def _repo_path(repos_dir: Path, repo: str) -> Path | None:
    owner, name = repo.split("/", 1)
    for candidate in (repos_dir / f"{owner}__{name}", repos_dir / owner / name, repos_dir / name):
        if candidate.is_dir():
            return candidate
    return None


def _anchor(graph: Graph, root: Path, name: str, source_path: str, source: str = "") -> list[Symbol]:
    source_path = source_path.replace("\\", "/")
    file_ids = {
        file_id
        for file_id, path in graph.path_of.items()
        if source_path == path or source_path.endswith("/" + path)
    }
    candidates = [
        symbol
        for symbol in graph.symbols
        if symbol.file_id in file_ids and symbol.name == name and symbol.kind != "module"
    ]
    if len(candidates) <= 1 or not source.strip():
        return candidates

    matched: list[Symbol] = []
    for file_id in file_ids:
        text = (root / graph.path_of[file_id]).read_text(errors="replace")
        offset = text.find(source.strip())
        if offset < 0:
            continue
        start = text.count("\n", 0, offset) + 1
        end = start + source.strip().count("\n")
        matched.extend(
            symbol
            for symbol in candidates
            if symbol.file_id == file_id and symbol.line_start <= end and symbol.line_end >= start
        )
    return matched if len(matched) == 1 else candidates


def _layers(
    start: set[int], adjacency: dict[int, set[int]], depth: int
) -> dict[int, set[int]]:
    seen = set(start)
    frontier = set(start)
    layers: dict[int, set[int]] = {}
    for hop in range(1, depth + 1):
        next_frontier = {target for source in frontier for target in adjacency.get(source, ())}
        next_frontier -= seen
        layers[hop] = next_frontier
        seen |= next_frontier
        frontier = next_frontier
    return layers


def _graphcode_gold(record: dict) -> dict[int, set[str]]:
    depth = int(record["hop_depth"])
    gold = record["gold"]
    if depth == 1:
        return {1: set(gold.get("calls", gold.get("hop_1", [])))}
    return {hop: set(gold.get(f"hop_{hop}", [])) for hop in range(1, depth + 1)}


def run_graphcode(dataset: Path, repos_dir: Path, limit: int | None = None) -> dict:
    """Run GraphCode-Bench JSONL against locally checked-out repositories."""
    records = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    if limit is not None:
        records = records[:limit]

    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        grouped[record["repo"]].append(record)

    cases: list[dict] = []
    skipped: list[dict] = []
    total_tp = total_fp = total_fn = exact = 0
    macro_f1 = 0.0

    with tempfile.TemporaryDirectory(prefix="toji-graphcode-") as tmp:
        for repo, repo_records in grouped.items():
            root = _repo_path(repos_dir, repo)
            if root is None:
                skipped.extend(
                    {"sample_id": record["sample_id"], "reason": f"repository not found: {repo}"}
                    for record in repo_records
                )
                continue

            db = Path(tmp) / f"{repo.replace('/', '__')}.db"
            index(root, db, force=True)
            store = Store(db)
            try:
                graph = Graph(store)
                forward, reverse = _call_adjacency(graph)
                for record in repo_records:
                    anchors = _anchor(
                        graph,
                        root,
                        record["metadata"]["anchor"],
                        record["metadata"]["anchor_file"],
                        record["metadata"].get("anchor_source", ""),
                    )
                    if len(anchors) != 1:
                        skipped.append(
                            {
                                "sample_id": record["sample_id"],
                                "reason": f"anchor resolved to {len(anchors)} symbols",
                            }
                        )
                        continue

                    depth = int(record["hop_depth"])
                    adjacency = reverse if record["question_type"] == "upstream" else forward
                    predicted_ids = _layers({anchors[0].id}, adjacency, depth)
                    predicted = {
                        hop: {graph.by_id[symbol_id].name for symbol_id in symbol_ids}
                        for hop, symbol_ids in predicted_ids.items()
                    }
                    gold = _graphcode_gold(record)
                    gold_pairs = {(hop, name) for hop, names in gold.items() for name in names}
                    predicted_pairs = {(hop, name) for hop, names in predicted.items() for name in names}
                    metrics = _score(gold_pairs, predicted_pairs)
                    total_tp += int(metrics["tp"])
                    total_fp += int(metrics["fp"])
                    total_fn += int(metrics["fn"])
                    macro_f1 += float(metrics["f1"])
                    is_exact = gold_pairs == predicted_pairs
                    exact += int(is_exact)
                    cases.append(
                        {
                            "sample_id": record["sample_id"],
                            "repo": repo,
                            "question_type": record["question_type"],
                            "hop_depth": depth,
                            "gold": {f"hop_{hop}": sorted(names) for hop, names in gold.items()},
                            "prediction": {f"hop_{hop}": sorted(names) for hop, names in predicted.items()},
                            "metrics": metrics,
                            "exact": is_exact,
                        }
                    )
            finally:
                store.close()

    if not cases:
        raise ValueError("no GraphCode-Bench samples could be scored")
    aggregate = _prf(total_tp, total_fp, total_fn)
    aggregate["macro_f1"] = macro_f1 / len(cases)
    aggregate["exact_match"] = exact / len(cases)
    return {
        "benchmark": "graphcode-bench-500",
        "attempted": len(records),
        "scored": len(cases),
        "skipped": skipped,
        "metrics": aggregate,
        "cases": cases,
    }


def _trace_language(program: Path) -> str | None:
    suffixes = {path.suffix for path in program.rglob("*") if path.is_file()}
    if suffixes & {".py", ".pyi"}:
        return "python"
    if suffixes & {".js", ".jsx", ".mjs", ".cjs"}:
        return "javascript"
    if ".java" in suffixes:
        return "java"
    return None


def _trace_name(graph: Graph, symbol: Symbol) -> str:
    path = Path(graph.path_of[symbol.file_id])
    module = ".".join(path.with_suffix("").parts)
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module if symbol.kind == "module" else f"{module}.{symbol.qualname}"


def _edge_set(callgraph: dict) -> set[tuple[str, str]]:
    return {
        (str(caller), str(callee))
        for caller, callees in callgraph.items()
        if isinstance(callees, list)
        for callee in callees
    }


def _traceeval_one(program: Path, language: str, db: Path) -> dict:
    index(program, db, force=True, jobs=1)
    store = Store(db)
    try:
        graph = Graph(store)
        forward, _ = _call_adjacency(graph)
        prediction: dict[str, list[str]] = {}
        for source_id, target_ids in forward.items():
            caller = _trace_name(graph, graph.by_id[source_id])
            prediction[caller] = sorted({_trace_name(graph, graph.by_id[target]) for target in target_ids})
        ground_truth = json.loads((program / "callgraph.json").read_text())
        metrics = _score(_edge_set(ground_truth), _edge_set(prediction))
        return {
            "benchmark_id": program.name,
            "benchmark_dir": str(program),
            "language": language,
            "ground_truth": ground_truth,
            "prediction": prediction,
            "metrics": metrics,
        }
    finally:
        store.close()


def run_traceeval(
    corpus: Path,
    *,
    ids: Path | None = None,
    languages: tuple[str, ...] = ("python", "javascript"),
    limit: int | None = None,
    workers: int = 4,
    output_dir: Path | None = None,
) -> dict:
    """Run TraceEval's supported Python/JavaScript programs and emit scorer-compatible files."""
    selected_ids: dict[str, set[str]] | None = None
    if ids is not None:
        raw_ids = json.loads(ids.read_text())
        selected_ids = {language: set(program_ids) for language, program_ids in raw_ids.items()}

    programs: list[tuple[Path, str]] = []
    unsupported = 0
    for callgraph in sorted(corpus.rglob("callgraph.json")):
        program = callgraph.parent
        language = _trace_language(program)
        if language is None:
            continue
        if selected_ids is not None and program.name not in selected_ids.get(language, set()):
            continue
        if language == "java":
            unsupported += 1
            continue
        if language not in languages:
            continue
        programs.append((program, language))
    if limit is not None:
        programs = programs[:limit]
    if not programs:
        raise ValueError("no supported TraceEval programs found")

    with tempfile.TemporaryDirectory(prefix="toji-traceeval-") as tmp:
        jobs = [
            (program, language, Path(tmp) / f"{language}-{program.name}-{number}.db")
            for number, (program, language) in enumerate(programs)
        ]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            records = list(executor.map(lambda job: _traceeval_one(*job), jobs))

    by_language: dict[str, list[dict]] = defaultdict(list)
    for record, (_, language) in zip(records, programs, strict=True):
        by_language[language].append(record)

    total_tp = total_fp = total_fn = 0
    summaries: dict[str, dict] = {}
    output_files: list[str] = []
    for language, language_records in sorted(by_language.items()):
        tp = sum(int(record["metrics"]["tp"]) for record in language_records)
        fp = sum(int(record["metrics"]["fp"]) for record in language_records)
        fn = sum(int(record["metrics"]["fn"]) for record in language_records)
        summaries[language] = {"scored": len(language_records), **_prf(tp, fp, fn)}
        total_tp += tp
        total_fp += fp
        total_fn += fn
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            output = output_dir / f"toji_{language}_t0.0_r0.json"
            payload = {
                "metadata": {
                    "model": "toji",
                    "language": language,
                    "num_benchmarks": len(language_records),
                    "aggregate_metrics": _prf(tp, fp, fn),
                },
                "results": {record["benchmark_id"]: record for record in language_records},
            }
            output.write_text(json.dumps(payload, indent=2) + "\n")
            output_files.append(str(output))

    return {
        "benchmark": "traceeval",
        "scored": len(records),
        "unsupported": unsupported,
        "metrics": _prf(total_tp, total_fp, total_fn),
        "by_language": summaries,
        "output_files": output_files,
    }
