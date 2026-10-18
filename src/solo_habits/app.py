"""Habits API: habits and routines, and their daily logs.

The cell keeps its own database. Authorization, execution records and audit
live in empire-platform and are reached over HTTP with the caller's token:

- reads ask ``/v1/authz/decide`` (``habits.habit:read`` / ``habits.routine:read``);
- writes run a recorded precheck, change rows in an open local transaction,
  start a platform execution bound to the exact record and every permission
  the change needs, commit, then finish the execution with row hashes.

Permissions: ``habits.habit:read|write|delete`` and ``habits.routine:read|write|delete``.
Logging a day (or removing a day's log) is a write on its habit or routine.
"""

import contextvars
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
)
from fastapi import (
    Request as HttpRequest,
)
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from solo_habits.auth import Principal, TokenVerifier, Unauthenticated
from solo_habits.platform import Action, Denied, Platform, PlatformUnavailable
from solo_habits.publisher import publish_pending

log = logging.getLogger("solo_habits")
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)
CORRELATION = re.compile(r"^[A-Za-z0-9-]{8,64}$")
SLUG = r"^[a-z0-9][a-z0-9-]{0,99}$"

Category = Literal[
    "health",
    "learning",
    "business",
    "creative",
    "mindfulness",
    "productivity",
    "social",
    "financial",
]
Frequency = Literal["daily", "weekly", "monthly"]
HabitStatus = Literal["active", "paused", "completed", "abandoned"]
RoutineType = Literal[
    "daily_morning", "daily_evening", "weekly_review", "monthly_review", "custom"
]
RoutineStatus = Literal["active", "paused", "archived"]


# --- models --------------------------------------------------------------------------------------


class HabitFields(BaseModel):
    # The streak fields are derived from the logs by the service; a write that names them is refused.
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=SLUG)
    description: str | None = Field(default=None, max_length=4000)
    category: Category | None = None
    frequency: Frequency | None = None
    target_count: int | None = Field(default=None, ge=1, le=1000)
    target_duration_minutes: int | None = Field(default=None, ge=0, le=100000)
    status: HabitStatus | None = None
    priority: int | None = Field(default=None, ge=0, le=10)
    schedule_days: list[str] | None = None
    preferred_time: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=8000)


class HabitCreate(HabitFields):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=SLUG)
    legacy_id: int | None = Field(default=None, gt=0)


class HabitUpdate(HabitFields):
    expected_version: int | None = Field(default=None, ge=1)


class Habit(BaseModel):
    id: int
    legacy_id: int | None
    name: str
    slug: str
    description: str | None
    category: str
    frequency: str
    target_count: int
    target_duration_minutes: int | None
    status: str
    priority: int
    current_streak: int
    best_streak: int
    last_completed_at: date | None
    schedule_days: list[str] | None
    preferred_time: str | None
    notes: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class RoutineFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, pattern=SLUG)
    description: str | None = Field(default=None, max_length=4000)
    routine_type: RoutineType | None = None
    steps: list[dict[str, Any]] | None = None
    status: RoutineStatus | None = None
    total_duration_minutes: int | None = Field(default=None, ge=0, le=100000)


class RoutineCreate(RoutineFields):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=SLUG)
    legacy_id: int | None = Field(default=None, gt=0)


class RoutineUpdate(RoutineFields):
    expected_version: int | None = Field(default=None, ge=1)


class Routine(BaseModel):
    id: int
    legacy_id: int | None
    name: str
    slug: str
    description: str | None
    routine_type: str
    steps: list[dict[str, Any]]
    status: str
    total_duration_minutes: int | None
    version: int
    created_at: datetime
    updated_at: datetime


class HabitLogPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed: bool | None = None
    count: int | None = Field(default=None, ge=0, le=100000)
    duration_minutes: int | None = Field(default=None, ge=0, le=100000)
    notes: str | None = Field(default=None, max_length=8000)


class HabitLog(BaseModel):
    id: int
    habit_id: int
    log_date: date
    completed: bool
    count: int
    duration_minutes: int | None
    notes: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class RoutineLogPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_steps: list[Any] | None = None
    skipped_steps: list[Any] | None = None
    duration_minutes: int | None = Field(default=None, ge=0, le=100000)
    mood_before: int | None = Field(default=None, ge=1, le=5)
    mood_after: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = Field(default=None, max_length=8000)


