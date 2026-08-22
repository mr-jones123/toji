"""SQLite graph store: files, symbols, edges. Single-writer, WAL."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .models import Edge, Symbol

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    lang TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualname TEXT NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    docstring TEXT,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_qual ON symbols(qualname);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    src_sym INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    dst_name TEXT NOT NULL,
    line INTEGER NOT NULL,
    UNIQUE(src_sym, kind, dst_name)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_sym);
"""


@dataclass(slots=True)
class FileRow:
    id: int
    path: str
    lang: str
    sha256: str
    size: int
    mtime: float


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # -- files -------------------------------------------------------------

    def get_files(self) -> dict[str, FileRow]:
        rows = self.conn.execute("SELECT * FROM files").fetchall()
        return {r["path"]: FileRow(**dict(r)) for r in rows}

    def upsert_file(self, path: str, lang: str, sha: str, size: int, mtime: float) -> int:
        cur = self.conn.execute(
            """INSERT INTO files (path, lang, sha256, size, mtime) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET lang=excluded.lang, sha256=excluded.sha256,
                   size=excluded.size, mtime=excluded.mtime
               RETURNING id""",
            (path, lang, sha, size, mtime),
        )
        return cur.fetchone()[0]

    def remove_file(self, file_id: int) -> None:
        self.conn.execute("DELETE FROM files WHERE id=?", (file_id,))

    # -- symbols / edges ----------------------------------------------------

    def replace_symbols(self, file_id: int, symbols: list[Symbol], edges: list[Edge]) -> None:
        """Atomic per-file rewrite: drop old symbols (cascade edges), insert new.

        Extractor emits edges with src_sym = index into the symbols list
        (module symbol is index 0). Real ids are assigned here.
        """
        self.conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
        before = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        first_id = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM symbols").fetchone()[0] + 1
        self.conn.executemany(
            """INSERT INTO symbols (file_id, kind, name, qualname, signature, docstring, line_start, line_end)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (file_id, s.kind, s.name, s.qualname, s.signature, s.docstring, s.line_start, s.line_end)
                for s in symbols
            ],
        )
        if edges:
            self.conn.executemany(
                """INSERT OR IGNORE INTO edges (src_sym, kind, dst_name, line)
                   VALUES (?, ?, ?, ?)""",
                [(first_id + e.src_sym, e.kind, e.dst_name, e.line) for e in edges],
            )
        after = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return after - before

    def load_graph(self) -> tuple[list[dict], list[Symbol], list[Edge]]:
        files = [dict(r) for r in self.conn.execute("SELECT * FROM files ORDER BY path")]
        symbols = [
            Symbol(**dict(r))
            for r in self.conn.execute("SELECT * FROM symbols ORDER BY file_id, line_start")
        ]
        edges = [
            Edge(**dict(r))
            for r in self.conn.execute("SELECT * FROM edges ORDER BY src_sym")
        ]
        return files, symbols, edges

    def stats(self) -> dict:
        files = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        symbols = self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        edges = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        calls = self.conn.execute("SELECT COUNT(*) FROM edges WHERE kind='calls'").fetchone()[0]
        by_lang = dict(self.conn.execute("SELECT lang, COUNT(*) FROM files GROUP BY lang").fetchall())
        by_kind = dict(self.conn.execute("SELECT kind, COUNT(*) FROM symbols GROUP BY kind").fetchall())
        by_edge = dict(self.conn.execute("SELECT kind, COUNT(*) FROM edges GROUP BY kind").fetchall())
        return {
            "files": files,
            "symbols": symbols,
            "edges": edges,
            "calls": calls,
            "files_by_lang": by_lang,
            "symbols_by_kind": by_kind,
            "edges_by_kind": by_edge,
        }
