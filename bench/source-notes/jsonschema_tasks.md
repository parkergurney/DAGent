# jsonschema tasks

Base commit: `cf1f6b02ab6fcf32f46f039720fa0add728f236f` ("Merge commit 'c8c0bdd52999e4e1a6a38ceca814d0943f3e7962'")

Repo versioning uses `hatch-vcs` (git-tag-based, like `setuptools_scm`). All
tags were pruned; `pip install -e .` falls back silently to a `0.1.devN+g<sha>`
version with zero tags, so no tag needed to be preserved.

## Protected paths

- `jsonschema/hidden_tests/` - do not read, modify, or delete. Reserved for evaluation.

## Tasks

### task1 - Speed up uniqueItems checking for arrays of objects

Risk: low (source fix merged 2026-05-12, after training cutoff)

When a schema uses `uniqueItems` on an array whose elements aren't simple,
sortable values (for example, arrays of objects), checking uniqueness falls
back to comparing every pair of elements against each other. For large
arrays this makes validation extremely slow, to the point of being
unusable on realistically sized documents.

Speed up uniqueness checking for these unsortable-item arrays so it stays
fast even for large inputs, without changing which arrays are considered
to have unique items (correctness for edge cases like `NaN`, booleans vs.
integers, and nested unhashable values must be unchanged).

- hidden_cmd: `pytest jsonschema/hidden_tests/test_uniqueitems_perf.py -q`

### task2 - Defer importing urllib.request until it's needed

Risk: medium (source fix merged 2025-10-06, ~4 months before cutoff)

Importing the library currently imports Python's `urllib.request` module
at the top of a module, even though that module is only actually needed
on the rare path where a schema needs to fetch a remote `http(s)`
reference. This adds unnecessary overhead (and side effects) to every
import of the library, even for the large majority of users who never
resolve a remote reference this way.

Defer that import so it only happens when it's actually needed, without
changing behavior for the (rare) code path that does use it.

- hidden_cmd: `pytest jsonschema/hidden_tests/test_lazy_urllib_import.py -q`

### task3 - Accept single-label hostnames in the "hostname" format

Risk: medium-high (source fix merged 2023-09-20, well before cutoff)

When validating strings against the `hostname` format, only fully
qualified, multi-label names are currently accepted. Single-label
hostnames like `localhost` are rejected as invalid, even though they are
valid hostnames.

Fix the hostname format check so single-label hostnames are accepted too,
without weakening the check for genuinely malformed strings.

- hidden_cmd: `pytest jsonschema/hidden_tests/test_hostname_single_label.py -q`

### task4 - Properly quote/escape unusual property names in error paths

Risk: medium (source fix merged 2025-07-17, ~6 months before cutoff)

Each validation error carries a JSONPath-like string describing where in
the instance the error occurred. Right now, this doesn't account for
property names that are empty, or that contain characters like `.`, `[`,
quotes, backslashes, or spaces. For example, a property literally named
`.` currently produces a path that reads as if it has two consecutive
separators, which is ambiguous and doesn't clearly convey the actual
property name.

Fix the path rendering so property names like these come out unambiguous
and quoted, for instance a property named `.` should render using a
quoted, bracketed form (e.g. `$['.']`) instead of being joined in with a
bare separator. Ordinary alphanumeric property names should keep
rendering the way they already do.

The exact quoting/escaping scheme is up to you, but it should be able to
round-trip the property names above unambiguously (distinguish them from
each other and from ordinary names).

- hidden_cmd: `pytest jsonschema/hidden_tests/test_json_path_escaping.py -q`

### task5 - Don't double-report properties that already failed their own schema

Risk: high (source fix merged 2023-03-28, well before cutoff)

When a schema uses `unevaluatedProperties` together with `properties`, a
property that's listed under `properties` but fails its own subschema
currently produces two separate errors: one for failing its own subschema,
and a second, confusing "unevaluated property" error - even though the
property was clearly accounted for by the schema.

A property that's covered by `properties`, whether or not it actually
passes its own subschema, should not also be flagged as unevaluated.
Properties that genuinely aren't mentioned anywhere in the schema should
still be flagged as unevaluated as before.

- hidden_cmd: `pytest jsonschema/hidden_tests/test_unevaluated_double_jeopardy.py -q`

### task6 - Correctly evaluate properties against additionalProperties/unevaluatedProperties subschemas

Risk: medium-high (source fix merged 2025-05-26)

Depends on: task5

Continuing to work in the same area: when `additionalProperties` or
`unevaluatedProperties` is itself set to a subschema (rather than just
`true`/`false`), the logic that figures out which instance properties
count as "already evaluated" needs to actually validate each remaining
instance property against that subschema, the same way each named
property under `properties` is checked against its own subschema.

Right now that's not happening correctly for the subschema case, so
properties that are genuinely valid under `additionalProperties` (or
`unevaluatedProperties`) can be wrongly reported as unevaluated. Fix the
evaluated-property calculation so it validates each candidate property
against the given subschema.

Note this only fully resolves once properties that fail their own
`properties` subschema also stop being double-reported (see task5); both
fixes touch the same evaluated-properties logic.

- hidden_cmd: `pytest jsonschema/hidden_tests/test_additional_properties_evaluated.py -q`
