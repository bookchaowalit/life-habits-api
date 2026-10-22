"""The one streak rule: consecutive scheduled days completed, walking back from today."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from solo_habits.app import streak, today_local

THURSDAY = date(2026, 10, 8)


def days_back(today: date, *offsets: int) -> set[date]:
    return {today - timedelta(days=n) for n in offsets}


def test_no_completed_days_is_no_streak():
    assert streak(set(), None, THURSDAY) == 0


def test_today_not_done_yet_does_not_break_the_streak():
    assert streak(days_back(THURSDAY, 1, 2, 3), None, THURSDAY) == 3


def test_today_done_counts_too():
    assert streak(days_back(THURSDAY, 0, 1), None, THURSDAY) == 2


def test_a_missed_day_ends_the_streak():
    assert streak(days_back(THURSDAY, 1, 3, 4), None, THURSDAY) == 1


def test_days_outside_the_schedule_neither_count_nor_break():
    monday, wednesday = date(2026, 10, 5), date(2026, 10, 7)
    # Wed 10-07, Mon 10-05 and Wed 09-30 are back to back on a Mon/Wed schedule; Mon 09-28 is missed.
    completed = {monday, wednesday, date(2026, 9, 30)}
    assert streak(completed, ["mon", "wed"], THURSDAY) == 3
    tuesday = date(2026, 10, 6)
    assert streak(completed | {tuesday}, ["mon", "wed"], THURSDAY) == 3


def test_schedule_names_ignore_case():
    assert streak({date(2026, 10, 7)}, ["WED"], THURSDAY) == 1


def test_a_schedule_with_no_known_weekday_means_every_day():
    assert streak(days_back(THURSDAY, 1, 2), ["someday"], THURSDAY) == 2


def test_the_walk_back_stops_after_a_year():
    every_day = days_back(THURSDAY, *range(500))
    assert streak(every_day, None, THURSDAY) == 366


@pytest.mark.parametrize(
    ("east", "west"), [("Pacific/Kiritimati", "Pacific/Pago_Pago")]
)
def test_today_is_the_owners_day_from_habits_timezone(monkeypatch, east, west):
    # UTC+14 and UTC-11 are 25 hours apart, so their dates always differ.
    monkeypatch.setenv("HABITS_TIMEZONE", east)
    ahead = today_local()
    monkeypatch.setenv("HABITS_TIMEZONE", west)
    behind = today_local()
    assert ahead > behind
