# SPEC-001 — L2 baseline: locked dependencies, strict types, domain rules under test

Standard: ADR-0004 — TDD + SDD (architecture-docs, `docs/architecture/decisions/ADR-0004-tdd-sdd-standard.md`).
Big Loop Plan Wave 2 (solo-empire `repository-catalog/roadmaps/BIG-LOOP-PLAN-2026-10-21.md`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-001 / 1.0 |
| Status | in review (implemented; owner approval pending) |
| Repository / path | bookchaowalit/life-habits-api / `specs/001-l2-baseline/spec.md` |
| Owner (maker) | Claude Code (Wave 2 cells session) |
| Reviewer (checker) | Fresh Claude reviewer session (no maker context) |
| Approver | Owner (bookchaowalit): pending, see the Wave 2 report owner actions |
| Requirement links | Big Loop DoD L2: `PRODUCT.md`, ≥ 3 test files, Sentry, type check; L1 lockfile |
| Main-only | yes: committed straight to `main`, so the red run is local (ADR-0004 rule 2, v1.1) |
| Created / approved | 2026-10-22 / pending owner |

## 1. Problem and intent

- **Problem:** The habits cell (three services from one image) had no lockfile (pip installed unpinned versions), no type, lint or coverage gates in CI, and the streak, validation and daily-log rules were tested only through one broad API test file.
- **Outcome:** the image installs exactly `uv.lock` (`uv sync --frozen`), CI runs Ruff, mypy
  (strict on `src/`) and a coverage gate on shared CI v1.1.1, and the cell's own rules have
  tests of their own beyond the generic contract suite.
- **Why now:** Wave 2 of the Big Loop Plan; milestone M1 is 2026-11-15.

## 2. Scope

| In scope | Out of scope (non-goals) |
|---|---|
| Lock file and frozen install, CI caller bump, Ruff/mypy config and fixes, rule tests, `PRODUCT.md` | New endpoints or fields; schema changes |

## 3. Behaviour — scenarios

### AC-01 — Today not done keeps the streak

- **Given** the three days before today completed
- **When** the streak is computed before today is logged
- **Then** it is 3
- **Test:** `tests/test_streak_rule.py::test_today_not_done_yet_does_not_break_the_streak` · level: unit / API with local fakes

### AC-02 — Unscheduled days neither count nor break

- **Given** a Mon/Wed schedule with three back-to-back scheduled days and an extra Tuesday
- **When** the streak is computed on Thursday
- **Then** it is 3 with or without the Tuesday
- **Test:** `tests/test_streak_rule.py::test_days_outside_the_schedule_neither_count_nor_break` · level: unit / API with local fakes

### AC-03 — A year at most

- **Given** 500 consecutive completed days
- **When** the streak is computed
- **Then** it is 366
- **Test:** `tests/test_streak_rule.py::test_the_walk_back_stops_after_a_year` · level: unit / API with local fakes

### AC-04 — Owner's day

- **Given** HABITS_TIMEZONE set to UTC+14 and then UTC-11
- **When** today_local() is read
- **Then** the UTC+14 date is later
- **Test:** `tests/test_streak_rule.py::test_today_is_the_owners_day_from_habits_timezone` · level: unit / API with local fakes

### AC-05 — Slug format

- **Given** a slug with capitals, a leading hyphen, a space, 101 characters or nothing
- **When** a habit is created
- **Then** 422
- **Test:** `tests/test_habit_validation.py::test_slugs_are_lower_case_letters_digits_and_hyphens` · level: unit / API with local fakes

### AC-06 — Stats group-by allow-list

- **Given** a group_by outside status and the kind's filters
- **When** stats are read
- **Then** 422 naming group_by
- **Test:** `tests/test_habit_validation.py::test_stats_group_only_by_status_or_the_kinds_filters` · level: unit / API with local fakes

### AC-07 — Removing the only log

- **Given** a habit with one completed log today
- **When** the log is deleted
- **Then** current_streak 0, best_streak stays 1, last_completed_at null
- **Test:** `tests/test_log_rules.py::test_removing_the_only_log_resets_the_streak_but_keeps_the_best` · level: unit / API with local fakes

### AC-08 — Uncompleted logs

- **Given** yesterday completed and today logged as not completed
- **When** the habit is read
- **Then** streak 1 and last_completed_at yesterday
- **Test:** `tests/test_log_rules.py::test_an_uncompleted_log_does_not_extend_the_streak` · level: unit / API with local fakes

### AC-09 — Log permission is per kind

- **Given** an actor with routine write but no habit write
- **When** they log a habit day
- **Then** 403
- **Test:** `tests/test_log_rules.py::test_logging_a_habit_needs_habit_write_not_routine_write` · level: unit / API with local fakes

## 4. Interfaces and data

| Interface | Change | Contract |
|---|---|---|
| s | o | l |
| s | o | l |
| P | O | S |
| G | E | T |
| P | U | T |
| G | E | T |

- **Data model / migration:** N/A — no schema change.
- **Backward compatibility:** same routes and payloads; only tooling, locks, typing fixes and tests changed unless a scenario above says otherwise.

## 5. Non-functional requirements

| NFR | Target | How it is tested |
|---|---|---|
| Performance | N/A — no hot-path change beyond one comparison per declared range | — |
| Security / privacy | Authorization unchanged; Sentry sends no PII and scrubs tokens/URLs | existing auth tests, Sentry tests |
| Reliability / observability | Reproducible install from the lock file; opt-in Sentry error reporting | image rehearsal before push; Sentry tests |
| Accessibility / UX | N/A — API only | — |

## 6. Test plan (TDD)

- **Test levels:** unit and API tests with local fakes; no hosted service is called.
- **Test data:** generated in each test; no production data.
- **Coverage:** CI gate `coverage-threshold: "78"` (measured 80.39% on 2026-10-22, minus 2 points).
- **Not test-first (exceptions):** tests that pin behaviour the service already had
  (characterization tests, marked below) were written after the code by nature; tooling and
  config changes are covered by CI itself.

### Red / green log

| Scenario | Test | Red: commit / run + failure message | Green: commit / run | Refactor note |
|---|---|---|---|---|
| AC-01 | `tests/test_streak_rule.py::test_today_not_done_yet_does_not_break_the_streak` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-02 | `tests/test_streak_rule.py::test_days_outside_the_schedule_neither_count_nor_break` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-03 | `tests/test_streak_rule.py::test_the_walk_back_stops_after_a_year` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-04 | `tests/test_streak_rule.py::test_today_is_the_owners_day_from_habits_timezone` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-05 | `tests/test_habit_validation.py::test_slugs_are_lower_case_letters_digits_and_hyphens` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-06 | `tests/test_habit_validation.py::test_stats_group_only_by_status_or_the_kinds_filters` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-07 | `tests/test_log_rules.py::test_removing_the_only_log_resets_the_streak_but_keeps_the_best` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-08 | `tests/test_log_rules.py::test_an_uncompleted_log_does_not_extend_the_streak` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-09 | `tests/test_log_rules.py::test_logging_a_habit_needs_habit_write_not_routine_write` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |

## 7. Risks and open questions

| # | Risk or question | Impact | Owner | Resolution / date |
|---|---|---|---|---|
| 1 | Stored rows written before a range rule existed may already be inverted | a patch touching one of those dates is refused until both are fixed | owner | patches that do not touch the range still work |

## 8. Change log

| Version | Date | Change | Scenarios affected | Approved by |
|---|---|---|---|---|
| 1.0 | 2026-10-22 | Implemented with the Wave 2 baseline | all | pending owner |

## Approval

| Gate | Role | Name / date | Decision | Evidence |
|---|---|---|---|---|
| Implemented (DoD) | Reviewer (checker) | Fresh Claude reviewer / 2026-10-22 | see Wave 2 report | CI run of the pushed tip |
| Implemented (DoD) | Approver | Owner | pending | Wave 2 report |

## Review (2026-10-24): changes requested

Checker: independent Claude reviewer subagent (`solo:solo-reviewer`, Sonnet, read-only, no maker context). Status unchanged until fixed.

- Tests run by the checker: .venv/bin/pytest -q --cov: 111 passed, 1 skipped
- Gap: All 9 cited tests pass, but §4 'Interfaces and data' table is corrupted (single characters per cell) and §5 performance + risk #1 are copy-paste from the range-rule specs. Regenerate §4 and drop the irrelevant text.
