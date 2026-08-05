#!/usr/bin/env bash
set -euo pipefail
DB=data/dogfood-b01.db
VERIFY="/Users/parkergurney/.venvs/dogfood/bin/python -m pytest -x -q"
# setup_cmd omitted -- deps pre-installed in ~/.venvs/dogfood
# protected_paths defaults to empty; visible tests are normal feature-work
# surface, so TDD-shaped tasks do not need a protected-path override.

# See dogfood/batch01-candidates.md for sourcing, category, and composition
# rationale for each task below.

# --- chain: 1/3 -> 2/3 -> 3/3, genuinely dependent -------------------------

t1=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Introspect CHECK constraints and comments from CREATE TABLE" \
  --brief "\`.transform()\` and friends only see PRAGMA-level schema info, which drops anything that lives solely in the original CREATE TABLE text -- check constraints, column/table comments. Add a way to parse that text back out. (issue #763)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t2=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "transform() drops check constraints, comments, and can't rebuild UNIQUE-backed auto-indexes" \
  --brief "Calling \`.transform()\` recreates the table without any check constraints or comments, and if a column has a bare UNIQUE constraint (backed by SQLite's own auto-index, no CREATE INDEX statement), the rebuild raises TransformError instead of preserving it. (issue #762)" \
  --verify-cmd "$VERIFY" --delivery-mode local --depends-on "$t1")

t3=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "transform() strips AUTOINCREMENT from the primary key" \
  --brief "Transforming a table (changing any column, not just the PK) silently removes AUTOINCREMENT from the primary key definition, so deleted rowids can get reused after a migration people didn't expect to touch that behavior. (issue #602)" \
  --verify-cmd "$VERIFY" --delivery-mode local --depends-on "$t2")

# --- underspecified ----------------------------------------------------

t4=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "table.get(value) doesn't support lookup by a non-PK unique column" \
  --brief "Users with an integer PK plus a separately unique-constrained column (e.g. name) have no way to fetch a row by that column without hand-writing a rows_where() query. Figure out and implement sensible retrieval-by-column semantics. (issue #588)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

t5=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Assess async support for sqlite-utils" \
  --brief "Multiple users have asked for async usage; there's no consensus on what \"async support\" should even mean here (wrap the sync API in a thread executor? adopt aiosqlite? something else?). Investigate the options against this codebase's actual architecture and recommend a direction. (issue #242)" \
  --verify-cmd "$VERIFY" --delivery-mode scout)

t13=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "row.update() convenience method" \
  --brief "Iterating for row in db[table] gives back plain dicts; mutating one and calling .update() on it does nothing to the database, forcing users to separately track and pass the primary key to table.update(pk, ...) instead. Design decisions include whether rows become a tracking type or stay plain dicts, whether persisting a change is explicit (.save()) or happens on attribute-set, and what happens when the table has no primary key. Pick an approach. (issue #267)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

# --- hard ----------------------------------------------------------------

t6=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "transform() orphans triggers, views, and indices tied to the table" \
  --brief ".transform() rebuilds a table under a new internal name and swaps it in, but triggers, views, and indices that reference the original table aren't consistently carried over or reattached -- some silently vanish, others are left dangling against a name that no longer exists. (issue #611)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

# --- straightforward -------------------------------------------------------

t7=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "rows_where(offset=...) errors when limit isn't also given" \
  --brief "Passing offset without limit to rows_where() / pks_and_rows_where() produces invalid SQL (OFFSET with no LIMIT) and raises OperationalError instead of just returning the offset rows. (issue #816)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t8=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "rows_from_file() crashes on an empty or whitespace-only file" \
  --brief "With no explicit format, format auto-detection runs csv.Sniffer against the file's leading bytes; on an empty/whitespace-only file this raises an uncaught csv.Error instead of yielding zero rows. (issue #808)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t9=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "No way to de-register a SQL function registered via register_function()" \
  --brief "Once a custom function is registered on a connection (e.g. from inside a migration script), there's no way to remove it, so it can leak into code that runs later on the same connection. (issue #589)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t10=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "No way to add an index at table-creation time" \
  --brief "Creating a table and then separately calling create_index() means a large table can go through a window with no index on a column you already know you'll query by. Give callers a way to request an index as part of creating the table. (issue #349)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t11=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Typo/rendering error in install/uninstall docs" \
  --brief "The install docs have a small wording or Sphinx-rendering error (a cross-reference renders as a literal en-dash instead of the intended text). Track it down and fix the doc source. (issue #493)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t12=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "insert CLI command doesn't report how many rows were ignored/replaced" \
  --brief "With --ignore or --replace, the insert command gives no feedback on how many incoming records were actually skipped or overwritten, which matters when piping large feeds. (issue #516)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

t14=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "create-table --sql sugar for CREATE TABLE AS SELECT" \
  --brief "There's no shorthand for CREATE TABLE foo AS SELECT ... -- users have to drop into raw SQL execution instead of using the create-table command they'd otherwise reach for. (issue #481)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

t15=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "table.update() raises a bare AssertionError for a missing row" \
  --brief "When the primary key passed to .update() doesn't match any row, callers get an opaque AssertionError with no message rather than a documented exception type." \
  --verify-cmd "$VERIFY" --delivery-mode local)

echo "batch01 created: $t1 $t2 $t3 $t4 $t5 $t6 $t7 $t8 $t9 $t10 $t11 $t12 $t13 $t14 $t15"
