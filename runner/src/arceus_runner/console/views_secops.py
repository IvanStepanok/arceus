"""Advisory triage (secops.py) in the console, for reading: the list of triage runs and a run's report, rendered from
its result.json as a test run's is (runs.report_page). Nothing here starts or stops a run or edits the team's notes:
a run is started by `arceus-runner secops run` or `secops.schedule` and stopped by `secops stop`, on the server."""

from __future__ import annotations

from starlette.responses import Response
from starlette.routing import Route

from arceus_runner import secops
from arceus_runner.console import runs
from arceus_runner.console.store import ConsoleError
from arceus_runner.console.web import Ctx, page
from arceus_runner.db import ReviewStore

SECTIONS = ("/secops/",)


def _cfg(ctx: Ctx):
    cfg, problems = ctx.config()
    if cfg is None:
        raise ConsoleError("config.yaml has errors: " + "; ".join(problems[:3]))
    return cfg


def index(ctx: Ctx) -> Response:
    cfg = _cfg(ctx)
    try:
        store = ReviewStore.read_only(cfg.paths.db)
    except (FileNotFoundError, RuntimeError) as e:
        rows, error = [], str(e)
    else:
        try:
            rows, error = secops.index_rows(store, cfg.secops.reports), ""
        finally:
            store.close()
    return ctx.render("runs.html", title="Advisory triage", kind="secops",
                      rows=runs.dress(rows, secops.head_of, "secops"), enabled=cfg.secops.enabled, error=error,
                      mode=cfg.mode, schedule=cfg.secops.schedule)


def run(ctx: Ctx) -> Response:
    n = ctx.number("n")
    return runs.report_page(ctx, _cfg(ctx), "secops", n, secops.run_body)


def live(ctx: Ctx) -> Response:
    n = ctx.number("n")
    return runs.report_live(ctx, _cfg(ctx), "secops", n)


def routes() -> list[Route]:
    return [Route("/secops/", page(index)),
            Route("/secops/{n}/", page(run)),
            Route("/secops/{n}/live", page(live, api=True))]
