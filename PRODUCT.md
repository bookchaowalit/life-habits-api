# Product

<!-- impeccable:product-schema 1 -->

## Platform

api

## Users

- The Solo Empire owner, through `life-dashboard-web` (habits pages), the cell's own MCP server
  (`habits-mcp`) and the hosted tool hub (`platform-mcp-remote-worker`).
- Automations with an `empire-platform` token, within the permissions
  `habits.habit:read|write|delete` and `habits.routine:read|write|delete`: the ops-toolkit client
  `infra/scripts/utils/habits_store.py` (`EMPIRE_HABITS_API`) and this repository's weekly
  auto-grant (`habit-autolog` automation, `habits.habit:read|write`).

## Product Purpose

One record of the habits and routines the owner keeps and a log per day for each, so streaks are
computed the same way for every writer. The service owns the streak: no caller can write
`current_streak`, `best_streak` or `last_completed_at`.

Success: every scheduled day of an active habit has a log (by hand or from the weekly
auto-grant), and the streak shown in every client is the one the service computed.

## Positioning

One capability, one service: its own API (Railway), its own database (Neon) with row-level
security per workspace, and authorization, executions, audit and the graph through
`empire-platform` over HTTP with the caller's own token. It fails closed: when the platform cannot
answer, reads and writes return 503.

## Operating Context

- Production: `https://habits-api-production-e516.up.railway.app` (`/health`), Railway project
  `life-habits-api`; `habits-api` sleeps when idle (Serverless). Deployed by merging to `main`
  after CI (Railway Wait for CI).
- One image, three services: `habits-api`, `habits-publisher` (`habits-publish`, cron every 15
  minutes, outbox to the platform graph) and `habits-autogrant` (`habits-autogrant`, cron
  `15 17 * * 6` UTC = Sunday 00:15 Asia/Bangkok).
- Data in Neon project `life-habits-api`, database `habits`; migrations are append-only.
- Monitored by `platform-uptime-worker`; errors go to Sentry without personal data when
  `SENTRY_DSN` is set.

## Capabilities and Constraints

- `/v1/habits` and `/v1/routines`: list (status, category/frequency or routine type, paging),
  stats (by status or the kind's filter columns only), get by id or slug, create (with
  `Idempotency-Key`, 8 to 200 characters), patch (with `expected_version`), delete.
- Daily logs: `PUT/DELETE /v1/{habits,routines}/{id}/logs/{date}` (one log per record and day;
  `?if_absent=true` never overwrites an existing completed log) and `/v1/habit-logs`,
  `/v1/routine-logs` (record, date range, completed, notes prefix, order).
- Streak rule: consecutive scheduled weekdays completed (every day without a schedule), walking
  back from today in `HABITS_TIMEZONE` (default Asia/Bangkok), at most a year back; today not yet
  done does not break it; `best_streak` is never lowered. Reads report the streak as of today.
- Validation: slugs are lower-case letters, digits and hyphens (unique per workspace); unknown
  fields are refused; `target_count` 1 to 1000, `priority` 0 to 10, routine moods 1 to 5.
- Weekly auto-grant: for each active habit, logs every scheduled day of the last 14 days (ending
  yesterday) that has no completed log, through this cell's own API.
- Out of scope: reminders to log a habit [ต้องยืนยัน: whether the notifications cell sends any].

## Measures

- Active habits with a log for yesterday (target: all scheduled ones).
- Days logged by the weekly auto-grant versus by hand [ต้องยืนยัน: owner target].
- Outbox rows unpublished after an hour (target: 0).
- 5xx rate and p95 latency on `/v1/habits` (Sentry and uptime worker).
