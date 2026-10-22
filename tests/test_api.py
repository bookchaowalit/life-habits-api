"""Habits API: authorization through the platform, logs, outbox, isolation, idempotency."""

import uuid

import pytest
from conftest import OWNER_ACTIONS

from solo_habits.platform import PlatformUnavailable
from solo_habits.publisher import publish_pending


@pytest.fixture
def owner(platform):
    actor, ws = uuid.uuid4(), uuid.uuid4()
    platform.grants[actor] = set(OWNER_ACTIONS)
    return actor, ws


def slug() -> str:
    return f"h-{uuid.uuid4().hex[:10]}"


def create_habit(client, headers, **extra):
    response = client.post(
        "/v1/habits", json={"name": "Run", "slug": slug(), **extra}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def superuser(cluster, query, params=()):
    with cluster.connect("postgres", "habits") as conn:
        return conn.execute(query, params).fetchall()


def test_habit_lifecycle_with_logs_outbox_and_audit(
    client, token, owner, platform, cluster
):
    headers = token(*owner)
    habit = create_habit(
        client, headers, category="health", schedule_days=["mon", "wed"]
    )
    assert habit["status"] == "active"
    assert habit["schedule_days"] == ["mon", "wed"]
    assert habit["version"] == 1
    path = f"/v1/habits/{habit['id']}"
    first = client.put(
        f"{path}/logs/2026-10-10",
        headers=headers,
        json={"count": 2, "duration_minutes": 30},
    ).json()
    again = client.put(
        f"{path}/logs/2026-10-10",
        headers=headers,
        json={"count": 2, "duration_minutes": 30},
    ).json()
    changed = client.put(
        f"{path}/logs/2026-10-10", headers=headers, json={"count": 3}
    ).json()
    assert (first["outcome"], again["outcome"], changed["outcome"]) == (
        "created",
        "unchanged",
        "updated",
    )
    logs = client.get(
        "/v1/habit-logs",
        headers=headers,
        params={"habit_id": habit["id"], "from": "2026-10-01"},
    ).json()
    assert [(entry["log_date"], entry["count"]) for entry in logs["items"]] == [
        ("2026-10-10", 3)
    ]
    logged = client.get(path, headers=headers).json()
    assert logged["last_completed_at"] == "2026-10-10"
    assert logged["version"] > 1
    renamed = client.patch(path, headers=headers, json={"name": "Run daily"}).json()
    assert renamed["version"] == logged["version"] + 1
    assert client.delete(f"{path}/logs/2026-10-10", headers=headers).status_code == 204
    assert (
        client.get(
            "/v1/habit-logs", headers=headers, params={"habit_id": habit["id"]}
        ).json()["items"]
        == []
    )
    assert client.delete(path, headers=headers).status_code == 204
    types = superuser(
        cluster,
        "SELECT type FROM habits.outbox WHERE subject = %s ORDER BY occurred_at",
        (f"empire:habits:habit:{habit['id']}",),
    )
    assert [t[0] for t in types] == [
        "habits.habit.created",
        "habits.habit.updated",
        "habits.habit.deleted",
    ]
    actions = [
        e.get("action")
        for e in platform.executions.values()
        if e.get("resource_id") == str(habit["id"])
    ]
    assert set(actions) >= {
        "habits.habit.created",
        "habits.habit.logged",
        "habits.habit.unlogged",
        "habits.habit.deleted",
    }


def test_routines_and_their_logs(client, token, owner):
    headers = token(*owner)
    routine = client.post(
        "/v1/routines",
        headers=headers,
        json={
            "name": "Morning",
            "slug": slug(),
            "steps": [{"order": 1, "title": "Water"}],
        },
    ).json()
    assert routine["routine_type"] == "daily_morning"
    assert routine["steps"][0]["title"] == "Water"
    log = client.put(
        f"/v1/routines/{routine['id']}/logs/2026-10-10",
        headers=headers,
        json={"completed_steps": [1], "mood_before": 3, "mood_after": 4},
    ).json()
    assert log["outcome"] == "created"
    assert log["log"]["completed_steps"] == [1]
    assert (
        client.put(
            f"/v1/routines/{routine['id']}/logs/2026-10-10",
            headers=headers,
            json={"mood_after": 9},
        ).status_code
        == 422
    )
    assert client.get(
        "/v1/routines/stats", headers=headers, params={"group_by": "routine_type"}
    ).json()["groups"]


def test_permissions_are_per_resource_kind(client, token, owner, platform):
    reader, ws = uuid.uuid4(), owner[1]
    platform.grants[reader] = {"habits.habit:read", "habits.routine:read"}
    habit = create_habit(client, token(*owner))
    assert (
        client.get(f"/v1/habits/{habit['id']}", headers=token(reader, ws)).status_code
        == 200
    )
    assert (
        client.patch(
            f"/v1/habits/{habit['id']}", headers=token(reader, ws), json={"name": "x"}
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/v1/habits/{habit['id']}/logs/2026-10-10",
            headers=token(reader, ws),
            json={},
        ).status_code
        == 403
    )
    writer = uuid.uuid4()
    platform.grants[writer] = {"habits.habit:read", "habits.habit:write"}
    assert (
        client.delete(
            f"/v1/habits/{habit['id']}", headers=token(writer, ws)
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/routines",
            headers=token(writer, ws),
            json={"name": "r", "slug": slug()},
        ).status_code
        == 403
    )


def test_workspaces_are_isolated_and_slugs_unique_per_workspace(
    client, token, owner, platform
):
    habit = create_habit(client, token(*owner))
    stranger, other_ws = uuid.uuid4(), uuid.uuid4()
    platform.grants[stranger] = set(OWNER_ACTIONS)
    assert (
        client.get(
            f"/v1/habits/{habit['id']}", headers=token(stranger, other_ws)
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/habits",
            headers=token(stranger, other_ws),
            json={"name": "Run", "slug": habit["slug"]},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/habits",
            headers=token(*owner),
            json={"name": "Run", "slug": habit["slug"]},
        ).status_code
        == 409
    )
    assert (
        client.get(f"/v1/habits/slug/{habit['slug']}", headers=token(*owner)).json()[
            "id"
        ]
        == habit["id"]
    )


def test_invalid_values_and_version_conflicts(client, token, owner):
    headers = token(*owner)
    habit = create_habit(client, headers)
    path = f"/v1/habits/{habit['id']}"
    assert client.patch(path, headers=headers, json={"name": None}).status_code == 422
    assert (
        client.patch(path, headers=headers, json={"category": "sleep"}).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/habits", headers=headers, json={"name": "x", "slug": "Bad Slug"}
        ).status_code
        == 422
    )
    conflict = client.patch(
        path, headers=headers, json={"name": "y", "expected_version": 9}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["version"] == 1


def test_idempotent_create_and_lost_answer_retry(client, token, owner, platform):
    key = f"create-{uuid.uuid4().hex}"
    headers = {**token(*owner), "Idempotency-Key": key}
    body = {"name": "Read", "slug": slug()}
    real_begin = platform.begin

    def lost(*args, **kwargs):
        real_begin(*args, **kwargs)
        raise PlatformUnavailable("lost")

    platform.begin = lost
    assert client.post("/v1/habits", headers=headers, json=body).status_code == 503
    platform.begin = real_begin
    first = client.post("/v1/habits", headers=headers, json=body)
    again = client.post("/v1/habits", headers=headers, json=body)
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert platform.executions[platform.keys[key]]["status"] == "succeeded"


def test_platform_down_fails_closed_and_bad_tokens_refused(
    client, token, owner, platform
):
    assert (
        client.get(
            "/v1/habits", headers=token(*owner, audience=["tasks-api"])
        ).status_code
        == 401
    )
    platform.down = True
    assert client.get("/v1/habits", headers=token(*owner)).status_code == 503


def test_publisher_sweeps_its_workspace(client, token, owner, platform, cluster):
    habit = create_habit(client, token(*owner))
    publisher = token(uuid.uuid4(), owner[1])["Authorization"].removeprefix("Bearer ")
    with cluster.connect("habits_publisher") as conn:
        done = publish_pending(conn, platform, publisher, limit=1000)
    assert done["failed"] == 0
    assert f"empire:habits:habit:{habit['id']}" in {
        e["subject"] for e in platform.published
    }


def test_runtime_role_sees_nothing_without_a_workspace(cluster, client, token, owner):
    create_habit(client, token(*owner))
    with cluster.connect("habits_api") as conn:
        assert conn.execute("SELECT count(*) FROM habits.habits").fetchone()[0] == 0


def test_streak_is_owned_by_the_service(client, token, owner):
    from datetime import timedelta

    from solo_habits.app import today_local

    headers = token(*owner)
    habit = create_habit(client, headers)  # no schedule: every day counts
    path = f"/v1/habits/{habit['id']}"
    today = today_local()
    for back in (2, 1):
        client.put(
            f"{path}/logs/{(today - timedelta(days=back)).isoformat()}",
            headers=headers,
            json={},
        )
    after_two = client.get(path, headers=headers).json()
    assert (after_two["current_streak"], after_two["best_streak"]) == (
        2,
        2,
    )  # today not done yet does not break it
    client.put(f"{path}/logs/{today.isoformat()}", headers=headers, json={})
    assert client.get(path, headers=headers).json()["current_streak"] == 3
    client.delete(
        f"{path}/logs/{(today - timedelta(days=1)).isoformat()}", headers=headers
    )
    broken = client.get(path, headers=headers).json()
    assert (
        broken["current_streak"],
        broken["best_streak"],
        broken["last_completed_at"],
    ) == (1, 3, today.isoformat())
    skipped = client.put(
        f"{path}/logs/{(today - timedelta(days=5)).isoformat()}",
        headers=headers,
        json={"completed": False},
    ).json()
    assert skipped["log"]["completed"] is False


def test_scheduled_days_only(client, token, owner):
    from datetime import date

    from solo_habits.app import streak

    monday = date(2026, 10, 5)
    days = {date(2026, 10, 5), date(2026, 10, 7)}  # Mon, Wed
    assert streak(days, ["mon", "wed"], date(2026, 10, 8)) == 2  # Thu is not scheduled
    assert streak(days, ["mon", "wed", "thu"], date(2026, 10, 9)) == 0  # Thu missed
    assert streak({monday}, None, monday) == 1


def test_logs_filter_by_notes_prefix_and_order(client, token, owner):
    headers = token(*owner)
    habit = create_habit(client, headers)
    path = f"/v1/habits/{habit['id']}"
    client.put(
        f"{path}/logs/2026-10-01", headers=headers, json={"notes": "Strava: run 5k"}
    )
    client.put(f"{path}/logs/2026-10-02", headers=headers, json={"notes": "manual"})
    found = client.get(
        "/v1/habit-logs",
        headers=headers,
        params={"habit_id": habit["id"], "notes_prefix": "Strava:", "order": "desc"},
    ).json()["items"]
    assert [entry["log_date"] for entry in found] == ["2026-10-01"]


def test_streak_fields_cannot_be_written(client, token, owner):
    headers = token(*owner)
    habit = create_habit(client, headers)
    for field, value in (
        ("best_streak", 0),
        ("current_streak", 9),
        ("last_completed_at", "2026-01-01"),
    ):
        assert (
            client.patch(
                f"/v1/habits/{habit['id']}", headers=headers, json={field: value}
            ).status_code
            == 422
        )
    assert (
        client.post(
            "/v1/habits",
            headers=headers,
            json={"name": "x", "slug": slug(), "best_streak": 5},
        ).status_code
        == 422
    )


def test_streak_read_is_as_of_today(client, token, owner, cluster):
    from datetime import timedelta

    from solo_habits.app import today_local

    headers = token(*owner)
    habit = create_habit(client, headers)
    old = today_local() - timedelta(days=5)
    client.put(
        f"/v1/habits/{habit['id']}/logs/{old.isoformat()}", headers=headers, json={}
    )
    with cluster.connect("postgres", "habits") as conn:  # stored value from a past day
        conn.execute(
            "UPDATE habits.habits SET current_streak = 4, best_streak = 4 WHERE id = %s",
            (habit["id"],),
        )
    seen = client.get(f"/v1/habits/{habit['id']}", headers=headers).json()
    assert (seen["current_streak"], seen["best_streak"]) == (0, 4)
    listed = next(
        h
        for h in client.get("/v1/habits", headers=headers).json()["items"]
        if h["id"] == habit["id"]
    )
    assert listed["current_streak"] == 0


def test_concurrent_first_logs_of_one_day_both_succeed(client, token, owner):
    from concurrent.futures import ThreadPoolExecutor

    headers = token(*owner)
    habit = create_habit(client, headers)
    path = f"/v1/habits/{habit['id']}/logs/2026-09-01"
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(
            pool.map(
                lambda _: client.put(path, headers=headers, json={}).status_code,
                range(2),
            )
        )
    assert codes == [200, 200]


def test_if_absent_never_overwrites_an_existing_completed_log(client, token, owner):
    headers = token(*owner)
    path = f"/v1/habits/{create_habit(client, headers)['id']}/logs"
    grant = {"completed": True, "count": 1, "notes": "auto-granted (weekly batch)"}
    conditional = {"if_absent": "true"}
    # No log yet: the conditional write creates it.
    assert (
        client.put(
            f"{path}/2026-10-09", headers=headers, json=grant, params=conditional
        ).json()["outcome"]
        == "created"
    )
    # A manual completed log written after the cron's read is left alone (count and notes kept).
    client.put(
        f"{path}/2026-10-10",
        headers=headers,
        json={"completed": True, "count": 4, "notes": "manual"},
    )
    kept = client.put(
        f"{path}/2026-10-10", headers=headers, json=grant, params=conditional
    ).json()
    assert kept["outcome"] == "unchanged"
    assert (
        kept["log"]["count"],
        kept["log"]["notes"],
    ) == (4, "manual")
    # An existing log that is not completed is still upgraded by the conditional write.
    client.put(
        f"{path}/2026-10-08", headers=headers, json={"completed": False, "count": 0}
    )
    assert (
        client.put(
            f"{path}/2026-10-08", headers=headers, json=grant, params=conditional
        ).json()["outcome"]
        == "updated"
    )
    # The default PUT still upserts over a completed log.
    overwritten = client.put(f"{path}/2026-10-10", headers=headers, json=grant).json()
    assert overwritten["outcome"] == "updated"
    assert overwritten["log"]["count"] == 1


def test_if_absent_on_routine_logs_keeps_any_existing_log(client, token, owner):
    headers = token(*owner)
    routine = client.post(
        "/v1/routines", json={"name": "Morning", "slug": slug()}, headers=headers
    ).json()
    path = f"/v1/routines/{routine['id']}/logs/2026-10-10"
    client.put(path, headers=headers, json={"notes": "mine"})
    kept = client.put(
        path, headers=headers, json={"notes": "other"}, params={"if_absent": "true"}
    ).json()
    assert kept["outcome"] == "unchanged"
    assert kept["log"]["notes"] == "mine"
