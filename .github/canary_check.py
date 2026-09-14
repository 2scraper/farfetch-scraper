#!/usr/bin/env python3
"""
Interpret a canary run: the exit code first, then the data it wrote.

Lives here rather than in the workflow for the same reason `ci_checks.py`
does — a Python block nested inside YAML inside a shell `run:` needs three
levels of quoting to survive — and for one more that the 2026-09-11 audit
made expensive: the workflow's exit-code table had drifted away from what the
codes actually mean. It had no entry for 5 or 6 at all, so a fetch failure
fell through to "unexpected exit code", and it explained 124 as "the page
never became ready" — which is wrong twice over. 124 is Selenium's watchdog
for CHROMEDRIVER failing to start, nothing to do with page readiness, and the
canary runs Playwright, which cannot return it. A table nobody can execute
drifts silently; this one is imported by the offline suite, which asserts it
against output_writer's constants.

Two callers, deliberately the same implementation (CLAUDE.md §17: one
implementation invoked from both CI and the offline suite, not a workflow
reimplementing a narrower copy of a shipped script):

    python3 .github/canary_check.py --exit-code 3 --allow-block
    python3 .github/canary_check.py --data canary_run

`--allow-block` is what separates the two canary signals. From a GitHub
runner — a datacentre address — Akamai refusing us says something about the
runner's IP, not about Farfetch or about this code, and a check that is red
every morning teaches everyone to ignore checks (CLAUDE.md §11). So the free
no-proxy signal treats a block as a SKIP and stays green, while the
proxy-backed signal treats the same code as a failure, because from a
residential exit a block IS news.
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from output_writer import (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_FETCH_FAILED,
                           EXIT_PARTIAL, EXIT_DRIVER_TIMEOUT)

# What each exit code means for a canary, and whether it is this canary's
# business to fail on. Kept as DATA next to the constants it names, so the
# workflow cannot quietly disagree with the engines about what a code means —
# which is exactly what had happened by the time of the audit.
#
# `blocked` is the one row whose verdict depends on where the run came from;
# every other row means the same thing from anywhere.
EXIT_MEANINGS = {
    0: ("OK — products parsed from a live run.", False),
    1: ("Crashed (exit 1) — see the traceback in the step above. A crash is "
        "always this repo's problem, never the site's.", True),
    2: ("Bad usage (exit 2) — the workflow is calling the CLI wrong, which is "
        "a workflow bug, not a site change.", True),
    EXIT_BLOCKED: (
        "Blocked before parsing (exit 3) — a challenge or an outright refusal "
        "stood between the run and the content.", True),
    EXIT_NO_PRODUCTS: (
        "The page was fetched and parsed and held 0 products (exit 4), on a "
        "URL known to have some. This is the shape a real site-side break "
        "takes — a changed JSON-LD shape or a dead product-link pattern.", True),
    EXIT_FETCH_FAILED: (
        "The page was never fetched (exit 5) — a navigation timeout, a dead "
        "or unauthenticated proxy, or an edge answering 4xx/5xx. Nothing can "
        "be concluded about the site from this run; check the proxy secret "
        "before suspecting Farfetch.", True),
    EXIT_PARTIAL: (
        "Partial run (exit 6) — some pages were fetched and some were not, so "
        "the output is real but incomplete. See canary_run.meta.json for "
        "which page numbers failed.", True),
    # Part of the contract, and listed so it cannot fall through to
    # "unexpected", but unreachable from this workflow: it is Selenium's
    # watchdog for chromedriver failing to START, and the canary runs
    # Playwright. Seeing it here would mean the workflow changed engine
    # without this table being updated — worth saying so explicitly.
    EXIT_DRIVER_TIMEOUT: (
        "Driver-startup watchdog (exit 124) — chromedriver could not be built "
        "in time. This is Selenium's code and the canary runs Playwright, so "
        "seeing it here means the workflow changed engine.", True),
}

# A single page of this category has returned ~96 products on every measured
# run, so three pages clear this comfortably. Deliberately far below the real
# figure rather than tracking it: a floor guards against a near-empty render,
# and a threshold that tracks a number which legitimately varies between runs
# is a check that goes stale on its own (CLAUDE.md §17, check 4).
PAGES_REQUESTED = 3
MIN_PRODUCTS = 120
MIN_PRICED_SHARE = 0.9
MIN_DOM_CONFIRMED_SHARE = 0.5


def check_exit_code(code: int, allow_block: bool) -> int:
    """Print what `code` means; return this job's own exit status."""
    meaning, fails = EXIT_MEANINGS.get(
        code, (f"Unexpected exit code {code} — not one this scraper documents. "
               f"See the step above.", True))

    if code == EXIT_BLOCKED and allow_block:
        print(f"::notice::{meaning} This canary runs from a GitHub runner, "
              f"which is a datacentre address, and no proxy secret is set — so "
              f"this says the runner's IP was refused, NOT that the site or "
              f"this code changed. Reported as a skip rather than a failure, "
              f"because a check that is red every morning is a check nobody "
              f"reads. Set the FARFETCH_PROXY secret for a signal that can "
              f"tell the two apart.")
        return 0

    if not fails:
        print(meaning)
        return 0

    print(f"::error::{meaning}")
    return 1


