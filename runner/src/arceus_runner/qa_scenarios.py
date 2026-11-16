"""Test scenarios: any number of named test runs of the platform (qa.py), each with its own instructions and its own
schedule. "Smoke" on weekdays at 07:00, "Regression" on Saturday at 02:00, "Payments" only by hand.

- **Where they live.** The name and the schedule in runner.db (`qa_scenarios`), so a scenario made or changed in the
  web console or with `arceus-runner qa scenario` counts on the runner's next pass, without a restart. The
  instructions in a file per scenario (`file_of`): scenario 1 keeps `qa.instructions`, which test runs read before
  there were scenarios; every later one has `scenarios/<n>.md` beside it. A scenario's number is never given to
  another one, also after it is deleted, so a past run, its report and the file's history keep meaning the same one.
- **How many.** At most `qa.max_scenarios` at once, deleted ones not counted.
- **When they run.** Each scenario's days and time of `schedule.timezone`. Two scenarios set for the same minute are
  both queued and run one after the other: they share the workspace. A scenario's time that comes while a run of
  that same scenario is queued or under way is skipped; a run of another scenario does not stop it being queued.
- **Deleted.** A deleted scenario queues nothing more. Its row stays (`deleted_at`), so its runs keep its name; a run
  of it still queued when it was deleted closes without starting, and one under way finishes by what it was given.
- **Upgrading.** The migration makes scenario 1, "Main", reading `qa.instructions`. The first time the runner starts
  with scenarios it gives `qa.schedule` to that scenario (`adopt_config`), once; after that `qa.schedule` is not read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from arceus_runner.db import QaScenario, ReviewStore

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_WORDS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
MAX_NAME = 60
ADOPTED = "qa.schedule_adopted"  # runner_state: qa.schedule was given to scenario 1 (or there was none to give)
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class Invalid(ValueError):
    """What a person entered for a scenario cannot be kept; the message says why, in their words."""


@dataclass(frozen=True)
class Plan:
    """A scenario's schedule as the runner reads it."""
    days: tuple[str, ...]
    at: time | None
    on: bool

    @property
    def active(self) -> bool:
        return self.on and bool(self.days) and self.at is not None


def plan_of(s: QaScenario) -> Plan:
    try:
        days = tuple(d for d in json.loads(s.days or "[]") if d in DAYS)
    except (ValueError, TypeError):
        days = ()
    at = None
    if s.at and (m := _TIME.match(s.at)):
        at = time(int(m.group(1)), int(m.group(2)))
    return Plan(days=tuple(d for d in DAYS if d in days), at=at, on=bool(s.scheduled))


def file_of(qa, s: QaScenario) -> Path:
    """The scenario's instructions file: `qa.instructions` for scenario 1, `scenarios/<n>.md` beside it for the rest."""
    return qa.instructions if not s.own_file else qa.instructions.parent / "scenarios" / f"{s.n}.md"


def history_kind(s: QaScenario) -> str:
    """Under which name the console keeps the versions of its instructions file."""
    return "qa.instructions" if not s.own_file else f"qa.instructions.{s.n}"


def describe(plan: Plan) -> str:
    """The schedule in words: `Mon, Wed, Fri at 09:00`, `Every day at 02:00`, or `By hand only`."""
    if not plan.active:
        return "By hand only"
    assert plan.at is not None
    if len(plan.days) == 7:
        days = "Every day"
    elif plan.days == DAYS[:5]:
        days = "Weekdays"
    elif plan.days == DAYS[5:]:
        days = "Weekends"
    else:
        days = ", ".join(DAY_WORDS[d] for d in plan.days)
    return f"{days} at {plan.at:%H:%M}"


def next_time(plan: Plan, now: datetime, tz: ZoneInfo) -> datetime | None:
    """When the scenario is next due by its schedule, after `now`; None when it runs by hand only."""
    if not plan.active:
        return None
    assert plan.at is not None
    local = now.astimezone(tz)
    for ahead in range(8):
        day = (local + timedelta(days=ahead)).date()
        if DAYS[day.weekday()] not in plan.days:
            continue
        due = datetime.combine(day, plan.at, tzinfo=tz)
        if due > local:
            return due
    return None


def clean(name: str, days: list[str], at: str) -> tuple[str, str, str | None, bool]:
    """What a person entered, checked and as runner.db keeps it: the name, the days as JSON, the time, whether it
    runs by itself. Days and a time make a schedule; neither, a scenario run only by hand; one without the other is
    refused, so that what the form shows is what happens."""
    name = " ".join((name or "").split())
    if not name:
        raise Invalid("Give the scenario a name.")
    if len(name) > MAX_NAME:
        raise Invalid(f"The name is longer than {MAX_NAME} characters.")
    unknown = [d for d in days if d not in DAYS]
    if unknown:
        raise Invalid(f"Unknown day: {unknown[0]}.")
    picked = [d for d in DAYS if d in days]
    at = (at or "").strip()
    if at and not _TIME.match(at):
        raise Invalid("The time is HH:MM, from 00:00 to 23:59.")
    if at:
        h, m = at.split(":")
        at = f"{int(h):02d}:{m}"
    if at and not picked:
        raise Invalid("Pick the days to run on, or clear the time for a scenario run only by hand.")
    if picked and not at:
        raise Invalid("Set the time to run at, or clear the days for a scenario run only by hand.")
    return name, json.dumps(picked), at or None, bool(picked and at)


def name_taken(store: ReviewStore, name: str, but: int | None = None) -> bool:
    return any(s.name.casefold() == name.casefold() and s.n != but for s in store.scenarios())


def create(store: ReviewStore, qa, name: str, days: list[str], at: str, by: str) -> QaScenario:
    """A new scenario, inside the caller's transaction. Refused past `qa.max_scenarios` or with a name in use."""
    name, days_json, at_value, on = clean(name, days, at)
    live = store.scenarios()
    if len(live) >= qa.max_scenarios:
        raise Invalid(f"There are {len(live)} scenarios already, the most this server keeps (qa.max_scenarios). "
                      "Delete one you no longer run first.")
    if name_taken(store, name):
        raise Invalid(f"A scenario named “{name}” exists already.")
    return store.add_scenario(name, days_json, at_value, on, by)


def adopt_config(store: ReviewStore, qa) -> None:
    """Give `qa.schedule` to scenario 1 the first time the runner starts with scenarios; once, also after a crash.
    A scenario 1 someone changed already (in the console, before the runner got here) keeps what they set."""
    with store.transaction():
        if store.state(ADOPTED) is not None:
            return
        given = None
        first = store.scenario(1)
        s = qa.schedule
        if s is not None and first is not None and not first.own_file and first.deleted_at is None \
                and first.version == 1:
            store.update_scenario(1, 1, "config.yaml", days=json.dumps([d for d in DAYS if d in s.days]),
                                  at=f"{s.at:%H:%M}", scheduled=True)
            given = {"days": list(s.days), "at": f"{s.at:%H:%M}"}
        store.set_state(ADOPTED, {"schedule": given})
