# tinydb tasks

Base commit: `71283fd738f03b06f0347a1515bc34c96de575ae` ("chore: release version 4.5.1") on branch `bench-base`.
Authoritative suite definition: `bench/tinydb-suite.toml`. This file is a narrative record, not the source of truth.

Every task below is grounded in a real, closed GitHub issue with a merged PR that fixes it.

## Protected paths

- `hidden_tests/` - do not read, modify, or delete. Reserved for evaluation.

## Tasks

### task1 - Make missing-ID handling consistent across update/remove

Issue/PR: #591 / #616. Real fix merged 2026-05-21 (post training-cutoff, low memorization risk on
its own), but that fix landed on a much later commit than this suite's base, so the base was set
far earlier in history (see "Base commit selection" below) to also fit task6. Adapted for the older
base: dropped the `get(doc_ids=...)` assertion, since that plural-list overload of `get()` didn't
exist yet at this point in tinydb's history (added 2023-05-21, still after this base).

When you call `update()` or `remove()` on a table and pass one or more document IDs, and one of
those IDs doesn't actually exist, the call currently raises a `KeyError`.
`get()` already handles a missing single ID gracefully (returns `None`).
Make `update()` and `remove()` behave the same way: silently skip IDs that don't exist, and return
only the list of IDs that were actually updated or removed.
If a request mixes existing and non-existing IDs, only the existing ones should be affected.

- hidden_cmd: `pytest hidden_tests/test_task1_doc_ids_consistency.py -q`

### task2 - Fix the LRU cache's value update on existing keys

Issue/PR: #560 / #573. Real fix, merged 2024-10-12 (after this suite's base of 2021-07-17,
medium-high risk given the age).

`LRUCache.set()` marks an existing key as recently-used but never actually replaces its stored
value. Fix it so replacing an existing key's value actually replaces it.

- hidden_cmd: `pytest hidden_tests/test_task2_lru_cache_set_update.py -q`

### task3 - Fix the LRU cache's handling of falsy cached values

Issue/PR: #596 / #597. Real fix, merged 2025-12-27 (medium risk).
Depends on: task2 (verified: task3's hidden test still fails with only task2's fix applied).

`LRUCache.set()` decides whether a key already exists via truthiness of the cached value, so a key
whose value is falsy (`0`, `False`, `""`) is treated as missing, corrupting LRU ordering and
eviction. Fix the existence check to be correct for falsy values too.

- hidden_cmd: `pytest hidden_tests/test_task3_lru_cache_falsy_values.py -q`

### task4 - Respect custom document classes consistently

Issue/PR: #545 / #546. Real fix, merged 2024-10-05 (high risk given the age).
Adapted for the older base: dropped the `insert_multiple()` sub-test, since at this point in
history `insert_multiple()` doesn't honor explicit document IDs at all yet (a separate,
unimplemented feature at this base, not the isinstance bug in scope here). Kept the `insert()` and
`upsert()` sub-tests, which do reproduce the exact isinstance-against-the-wrong-class bug.

Several places that need to detect "this document already has an explicit ID" check against the
built-in `Document` class directly instead of `self.document_class` (a documented customization
point). Fix the ID-detection logic to consistently respect whichever document class the table is
configured to use.

- hidden_cmd: `pytest hidden_tests/test_task4_document_class_isinstance.py -q`

### task5 - Allow a table to persist even when empty

Issue/PR: #513 / #518. Real fix, merged 2024-10-07 (high risk given the age).

Add a `persist_empty` option so a table can be written to storage immediately on creation, even
with zero documents, instead of only appearing once something is inserted.

- hidden_cmd: `pytest hidden_tests/test_task5_persist_empty_tables.py -q`

### task6 - Make remove() prefer doc_ids over cond, like the other methods

Issue/PR: #424 (self-filed PR/issue, real, merged 2021-08-14). This is the task that determines the
base commit: it's the earliest real, still-applicable, merged-PR fix available anywhere in tinydb's
post-v4.0-rewrite history that wasn't already used by another task (see "Base commit selection"
below). No tests were included in the linked PR; hidden tests are fresh and strictly
behavior-based.

`get()`, `contains()`, and `update()` all prefer an explicit `doc_ids` argument over `cond` when
both are given. `remove()` checks `cond` first instead, so passing both silently ignores the given
IDs. Make `remove()` match the others: `doc_ids` should win.

- hidden_cmd: `pytest hidden_tests/test_task6_remove_doc_ids_priority.py -q`

## Base commit selection

Originally based at `9175dcb` (2024-10-05), with task6/task7 filled by fresh,
no-upstream-PR-fix tasks (field-comparison / no-reference-leak). Per request, those two were
replaced with real merged-PR-backed issues instead.

Every real (behavior-changing) merged-PR fix between `9175dcb` and current HEAD was already used
by tasks 1-5; none were left over. The only other unused real, merged-PR, still-applicable fix
anywhere in the post-rewrite history (commit `50d0cd8`, Nov 2019, onward - anything older uses a
pre-rewrite API tasks 1-5 don't work with) was `#424`, merged 2021-08-14. So the base moved back to
`#424`'s parent commit, `71283fd` (2021-07-17), and tasks 1/4 were trimmed to drop the two
assertions that depended on APIs (`get(doc_ids=...)`, `insert_multiple()` honoring explicit IDs)
that didn't exist yet at that point in history. Only one further merged-PR candidate turned up (not
two), so per instruction the suite ships as 6 tasks rather than force a second, weaker match.
