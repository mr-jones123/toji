# toji

> Codebase graph memory: index structure once, answer with evidence forever.

[![PyPI](https://img.shields.io/pypi/v/toji)](https://pypi.org/project/toji/)
[![Python](https://img.shields.io/pypi/pyversions/toji)](https://pypi.org/project/toji/)
[![Release](https://github.com/mr-jones123/toji/actions/workflows/release.yml/badge.svg)](https://github.com/mr-jones123/toji/actions/workflows/release.yml)
[![License](https://img.shields.io/badge/license-unreleased-lightgrey)]()

toji parses your codebase (Python, TypeScript, TSX, JavaScript) into a queryable
graph — symbols, signatures, docstrings, call/import/inherit edges — stored in
SQLite. No source is stored; `read` always fetches live lines from disk.

Built for AI reviewers and anyone tired of skimming ten files to answer
"who calls this?": every answer carries exact `path:line` evidence, ambiguous
names are surfaced instead of guessed, and unresolved calls are reported as
unknown rather than invented.

## Install

```bash
pip install toji            # or: uv tool install toji
```

Or install as an agent skill (instructions for AI coding agents):

```bash
npx skills add mr-jones123/toji
```

## Quickstart

```bash
toji index .                # index current directory -> .toji/graph.db
toji map src/               # skeleton of a module: symbols, signatures, docstrings
toji find "classify"        # regex search over symbol names
toji callers classify_change    # who calls it, with file:line evidence
toji blast classify_change  # BFS blast radius: what a change affects, hop-ranked
toji read classify_change   # the symbol's actual source lines
```

Reindexing is incremental and content-hashed: only changed files re-parse.
A full 12k-file monorepo indexes in ~40s; unchanged reindex scans are sub-second.

## Commands

| Command | Evidence returned |
|---|---|
| `index [PATH] [--force]` | build/rebuild the graph (incremental by content hash) |
| `map [FILE\|SYM]` | file skeleton or symbol detail |
| `find <regex>` | matching symbols |
| `calls <sym>` | forward call edges + unresolved callees |
| `callers <sym>` | reverse call edges (1 hop) |
| `blast <sym> [--depth N] [--forward]` | affected symbols across calls/contains/inherits |
| `deps <file>` | import edges both directions, resolved to files |
| `read <sym>` | the symbol's live source lines from disk |
| `stats` | index size, resolution quality metrics |

Every command accepts `--json` for stable machine-readable output.

## How resolution works

Call edges store the callee *as written* (`obj.method`, `helper`). At query
time they resolve through file-local scopes, the importing file's own imports,
then global unique suffix/bare-name matches:

- **Confident** matches resolve with the exact call-site line attached.
- **Ambiguous** names list every candidate instead of guessing.
- **Unresolved** calls (stdlib, dynamic dispatch) are counted and shown —
  never fabricated. `toji stats` reports the ratio so you know what the graph
  is confident about.

`blast` and `callers` merge same-named definitions (e.g. a method implemented
by several cloud adapters) and traverse from all of them, reporting the count;
`read`, `calls`, and `map` require a single symbol and will ask you to
disambiguate instead.

## Releasing

Conventional Commits drive versions automatically on push to `main`
(`fix:` → patch, `feat:` → minor, `BREAKING CHANGE:` → major). The release
workflow tests, bumps the version, generates the changelog, tags, builds, and
publishes to PyPI. Configure either PyPI trusted publishing or a `PYPI_TOKEN`
secret to enable publishing.

## License

TBD.
