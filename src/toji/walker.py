"""File discovery: gitignore-aware walk over supported languages."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pathspec

from .models import LANG_BY_EXT

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".toji", ".tox",
    "node_modules", "__pycache__", ".venv", "venv", "env", "virtualenv",
    "dist", "build", "out", "target",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
    ".idea", ".vscode", ".next", ".nuxt", ".turbo", "coverage", ".pytest_cache",
}

# dotenv files: .env, .env.local, .env.example, ... (never code, never index)
ENV_FILE_PREFIXES = (".env",)

MAX_FILE_BYTES = 2_000_000  # skip pathological single files (minified bundles etc.)


@dataclass(slots=True)
class WalkedFile:
    abs_path: Path
    rel_path: str  # slash-separated, relative to root
    lang: str


def _read_gitignore(root: Path) -> pathspec.PathSpec | None:
    gi = root / ".gitignore"
    try:
        if gi.is_file():
            lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines:
                return pathspec.PathSpec.from_lines("gitignore", lines)
    except OSError:
        pass
    return None


def discover(root: Path, max_bytes: int = MAX_FILE_BYTES) -> list[WalkedFile]:
    """Walk root, returning supported-language files sorted by rel path.

    Honors .gitignore (with negation support for files; pruning an ignored
    directory also prunes negations inside it — documented limitation) plus a
    built-in denylist of dependency/build dirs.
    """
    spec = _read_gitignore(root)
    out: list[WalkedFile] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        # prune ignored directories in place
        kept = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if d in DEFAULT_IGNORE_DIRS or d.startswith(ENV_FILE_PREFIXES):
                continue
            if spec is not None and spec.match_file(rel + "/"):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in sorted(filenames):
            if fn.startswith(ENV_FILE_PREFIXES):
                continue
            ext = Path(fn).suffix.lower()
            lang = LANG_BY_EXT.get(ext)
            if lang is None:
                continue
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if spec is not None and spec.match_file(rel):
                continue
            p = Path(dirpath) / fn
            try:
                if p.is_symlink() or p.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
            out.append(WalkedFile(abs_path=p, rel_path=rel, lang=lang))
    return out
