#!/usr/bin/env bash
set -euo pipefail
DB=data/dogfood-b03.db
VERIFY="/Users/parkergurney/.venvs/dogfood/bin/python -m pytest -x -q"
# setup_cmd omitted -- deps pre-installed in ~/.venvs/dogfood
# protected_paths defaults to empty; visible tests/docs are normal feature-work
# surface for this dogfood batch.

# See dogfood/batch03-candidates.md for sourcing, category, and composition
# rationale for each task below.

# --- chain: insert-files/sqlar enrichment, genuinely dependent --------------

t1=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "insert-files can add fixed literal metadata columns" \
  --brief "Extend the -c/--column spec so users can populate a column with a fixed value while importing files, e.g. file_type:text:gif, without a second update pass. Keep existing metadata shorthands and compound --pk behavior working. (issue #140)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t2=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "insert-files can store sqlar-compatible compressed content" \
  --brief "Add an option or column mode for compressed file payloads compatible with SQLite's sqlar expectations, without regressing existing BLOB/TEXT content imports. Reuse the insert-files column parsing from the fixed-literal metadata work where it fits. (issue #141)" \
  --verify-cmd "$VERIFY" --delivery-mode local --depends-on "$t1")

t3=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "insert-files can convert fields during import" \
  --brief "Allow insert-files to apply conversion logic while importing so sqlar-style archives do not require running sqlite-utils convert as a second command after insertion. Integrate with the existing convert helper patterns rather than inventing a separate mini-language. (issue #597)" \
  --verify-cmd "$VERIFY" --delivery-mode local --depends-on "$t2")

# --- straightforward --------------------------------------------------------

t4=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Import JSON records from a top-level list key" \
  --brief "JSON shaped like {\"List\": [{...}]} cannot be imported directly today; users have to run it through jq .List first. Add a CLI option, likely a --json-path/--json-key style selector, so insert/upsert can import the list under a named top-level key. (issue #489)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t5=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Declared JSON columns should render as JSON without --json-cols" \
  --brief "When a SQLite column is declared as JSON, output modes that already support nested JSON should decode those values automatically instead of requiring users to pass --json-cols and forcing sqlite-utils to guess by trying json.loads() on all text columns. (issue #579)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t6=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Python API helper for writing CSV output" \
  --brief "The CLI can render query/table output as CSV, but the Python API has no equivalent convenience method. Add a small documented helper that writes rows to a file-like object or returns CSV text, preserving the CLI's header behavior where practical. (issue #580)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t7=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "rows command supports transposed output" \
  --brief "Add a rows --transpose or equivalent mode so wide records can be displayed as key/value blocks, similar to psql extended display, while preserving the existing table/json/csv output modes. (issue #535)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t8=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "CSV progress and errors are misleading for UTF-16 input" \
  --brief "Against UTF-16 CSV input, progress/error reporting can stall or hide the underlying exception. Make failures visible and progress accounting honest for encoded streams, with a regression test using --encoding utf-16-le. (issue #439)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t9=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "insert CLI supports --extract" \
  --brief "Wire the Python API's extracts= insertion feature through the sqlite-utils insert command so imports can create lookup tables in one step, including docs and tests for at least one extracted column. (issue #352)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

# --- hard / PR delivery -----------------------------------------------------

t10=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "rows_where() and delete_where() support attached databases" \
  --brief "Tables from attached aliases currently fail existence/table-name checks for methods such as rows_where() and delete_where(). Make attached/temp schemas first-class where the Table object has an alias, without breaking normal main-schema table lookup. (issue #432)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

# --- underspecified ---------------------------------------------------------

t11=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Optional automatic JSON deserialization for returned rows" \
  --brief "sqlite-utils serializes dict/list values to JSON on insert, but rows fetched through the Python API come back as strings. Design and implement an opt-in way to deserialize JSON strings back into dict/list values while preserving default compatibility. (issue #612)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t12=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Custom JSON encoder and decoder hooks" \
  --brief "Let callers provide project-specific JSON encoding/decoding for values such as sets, enums, or sparse matrices instead of relying only on the default json module behavior. Decide whether this belongs on Database, Table operations, or both, and document the chosen API. (issue #521)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

# --- straightforward tail ---------------------------------------------------

t13=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "convert scripts can receive extra variables from the CLI" \
  --brief "Add a way to pass user-specified variables into sqlite-utils insert --convert and related conversion snippets so scripts do not have to hard-code environment-specific constants such as a server name or import label. (issue #492)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t14=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Trim leading and trailing whitespace across selected tables" \
  --brief "Add a CLI utility or option for trimming whitespace from text columns across one or more tables, replacing the manual SQL recipe users currently paste together during ETL cleanup. (issue #523)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

echo "batch03 created: $t1 $t2 $t3 $t4 $t5 $t6 $t7 $t8 $t9 $t10 $t11 $t12 $t13 $t14"
