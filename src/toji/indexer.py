"""Index orchestration: discovery -> hash-skip -> parallel extract -> store.

Change detection is content-addressed: sha256 of each file is the ground
truth. A cheap (size, mtime) fast path skips reading untouched files, but any
mtime/size mismatch is settled by hashing — so a `git checkout` that rewrites
mtimes without changing content re-reads but does NOT re-extract.
"""

from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .extract import extract
from .store import Store
from .walker import WalkedFile, discover


@dataclass(slots=True)
class IndexReport:
    new: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    failed: list[str] = field(default_factory=list)
    symbols: int = 0
    edges: int = 0  # edges actually written (post-dedupe)
    elapsed: float = 0.0

    @property
    def indexed(self) -> int:
        return self.new + self.changed


def index(root: Path, db: Path, force: bool = False, jobs: int | None = None) -> IndexReport:
    t0 = time.monotonic()
    report = IndexReport()
    store = Store(db)
    try:
        if force:
            for t in ("edges", "symbols", "files"):
                store.conn.execute(f"DELETE FROM {t}")
            store.conn.commit()

        found = discover(root)
        known = store.get_files()
        found_paths = {w.rel_path for w in found}

        # removed files
        removed = sorted(known.keys() - found_paths)
        for p in removed:
            store.remove_file(known[p].id)
        report.removed = len(removed)

        # phase 1: cheap (size, mtime) fast path — no I/O beyond stat
        needs_hash: list[tuple[WalkedFile, object]] = []
        for wf in found:
            prev = known.get(wf.rel_path)
            st = wf.abs_path.stat()
            if prev is not None and not force and prev.size == st.st_size and prev.mtime == st.st_mtime:
                report.unchanged += 1
                continue
            needs_hash.append((wf, prev))

        def _run(wf: WalkedFile, prev):
            """Read once, hash, extract only if the content actually changed."""
            try:
                src = wf.abs_path.read_bytes()
                sha = hashlib.sha256(src).hexdigest()
                if prev is not None and not force and prev.sha256 == sha:
                    # mtime/size lied; content is identical — refresh row, no extract
                    return wf, sha, [], [], None, True
                syms, edges = extract(wf.lang, src, 0, wf.rel_path)
                return wf, sha, syms, edges, None, False
            except Exception as exc:  # noqa: BLE001 - per-file isolation
                return wf, "", [], [], exc, False

        workers = jobs or min(8, (os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_run, wf, prev) for wf, prev in needs_hash]
            for fut in futs:
                wf, sha, syms, edges, err, hash_unchanged = fut.result()
                if err is not None:
                    report.failed.append(f"{wf.rel_path}: {err}")
                    continue
                if hash_unchanged:
                    report.unchanged += 1
                    st = wf.abs_path.stat()
                    store.upsert_file(wf.rel_path, wf.lang, sha, st.st_size, st.st_mtime)  # refresh mtime
                    continue
                if known.get(wf.rel_path) is None:
                    report.new += 1
                else:
                    report.changed += 1
                st = wf.abs_path.stat()
                file_id = store.upsert_file(wf.rel_path, wf.lang, sha, st.st_size, st.st_mtime)
                report.edges += store.replace_symbols(file_id, syms, edges)
                report.symbols += len(syms)

        store.conn.commit()
        store.set_meta("root", str(root.resolve()))
        store.conn.commit()
    finally:
        store.close()
    report.elapsed = time.monotonic() - t0
    return report