class RoutineLog(BaseModel):
    id: int
    routine_id: int
    log_date: date
    completed_steps: list[Any]
    skipped_steps: list[Any]
    duration_minutes: int | None
    mood_before: int | None
    mood_after: int | None
    notes: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class Items(BaseModel):
    items: list[dict[str, Any]]


class LogResult(BaseModel):
    outcome: Literal["created", "updated", "unchanged"]
    log: dict[str, Any]


class Stats(BaseModel):
    groups: dict[str, int]


# --- resources -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Kind:
    """One owning resource (habit or routine) and its daily log table."""

    name: str
    table: str
    log_table: str
    log_key: str
    columns: tuple[str, ...]
    log_columns: tuple[str, ...]
    json_columns: frozenset[str]
    log_json_columns: frozenset[str]
    required: frozenset[str]
    model: type[BaseModel]
    log_model: type[BaseModel]

    @property
    def select(self) -> str:
        return (
            "id, legacy_id, "
            + ", ".join(self.columns)
            + ", version, created_at, updated_at"
        )

    @property
    def log_select(self) -> str:
        return (
            f"id, {self.log_key}, log_date, "
            + ", ".join(self.log_columns)
            + ", version, created_at, updated_at"
        )

    def permission(self, right: str) -> str:
        return f"habits.{self.name}:{right}"


HABIT = Kind(
    name="habit",
    table="habits.habits",
    log_table="habits.habit_logs",
    log_key="habit_id",
    columns=(
        "name",
        "slug",
        "description",
        "category",
        "frequency",
        "target_count",
        "target_duration_minutes",
        "status",
        "priority",
        "current_streak",
        "best_streak",
        "last_completed_at",
        "schedule_days",
        "preferred_time",
        "notes",
    ),
    log_columns=("completed", "count", "duration_minutes", "notes"),
    json_columns=frozenset({"schedule_days"}),
    log_json_columns=frozenset(),
    required=frozenset(
        {
            "name",
            "slug",
            "category",
            "frequency",
            "target_count",
            "status",
            "priority",
            "current_streak",
            "best_streak",
        }
    ),
    model=Habit,
    log_model=HabitLog,
)

ROUTINE = Kind(
    name="routine",
    table="habits.routines",
    log_table="habits.routine_logs",
    log_key="routine_id",
    columns=(
        "name",
        "slug",
        "description",
        "routine_type",
        "steps",
        "status",
        "total_duration_minutes",
    ),
    log_columns=(
        "completed_steps",
        "skipped_steps",
        "duration_minutes",
        "mood_before",
        "mood_after",
        "notes",
    ),
    json_columns=frozenset({"steps"}),
    log_json_columns=frozenset({"completed_steps", "skipped_steps"}),
    required=frozenset({"name", "slug", "routine_type", "steps", "status"}),
    model=Routine,
    log_model=RoutineLog,
)

LOG_REQUIRED = frozenset({"completed", "count", "completed_steps", "skipped_steps"})
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def today_local() -> date:
    """The owner's day (HABITS_TIMEZONE, default Asia/Bangkok), not the server's."""
    from zoneinfo import ZoneInfo

    return datetime.now(
        ZoneInfo(os.environ.get("HABITS_TIMEZONE", "Asia/Bangkok"))
    ).date()


def streak(
    completed_days: set[date], schedule_days: list[str] | None, today: date
) -> int:
    """Consecutive scheduled days completed, walking back from today.

    The one streak rule for every writer: only scheduled weekdays count (every
    day when there is no schedule); today not yet done does not break it; at
    most a year back.
    """
    scheduled = {
        WEEKDAYS[d.lower()] for d in (schedule_days or []) if d.lower() in WEEKDAYS
    } or set(range(7))
    count, day = 0, today
    for _ in range(366):
        if day.weekday() in scheduled:
            if day in completed_days:
                count += 1
            elif day != today:
                break
        day = date.fromordinal(day.toordinal() - 1)
    return count


def row_hash(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str).encode()
    ).hexdigest()


@dataclass
class Work:
    """One write's open local transaction; ``marker`` tags its outbox rows until the execution exists."""

    conn: psycopg.Connection
    marker: uuid.UUID
    caller: Principal


