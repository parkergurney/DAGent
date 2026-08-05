# batch03 candidate tasks - sqlite-utils

Mined from `github.com/simonw/sqlite-utils` open issues on 2026-08-05, with
batch01 and batch02 issue numbers excluded:

- batch01: #763, #762, #602, #588, #242, #611, #816, #808, #589, #349, #493,
  #516, #267, #481, #815
- batch02: #824, #822, #578, #431, #650, #479, #488, #147, #582, #444, #474,
  #478, #484, #558, plus the repeated #816/#808 exclusions

The dependency chain is the `insert-files` / sqlar path. `#140` expands the
column spec grammar to allow fixed literal columns, which is the least invasive
place to establish richer per-file column parsing. `#141` then reuses the same
file metadata/content pipeline to support compressed content. `#597` is a
consumer on top of that same command: sqlar-style archives currently require
`insert-files` followed by `convert`, and are easier to implement once the
command can already express richer derived columns.

| # | title | one-line brief | source | files likely touched | delivery | category | depends-on |
|---|---|---|---|---|---|---|---|
| 1 | `insert-files` can add fixed literal metadata columns | Extend the `-c/--column` spec so users can populate a column with a fixed value while importing files, e.g. `file_type:text:gif`, without a second update pass. | issue #140 | sqlite_utils/cli.py, tests/test_insert_files.py, docs/cli.rst | local | chain (1/3, root) | - |
| 2 | `insert-files` can store sqlar-compatible compressed content | Add an option/column mode for compressed file payloads compatible with SQLite's sqlar expectations, without regressing existing BLOB/TEXT content imports. | issue #141 | sqlite_utils/cli.py, tests/test_insert_files.py, docs/cli.rst | local | chain (2/3) | #1 |
| 3 | `insert-files` can convert fields during import | Allow `insert-files` to apply conversion logic while importing so sqlar-style archives do not require a separate `sqlite-utils convert` command after insertion. | issue #597 | sqlite_utils/cli.py, tests/test_insert_files.py, docs/cli.rst | local | chain (3/3) | #2 |
| 4 | Add a `--json-path` style option for JSON files with one top-level list key | Import JSON shaped like `{ "List": [{...}] }` directly by selecting the list key/path instead of forcing users through `jq .List` first. | issue #489 | sqlite_utils/cli.py, tests/test_cli_insert.py, docs/cli.rst | local | straightforward | - |
| 5 | `query`/`rows` should treat declared `JSON` columns as JSON output columns | When a SQLite column is declared as `JSON`, output modes that already support nested JSON should decode those columns without requiring users to manually pass `--json-cols`. | issue #579 | sqlite_utils/cli.py, sqlite_utils/db.py, tests/test_cli.py | local | straightforward | - |
| 6 | Python API helper for writing CSV output | The CLI can render query/table output as CSV, but the Python API has no equivalent convenience method; add a small helper that writes rows to a file-like object or returns CSV text. | issue #580 | sqlite_utils/db.py, docs/python-api.rst, tests | local | straightforward | - |
| 7 | `rows` supports transposed output for wide rows | Add a `rows --transpose` or equivalent mode so wide records can be displayed as key/value blocks, similar to psql extended display. | issue #535 | sqlite_utils/cli.py, tests/test_cli.py, docs/cli.rst | local | straightforward | - |
| 8 | CSV progress/error reporting is misleading for UTF-16 input | Against UTF-16 CSV input, progress and failure reporting can stall or hide the underlying exception; make failures visible and progress accounting honest for encoded streams. | issue #439 | sqlite_utils/cli.py, tests/test_cli.py | local | straightforward | - |
| 9 | Add CLI support for `insert --extract` | Wire the Python API's `extracts=` insertion feature through the `sqlite-utils insert` command so imports can create lookup tables in one step. | issue #352 | sqlite_utils/cli.py, tests/test_cli_insert.py, docs/cli.rst | local | straightforward | - |
| 10 | Support `rows_where()` and `delete_where()` against attached databases | Tables from attached aliases currently fail existence/table-name checks for methods such as `rows_where()` and `delete_where()`; make attached/temp schemas first-class where the `Table` object has an alias. | issue #432 | sqlite_utils/db.py, tests/test_rows.py, tests/test_delete.py | pr | hard | - |
| 11 | Add optional automatic JSON deserialization to returned rows | Provide an opt-in way for rows fetched through the Python API to deserialize JSON strings back into dict/list values, while preserving default compatibility. | issue #612 | sqlite_utils/db.py, docs/python-api.rst, tests | local | underspecified | - |
| 12 | Add custom JSON encoder/decoder hooks | Let callers provide project-specific JSON encoding/decoding for values such as sets, enums, or sparse matrices instead of relying only on the default json module behavior. | issue #521 | sqlite_utils/db.py, docs/python-api.rst, tests | pr | underspecified | - |
| 13 | `convert` scripts can receive extra variables from the CLI | Add a way to pass user-specified variables into `sqlite-utils insert --convert`/related conversion snippets so scripts do not have to hard-code environment-specific constants. | issue #492 | sqlite_utils/cli.py, tests/test_cli_insert.py, tests/test_cli_convert.py, docs/cli.rst | local | straightforward | - |
| 14 | Trim leading/trailing whitespace across selected tables | Add a CLI utility or option for trimming whitespace from text columns across one or more tables, replacing the documented manual SQL recipe people currently paste together. | issue #523 | sqlite_utils/cli.py, tests/test_cli.py, docs/cli.rst | local | straightforward | - |

## Composition check

3 chain (#1-3), 8 standalone straightforward (#4-9, #13-14), 2
underspecified (#11, #12), and 1 hard (#10). The chain root (#1) is also a
straightforward implementation shape, but counted as chain here to keep the
14-task total honest. Delivery: 12 local, 2 pr (#10, #12), 0 scout. This keeps
local delivery as the default while still exercising PR delivery. No protected
paths are set; visible tests and docs are intended feature-work surface for
these dogfood tasks.
