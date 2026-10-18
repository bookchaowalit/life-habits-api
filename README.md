# life-habits-api
The habits cell: habits and routines and their daily logs. It keeps its own
Neon database and reuses [empire-platform](https://github.com/bookchaowalit/platform-empire)
for identity, authorization, execution and audit records, and the graph, over
HTTP with the caller's own token (same pattern as
[decisions](https://github.com/bookchaowalit/life-decisions-api)).

| | |
|---|---|
| API | Railway project `life-habits-api`, service `habits-api` (Serverless: sleeps when idle) |
| Outbox sweep | service `habits-publisher`, cron `*/15 * * * *` |
| Weekly auto-grant | service `habits-autogrant`, cron `15 17 * * 6` UTC (Sunday 00:15 Asia/Bangkok) |
| Data | Neon project `life-habits-api`, database `habits` |
| Registry | solo-empire `repository-catalog/registries/online-systems.yaml` (`habits`) |

## Rules

- Writes: recorded precheck → local change → platform execution bound to the
  exact record → commit → finish with row hashes. Fails closed (503) when the
  platform cannot answer.
- **Streaks belong to the service.** Every change to a habit's logs recomputes
  `current_streak`, `best_streak` (never lowered) and `last_completed_at` in the
  same transaction: consecutive scheduled weekdays (every day without a
  schedule) completed, walking back from today in `HABITS_TIMEZONE`
  (default Asia/Bangkok); today not yet done does not break the streak.
- Permissions: `habits.habit:read|write|delete`, `habits.routine:read|write|delete`.
  Logging or un-logging a day is a write on its habit or routine.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/habits` · `/v1/routines` | list (`status`, `category`/`frequency`, `routine_type`, paging) |
| GET | `/v1/{habits,routines}/stats` | counts by status or a filter column |
| GET | `/v1/{habits,routines}/{id}` · `/slug/{slug}` | one record |
| POST | `/v1/habits` · `/v1/routines` | create (`Idempotency-Key` supported) |
| PATCH / DELETE | `/v1/{habits,routines}/{id}` | change (`expected_version`) / delete |
| GET | `/v1/habit-logs` · `/v1/routine-logs` | logs (`habit_id`/`routine_id`, `from`, `to`, `completed`, `notes_prefix`, `order`) |
| PUT / DELETE | `/v1/{habits,routines}/{id}/logs/{date}` | log a day (upsert; `?if_absent=true` keeps an existing completed log) / remove it |

## Weekly auto-grant

`habits-autogrant [--dry-run] [--days N]` (`src/solo_habits/autogrant.py`) replaces the
retired local `weekly_habit_autogranted` job. For each active habit and each of the
last N days (default 14, ending yesterday in Asia/Bangkok) on one of its
`schedule_days` (every day when unset) without a completed log, it logs the day
(`completed: true, count: 1`, note `auto-granted (weekly batch)`). It calls this
cell's own HTTP API as the `habit-autolog` automation (`habits.habit:read|write`),
never the database, so execution and audit records apply, and streaks are
recomputed by the service. The 14-day window overlaps the previous weekly runs, so a day that failed once is
retried the next week. Idempotent: days with a completed log are skipped, and each
grant is a conditional write (`PUT .../logs/{date}?if_absent=true`) that leaves a
completed log written meanwhile (for example a manual one) untouched, so re-running
never double-logs or overwrites. Zero habits is a
successful no-op. Prints one JSON line per run; exit 1 if the API fails or a day
could not be logged (retried next run), 2 on missing configuration. Variables
(Railway only): `HABITS_API_URL` (the `habits-api` public https URL),
`HABITS_AUTOGRANT_TOKEN` (the `habit-autolog` automation token; rotate with the other
cell tokens). Deploy: `railway up --ci --service habits-autogrant`; check:
`railway run --service habits-autogrant -- habits-autogrant --dry-run`.

## MCP server (`habits-mcp`)

`solo_habits.mcp_server` is this cell's MCP server (stdio). Every tool calls the cell's HTTP API with the caller's own
token, so MCP has exactly the authorization the API has: it never touches the database, and no tool accepts
an actor, workspace, delegation or token argument (strict models reject unknown arguments before any HTTP
call). Errors reach the client as a fixed message from the HTTP status only (for example `403: forbidden`), never upstream text; tokens with whitespace or control characters are refused.

- Tools: `habits.habit.{list,get,streaks,stats,logs}`, `habits.routine.{list,get,stats,logs}`.
- Write tools: `habits.habit.log` and `habits.routine.log` (log or correct one day; `if_absent` keeps an existing completed log). Set `EMPIRE_MCP_READONLY=1` to register only the read tools.
- URL: `EMPIRE_HABITS_API` (env, else the owner-only `~/.config/empire-platform/client.env`); https only (http only on loopback), no credentials, query or fragment.
- Token, first match wins: `EMPIRE_TOKEN`, `EMPIRE_TOKEN_FILE` (mode 600), then the automation token named by
  `EMPIRE_MCP_AUTOMATION` (default `owner-cli`, lower-case letters, digits and hyphens) from `EMPIRE_AUTOMATION_TOKEN_<NAME>` or
  `~/.config/empire-platform/tokens/<name>.jwt`.
- Run: `uv pip install --python .venv/bin/python -e ".[mcp]"`, then `.venv/bin/habits-mcp` (see `.mcp.json`).
- Tests: `.venv/bin/pytest tests/test_mcp.py` (fake HTTP transport; no database, no network).

## Develop and operate

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q          # add ../platform-empire[dev] for the end-to-end test
NEON_API_KEY=... .venv/bin/habits-db provision
npm install && railway config apply && railway up --ci --service habits-api
```

Railway variables (never in the repository): `SENTRY_DSN` (error reporting; unset = off, see
`src/solo_habits/_sentry.py`), `HABITS_DB_MODE=hosted`,
`HABITS_DB_URL_API` / `HABITS_DB_URL_PUBLISHER`, `HABITS_JWT_PUBLIC_KEY_PEM`,
`HABITS_JWT_ISSUER`, `EMPIRE_PLATFORM_URL`, `HABITS_PUBLISHER_TOKEN`.