@dataclass
class Plan:
    permission: str
    resource_id: str
    audit: dict[str, Any]
    result: Any


def create_app(
    connect: Callable[[], psycopg.Connection],
    verifier: TokenVerifier,
    platform: Platform,
    publisher_token: Callable[[], str | None] = lambda: None,
) -> FastAPI:
    app = FastAPI(title="Habits API", version="1.0.0", description=__doc__)

    @app.middleware("http")
    async def correlate(request: HttpRequest, call_next):
        given = request.headers.get("x-correlation-id", "")
        token = correlation_id.set(
            given if CORRELATION.match(given) else uuid.uuid4().hex
        )
        try:
            response = await call_next(request)
        finally:
            value = correlation_id.get()
            correlation_id.reset(token)
        response.headers["X-Correlation-Id"] = value
        return response

    def principal(
        authorization: Annotated[str | None, Header()] = None,
        x_empire_channel: Annotated[str | None, Header()] = None,
    ) -> Principal:
        try:
            return verifier.verify(authorization, x_empire_channel)
        except Unauthenticated as exc:
            raise HTTPException(
                status_code=401, detail={"error": "unauthenticated", "reason": str(exc)}
            ) from exc

    Caller = Annotated[Principal, Depends(principal)]

    @contextmanager
    def bound(caller: Principal) -> Iterator[psycopg.Connection]:
        try:
            conn = connect()
        except psycopg.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail={"error": "unavailable"}
            ) from exc
        conn.row_factory = dict_row
        try:
            conn.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(caller.workspace),),
            )
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def allow(
        caller: Principal,
        action: str,
        resource_type: str,
        resource_id: str = "*",
        *,
        not_found: bool = False,
    ) -> None:
        try:
            allowed = platform.decide(
                caller.token, caller.channel, Action(action, resource_type, resource_id)
            )
        except Denied as exc:
            raise HTTPException(
                status_code=401,
                detail={"error": "unauthenticated", "reason": exc.reason},
            ) from exc
        except PlatformUnavailable as exc:
            raise HTTPException(
                status_code=503, detail={"error": "authorization_unavailable"}
            ) from exc
        if not allowed:
            if not_found:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            raise HTTPException(status_code=403, detail={"error": "deny"})

    def write(
        caller: Principal,
        kind: Kind,
        op: Callable[[Work], Plan],
        *,
        precheck: tuple[str, str],
        idempotency_key: str | None = None,
    ) -> Any:
        """Precheck, local change, platform execution bound to the exact record, commit, finish.

        A replay of the same operation (same idempotency key) continues a "running"
        execution whose answer was lost and returns the existing result for a
        finished one.
        """
        allow(caller, precheck[0], kind.name, precheck[1])
        with bound(caller) as conn:
            work = Work(conn, uuid.uuid4(), caller)
            try:
                plan = op(work)
                try:
                    execution, replayed, status = platform.begin(
                        caller.token,
                        caller.channel,
                        Action(plan.permission, kind.name, plan.resource_id),
                        idempotency_key,
                        correlation_id.get(),
                    )
                except Denied as exc:
                    code = exc.status if exc.status in (401, 403, 409) else 403
                    raise HTTPException(
                        status_code=code,
                        detail={"error": exc.decision, "reason": exc.reason},
                    ) from exc
                except PlatformUnavailable as exc:
                    raise HTTPException(
                        status_code=503, detail={"error": "authorization_unavailable"}
                    ) from exc
                if replayed and status == "succeeded":
                    conn.rollback()
                    return plan.result
                if replayed and status != "running":
                    raise HTTPException(
                        status_code=409, detail={"error": "replayed", "status": status}
                    )
                conn.execute(
                    "UPDATE habits.outbox SET execution_id = %s WHERE execution_id = %s",
                    (execution, work.marker),
                )
                conn.commit()
            except psycopg.errors.UniqueViolation as exc:
                raise HTTPException(
                    status_code=409, detail={"error": "conflict"}
                ) from exc
            except psycopg.errors.ForeignKeyViolation as exc:
                raise HTTPException(
                    status_code=422, detail={"error": "reference_not_found"}
                ) from exc
            except (
                psycopg.errors.NotNullViolation,
                psycopg.errors.CheckViolation,
            ) as exc:
                raise HTTPException(
                    status_code=422, detail={"error": "invalid_value"}
                ) from exc
        try:
            platform.finish(
                caller.token,
                caller.channel,
                execution,
                {"status": "succeeded", "service": "habits", **plan.audit},
            )
        except (PlatformUnavailable, Denied):
            log.warning(
                json.dumps(
                    {"event": "habits.finish_failed", "execution": str(execution)}
                )
            )
        log.info(
            json.dumps(
                {
                    "event": "habits.write",
                    "action": plan.permission,
                    "execution": str(execution),
                    "actor": str(caller.actor),
                    "workspace": str(caller.workspace),
                }
            )
        )
        return plan.result

    def audit(
        kind: Kind, verb: str, record_id: int, before: dict | None, after: dict | None
    ) -> dict[str, Any]:
        return {
            "action": f"habits.{kind.name}.{verb}",
            "resource_type": kind.name,
            "resource_id": str(record_id),
            "before_hash": row_hash(before),
            "after_hash": row_hash(after),
        }

    def emit(
        work: Work, kind: Kind, verb: str, before: dict | None, after: dict | None
    ) -> dict[str, Any]:
        current = after or before or {}
        payload: dict[str, Any] = {
            "version": current.get("version", 0) + (1 if after is None else 0),
            "deleted": after is None,
        }
        if after is not None:
            payload.update(name=after["name"], status=after["status"])
        work.conn.execute(
            "INSERT INTO habits.outbox (workspace_id, type, subject, execution_id, classification, payload,"
            " correlation_id) VALUES (%s, %s, %s, %s, 'confidential', %s, %s)",
            (
                work.caller.workspace,
                f"habits.{kind.name}.{verb}",
                f"empire:habits:{kind.name}:{current['id']}",
                work.marker,
                json.dumps(payload, default=str),
                correlation_id.get(),
            ),
        )
        return audit(kind, verb, current["id"], before, after)

    def publish_soon(background: BackgroundTasks, caller: Principal) -> None:
        token = publisher_token()
        if not token:
            return

        def run() -> None:
            try:
                with bound(caller) as conn:
                    publish_pending(conn, platform, token, limit=20)
            except Exception as exc:  # noqa: BLE001 - the cron sweep retries; never fail the request for this
                log.warning(
                    json.dumps(
                        {
                            "event": "habits.inline_publish_failed",
                            "error": type(exc).__name__,
                        }
                    )
                )

        background.add_task(run)

    def values(
        kind: Kind, body: BaseModel, names: set[str], *, logs: bool = False
    ) -> dict[str, Any]:
        columns = kind.log_columns if logs else kind.columns
        required = LOG_REQUIRED if logs else kind.required
        json_columns = kind.log_json_columns if logs else kind.json_columns
        data = {
            name: getattr(body, name)
            for name in columns
            if name in names and hasattr(body, name)
        }
        nulls = sorted(name for name in required if name in data and data[name] is None)
        if nulls:
            raise HTTPException(
                status_code=422, detail={"error": "invalid_value", "fields": nulls}
            )
        for name in json_columns & set(data):
            data[name] = json.dumps(data[name]) if data[name] is not None else None
        return data

    def changed(
        kind: Kind, before: dict[str, Any], data: dict[str, Any], *, logs: bool = False
    ) -> dict[str, Any]:
        json_columns = kind.log_json_columns if logs else kind.json_columns
        out = {}
        for name, value in data.items():
            current = (
                json.dumps(before.get(name))
                if name in json_columns and before.get(name) is not None
                else before.get(name)
            )
            if (
                str(current) != str(value)
                if current is not None and value is not None
                else current != value
            ):
                out[name] = value
        return out

    def refresh_streak(work: Work, habit_id: int) -> None:
        """Recompute the habit's streak fields from its logs, in the writing transaction."""
        habit = work.conn.execute(
            "SELECT schedule_days, best_streak FROM habits.habits WHERE id = %s FOR UPDATE",
            (habit_id,),
        ).fetchone()
        done = {
            row["log_date"]
            for row in work.conn.execute(
                "SELECT log_date FROM habits.habit_logs WHERE habit_id = %s AND completed AND log_date >= %s",
                (habit_id, date.fromordinal(today_local().toordinal() - 400)),
            )
        }
        last = work.conn.execute(
            "SELECT max(log_date) AS d FROM habits.habit_logs WHERE habit_id = %s AND completed",
            (habit_id,),
        ).fetchone()["d"]
        current = streak(done, habit["schedule_days"], today_local())
        work.conn.execute(
            "UPDATE habits.habits SET current_streak = %s, best_streak = greatest(best_streak, %s),"
            " last_completed_at = %s, version = version + 1, updated_at = now()"
            " WHERE id = %s AND (current_streak, last_completed_at) IS DISTINCT FROM (%s, %s)",
            (current, current, last, habit_id, current, last),
        )

    def as_of_today(
        conn: psycopg.Connection, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Habits with current_streak as of the owner's today (a missed day breaks it before any new log)."""
        if not rows:
            return rows
        today = today_local()
        done: dict[int, set[date]] = {}
        for row in conn.execute(
            "SELECT habit_id, log_date FROM habits.habit_logs WHERE habit_id = ANY(%s)"
            " AND completed AND log_date >= %s",
            ([r["id"] for r in rows], date.fromordinal(today.toordinal() - 400)),
        ):
            done.setdefault(row["habit_id"], set()).add(row["log_date"])
        out = []
        for row in rows:
            current = streak(done.get(row["id"], set()), row["schedule_days"], today)
            out.append(
                {
                    **row,
                    "current_streak": current,
                    "best_streak": max(row["best_streak"], current),
                }
            )
        return out

    def load(
        conn: psycopg.Connection, kind: Kind, record_id: int, *, lock: bool = False
    ) -> dict[str, Any]:
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
        row = conn.execute(
            f"SELECT {kind.select} FROM {kind.table} WHERE id = %s{' FOR UPDATE' if lock else ''}",
            (record_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return row

    def reserve(caller: Principal, kind: Kind, key: str) -> int:
        if not 8 <= len(key) <= 200:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_value", "fields": ["Idempotency-Key"]},
            )
        with bound(caller) as conn:
            conn.execute(
                "INSERT INTO habits.create_keys (workspace_id, actor, kind, key, record_id)"
                f" VALUES (%s, %s, %s, %s, nextval(pg_get_serial_sequence('{kind.table}', 'id')))"
                " ON CONFLICT DO NOTHING",
                (caller.workspace, caller.actor, kind.name, key),
            )
            row = conn.execute(
                "SELECT record_id FROM habits.create_keys WHERE actor = %s AND kind = %s AND key = %s",
                (caller.actor, kind.name, key),
            ).fetchone()
            conn.commit()
        return int(row["record_id"])

    # --- generic routes, registered once per kind ------------------------------------------------

    def register(
        kind: Kind,
        plural: str,
        create_model: type[BaseModel],
        update_model: type[BaseModel],
        log_model_in: type[BaseModel],
        filters: tuple[str, ...],
    ) -> None:
        model, log_out = kind.model, kind.log_model

        @app.get(f"/v1/{plural}", response_model=Items, name=f"list_{plural}")
        def list_records(
            caller: Caller,
            status: Annotated[list[str] | None, Query()] = None,
            category: str | None = None,
            frequency: str | None = None,
            routine_type: str | None = None,
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
            offset: Annotated[int, Query(ge=0)] = 0,
        ) -> Items:
            allow(caller, kind.permission("read"), kind.name)
            where, params = ["TRUE"], []
            for column, value in (
                ("category", category),
                ("frequency", frequency),
                ("routine_type", routine_type),
            ):
                if value is not None and column in filters:
                    where.append(f"{column} = %s")
                    params.append(value)
            if status:
                where.append("status = ANY(%s)")
                params.append(list(status))
            with bound(caller) as conn:
                rows = conn.execute(
                    f"SELECT {kind.select} FROM {kind.table} WHERE {' AND '.join(where)}"
                    " ORDER BY id LIMIT %s OFFSET %s",
                    [*params, limit, offset],
                ).fetchall()
                if kind is HABIT:
                    rows = as_of_today(conn, rows)
            return Items(items=[model(**row).model_dump(mode="json") for row in rows])

        @app.get(f"/v1/{plural}/stats", response_model=Stats, name=f"stats_{plural}")
        def stats(caller: Caller, group_by: str = "status") -> Stats:
            if group_by not in ("status", *filters):
                raise HTTPException(
                    status_code=422,
                    detail={"error": "invalid_value", "fields": ["group_by"]},
                )
            allow(caller, kind.permission("read"), kind.name)
            with bound(caller) as conn:
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- group_by is checked against a fixed allow-list before use; values are %s parameters
                rows = conn.execute(
                    f"SELECT {group_by} AS key, count(*) AS n FROM {kind.table} GROUP BY 1"
                ).fetchall()
            return Stats(groups={row["key"]: row["n"] for row in rows})

        @app.get(
            f"/v1/{plural}/slug/{{slug}}",
            response_model=model,
            name=f"{kind.name}_by_slug",
        )
        def by_slug(slug: str, caller: Caller):
            allow(caller, kind.permission("read"), kind.name, not_found=True)
            with bound(caller) as conn:
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                row = conn.execute(
                    f"SELECT {kind.select} FROM {kind.table} WHERE slug = %s", (slug,)
                ).fetchone()
                if row is not None and kind is HABIT:
                    row = as_of_today(conn, [row])[0]
            if row is None:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            return model(**row)

        @app.get(
            f"/v1/{plural}/{{record_id}}", response_model=model, name=f"get_{kind.name}"
        )
        def get_record(record_id: int, caller: Caller):
            allow(
                caller,
                kind.permission("read"),
                kind.name,
                str(record_id),
                not_found=True,
            )
            with bound(caller) as conn:
                row = load(conn, kind, record_id)
                return model(**(as_of_today(conn, [row])[0] if kind is HABIT else row))

        @app.post(
            f"/v1/{plural}",
            status_code=201,
            response_model=model,
            name=f"create_{kind.name}",
        )
        def create(
            body: create_model,
            caller: Caller,
            background: BackgroundTasks,  # type: ignore[valid-type]
            idempotency_key: Annotated[str | None, Header()] = None,
        ):
            data = values(kind, body, body.model_fields_set | {"name", "slug"})
            data = {k: v for k, v in data.items() if v is not None}
            reserved = (
                reserve(caller, kind, idempotency_key) if idempotency_key else None
            )

            def op(work: Work) -> Plan:
                if reserved is not None:
                    # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                    existing = work.conn.execute(
                        f"SELECT {kind.select} FROM {kind.table} WHERE id = %s",
                        (reserved,),
                    ).fetchone()
                    if existing is not None:
                        return Plan(
                            kind.permission("write"),
                            str(reserved),
                            audit(kind, "created", reserved, None, existing),
                            existing,
                        )
                columns, params = (
                    ["workspace_id", *data],
                    [caller.workspace, *data.values()],
                )
                if body.legacy_id is not None:
                    columns.append("legacy_id")
                    params.append(body.legacy_id)
                overriding = ""
                if reserved is not None:
                    columns.append("id")
                    params.append(reserved)
                    overriding = " OVERRIDING SYSTEM VALUE"
                row = work.conn.execute(
                    f"INSERT INTO {kind.table} ({', '.join(columns)}){overriding}"
                    f" VALUES ({', '.join(['%s'] * len(columns))}) RETURNING {kind.select}",
                    params,
                ).fetchone()
                return Plan(
                    kind.permission("write"),
                    str(row["id"]),
                    emit(work, kind, "created", None, row),
                    row,
                )

            row = write(
                caller,
                kind,
                op,
                precheck=(kind.permission("write"), "new"),
                idempotency_key=idempotency_key,
            )
            publish_soon(background, caller)
            return model(**row)

        @app.patch(
            f"/v1/{plural}/{{record_id}}",
            response_model=model,
            name=f"update_{kind.name}",
        )
        def update(
            record_id: int,
            body: update_model,
            caller: Caller,  # type: ignore[valid-type]
            background: BackgroundTasks,
        ):
            requested = values(kind, body, body.model_fields_set - {"expected_version"})

            def op(work: Work) -> Plan:
                before = load(work.conn, kind, record_id, lock=True)
                if (
                    body.expected_version is not None
                    and before["version"] != body.expected_version
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "version_conflict",
                            "version": before["version"],
                        },
                    )
                data = changed(kind, before, requested)
                if not data:
                    return Plan(
                        kind.permission("write"),
                        str(record_id),
                        audit(kind, "unchanged", record_id, before, before),
                        before,
                    )
                assignments = ", ".join(f"{name} = %s" for name in data)
                row = work.conn.execute(
                    f"UPDATE {kind.table} SET {assignments}, version = version + 1, updated_at = now()"
                    f" WHERE id = %s RETURNING {kind.select}",
                    [*data.values(), record_id],
                ).fetchone()
                return Plan(
                    kind.permission("write"),
                    str(record_id),
                    emit(work, kind, "updated", before, row),
                    row,
                )

            row = write(
                caller, kind, op, precheck=(kind.permission("write"), str(record_id))
            )
            publish_soon(background, caller)
            return model(**row)

        @app.delete(
            f"/v1/{plural}/{{record_id}}", status_code=204, name=f"delete_{kind.name}"
        )
        def delete(record_id: int, caller: Caller, background: BackgroundTasks) -> None:
            def op(work: Work) -> Plan:
                before = load(work.conn, kind, record_id, lock=True)
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                work.conn.execute(
                    f"DELETE FROM {kind.table} WHERE id = %s", (record_id,)
                )
                return Plan(
                    kind.permission("delete"),
                    str(record_id),
                    emit(work, kind, "deleted", before, None),
                    None,
                )

            write(
                caller, kind, op, precheck=(kind.permission("delete"), str(record_id))
            )
            publish_soon(background, caller)

        @app.get(
            f"/v1/{kind.name}-logs", response_model=Items, name=f"{kind.name}_logs"
        )
        def list_logs(
            caller: Caller,
            record_id: Annotated[int | None, Query(alias=kind.log_key)] = None,
            date_from: Annotated[date | None, Query(alias="from")] = None,
            date_to: Annotated[date | None, Query(alias="to")] = None,
            completed: bool | None = None,
            notes_prefix: Annotated[str | None, Query(max_length=100)] = None,
            order: Literal["asc", "desc"] = "asc",
            limit: Annotated[int, Query(ge=1, le=1000)] = 500,
            offset: Annotated[int, Query(ge=0)] = 0,
        ) -> Items:
            allow(caller, kind.permission("read"), kind.name)
            where, params = ["TRUE"], []
            if record_id is not None:
                where.append(f"{kind.log_key} = %s")
                params.append(record_id)
            if date_from is not None:
                where.append("log_date >= %s")
                params.append(date_from)
            if date_to is not None:
                where.append("log_date <= %s")
                params.append(date_to)
            if completed is not None and "completed" in kind.log_columns:
                where.append("completed = %s")
                params.append(completed)
            if notes_prefix:
                where.append("starts_with(coalesce(notes, ''), %s)")
                params.append(notes_prefix)
            direction = "DESC" if order == "desc" else "ASC"
            with bound(caller) as conn:
                rows = conn.execute(
                    f"SELECT {kind.log_select} FROM {kind.log_table} WHERE {' AND '.join(where)}"
                    f" ORDER BY log_date {direction}, id {direction} LIMIT %s OFFSET %s",
                    [*params, limit, offset],
                ).fetchall()
            return Items(items=[log_out(**row).model_dump(mode="json") for row in rows])

        @app.put(
            f"/v1/{plural}/{{record_id}}/logs/{{log_date}}",
            response_model=LogResult,
            name=f"put_{kind.name}_log",
        )
        def put_log(
            record_id: int,
            log_date: date,
            body: log_model_in,
            caller: Caller,  # type: ignore[valid-type]
            if_absent: Annotated[
                bool,
                Query(
                    description="Conditional write: leave an existing completed log"
                    " (any log for kinds without completed) untouched"
                ),
            ] = False,
        ):
            data = values(kind, body, body.model_fields_set, logs=True)

            def op(work: Work) -> Plan:
                load(
                    work.conn, kind, record_id, lock=True
                )  # serializes writes to this record's logs
                before = work.conn.execute(
                    f"SELECT {kind.log_select} FROM {kind.log_table}"
                    f" WHERE {kind.log_key} = %s AND log_date = %s FOR UPDATE",
                    (record_id, log_date),
                ).fetchone()
                if before is None:
                    columns = ["workspace_id", kind.log_key, "log_date", *data]
                    row = work.conn.execute(
                        f"INSERT INTO {kind.log_table} ({', '.join(columns)})"
                        f" VALUES ({', '.join(['%s'] * len(columns))}) RETURNING {kind.log_select}",
                        [caller.workspace, record_id, log_date, *data.values()],
                    ).fetchone()
                    outcome = "created"
                elif if_absent and before.get("completed", True):
                    # Decided under the record lock and row lock above, so a log written after the caller's
                    # read is never overwritten.
                    row, outcome = before, "unchanged"
                else:
                    diff = changed(kind, before, data, logs=True)
                    row, outcome = before, "unchanged"
                    if diff:
                        assignments = ", ".join(f"{name} = %s" for name in diff)
                        row = work.conn.execute(
                            f"UPDATE {kind.log_table} SET {assignments}, version = version + 1, updated_at = now()"
                            f" WHERE id = %s RETURNING {kind.log_select}",
                            [*diff.values(), before["id"]],
                        ).fetchone()
                        outcome = "updated"
                if kind is HABIT and outcome != "unchanged":
                    refresh_streak(work, record_id)
                return Plan(
                    kind.permission("write"),
                    str(record_id),
                    audit(kind, "logged", record_id, before, row),
                    (outcome, row),
                )

            outcome, row = write(
                caller, kind, op, precheck=(kind.permission("write"), str(record_id))
            )
            return LogResult(
                outcome=outcome, log=log_out(**row).model_dump(mode="json")
            )

        @app.delete(
            f"/v1/{plural}/{{record_id}}/logs/{{log_date}}",
            status_code=204,
            name=f"delete_{kind.name}_log",
        )
        def delete_log(record_id: int, log_date: date, caller: Caller) -> None:
            def op(work: Work) -> Plan:
                load(work.conn, kind, record_id, lock=True)
                before = work.conn.execute(
                    f"SELECT {kind.log_select} FROM {kind.log_table}"
                    f" WHERE {kind.log_key} = %s AND log_date = %s FOR UPDATE",
                    (record_id, log_date),
                ).fetchone()
                if before is None:
                    raise HTTPException(status_code=404, detail={"error": "not_found"})
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                work.conn.execute(
                    f"DELETE FROM {kind.log_table} WHERE id = %s", (before["id"],)
                )
                if kind is HABIT:
                    refresh_streak(work, record_id)
                return Plan(
                    kind.permission("write"),
                    str(record_id),
                    audit(kind, "unlogged", record_id, before, None),
                    None,
                )

            write(caller, kind, op, precheck=(kind.permission("write"), str(record_id)))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    register(
        HABIT,
        "habits",
        HabitCreate,
        HabitUpdate,
        HabitLogPut,
        ("category", "frequency"),
    )
    register(
        ROUTINE,
        "routines",
        RoutineCreate,
        RoutineUpdate,
        RoutineLogPut,
        ("routine_type",),
    )
    return app


