---
name: toji
description: 'Use when you need to understand a codebase, answer questions about callers, callees, imports, estimate blast radius of a change, or review code with evidence instead of guessing. toji indexes code structure (symbols, signatures, docstrings, call/import/inherit edges) into a queryable graph; no source is stored. Triggers: "who calls X", "what breaks if I change X", "map this module", "review this PR with evidence", "how do A and B connect".'
---

# toji — codebase graph memory

Index a codebase once, then answer structural questions with exact `file:line`
evidence — no file-by-file skimming.

## When to use

Use toji instead of reading files directly when the question is structural:
where a symbol is defined, who calls it, what it calls, what imports what, or
what a change breaks. After toji narrows you to the relevant lines, use
`toji read` (or the file) for the actual source.

## CLI availability

The `toji` command must be installed on the machine:

```bash
pip install toji          # published package
uv tool install toji      # or as a standalone tool
```

If the command is missing, run one of the above (or build from source with
`uv tool install .`).

## Workflow

1. **Index first.** If `<root>/.toji/graph.db` does not exist (or code changed
   since), run:

   ```bash
   toji index [PATH]       # default: current directory
   ```

   Reindexing is incremental and content-hashed: only changed files re-parse.
   The index lives at `<PATH>/.toji/graph.db`; pass `--db <path>` to point
   any query at a specific index.

2. **Find the symbol** (regex over symbol names/qualnames):

   ```bash
   toji find <regex>        # -> path:line kind qualname rows
   ```

3. **Answer structural questions:**

   | Command | Evidence returned |
   |---|---|
   | `toji map [FILE\|SYM]` | file skeleton (signatures + docstrings) or symbol detail |
   | `toji callers <sym>` | who calls it (1 hop, reverse) |
   | `toji calls <sym>` | what it calls + unresolved callees |
   | `toji blast <sym> [--depth N] [--forward]` | BFS blast radius: affected symbols, hop-ranked, each with the exact call-site line |
   | `toji deps <file>` | import edges both directions, resolved to files |
   | `toji read <sym>` | the symbol's actual source lines from disk (def line highlighted) |
   | `toji stats` | index size + unresolved-call count |

4. **Read only what matters.** `toji read <sym> [--context N]` prints the exact
   line range; that is the source of truth for behavior claims.

## Evidence contract

- **Cite what you verified.** Every `calls`/`callers`/`blast`/`deps` result
  carries `path:line` — when you make a claim from it, keep that reference.
  When you need the body, `read` it before asserting behavior.
- **Unresolved means unknown.** toji stores callees as written text and
  resolves them heuristically (exact name -> unique suffix -> unique bare
  name). Calls it cannot resolve are listed explicitly as `unresolved` —
  never treat them as facts, and never invent what they might be.
- **Ambiguity handling.** `read`, `calls`, and `map` require a single symbol:
  an ambiguous name errors and lists every candidate with `path:line` —
  disambiguate with the full qualname or a `find` result. `blast` and
  `callers` instead merge all same-named definitions (e.g. a method
  implemented by several adapters) and traverse from every one, reporting
  the count in the header (`across N definitions`). Same-name merging is
  exhaustive, never a guess.
- **`--json` for machine use.** Every command supports `--json` and emits
  stable structured output (`{path, line, kind, qualname, ...}`) suitable for
  parsing; omit it for human-readable rich tables.
- **Blast radius direction.** `blast` is reverse by default (callers,
  containers, subclasses — what a change affects). `--forward` adds callees.
  `--depth` caps hops; large graphs truncate at `--max-nodes` (default 200)
  with an explicit notice.
- **No source is stored.** Signatures and docstrings yes; bodies no. `read`
  always fetches live source — if the file changed since indexing, reindex
  (`toji index`) to refresh line numbers.

## Model limitations (state them, don't hide them)

- Call resolution is name-based, not type-aware. Dynamic dispatch, factories,
  and same-named symbols in different modules are handled conservatively:
  unresolved or ambiguous, never guessed.
- TS/JS docstrings are not captured (Python only).
- `import * as X` (TS namespace imports) and star re-exports resolve as
  module edges, not per-symbol edges.
