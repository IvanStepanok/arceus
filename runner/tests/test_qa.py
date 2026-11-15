"""Test runs of the platform: queued by command or schedule, run in Bulava, reported as a page of their own."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from arceus_runner import qa_report
from arceus_runner.config import QaConfig, Secrets
from arceus_runner.metrics import Metrics
from arceus_runner.cli import qa_check
from arceus_runner.qa import Limits, load_findings, outcome_of

from .conftest import GL_TOKEN, YT_TOKEN
from .test_delivery import World, git, take

EXAMPLE = Path(__file__).parents[2] / "config" / "qa-instructions.example.md"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
RID = "qa-1"


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    monkeypatch.setattr("arceus_runner.health.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("arceus_runner.bulava._writable_by_us", lambda root: [])


def world(tmp_path, instructions: str | None = "Platform: https://staging.example.com\nCheck sign-in.\n",
          console: dict | None = None, **qa) -> World:
    path = tmp_path / "qa" / "instructions.md"
    if instructions is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(instructions)
    settings = {"qa": {"enabled": True, "instructions": str(path), "reports": str(tmp_path / "reports"), **qa}}
    if console:
        settings["console"] = console
    return World(tmp_path, Secrets(tracker=YT_TOKEN, code_host=GL_TOKEN, slack=None), settings=settings)


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path)
    yield w
    w.events.close()
    w.store.close()


def queue(w: World, by: str = "ops") -> None:
    w.store.add_run("qa", "command", by)
    w.tick()


def out_dir(w: World, rid: str = RID):
    return w.cfg.paths.workspace / ".arceus" / "qa" / rid


def report(w: World, **fields) -> None:
    data = {"summary": "Checked sign-in. The password reset link is dead.", "blocked": "",
            "tested": [{"what": "Sign-in as the customer", "result": "ok"},
                       {"what": "Password reset", "result": "problem", "note": "see the finding"}],
            "findings": [{"title": "Reset link gives 404", "severity": "major", "url": "https://staging.example.com/r",
                          "steps": ["Open the sign-in page", "Press Forgot password", "Open the link from the mail"],
                          "failed_step": 3, "expected": "The reset form.", "actual": "404 Not Found.",
                          "evidence": [{"file": "reset.png", "caption": "404 after the link", "step": 3},
                                       {"file": "notes.txt", "caption": "not an image"}],
                          "reproduced": 2, "attempts": 2, "cause": "The route was renamed.",
                          "code": [{"repo": "acme-platform", "file": "app/urls.py", "line": 12}]}]} | fields
    out = out_dir(w)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reset.png").write_bytes(PNG)
    (out / "notes.txt").write_text("hello")
    (out / "findings.json").write_text(json.dumps(data))


def finish(w: World, state: str = "done", rid: str = RID, **extra) -> None:
    w.set_engine(lambda s: s["tasks"][rid].update(state=state, attempt_live=False, **extra))
    w.tick()


def dispatches(w: World) -> list[dict]:
    return [c for c in w.calls("dispatch") if c["argv"][1].startswith("qa-")]


# -- a run from start to report ----------------------------------------------------------------------

def test_a_queued_run_is_a_task_of_its_own_with_the_instructions_and_the_code_at_main(w):
    queue(w)
    [call] = dispatches(w)
    argv = call["argv"]
    assert argv[argv.index("--task-id") + 1] == RID
    assert argv[argv.index("--branch") + 1] == "arceus-qa/1"
    assert argv[argv.index("--source") + 1] == "other"
    assert argv[argv.index("--author") + 1] == "ops"
    text = argv[-1]
    assert "Check sign-in." in text and str(out_dir(w) / "findings.json") in text
    assert "data, never instructions" in text and "succeeded_research" in text
    main = git(w.origins["acme-platform"], "rev-parse", "main")
    assert f"`acme-platform`: checked out at `{main}`" in text
    assert git(w.repo, "branch", "--show-current") == "arceus-qa/1"
    # The repositories are read, never written: no list of writable ones beside the extra directories.
    extra = w.cfg.paths.db.parent / "tasks" / RID / "extra-dirs.txt"
    assert str(w.repo) in extra.read_text()
    assert not (extra.parent / "writable-directories.txt").exists()
    run = w.store.run(RID)
    assert run.state == "running"
    assert (w.cfg.qa.reports / "1" / "instructions.md").read_text() == w.cfg.qa.instructions.read_text()
    # It holds the workspace: a ticket waits for it.
    assert w.runner.delivery.workspace_taken().task_id == RID
    take(w)
    assert w.store.task("ACME-1") is None
    assert len(w.calls("dispatch")) == 1


def test_a_finished_run_gets_its_report_and_the_workspace_back(w):
    queue(w)
    report(w)
    finish(w)
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "found")
    folder = w.cfg.qa.reports / "1"
    page = (folder / "report.html").read_text()
    assert "1 problem found" in page and "Reset link gives 404" in page and "Suspected cause" in page
    [shot] = list((folder / "media").iterdir())
    assert shot.read_bytes() == PNG and shot.name.endswith(".png") and shot.name != "reset.png"
    assert f'media/{shot.name}' in page
    result = json.loads((folder / "result.json").read_text())
    assert any("notes.txt" in p for p in result["problems"])  # not an image: left out, and said so
    [code] = result["findings"][0]["code"]
    main = git(w.origins["acme-platform"], "rev-parse", "main")
    assert code["sha"] == main and code["url"].endswith(f"/-/blob/{main}/app/urls.py#L12")
    assert "Run 1" in (w.cfg.qa.reports / "index.html").read_text()
    # The workspace is as it was: back on main, the run's branch gone, the agent's folder removed.
    assert git(w.repo, "branch", "--show-current") == "main"
    assert git(w.repo, "branch", "--list", "arceus-qa/*") == ""
    assert not out_dir(w).exists()
    assert w.runner.delivery.workspace_taken() is None
    [published] = w.events_named("qa_published")
    assert (published["outcome"], published["findings"]) == ("found", 1)
    assert published["url"] == "http://localhost:3300/1/" and published["tunnel"] is True


@pytest.mark.parametrize("console, url, tunnel", [
    ({"enabled": True, "public_url": "https://console.example.com"}, "https://console.example.com/qa/1/", False),
    # An address kept for later does not count while the console is off: it serves nothing then.
    ({"enabled": False, "public_url": "https://console.example.com"}, "http://localhost:3300/1/", True)])
def test_the_published_report_is_where_it_can_be_opened(tmp_path, console, url, tunnel):
    w = world(tmp_path, console=console)
    try:
        queue(w)
        report(w)
        finish(w)
        [published] = w.events_named("qa_published")
        assert (published["url"], published["tunnel"]) == (url, tunnel)
    finally:
        w.events.close()
        w.store.close()


def test_a_run_that_could_not_test_says_so_and_never_passes(w):
    queue(w)
    report(w, findings=[], tested=[], blocked="The site answers 502 on every page.")
    finish(w)
    run = w.store.run(RID)
    assert run.outcome == "blocked"
    assert "Could not test" in (w.cfg.qa.reports / "1" / "report.html").read_text()


def test_a_run_without_a_findings_file_is_unfinished(w):
    queue(w)
    finish(w)
    run = w.store.run(RID)
    assert run.outcome == "unfinished"
    result = json.loads(run.result)
    assert "the agent wrote no findings file" in result["problems"]
    assert "the agent did not say what it tested" in result["doubts"]


def test_a_run_that_failed_in_the_engine_is_unfinished_whatever_it_wrote(w):
    queue(w)
    report(w)
    finish(w, state="failed", state_reason="stalled")
    run = w.store.run(RID)
    assert run.outcome == "unfinished" and "stalled" in run.reason
    page = (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert "did not finish as it should" in page and "Reset link gives 404" in page


# The engine's record of a production run whose report the closing review still disputed after its last round
# (staging, run 2): the testing finished, nothing was committed, and the objections were about how the report put it.
OBJECTION = ("STATE: COMPLETE\nVERDICT: FAIL\n\n1. **The change ledger** does not list the records the run created.\n"
             "2. Finding 1's screenshot does not show the failed step.")


def disputed(w: World, review: str = "failed", why: str = OBJECTION, commits=()) -> None:
    finish(w, state="failed", state_reason="needs-user",
           receipt={"result": "succeeded_research", "review": review, "commits": list(commits), "review_why": why})


WHOLE = [{"title": "Reset link gives 404", "severity": "major", "steps": ["Open the link from the mail"],
          "expected": "The reset form.", "actual": "404 Not Found.", "evidence": [{"file": "reset.png", "step": 1}],
          "reproduced": 2, "attempts": 2}]


def test_a_report_the_closing_review_disputed_keeps_its_findings_and_shows_the_objections(w):
    queue(w)
    report(w, findings=WHOLE)
    disputed(w)
    run = w.store.run(RID)
    assert (run.state, run.outcome, run.reason) == ("done", "found", None)
    result = json.loads((w.cfg.qa.reports / "1" / "result.json").read_text())
    assert result["fatal"] == [] and result["engine_disputed"] is True
    assert result["engine_review"].startswith("1. The change ledger")
    assert "VERDICT" not in result["engine_review"] and "**" not in result["engine_review"]
    page = (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert "1 problem found" in page and "Reset link gives 404" in page
    assert "did not pass its closing review" in page and "records the run created" in page
    assert "did not finish as it should" not in page and "Codex" not in page
    # The console builds its page from the same result.json.
    title, body = qa_report.run_body(result, "acme", ZoneInfo("UTC"))
    assert "did not pass its closing review" in body and title == "Run 1: 1 problem found"
    [published] = w.events_named("qa_published")
    assert published["outcome"] == "found"


def test_a_disputed_report_that_found_nothing_is_never_a_pass_and_every_view_says_why(w):
    queue(w)
    report(w, findings=[], tested=[{"what": "Sign-in as the customer", "result": "ok"}])
    disputed(w)
    run = w.store.run(RID)
    assert run.outcome == "partial" and "closing review" in run.reason
    head = qa_report.DISPUTED_PARTIAL
    assert head in (w.cfg.qa.reports / "1" / "report.html").read_text()
    from arceus_runner.qa import index_rows
    from arceus_runner.console import runs
    [row] = runs.dress(index_rows(w.store, w.cfg.qa.reports), qa_report.head_of)
    assert row["result_text"] == head
    assert head in qa_report.render_index(index_rows(w.store, w.cfg.qa.reports), "acme", ZoneInfo("UTC"))


def test_a_disputed_report_without_its_objections_is_still_disputed(w):
    queue(w)
    report(w, findings=[], tested=[{"what": "Sign-in as the customer", "result": "ok"}])
    disputed(w, why="STATE: COMPLETE\nVERDICT: FAIL\n")
    run = w.store.run(RID)
    assert run.outcome == "partial"
    result = json.loads(run.result)
    assert result["engine_disputed"] is True and result["engine_review"] == ""
    assert "did not say what it objected to" in (w.cfg.qa.reports / "1" / "report.html").read_text()


@pytest.mark.parametrize("tested", [
    [],                                                    # names no check at all
    [{"what": "Sign-in as the customer", "result": "skipped"}],   # none done
    [{"what": "Sign-in as the customer", "result": "blocked"}],
])
def test_a_disputed_report_that_shows_no_testing_stays_unfinished_with_its_findings_kept(w, tested):
    queue(w)
    report(w, findings=WHOLE, tested=tested)
    disputed(w)
    run = w.store.run(RID)
    assert run.outcome == "unfinished" and "names no check that was done" in run.reason
    assert len(json.loads(run.result)["findings"]) == 1
    page = (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert "Did not finish" in page and "Reset link gives 404" in page and "did not pass its closing review" in page


def test_a_disputed_report_the_runner_could_not_read_whole_stays_unfinished(w):
    queue(w)
    report(w)   # its second piece of evidence is not an image: left out
    disputed(w)
    run = w.store.run(RID)
    assert run.outcome == "unfinished" and "could not be read" in run.reason
    assert len(json.loads(run.result)["findings"]) == 1


def test_a_disputed_report_that_tested_nothing_stays_unfinished(w):
    queue(w)
    report(w, findings=[], tested=[])
    disputed(w)
    assert w.store.run(RID).outcome == "unfinished"


@pytest.mark.parametrize("review, why, commits", [
    ("passed", "", ()),                    # passed, but left something for a person
    ("failed", "the review could not run", ()),  # no verdict to show
    ("failed", OBJECTION, ("abc123",)),    # a tester commits nothing
])
def test_needs_user_for_any_other_reason_is_still_an_unfinished_run(w, review, why, commits):
    queue(w)
    report(w)
    disputed(w, review=review, why=why, commits=commits)
    run = w.store.run(RID)
    assert run.outcome == "unfinished" and "needs-user" in run.reason
    assert "engine_review" not in json.loads(run.result)


# Bulava 1.20: a test run is sent with its own review of the closing report, and a report still disputed at the
# limit is accepted with the reviewer's notes: the task ends `done`, the review `passed`, `accepted_with_notes`.

def accepted_with_notes(w: World, why: str = OBJECTION, **receipt) -> None:
    finish(w, receipt={"result": "succeeded_research", "review": "passed", "accepted_with_notes": True,
                       "commits": [], "review_why": why, **receipt})


def test_a_run_is_sent_with_its_own_review_of_the_report_kept_outside_the_workspace(w):
    from arceus_runner.qa import REPORT_CRITERIA
    queue(w)
    [call] = dispatches(w)
    argv = call["argv"]
    assert argv[argv.index("--report-max-rounds") + 1] == "3"
    assert argv[argv.index("--report-at-limit") + 1] == "accept"
    criteria = Path(argv[argv.index("--report-criteria") + 1])
    assert criteria.read_text() == REPORT_CRITERIA and "undoing each record is not asked for" in REPORT_CRITERIA
    assert criteria.parent == w.cfg.paths.db.parent / "tasks" / RID
    assert w.cfg.paths.workspace.resolve() not in criteria.resolve().parents
    # The flags come before the folder and the text, which stay the last two arguments.
    assert argv[-2] == str(w.cfg.paths.workspace) and "## The closing review" in argv[-1]
    assert REPORT_CRITERIA in argv[-1] and "up to 3 rounds" in argv[-1]


def test_the_engine_review_reads_the_findings_through_a_link_in_artifacts_and_it_goes_with_the_run(w):
    queue(w)
    ws = w.cfg.paths.workspace.resolve()
    link = ws / "artifacts" / RID / "findings.txt"
    assert link.is_symlink() and not Path(link.readlink()).is_absolute()
    report(w, findings=WHOLE)
    assert link.resolve() == (out_dir(w) / "findings.json").resolve()
    assert json.loads(link.read_text())["findings"][0]["title"] == "Reset link gives 404"
    text = dispatches(w)[0]["argv"][-1]
    assert f"starting with `artifacts/{RID}/findings.txt`" in text and "compact JSON" in text
    finish(w)
    assert not (ws / "artifacts" / RID).exists() and w.store.run(RID).state == "done"


def test_no_link_is_made_through_an_artifacts_folder_that_is_itself_a_link(w, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    w.cfg.paths.workspace.mkdir(parents=True, exist_ok=True)
    (w.cfg.paths.workspace / "artifacts").symlink_to(elsewhere)
    queue(w)
    assert list(elsewhere.iterdir()) == [] and w.events_named("qa_review_link_skipped")
    assert w.store.run(RID).state == "running"
    report(w, findings=WHOLE)
    finish(w)
    assert elsewhere.is_dir() and w.store.run(RID).outcome == "found"


def test_a_resumed_run_is_not_sent_the_review_again_the_engine_keeps_it(w):
    queue(w)
    w.set_engine(lambda s: s["tasks"][RID].update(attempt_live=False))
    w.tick()
    [send] = [c for c in w.calls("worker-send") if RID in c["argv"]]
    assert not any(a.startswith("--report") for a in send["argv"])


def test_a_report_accepted_with_notes_keeps_its_findings_and_shows_the_objections(w):
    queue(w)
    report(w, findings=WHOLE)
    accepted_with_notes(w)
    run = w.store.run(RID)
    assert (run.state, run.outcome, run.reason) == ("done", "found", None)
    result = json.loads(run.result)
    assert result["fatal"] == [] and result["engine_disputed"] is True
    assert result["engine_review"].startswith("1. The change ledger")
    page = (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert "1 problem found" in page and "did not pass its closing review" in page and "records the run created" in page


def test_a_report_accepted_with_notes_that_found_nothing_is_never_a_pass(w):
    queue(w)
    report(w, findings=[], tested=[{"what": "Sign-in as the customer", "result": "ok"}])
    accepted_with_notes(w, why="1. The summary counts 14 accounts, the checks name 13.")
    run = w.store.run(RID)
    assert run.outcome == "partial" and "closing review" in run.reason
    assert json.loads(run.result)["engine_review"] == "1. The summary counts 14 accounts, the checks name 13."
    assert qa_report.DISPUTED_PARTIAL in (w.cfg.qa.reports / "1" / "report.html").read_text()


def test_a_report_accepted_with_notes_that_shows_no_testing_stays_unfinished(w):
    queue(w)
    report(w, findings=WHOLE, tested=[])
    accepted_with_notes(w)
    run = w.store.run(RID)
    assert run.outcome == "unfinished" and "names no check that was done" in run.reason


@pytest.mark.parametrize("receipt", [
    {"accepted_with_notes": False},                       # an ordinary pass
    {"accepted_with_notes": True, "commits": ["abc123"]},  # a tester commits nothing
    {"accepted_with_notes": True, "result": "succeeded_changes"},
])
def test_only_a_report_let_through_at_its_limit_counts_as_accepted_with_notes(w, receipt):
    queue(w)
    report(w, findings=[], tested=[{"what": "Sign-in as the customer", "result": "ok"}])
    accepted_with_notes(w, **receipt)
    result = json.loads(w.store.run(RID).result)
    assert "engine_disputed" not in result and "engine_review" not in result


def test_an_unreadable_review_at_the_limit_is_still_an_unfinished_run(w):
    """With `accept`, the engine lets through only a readable FAIL; anything else ends needs-user, unreadable."""
    queue(w)
    report(w, findings=WHOLE)
    disputed(w, why="the reviewer's answer had no verdict")
    run = w.store.run(RID)
    assert run.outcome == "unfinished" and "needs-user" in run.reason


def test_live_test_runs_need_an_engine_that_takes_their_review_of_the_report(w, tmp_path):
    from arceus_runner import bulava, loop
    root = tmp_path / "old-engine"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "dispatch.sh").write_text("#!/bin/sh\n# --task-id --branch --base --dirty SUPERVISOR_PROFILE\n")
    old = bulava.probe(root)
    assert old.missing_for_test_runs() == ["dispatch.sh --report-max-rounds --report-criteria",
                                           "dispatch.sh --report-at-limit accept"]
    with pytest.raises(loop.LiveModeUnavailable, match="--report-criteria"):
        loop.require_mode_ready(w.cfg, old)
    (root / "bin" / "dispatch.sh").write_text("#!/bin/sh\n--report-criteria) ;; --report-at-limit) ;;\n")
    assert bulava.probe(root).missing_for_test_runs() == []


# A report longer than the engine's review reads of one file comes in parts, each reviewed in an attempt of its own.

CHECK = {"what": "Sign-in as the customer", "result": "ok"}


def report_in_parts(w: World, second: dict | None = None, **first) -> None:
    report(w, **{"findings": WHOLE, "tested": [CHECK], "parts": 2, "checks": 2, "findings_count": 2, **first})
    part = second if second is not None else {
        "tested": [{"what": "Password reset", "result": "problem"}],
        "findings": [{**WHOLE[0], "title": "Reset mail never arrives"}]}
    (out_dir(w) / "findings-2.json").write_text(json.dumps(part))


def passed(w: World) -> None:
    """An attempt whose part the review passed: the engine writes the receipt afresh for every attempt."""
    finish(w, receipt={"result": "succeeded_research", "review": "passed", "commits": [], "review_why": ""})


def sends(w: World) -> list[dict]:
    return [c for c in w.calls("worker-send") if RID in c["argv"]]


def test_a_report_in_parts_is_reviewed_part_by_part_and_published_whole(w):
    queue(w)
    report_in_parts(w)
    accepted_with_notes(w, why="STATE: COMPLETE\nVERDICT: FAIL\n1. Finding 1 names no URL.")
    # The first part is done: the runner links the second and asks the agent to finish again naming it.
    run = w.store.run(RID)
    assert run.state == "running"
    [send] = sends(w)
    argv = send["argv"]
    assert argv[argv.index("--message-id") + 1] == f"review-{RID}-2"
    assert f"`artifacts/{RID}/findings-2.txt`" in argv[-1] and "findings.json" in argv[-1]
    link = w.cfg.paths.workspace.resolve() / "artifacts" / RID / "findings-2.txt"
    assert link.resolve() == (out_dir(w) / "findings-2.json").resolve()
    assert w.events_named("qa_review_part")[0]["part"] == "findings-2.json"
    passed(w)   # the second part passes
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "found")
    result = json.loads(run.result)
    assert [f["title"] for f in result["findings"]] == ["Reset link gives 404", "Reset mail never arrives"]
    assert len(result["tested"]) == 2 and result["problems"] == []
    # The objections on the first part stay, named by the part they are about.
    assert result["engine_disputed"] is True and result["engine_review"] == "findings.json:\n1. Finding 1 names no URL."
    assert len(sends(w)) == 1


def test_objections_on_the_last_part_are_kept_with_its_name(w):
    queue(w)
    report_in_parts(w)
    passed(w)
    accepted_with_notes(w, why="STATE: COMPLETE\nVERDICT: FAIL\n1. The second finding has no screenshot.")
    result = json.loads(w.store.run(RID).result)
    assert result["engine_review"] == "findings-2.json:\n1. The second finding has no screenshot."


def test_a_report_in_parts_every_part_passed_is_clean(w):
    queue(w)
    report_in_parts(w)
    passed(w)
    passed(w)
    result = json.loads(w.store.run(RID).result)
    assert "engine_disputed" not in result and w.store.run(RID).outcome == "found"


def test_a_busy_engine_gets_the_part_again_and_a_restart_never_sends_two(w):
    queue(w)
    report_in_parts(w)
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 6}))
    passed(w)
    assert w.store.run(RID).state == "running" and len(sends(w)) == 1
    snap = json.loads(w.store.run(RID).snapshot)
    assert snap["review"]["sent"] is False
    w.tick()                                   # sent now
    assert json.loads(w.store.run(RID).snapshot)["review"]["sent"] is True
    # A runner that died after the send and before marking it: the same message again, taken once.
    run = w.store.run(RID)
    snap = json.loads(run.snapshot)
    w.store.update_run(RID, snapshot=json.dumps({**snap, "review": {**snap["review"], "sent": False}}))
    w.tick()
    attempts = w.engine_state()["tasks"][RID]["attempts"]
    assert [a["kind"] for a in attempts] == ["initial", "continue"]
    passed(w)
    assert w.store.run(RID).outcome == "found"


def test_a_part_changed_after_its_review_counts_as_not_reviewed(w):
    queue(w)
    report_in_parts(w)
    passed(w)
    data = json.loads((out_dir(w) / "findings.json").read_text())
    (out_dir(w) / "findings.json").write_text(json.dumps({**data, "summary": "Rewritten after its review."}))
    passed(w)
    result = json.loads(w.store.run(RID).result)
    assert result["engine_disputed"] is True and "findings.json changed after its review" in result["engine_review"]


def test_a_part_the_review_never_reached_is_said_so(w):
    queue(w)
    report_in_parts(w)
    w.set_engine(lambda s: s["next_rc"].update({"worker-send": 2}))   # the engine refuses the next attempt
    passed(w)
    w.tick()
    run = w.store.run(RID)
    assert run.state == "done" and run.outcome == "found"
    review = json.loads(run.result)["engine_review"]
    assert "findings-2.json was not reviewed before the run ended (the engine did not take it" in review
    assert w.events_named("qa_review_part_refused")


def test_a_part_added_after_the_parts_were_counted_is_not_reviewed(w):
    queue(w)
    report_in_parts(w)
    passed(w)
    (out_dir(w) / "findings-3.json").write_text(json.dumps({"tested": [CHECK]}))
    passed(w)
    result = json.loads(w.store.run(RID).result)
    assert "findings-3.json was not reviewed before the run ended" in result["engine_review"]
    assert any("says 2 parts, the report has 3" in p for p in result["problems"])


def test_a_run_that_runs_out_of_time_between_parts_says_which_were_not_reviewed(w):
    queue(w)
    report_in_parts(w)
    w.now += w.cfg.qa.timeout + timedelta(minutes=1)
    passed(w)
    run = w.store.run(RID)
    assert run.state == "done" and sends(w) == []
    assert "findings-2.json was not reviewed before the run ended" in json.loads(run.result)["engine_review"]


@pytest.mark.parametrize("first, second, problem", [
    ({"parts": 3}, None, "says 3 parts, the report has 2"),
    ({"checks": 5}, None, "says 5 checks, the report has 2"),
    ({"findings_count": None}, None, "does not say how many findings"),
    ({}, {"tested": [{"what": "x" * 300, "result": "ok", "note": "é" * 300}] * 120}, "findings-2.json is"),
])
def test_what_the_first_part_says_of_the_whole_and_a_part_too_long_to_review_are_checked(w, first, second, problem):
    queue(w)
    report_in_parts(w, second, **first)
    passed(w)
    passed(w)
    result = json.loads(w.store.run(RID).result)
    assert any(problem in p for p in result["problems"]), result["problems"]


def test_a_gap_in_the_numbering_is_not_read_as_the_end_of_the_report(w):
    queue(w)
    report(w, findings=WHOLE, tested=[CHECK])
    (out_dir(w) / "findings-3.json").write_text(json.dumps({"tested": [CHECK]}))
    passed(w)
    run = w.store.run(RID)
    assert any("not numbered without a gap" in p and "findings-3.json" in p for p in json.loads(run.result)["problems"])


def test_a_session_interrupted_while_a_later_part_is_reviewed_is_told_which_part_to_name(w):
    queue(w)
    report_in_parts(w)
    passed(w)
    w.set_engine(lambda s: s["tasks"][RID].update(attempt_live=False))   # the part's session died
    w.tick()
    resume = [c for c in sends(w) if c["argv"][c["argv"].index("--message-id") + 1].startswith("resume-")]
    assert len(resume) == 1 and f"`artifacts/{RID}/findings-2.txt`" in resume[0]["argv"][-1]


def test_the_brief_tells_how_to_split_a_long_report(w):
    queue(w)
    text = dispatches(w)[0]["argv"][-1]
    assert "`findings-2.json`" in text and "under 40 KB" in text and "`\"findings_count\"`" in text
    assert "one part at a time" in text


def test_without_instructions_nothing_is_dispatched_and_the_report_says_why(tmp_path):
    w = world(tmp_path, instructions=None)
    try:
        queue(w)
        assert dispatches(w) == []
        run = w.store.run(RID)
        assert (run.state, run.outcome) == ("done", "unfinished")
        assert "does not exist" in run.reason
        assert "does not exist" in (w.cfg.qa.reports / "1" / "report.html").read_text()
    finally:
        w.events.close()
        w.store.close()


def test_the_example_as_setup_installs_it_is_empty_and_never_tested(tmp_path):
    w = world(tmp_path, instructions=EXAMPLE.read_text())
    try:
        assert "staging.example.com" in w.cfg.qa.instructions.read_text()
        check = qa_check(w.cfg)
        assert check.status == "fail" and "empty (comments only)" in check.detail
        queue(w)
        assert dispatches(w) == []
        run = w.store.run(RID)
        assert (run.state, run.outcome) == ("done", "unfinished") and "empty (comments only)" in run.reason
    finally:
        w.events.close()
        w.store.close()


def test_the_example_does_not_fail_the_check_while_qa_is_off(tmp_path):
    w = world(tmp_path, instructions=EXAMPLE.read_text(), enabled=False)
    try:
        assert qa_check(w.cfg).status == "ok"
    finally:
        w.events.close()
        w.store.close()


def test_the_agent_gets_the_instructions_without_their_comments_and_the_run_keeps_that_text(tmp_path):
    w = world(tmp_path, instructions=EXAMPLE.read_text() + "\nStaging: https://qa.example.org\n<!-- a note -->\n"
                                                           "Check the cart.\n")
    try:
        assert qa_check(w.cfg).status == "ok"
        queue(w)
        [call] = dispatches(w)
        text = call["argv"][-1].split("## The instructions", 1)[1]
        assert text.strip() == "Staging: https://qa.example.org\n\nCheck the cart."
        assert "staging.example.com" not in text and "a note" not in text and "<!--" not in text
        kept = (w.cfg.qa.reports / "1" / "instructions.md").read_text()
        assert kept == "Staging: https://qa.example.org\n\nCheck the cart.\n"
    finally:
        w.events.close()
        w.store.close()


def test_stop_ends_the_run_and_keeps_what_it_found(w):
    queue(w)
    report(w)
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    run = w.store.run(RID)
    assert (run.state, run.outcome) == ("done", "stopped")
    assert ["stop", str(w.cfg.paths.workspace)] in [c["argv"] for c in w.calls("night-shift")]
    assert "Reset link gives 404" in (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert git(w.repo, "branch", "--show-current") == "main"


def test_a_queued_run_that_is_stopped_never_starts(w):
    w.store.add_run("qa", "command", "ops")
    w.store.update_run(RID, stop_requested=1)
    w.tick()
    assert dispatches(w) == [] and w.store.run(RID).outcome == "stopped"


def test_a_crash_between_dispatch_and_its_record_does_not_dispatch_twice(w):
    queue(w)
    w.store.update_run(RID, state="preparing")
    w.restart()
    w.tick()
    assert len(dispatches(w)) == 1 and w.store.run(RID).state == "running"


def test_the_run_goes_before_a_queued_ticket(w):
    w.store.add_run("qa", "command", "ops")
    take(w)
    assert [c["argv"][1] for c in w.calls("dispatch")] == [RID]


def test_grafana_sees_the_agent_testing(w):
    queue(w)
    m = Metrics()
    m.update_busy(w.store)
    assert m.registry.get_sample_value("runner_agent_busy") == 2
    m.update(w.runner)
    assert m.registry.get_sample_value("runner_task", {"task_id": RID, "state": "testing: running",
                                                       "summary": "Platform test, run 1"}) == 1


# -- the schedule -------------------------------------------------------------------------------------

def test_the_schedule_queues_its_time_once(tmp_path):
    # NOW is Tuesday 2026-10-20 07:00 UTC, 10:00 in Kyiv.
    w = world(tmp_path, schedule={"days": ["tue"], "at": "09:30"})
    try:
        w.tick()
        [run] = w.store.runs("qa")
        assert (run.source, run.requested_by) == ("schedule", "schedule")
        report(w)
        finish(w)
        w.restart()
        w.now += timedelta(minutes=10)
        w.tick()
        assert len(w.store.runs("qa")) == 1  # the same slot: not again, also after a restart
    finally:
        w.events.close()
        w.store.close()


def test_a_scheduled_time_missed_by_more_than_an_hour_is_not_made_up(tmp_path):
    w = world(tmp_path, schedule={"days": ["tue"], "at": "08:00"})
    try:
        w.tick()
        assert w.store.runs("qa") == []
    finally:
        w.events.close()
        w.store.close()


# -- reading what the agent wrote ----------------------------------------------------------------------

def test_findings_are_read_defensively(tmp_path):
    folder, dest = tmp_path / "out", tmp_path / "dest"
    folder.mkdir()
    (folder / "real.png").write_bytes(PNG)
    (folder / "fake.png").write_text("not a png")
    (tmp_path / "secret.png").write_bytes(PNG)
    (folder / "link.png").symlink_to(tmp_path / "secret.png")
    (folder / "findings.json").write_text(json.dumps({
        "summary": "x" * 2000, "tested": [{"what": "A", "result": "weird"}, "nonsense"],
        "findings": [{"title": "One", "severity": "apocalyptic", "url": "javascript:alert(1)",
                      "steps": ["a"], "failed_step": 9,
                      "evidence": [{"file": "real.png", "step": 1}, {"file": "fake.png"}, {"file": "link.png"},
                                   {"file": "../secret.png"}]},
                     {"severity": "minor"}]}))
    data, problems = load_findings(folder, dest)
    assert len(data["summary"]) < 1600
    assert data["tested"] == [{"what": "A", "result": "skipped", "note": ""}]
    [f] = data["findings"]
    assert (f["severity"], f["url"], f["failed_step"]) == ("major", "", None)
    assert [e["step"] for e in f["evidence"]] == [1]
    assert [p.name for p in dest.iterdir()] == [f["evidence"][0]["file"]]
    assert any("fake.png" in p for p in problems) and any("link.png" in p for p in problems)
    assert any("../secret.png" in p for p in problems) and any("finding 2" in p for p in problems)


def test_what_goes_past_the_limits_is_left_out_by_count_and_by_name(tmp_path):
    folder, dest = tmp_path / "out", tmp_path / "dest"
    folder.mkdir()
    for i in range(3):
        (folder / f"s{i}.png").write_bytes(PNG + bytes([i]))
    (folder / "findings.json").write_text(json.dumps({
        "tested": [{"what": f"Check {i}", "result": "ok"} for i in range(15)],
        "findings": [{"title": f"Bug {i}", "evidence": [{"file": f"s{i}.png"}]} for i in range(3)]}))
    data, problems = load_findings(folder, dest, Limits(checks=3, findings=2, files=1))
    assert [t["what"] for t in data["tested"]] == ["Check 0", "Check 1", "Check 2"]
    assert [f["title"] for f in data["findings"]] == ["Bug 0", "Bug 1"]
    assert [len(f["evidence"]) for f in data["findings"]] == [1, 0]
    [checks] = [p for p in problems if "qa.max_checks" in p]
    assert "lists 15 checks" in checks and "12 are left out" in checks
    assert "\u201cCheck 3\u201d" in checks and "Check 12" in checks and "Check 13" not in checks and "2 more" in checks
    [findings] = [p for p in problems if "qa.max_findings" in p]
    assert "has 3 findings" in findings and "\u201cBug 2\u201d" in findings
    assert any("s1.png" in p and "qa.max_files" in p for p in problems)
    # The truncated report is not read as whole: never "No problems found", even when every kept check is ok.
    assert outcome_of({**data, "findings": []}, problems, [], False)[0] == "unfinished"


@pytest.mark.parametrize("limit, outcome", [(20, "unfinished"), (100, "partial")])
def test_checks_the_agent_did_not_get_to_are_never_a_pass(tmp_path, limit, outcome):
    """As the brief asks: every check an entry, the ones not done "skipped", whether or not they fit the limit."""
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "findings.json").write_text(json.dumps({
        "summary": "Checked 20 of 30; checks 20 to 29 did not fit.",
        "tested": [{"what": f"Check {i}", "result": "ok" if i < 20 else "skipped"} for i in range(30)],
        "findings": []}))
    data, problems = load_findings(folder, tmp_path / "dest", Limits(checks=limit))
    assert outcome_of(data, problems, [], False)[0] == outcome


def test_checks_past_the_limit_are_never_a_silent_pass(tmp_path):
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "findings.json").write_text(json.dumps({
        "tested": [{"what": f"Check {i}", "result": "ok"} for i in range(30)], "findings": []}))
    data, problems = load_findings(folder, tmp_path / "dest", Limits(checks=20))
    assert len(data["tested"]) == 20 and any("10 are left out" in p for p in problems)
    outcome, doubts = outcome_of(data, problems, [], False)
    assert outcome != "passed" and doubts


def test_a_findings_file_past_its_size_is_not_read(tmp_path):
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "findings.json").write_text(json.dumps({"summary": "x" * 3000}))
    data, [problem] = load_findings(folder, tmp_path / "dest", Limits(report_bytes=1024))
    assert data == {} and "qa.max_report_mb" in problem


def test_the_limits_come_from_the_config_and_the_agent_is_told_them(tmp_path):
    assert Limits.of(QaConfig()) == Limits()
    w = world(tmp_path, max_checks=1500, max_findings=400, max_files=50, max_report_mb=16, max_media_mb=2048)
    try:
        assert Limits.of(w.cfg.qa) == Limits(checks=1500, findings=400, files=50, report_bytes=16 * 1024 * 1024,
                                             media_bytes=2048 * 1024 * 1024)
        queue(w)
        [call] = dispatches(w)
        brief = call["argv"][-1]
        assert "at most 1500 entries of `tested`, 400 findings and 50 screenshots" in brief
        # One entry per check: a list of checks folded into a `note` would be cut short with no word of it.
        assert "one entry per check" in brief and "never several checks in one entry" in brief
        # Every check is an entry, the ones not done "skipped": coverage never lives in `summary` alone.
        assert "also one you did not get to: that one is \"skipped\"" in brief
        assert "nor tell of it only in `summary`" in brief
    finally:
        w.events.close()
        w.store.close()


@pytest.mark.parametrize("tested, problems, outcome, doubt", [
    (["ok"], [], "passed", None),
    (["ok", "skipped"], [], "partial", None),
    (["ok", "blocked"], [], "partial", None),
    ([], [], "unfinished", "did not say what it tested"),
    (["skipped"], [], "unfinished", "every check was skipped"),
    (["blocked", "skipped"], [], "blocked", "none of the checks could be done"),
    (["ok", "problem"], [], "unfinished", "no finding says what the problem is"),
    (["ok"], ["finding 1 has no readable title"], "unfinished", "could not be read"),
])
def test_no_problems_found_only_when_something_was_checked_and_the_report_holds_whole(tested, problems, outcome,
                                                                                       doubt):
    found = {"tested": [{"what": str(i), "result": r} for i, r in enumerate(tested)], "findings": []}
    got, doubts = outcome_of(found, problems, [], False)
    assert got == outcome
    assert (doubt is None and doubts == []) or any(doubt in d for d in doubts)


def test_the_other_outcomes_go_before_the_checks():
    checked = {"tested": [{"what": "A", "result": "ok"}], "findings": []}
    assert outcome_of(checked, [], ["timed out"], False) == ("unfinished", [])
    assert outcome_of({**checked, "findings": [{}]}, ["x"], [], False) == ("found", [])
    assert outcome_of({**checked, "blocked": "down"}, [], [], False) == ("blocked", [])
    assert outcome_of(checked, [], ["x"], True) == ("stopped", [])


@pytest.mark.parametrize("tested, findings, outcome, head", [
    ([{"what": "Payments", "result": "skipped"}], [], "unfinished", "Did not finish"),
    ([{"what": "Sign-in", "result": "problem"}], [], "unfinished", "Did not finish"),
    ([{"what": "Sign-in", "result": "blocked"}], [], "blocked", "Could not test"),
    ([{"what": "Sign-in", "result": "ok"}], [{"severity": "major"}], "unfinished", "Did not finish"),  # no title
    ([{"what": "Sign-in", "result": "ok"}, {"what": "Admin", "result": "blocked"}], [], "partial",
     "No problems in what was checked; some could not be checked"),
])
def test_a_run_that_did_not_check_everything_never_reads_as_no_problems(w, tested, findings, outcome, head):
    queue(w)
    report(w, tested=tested, findings=findings, summary="All good.")
    finish(w)
    run = w.store.run(RID)
    assert run.outcome == outcome
    page = (w.cfg.qa.reports / "1" / "report.html").read_text()
    assert head in page and "No problems found" not in page
    assert w.events_named("qa_published")[-1]["outcome"] == outcome


# -- the reports server ---------------------------------------------------------------------------------

def test_the_server_shows_the_list_reports_and_their_media_and_nothing_else(w):
    queue(w)
    report(w)
    finish(w)
    [shot] = list((w.cfg.qa.reports / "1" / "media").iterdir())
    port, stop = qa_report.serve(w.cfg.qa.reports, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{port}"

    def get(path: str) -> tuple[int, bytes, str]:
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, b"", ""
    try:
        status, body, kind = get("/")
        assert status == 200 and b"Run 1" in body and kind.startswith("text/html")
        assert get("/1/")[0] == 200 and b"Reset link gives 404" in get("/1/")[1]
        assert get("/1")[0] == 200  # redirected to /1/
        assert get(f"/1/media/{shot.name}")[1:] == (PNG, "image/png")
        for refused in ("/1/instructions.md", "/1/result.json", "/1/media/../instructions.md", "/../runner.db",
                        "/1/media/reset.png", "/index.html"):
            assert get(refused)[0] == 404, refused
    finally:
        stop()


def test_the_list_of_runs_is_there_before_any_run_and_shows_queued_and_running_ones(w):
    from arceus_runner.qa import index_page
    port, stop = qa_report.serve(w.cfg.qa.reports, "127.0.0.1", 0, index=lambda: index_page(w.cfg))

    def page() -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            assert r.status == 200
            return r.read().decode()
    try:
        assert "No runs yet" in page()
        w.store.add_run("qa", "command", "ops")
        assert "Run 1" in page() and "Queued" in page() and 'href="1/"' not in page()
        w.tick()
        assert w.store.run(RID).state == "running" and "Testing now" in page()
        w.store.add_run("qa", "command", "ops")
        listed = page()
        assert "Run 2" in listed and "Queued" in listed
        report(w)
        finish(w)
        assert 'href="1/"' in page() and "1 problem found" in page()
    finally:
        stop()


def test_a_report_escapes_what_the_agent_wrote(tmp_path):
    view = {"n": 3, "outcome": "found", "source": "command", "requested_by": "ops",
            "findings": [{"title": "<script>alert(1)</script>", "severity": "minor", "steps": ["<b>x</b>"],
                          "evidence": [], "code": [], "url": ""}], "tested": [], "repos": []}
    page = qa_report.render_run(view, "acme", ZoneInfo("Europe/Kyiv"))
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "<b>x</b>" not in page
