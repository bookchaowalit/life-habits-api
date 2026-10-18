"""Weekly habit auto-grant: log the scheduled days nobody logged during the past week.

Runs as the cell's cron service (``habits-autogrant``), replacing the retired local
cron job ``weekly_habit_autogranted``. For each active habit and each of the last
``--days`` days (default 14, ending yesterday in Asia/Bangkok) that falls on one of
the habit's ``schedule_days`` (every day when none is set), a day without a
completed log gets one (``completed: true, count: 1``, note ``auto-granted (weekly
batch)``). Streaks stay the service's job: every log recomputes them.

Everything goes through the habits cell's own public HTTP API with the
``habit-autolog`` automation token (``habits.habit:read`` and ``habits.habit:write``),
so the platform write path (precheck, execution, audit) applies; the database is
never touched directly. Idempotent: a day that already has a completed log is
skipped, and each grant is a conditional write (``?if_absent=true``): a completed log
that appears after the read, such as a manual one, is left untouched by the API
itself. Re-running (or overlapping runs) never double-logs.

    habits-autogrant [--dry-run] [--days N]

Environment: ``HABITS_API_URL`` and ``HABITS_AUTOGRANT_TOKEN``. One JSON line per run
goes to stdout; nothing secret is printed. Exit 0 on success (including zero habits),
1 when the API fails, 2 on missing configuration.
"""

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ._sentry import capture

BANGKOK = ZoneInfo("Asia/Bangkok")
DAY_ABBR = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
NOTE = "auto-granted (weekly batch)"
PAGE = 500
MAX_DAYS = 31
DEFAULT_DAYS = (
    14  # a weekly run covers two weeks, so a day that failed once gets a second chance
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req, fp, code, msg, headers, newurl
    ):  # never forward a token
        return None


class RequestError(RuntimeError):
    """The request could not be built or sent; the message is fixed so no header value can leak."""

    def __init__(self) -> None:
        super().__init__("request_failed")


def bad_value(value: str) -> bool:
    """True when ``value`` has whitespace or control characters (CR/LF would reach the header)."""
    return any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)


class ApiError(RuntimeError):
    def __init__(self, status: int, where: str) -> None:
        super().__init__(f"{where}: HTTP {status}")
        self.status = status


class Http:
    """Bearer JSON client for the habits API (https only, no redirects)."""

    def __init__(self, base: str, token: str, timeout: float = 30.0) -> None:
        parts = urllib.parse.urlsplit(base)
        if parts.scheme != "https" and parts.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise SystemExit("HABITS_API_URL must be https")
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def call(
        self, method: str, path: str, body: Any = None, params: dict | None = None
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        query = f"?{urllib.parse.urlencode(clean, doseq=True)}" if clean else ""
        try:
            request = urllib.request.Request(
                f"{self.base}{path}{query}",
                method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Empire-Channel": "workflow",
                    "Content-Type": "application/json",
                },
            )
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, f"{method} {path}") from None
        except (ValueError, TypeError, http.client.HTTPException):
            raise RequestError() from None  # never include the exception text: it can quote the header


def pages(http: Http, path: str, params: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        page = http.call(
            "GET", path, params={**params, "limit": PAGE, "offset": len(out)}
        )["items"]
        out.extend(page)
        if len(page) < PAGE:
            return out


def scheduled(habit: dict[str, Any], weekday: str) -> bool:
    days = habit.get("schedule_days") or []
    return not days or weekday in {str(d).lower() for d in days}


def run(http: Http, today: date, days: int, *, dry_run: bool) -> dict[str, Any]:
    counts: dict[str, Any] = {"habits": 0, "granted": 0, "skipped": 0, "errors": 0}
    habits = pages(http, "/v1/habits", {"status": ["active"]})
    counts["habits"] = len(habits)
    if not habits:
        return counts
    start, end = today - timedelta(days=days), today - timedelta(days=1)
    done = {
        (log["habit_id"], str(log["log_date"])[:10])
        for log in pages(
            http,
            "/v1/habit-logs",
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "completed": "true",
                "order": "asc",
            },
        )
    }
    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        weekday, stamp = DAY_ABBR[day.weekday()], day.isoformat()
        for habit in habits:
            if not scheduled(habit, weekday):
                continue
            if (habit["id"], stamp) in done:
                counts["skipped"] += 1
                continue
            if dry_run:
                counts["granted"] += 1
                continue
            try:
                outcome = http.call(
                    "PUT",
                    f"/v1/habits/{int(habit['id'])}/logs/{stamp}",
                    {"completed": True, "count": 1, "notes": NOTE},
                    params={"if_absent": "true"},
                )["outcome"]
            except ApiError as exc:  # one failed day never blocks the rest; it is retried next run (the 14-day window overlaps)
                if not counts["errors"]:
                    capture(exc)  # one Sentry event per run, not one per failed day
                counts["errors"] += 1
                print(
                    json.dumps(
                        {
                            "event": "habits.autogrant.log_failed",
                            "habit_id": habit["id"],
                            "date": stamp,
                            "detail": str(exc),
                        }
                    ),
                    file=sys.stderr,
                )
                continue
            counts["granted" if outcome in ("created", "updated") else "skipped"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="habits-autogrant", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would be granted; write nothing",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"how many past days to cover (1-{MAX_DAYS}, default {DEFAULT_DAYS})",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.days <= MAX_DAYS:
        parser.error(f"--days must be between 1 and {MAX_DAYS}")
    env = os.environ
    url, token = (
        env.get("HABITS_API_URL", "").strip(),
        env.get("HABITS_AUTOGRANT_TOKEN", "").strip(),
    )
    missing = [
        k
        for k, v in (("HABITS_API_URL", url), ("HABITS_AUTOGRANT_TOKEN", token))
        if not v
    ]
    if missing:
        print(
            json.dumps(
                {
                    "event": "habits.autogrant",
                    "error": "missing_config",
                    "missing": missing,
                }
            )
        )
        return 2
    if bad_value(url) or bad_value(token):  # fixed message: never echo either value
        print(json.dumps({"event": "habits.autogrant", "error": "invalid_config"}))
        return 2
    today = datetime.now(BANGKOK).date()
    try:
        counts = run(Http(url, token), today, args.days, dry_run=args.dry_run)
    except RequestError as exc:
        capture(exc)
        print(json.dumps({"event": "habits.autogrant", "error": "request_failed"}))
        return 1
    except (ApiError, OSError, urllib.error.URLError) as exc:
        capture(exc)
        print(
            json.dumps(
                {
                    "event": "habits.autogrant",
                    "error": type(exc).__name__,
                    "detail": str(exc)[:200],
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "event": "habits.autogrant",
                "dry_run": args.dry_run,
                "days": args.days,
                "today": today.isoformat(),
                **counts,
            }
        )
    )
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
