#!/usr/bin/env bash
set -euo pipefail
DB=data/dogfood-b02.db
VERIFY="/Users/parkergurney/.venvs/dogfood/bin/python -m pytest -x -q"
# setup_cmd omitted -- deps pre-installed in ~/.venvs/dogfood
# No TDD_PROTECTED override this batch -- under the new protected-path
# semantics, protected_path_modified only fires on edits to a file that
# already existed at base_sha, so a brand-new regression test always passes
# clean against the default tests/** protected-paths. See
# dogfood/batch02-candidates.md.

# See dogfood/batch02-candidates.md for sourcing, category, and composition
# rationale for each task below.

# --- chain: root -> two consumers, genuinely dependent ----------------------

t1=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Table.indexes/xindexes break on identifiers containing a double quote" \
  --brief "PRAGMA index_list and the index-name wrapping in these two properties build SQL with raw f-string interpolation instead of quote_identifier(), so a table or index name containing a double quote produces malformed SQL and an OperationalError instead of working. (issue #824)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t2=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "transform(rename=...) raises TransformError when the renamed column has an index" \
  --brief "Renaming a column via transform() on a table that has an index on that column raises TransformError claiming the column is not in updated table, even though a renamed column still exists -- the check conflates col in rename with col in drop. (issue #822)" \
  --verify-cmd "$VERIFY" --delivery-mode local --depends-on "$t1")

t3=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "detect_fts() has the same missing-quote_identifier() bug as #824" \
  --brief "The LIKE pattern detect_fts() builds to find a table's FTS shadow table interpolates content=\"{self.name}\" without doubling embedded quotes -- the identical bug class as #824's indexes/xindexes fix, in a different method. Apply the same fix here." \
  --verify-cmd "$VERIFY" --delivery-mode local --depends-on "$t1")

# --- underspecified ----------------------------------------------------

t4=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Plugin hook for adding new output/input formats" \
  --brief "--csv/--tsv are hard-coded into query/insert; there's no way for a plugin to register a new format (e.g. geojson) for either direction. The issue thread itself is undecided between --format x vs. reusing --fmt, and whether reading and writing need one hook or two -- pick a design and implement it on top of the existing hookspecs.py/pluggy plumbing. (issue #578)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

t5=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title ".m2m() can't relate a table to itself" \
  --brief "Calling .m2m() where the related table is the same as the current table collapses both foreign-key columns into one (PRIMARY KEY ([people_id], [people_id])), losing the second relationship entirely. The maintainer's own thread is unresolved between explicit left_name=/right_name= kwargs and auto-detecting the self-reference and suffixing columns (_1/_2), with the open question of which suffix means which end. Pick an approach. (issue #431)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

# --- hard ----------------------------------------------------------------

t6=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "Support SQLite URI paths (file:foo.db?mode=ro&immutable=1) in the CLI" \
  --brief "Every CLI command's PATH argument is a Click path type that requires the file to already exist, so a SQLite URI (which sqlite3 and the sqlite3 CLI both accept natively) is rejected before it ever reaches sqlite3.connect(..., uri=True). Fixing it without breaking plain paths means touching the shared path-argument handling used across every command, not one call site. (issue #650)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

# --- straightforward -------------------------------------------------------

t7=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title ".vacuum() raises OperationalError when called inside an open transaction" \
  --brief "VACUUM can't run inside a transaction, but .vacuum() just executes it directly instead of committing first, so any code that calls it after other writes on the same connection gets a raw OperationalError instead of it just working. (issue #479)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t8=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title ".transform() casting text to integer/float should turn empty strings into null" \
  --brief "Converting a text column to integer/float via --type leaves empty-string values as \"\" instead of NULL, which is invalid for the new column type and not what anyone converting CSV-sourced data expects. (issue #488)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t9=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "SQLITE_MAX_VARS is a hard-coded module constant, not configurable" \
  --brief "The 999-var batching limit is hard-coded even though most real SQLite builds (Homebrew, Debian) are compiled with a far higher SQLITE_MAX_VARIABLE_NUMBER, and the stdlib sqlite3 module doesn't expose a way to detect it -- so insert_all() batches far smaller than necessary by default. Expose it as a value callers can override. (issue #147)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t10=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "CSV input containing NUL bytes crashes the insert instead of being handled" \
  --brief "A NUL byte anywhere in a CSV/TSV file makes Python's csv module raise mid-parse, aborting the whole insert with no way to skip or sanitize the offending bytes. (issue #582)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t11=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "CLI insert has no --extras-key/--ignore-extras equivalents of the Python API kwargs" \
  --brief "extras_key= and ignore_extras= exist on the Python .insert()/.upsert() methods but were never wired up as CLI flags, so CLI users can't reach behavior the library already supports. (issue #444)" \
  --verify-cmd "$VERIFY" --delivery-mode pr)

t12=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "No way to specify column names when inserting headerless CSV/TSV" \
  --brief "--no-headers names columns untitled_1, untitled_2, ... and the documented workaround is renaming them afterward via transform --rename, which is slow on a large table. Add a CLI option to name them at insert time. (issue #474)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t13=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "sqlite-utils tables can't filter to specific table names" \
  --brief "The command always lists every table; with --counts against a database that has one huge table, there's no way to scope the run to just the tables you care about. (issue #478)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t14=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "convert recipes (r.jsonsplit() etc.) aren't available inside --functions blocks" \
  --brief "The recipe helpers documented for sqlite-utils convert aren't exposed to the separate --functions code-block mechanism, so the same transformation has to be reimplemented by hand there. (issue #484)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

t15=$(orchestrator add-task --db "$DB" --repo sqlite-utils \
  --title "No way to declare a UNIQUE column at table-creation time" \
  --brief ".create() supports not_null=[...] but the only way to add a UNIQUE constraint is a separate create_index(unique=True) call after the table already exists, leaving a window where the constraint isn't enforced. (issue #558)" \
  --verify-cmd "$VERIFY" --delivery-mode local)

echo "batch02 created: $t1 $t2 $t3 $t4 $t5 $t6 $t7 $t8 $t9 $t10 $t11 $t12 $t13 $t14 $t15"
