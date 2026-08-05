# batch02 candidate tasks — sqlite-utils

Mined from `github.com/simonw/sqlite-utils` open issues, none reused from batch01
(#763, #762, #602, #588, #242, #611, #816, #808, #589, #349, #493, #516, #267,
#481, #815 all excluded).
Repo currently has 84 open issues, not the 295 assumed going in — worth
correcting before this becomes a stale planning number; still 69 unused after
this batch.

The dependency chain is real, not narrative: `#824` is the root cause (the
`Table.indexes`/`xindexes` PRAGMA properties build SQL with naive f-string
quoting instead of `quote_identifier()` — confirmed at `db.py:2286` and
`:2308`, both missed by the #678 migration that converted everything else).
`#822`'s `TransformError` is raised from inside the exact loop that consumes
`self.indexes` (`db.py:2839`, inside `transform_sql()`), so it's a genuine
downstream consumer, not just nearby code.
The third leg is self-authored: `#824`'s own issue body flags a sibling
instance of the identical bug in `detect_fts()` (`db.py:3513`,
`content="{self.name}"`, same missing `quote_identifier()`) as "related, same
family, can file separately" — verified live, still unfixed.
Both #822 and the self-authored task depend on #824 directly (a root fanning
to two consumers), not a strict linear 1→2→3 — noted honestly rather than
forced into a shape it isn't.

| # | title | one-line brief | source | files likely touched | delivery | category | depends-on |
|---|---|---|---|---|---|---|---|
| 1 | `Table.indexes`/`xindexes` break on identifiers containing a double quote | `PRAGMA index_list` and the index-name wrapping in these two properties build SQL with raw f-string interpolation instead of `quote_identifier()`, so a table or index name containing `"` produces malformed SQL and an `OperationalError` instead of working. | issue #824 | sqlite_utils/db.py | local | chain (1/3, root) | — |
| 2 | `.transform(rename=...)` raises `TransformError` when the renamed column has an index | Renaming a column via `transform()` on a table that has an index on that column raises `TransformError` claiming the column "is not in updated table", even though a renamed column still exists — the check conflates `col in rename` with `col in drop`. | issue #822 | sqlite_utils/db.py | local | chain (2/3) | #1 |
| 3 | `detect_fts()` has the same missing-`quote_identifier()` bug as `#824` | The `LIKE` pattern `detect_fts()` builds to find a table's FTS shadow table interpolates `content="{self.name}"` without doubling embedded quotes — the identical bug class as `#824`, in a different method, explicitly named in that issue as unfixed. | authored (db.py, flagged in #824) | sqlite_utils/db.py | local | chain (3/3) | #1 |
| 4 | Plugin hook for adding new output/input formats | `--csv`/`--tsv` are hard-coded into `query`/`insert`; there's no way for a plugin to register a new format (e.g. geojson) for either direction. The issue thread itself is undecided between `--format x` vs. reusing `--fmt`, and whether reading and writing need one hook or two — pick a design and implement it on top of the existing `hookspecs.py`/pluggy plumbing. | issue #578 | sqlite_utils/hookspecs.py, sqlite_utils/cli.py | pr | underspecified | — |
| 5 | `.m2m()` can't relate a table to itself | Calling `.m2m()` where the related table is the same as the current table collapses both foreign-key columns into one (`PRIMARY KEY ([people_id], [people_id])`), losing the second relationship entirely. The maintainer's own thread is unresolved between explicit `left_name=`/`right_name=` kwargs and auto-detecting the self-reference and suffixing columns (`_1`/`_2`) — with the open question of which suffix means which end. Pick an approach. | issue #431 | sqlite_utils/db.py | local | underspecified | — |
| 6 | Support SQLite URI paths (`file:foo.db?mode=ro&immutable=1`) in the CLI | Every CLI command's `PATH` argument is a Click path type that requires the file to already exist, so a SQLite URI (which `sqlite3` and the `sqlite3` CLI both accept natively) is rejected before it ever reaches `sqlite3.connect(..., uri=True)`. Fixing it without breaking plain paths means touching the shared path-argument handling used across every command, not one call site. | issue #650 | sqlite_utils/cli.py, sqlite_utils/db.py | pr | hard | — |
| 7 | `.vacuum()` raises `OperationalError` when called inside an open transaction | `VACUUM` can't run inside a transaction, but `.vacuum()` just executes it directly instead of committing first, so any code that calls it after other writes on the same connection gets a raw `OperationalError` instead of it just working. | issue #479 | sqlite_utils/db.py | local | straightforward | — |
| 8 | `.transform()` casting text to integer/float should turn empty strings into `null` | Converting a text column to `integer`/`float` via `--type` leaves empty-string values as `""` instead of `NULL`, which is invalid for the new column type and not what anyone converting CSV-sourced data expects. | issue #488 | sqlite_utils/db.py | local | straightforward | — |
| 9 | `SQLITE_MAX_VARS` is a hard-coded module constant, not configurable | The 999-var batching limit is hard-coded even though most real SQLite builds (Homebrew, Debian) are compiled with a far higher `SQLITE_MAX_VARIABLE_NUMBER`, and the stdlib `sqlite3` module doesn't expose a way to detect it — so `insert_all()` batches far smaller than necessary by default. Expose it as a value callers can override. | issue #147 | sqlite_utils/db.py | local | straightforward | — |
| 10 | CSV input containing NUL bytes crashes the insert instead of being handled | A NUL byte anywhere in a CSV/TSV file makes Python's `csv` module raise mid-parse, aborting the whole insert with no way to skip or sanitize the offending bytes. | issue #582 | sqlite_utils/utils.py | local | straightforward | — |
| 11 | CLI `insert` has no `--extras-key`/`--ignore-extras` equivalents of the Python API kwargs | `extras_key=` and `ignore_extras=` exist on the Python `.insert()`/`.upsert()` methods but were never wired up as CLI flags, so CLI users can't reach behavior the library already supports. | issue #444 | sqlite_utils/cli.py | pr | straightforward | — |
| 12 | No way to specify column names when inserting headerless CSV/TSV | `--no-headers` names columns `untitled_1`, `untitled_2`, ... and the documented workaround is renaming them afterward via `transform --rename`, which is slow on a large table. Add a CLI option to name them at insert time. | issue #474 | sqlite_utils/cli.py | local | straightforward | — |
| 13 | `sqlite-utils tables` can't filter to specific table names | The command always lists every table; with `--counts` against a database that has one huge table, there's no way to scope the run to just the tables you care about. | issue #478 | sqlite_utils/cli.py | local | straightforward | — |
| 14 | `convert` recipes (`r.jsonsplit()` etc.) aren't available inside `--functions` blocks | The recipe helpers documented for `sqlite-utils convert` aren't exposed to the separate `--functions` code-block mechanism, so the same transformation has to be reimplemented by hand there. | issue #484 | sqlite_utils/cli.py | local | straightforward | — |
| 15 | No way to declare a `UNIQUE` column at table-creation time | `.create()` supports `not_null=[...]` but the only way to add a `UNIQUE` constraint is a separate `create_index(unique=True)` call after the table already exists, leaving a window where the constraint isn't enforced. | issue #558 | sqlite_utils/db.py | local | straightforward | — |

## Composition check

9 straightforward (#7-15) / 3 chain (#1-3, root `#824` fans out to two real
consumers, `#822` and the self-authored `detect_fts()` fix — see chain note
above) / 2 underspecified (#4, #5) / 1 hard (#6).
Both underspecified tasks are non-scout (`pr` and `local`), so both can
actually reach `worker.asked` → supervisor — the gap batch01 hit with its
scout-mode underspecified task doesn't recur here. No scout task this batch;
skipped rather than forced, since the only well-scoped investigation-shaped
issue in the remaining pool (`#242`-style async questions are used up) didn't
clear the bar for "real, not busywork."
Delivery: 12 local, 3 pr (#4, #6, #11), 0 scout.
Protected paths: after batch02, the project default is empty because visible
tests are normal feature-work surface. Use `--protected-paths` only for
benchmark/hidden/instructor-owned checks a worker must not rewrite. Every
brief still gets the standing commit-before-done protocol line automatically
(`sdk_worker.py`'s `_PROTOCOL`), so it doesn't need restating per brief.
