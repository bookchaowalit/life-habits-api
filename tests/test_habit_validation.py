"""What a habit or routine accepts: defaults, slugs, ranges, unknown and derived fields."""

from __future__ import annotations

import uuid

import pytest
from conftest import create_record, new_slug


def test_a_new_habit_gets_the_documented_defaults(client, token, owner):
    habit = create_record(client, token(*owner))
    assert {
        key: habit[key]
        for key in (
            "category",
            "frequency",
            "target_count",
            "priority",
            "status",
            "current_streak",
            "best_streak",
        )
    } == {
        "category": "productivity",
        "frequency": "daily",
        "target_count": 1,
        "priority": 3,
        "status": "active",
        "current_streak": 0,
        "best_streak": 0,
    }


def test_a_new_routine_starts_active_with_no_steps(client, token, owner):
    routine = create_record(client, token(*owner), "routines")
    assert routine["routine_type"] == "daily_morning"
    assert routine["status"] == "active"
    assert routine["steps"] == []


@pytest.mark.parametrize("slug", ["Run", "-run", "run daily", "r" * 101, ""])
def test_slugs_are_lower_case_letters_digits_and_hyphens(client, token, owner, slug):
    response = client.post(
        "/v1/habits", headers=token(*owner), json={"name": "Run", "slug": slug}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"target_count": 0},
        {"priority": 11},
        {"target_duration_minutes": -1},
        {"frequency": "hourly"},
        {"colour": "red"},
    ],
)
def test_out_of_range_or_unknown_habit_fields_are_refused(client, token, owner, extra):
    response = client.post(
        "/v1/habits",
        headers=token(*owner),
        json={"name": "Run", "slug": new_slug(), **extra},
    )
    assert response.status_code == 422


def test_required_fields_cannot_be_cleared(client, token, owner):
    headers = token(*owner)
    routine = create_record(client, headers, "routines")
    response = client.patch(
        f"/v1/routines/{routine['id']}", headers=headers, json={"steps": None}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == ["steps"]


def test_slugs_are_unique_within_a_workspace(client, token, owner):
    headers = token(*owner)
    habit = create_record(client, headers)
    response = client.post(
        "/v1/habits", headers=headers, json={"name": "Again", "slug": habit["slug"]}
    )
    assert response.status_code == 409
    other_workspace = token(owner[0], uuid.uuid4())
    assert (
        client.post(
            "/v1/habits",
            headers=other_workspace,
            json={"name": "Again", "slug": habit["slug"]},
        ).status_code
        == 201
    )


@pytest.mark.parametrize(
    ("plural", "group_by", "allowed"),
    [
        ("habits", "category", True),
        ("habits", "frequency", True),
        ("habits", "routine_type", False),
        ("routines", "routine_type", True),
        ("routines", "category", False),
        ("habits", "name", False),
    ],
)
def test_stats_group_only_by_status_or_the_kinds_filters(
    client, token, owner, plural, group_by, allowed
):
    response = client.get(
        f"/v1/{plural}/stats", headers=token(*owner), params={"group_by": group_by}
    )
    assert (response.status_code == 200) is allowed
    if not allowed:
        assert response.json()["detail"]["fields"] == ["group_by"]


def test_an_unchanged_patch_keeps_the_version(client, token, owner):
    headers = token(*owner)
    habit = create_record(client, headers, schedule_days=["mon"])
    same = client.patch(
        f"/v1/habits/{habit['id']}",
        headers=headers,
        json={"name": "Run", "schedule_days": ["mon"]},
    ).json()
    assert same["version"] == 1