def main() -> None:
    import uvicorn

    from solo_habits.db import default_cluster
    from solo_habits.platform import HttpPlatform

    hosted = os.environ.get("HABITS_DB_MODE") == "hosted"
    pem = os.environ.get("HABITS_JWT_PUBLIC_KEY_PEM", "").strip()
    # A second key for the same issuer during a signing-key rollover; unset = off.
    next_pem = os.environ.get("HABITS_JWT_NEXT_PUBLIC_KEY_PEM", "").strip()
    issuer = os.environ.get("HABITS_JWT_ISSUER", "")
    platform_url = os.environ.get("EMPIRE_PLATFORM_URL", "")
    if not pem or not issuer or not platform_url:
        raise SystemExit(
            "set HABITS_JWT_PUBLIC_KEY_PEM, HABITS_JWT_ISSUER and EMPIRE_PLATFORM_URL"
        )
    if hosted and issuer == "empire-platform-dev":
        raise SystemExit("hosted mode refuses the dev issuer")
    cluster = default_cluster()
    app = create_app(
        lambda: cluster.connect("habits_api"),
        TokenVerifier(
            pem, issuer, extra_public_key_pems=(next_pem,) if next_pem else ()
        ),
        HttpPlatform(platform_url),
        publisher_token=lambda: os.environ.get("HABITS_PUBLISHER_TOKEN") or None,
    )
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0" if hosted else "127.0.0.1"),
        port=int(os.environ.get("PORT", "3611")),
        proxy_headers=False,
    )