def check_data(prefix: str) -> int:
    """Exit 0 only means 'some products were written'.

    It says nothing about whether the run really rendered: a snapshot taken
    before prices load still writes rows, just with every price null.
    """
    rows = json.loads(Path(f"{prefix}.json").read_text())
    meta = json.loads(Path(f"{prefix}.meta.json").read_text())

    fail = []
    if len(rows) < MIN_PRODUCTS:
        fail.append(f"only {len(rows)} products, expected at least "
                    f"{MIN_PRODUCTS} across {PAGES_REQUESTED} pages")

    priced = sum(1 for r in rows if r.get("price") is not None)
    if rows and priced / len(rows) < MIN_PRICED_SHARE:
        fail.append(f"only {priced}/{len(rows)} rows have a non-null price — "
                    f"looks like a snapshot taken before prices rendered")

    # The point of running more than one page: catch pagination dying.
    # `no_new_products` after a single page means the next-page link AND the
    # ?page= fallback both failed to reach new content.
    if meta.get("pages_completed", 0) < PAGES_REQUESTED:
        fail.append(f"pagination stopped after {meta.get('pages_completed')} "
                    f"of {PAGES_REQUESTED} pages (stop_reason="
                    f"{meta.get('stop_reason')!r}) — the next-page selector or "
                    f"the ?page= fallback has probably broken")
    if meta.get("status") != "complete":
        fail.append(f"run status is {meta.get('status')!r}, not 'complete'")

    dom_confirmed = sum(1 for r in rows if r.get("price_source") == "jsonld+dom")
    if rows and dom_confirmed / len(rows) < MIN_DOM_CONFIRMED_SHARE:
        fail.append(f"only {dom_confirmed}/{len(rows)} rows had their price "
                    f"confirmed against a rendered tile — discounted rows may "
                    f"carry pre-promo prices")

    for line in fail:
        print(f"::error::{line}")
    if not fail:
        print(f"{len(rows)} products, {priced} priced, {dom_confirmed} "
              f"DOM-confirmed, {meta.get('pages_completed')} pages — all "
              f"thresholds cleared.")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exit-code", type=int,
                    help="interpret the scraper's exit code")
    ap.add_argument("--allow-block", action="store_true",
                    help="treat exit 3 as a skip, not a failure — for the "
                         "no-proxy signal, where a datacentre block says "
                         "nothing about the site")
    ap.add_argument("--data", metavar="PREFIX",
                    help="check the data a successful run wrote")
    args = ap.parse_args()

    if args.exit_code is None and not args.data:
        ap.error("pass --exit-code and/or --data")

    rc = 0
    if args.exit_code is not None:
        rc |= check_exit_code(args.exit_code, args.allow_block)
    if args.data:
        rc |= check_data(args.data)
    return rc


if __name__ == "__main__":
    sys.exit(main())
