"""Daily log rules: one log per day, what a log may hold, and how logs drive the streak."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from conftest import create_record

from solo_habits import app as app_module

TODAY = date(2026, 10, 8)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(app_module, "today_local", lambda: TODAY)


def put_log(client, headers, habit_id, day, plural="habits", **body):
    return client.put(
        f"/v1/{plural}/{habit_id}/logs/{day.isoformat()}", headers=headers, json=body
    )


def test_a_log_needs_an_existing_habit(client, token, owner):
    assert put_log(client, token(*owner), 999_999_999, TODAY).status_code == 404


@pytest.mark.parametrize(
    "body", [{"completed": None}, {"count": None}, {"count": -1}, {"extra": 1}]
)
def test_habit_logs_refuse_missing_or_invalid_values(client, token, owner, body):
    headers = token(*owner)
    habit = create_record(client, headers)
    assert put_log(client, headers, habit["id"], TODAY, **body).status_code == 422


@pytest.mark.parametrize("mood", [0, 6])
def test_routine_moods_are_one_to_five(client, token, owner, mood):
    headers = token(*owner)
    routine = create_record(client, headers, "routines")
    response = put_log(
        client, headers, routine["id"], TODAY, "routines", mood_before=mood
    )
    assert response.status_code == 422


def test_an_uncompleted_log_does_not_extend_the_streak(client, token, owner):
    headers = token(*owner)
    habit = create_record(client, headers)
    put_log(client, headers, habit["id"], TODAY - timedelta(days=1))
    put_log(client, headers, habit["id"], TODAY, completed=False)
    current = client.get(f"/v1/habits/{habit['id']}", headers=headers).json()
    assert current["current_streak"] == 1
    assert current["last_completed_at"] == (TODAY - timedelta(days=1)).isoformat()


def test_removing_the_only_log_resets_the_streak_but_keeps_the_best(
    client, token, owner
):
    headers = token(*owner)
    habit = create_record(client, headers)
    put_log(client, headers, habit["id"], TODAY)
    removed = client.delete(
        f"/v1/habits/{habit['id']}/logs/{TODAY.isoformat()}", headers=headers
    )
    assert removed.status_code == 204
    current = client.get(f"/v1/habits/{habit['id']}", headers=headers).json()
    assert current["current_streak"] == 0
    assert current["best_streak"] == 1
    assert current["last_completed_at"] is None


def test_removing_a_log_that_does_not_exist_is_not_found(client, token, owner):
    headers = token(*owner)
    habit = create_record(client, headers)
    response = client.delete(
        f"/v1/habits/{habit['id']}/logs/{TODAY.isoformat()}", headers=headers
    )
    assert response.status_code == 404


def test_logging_a_habit_needs_habit_write_not_routine_write(
    client, token, owner, platform
):
    habit = create_record(client, token(*owner))
    routine_writer = uuid.uuid4()
    platform.grants[routine_writer] = {"habits.habit:read", "habits.routine:write"}
    response = put_log(client, token(routine_writer, owner[1]), habit["id"], TODAY)
    assert response.status_code == 403


def test_logs_list_by_date_range_and_completion(client, token, owner):
    headers = token(*owner)
    habit = create_record(client, headers)
    for back, done in ((3, True), (2, False), (1, True), (0, True)):
        put_log(
            client, headers, habit["id"], TODAY - timedelta(days=back), completed=done
        )
    items = client.get(
        "/v1/habit-logs",
        headers=headers,
        params={
            "habit_id": habit["id"],
            "from": (TODAY - timedelta(days=2)).isoformat(),
            "to": (TODAY - timedelta(days=1)).isoformat(),
            "completed": True,
        },
    ).json()["items"]
    assert [i["log_date"] for i in items] == [(TODAY - timedelta(days=1)).isoformat()]
