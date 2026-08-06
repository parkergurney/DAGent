# arrow tasks

## Protected paths

- `hidden_tests/` - do not read, modify, or delete. Reserved for evaluation.

## Base commit

`6321d81485566cfe5878834e01b5e17cbe4a815b` (arrow-py/arrow, "Bump actions/cache from 3 to 4 (#1195)", 2024-10-17).
This is the parent of the earliest fix among the tasks below.
All later upstream history has been pruned; the repo's `bench-base` branch sits on top of this commit plus one housekeeping commit that gitignores `hidden_tests/`.

## Recency note

Only task 6 postdates the assumed training cutoff (late Jan 2026).
Tasks 1-5 are all older and are flagged individually below - treat any success on them with that in mind.

## Tasks

### 1. Let `shift()` skip the DST/imaginary-time check

**Flag:** merged 2024-10-19, well before the cutoff.

**Brief:** Arrow's `shift()` method always checks whether the shifted result lands on a datetime that doesn't actually exist (an "imaginary" local time caused by a DST transition, such as the hour that gets skipped when clocks spring forward) and silently corrects it if so.
This check adds overhead, and callers who know their date range never crosses a DST boundary have no way to skip it.
Add a `check_imaginary` keyword argument to `shift()` (default `True`, preserving current behavior) that, when set to `False`, skips the DST/imaginary-time check and returns the raw shifted result as-is.

**hidden_cmd:** `python -m pytest -o addopts="" hidden_tests/test_shift_check_imaginary.py -q`

### 2. Add a strict RFC 3339 format with a "T" separator

**Flag:** merged 2025-05-18, before the cutoff.

**Brief:** Arrow's existing `FORMAT_RFC3339` format string uses a space between the date and time portions.
Per RFC 3339, the "T" separator should be used to avoid ambiguity (this already matches what `str()` on an Arrow object produces).
Add a new built-in format constant, `FORMAT_RFC3339_STRICT`, identical to `FORMAT_RFC3339` except that it uses "T" instead of a space as the date/time separator, usable with `.format()` exactly like the existing constant.
Leave `FORMAT_RFC3339` itself unchanged.

**hidden_cmd:** `python -m pytest -o addopts="" hidden_tests/test_format_rfc3339_strict.py -q`

### 3. Migrate timezone handling to the standard library's zoneinfo (hard, multi-file)

**Flag:** merged 2025-08-06, before the cutoff.

**Brief:** Arrow currently resolves timezone names (like `"America/Anchorage"` or `"local"`) and builds its internal UTC/local tzinfo objects using `dateutil`'s timezone utilities. Python's standard library now has its own timezone database support (`zoneinfo`, with a `backports.zoneinfo` package for versions that predate it).
Migrate Arrow's timezone handling so that construction, parsing, and formatting all build tzinfo objects using the standard library's zoneinfo support instead of dateutil's timezone utilities, for UTC, `"local"`, and named IANA zones.
Preserve existing behavior for ordinary times.
Pay close attention to how a local wall-clock time that falls in a DST "gap" (one that never actually occurred, like 2:01 AM on a spring-forward day) gets resolved - it should resolve using the UTC offset that was in effect immediately before the transition, not after it.

**hidden_cmd:** `python -m pytest -o addopts="" hidden_tests/test_zoneinfo_dst_migration.py -q`

### 4. Support `week_start` in `floor()` and `ceil()`

**Flag:** merged 2025-10-02, before the cutoff.

**Brief:** Arrow's `span()` method already accepts a `week_start` keyword to control which weekday a `"week"` frame starts on, but the `floor()` and `ceil()` convenience methods (thin wrappers around `span()`) don't expose that option - calling `floor('week', week_start=7)` currently raises an error.
Update `floor()` and `ceil()` to accept `week_start` (and any other keyword arguments `span()` supports) and pass them through, so both methods support the same customization `span()` already does.

**hidden_cmd:** `python -m pytest -o addopts="" hidden_tests/test_floor_ceil_week_start.py -q`

### 5. Fix `humanize()` overcounting months across short months

**Flag:** merged 2025-10-02, before the cutoff. Also: this one has no linked GitHub issue - the brief below is paraphrased from the fix's own diff/behavior rather than an issue report, since none exists for this specific change. Everything else about it (real merged PR, real behavior change) is as described.

**depends_on:** none (but task 6 depends on this one)

**Brief:** `humanize()`'s output for differences in the multi-week-to-year range currently computes how many months apart two dates are by subtracting `(year * 12 + month)` for each date. This ignores the day-of-month, so it overcounts whenever the later date's day-of-month is earlier than the starting date's - for example, January 31 to May 1 is really 3 full months plus one extra day, but the naive subtraction reports it as 4 months.
Fix `humanize()` so it computes the actual number of elapsed calendar months/years using proper date arithmetic (a calendar month has only fully elapsed once you've reached, or passed, the same day-of-month in the later month) rather than naive month-number subtraction.

**hidden_cmd:** `python -m pytest -o addopts="" hidden_tests/test_humanize_calendar_months.py -q`

### 6. Fix `humanize()` mislabeling ~16-day differences as "a month"

**Flag:** merged 2026-02-19 - this one postdates the assumed cutoff. Two caveats worth knowing: the GitHub issue behind it is still open upstream (the reporter felt the fix was only a partial improvement and asked for a broader rethink of the rounding scheme), and the merged fix was later reverted on 2026-04-30 for undocumented reasons. The specific, narrow bug described below is real and the described fix genuinely resolves it, independent of whatever prompted that later revert.

**depends_on:** 5 (requires the calendar-aware month/year difference logic from task 5 to already be in place)

**Brief:** `humanize()` has a rule that rounds a date difference up to a full calendar month whenever more than 14 days separate the two dates, even if no actual calendar month boundary has been crossed yet.
This causes differences like 15-16 days within the same calendar month to be reported as "a month" (or "in a month") even though they're clearly closer to two weeks - for example, "today" January 9, an event on January 24 (15 days later) correctly shows "in two weeks", but one on January 25 (16 days later) incorrectly shows "in a month", which isn't close to accurate.
Fix `humanize()` so a difference is only described in month terms once at least one real calendar month has actually elapsed; differences that stay within the current month should continue to be described using weeks, even close to the 2-week mark.
Genuine calendar-month differences (a full month later, even if that happens to be 28-31 raw days) should still be described as "a month" as before.

**hidden_cmd:** `python -m pytest -o addopts="" hidden_tests/test_humanize_16day_weeks.py -q`
