"""
output_writer.py
-----------------
Shared product model + JSON/CSV writers used by all three scrapers.
"""

import csv
import json
import uuid

import notify
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional, List, Set


@dataclass
class Product:
    source: str = "farfetch.com"
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    sku: Optional[str] = None
    title: Optional[str] = None
    brand: Optional[str] = None
    price: Optional[float] = None
    # No guessed default: a caller that doesn't know the currency should say
    # so (None) rather than silently claiming USD.
    currency: Optional[str] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Where `price` came from, because the same column can hold two figures
    # with different confidence and nothing used to say which:
    #   "jsonld+dom" — the rendered tile was found and reconciled with the
    #                  JSON-LD figure: either it corrected the price to the
    #                  one a customer pays, or the tile showed a single price
    #                  confirming there is no discount. Trustworthy.
    #   "jsonld"     — structured data only; the tile was missing (this site
    #                  renders a variable fraction of them) or disagreed, so
    #                  on a discounted item this may be the PRE-PROMO price.
    #   "dom"        — the CSS/URL fallback path: read from the tile's own
    #                  text, with no JSON-LD to cross-check.
    # Without this, two runs that differed only in how much had rendered
    # produced a false "price changed" in diff_runs.py.
    price_source: Optional[str] = None


def dedupe_by_sku(products: List[Product], seen: Set[str]) -> List[Product]:
    """Drop products whose sku already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating NEXT_PAGE_SELECTOR link then re-parses a page without
    duplicating its rows into the final output. A product with no sku (the
    fallback parser failing to recover one) is always kept: there is nothing
    to key a duplicate check on, and dropping it would be a silent data loss
    rather than a duplicate removal.
    """
    fresh = []
    for p in products:
        if p.sku is None or p.sku not in seen:
            if p.sku is not None:
                seen.add(p.sku)
            fresh.append(p)
    return fresh


def write_json(products: List[Product], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: List[Product], path: str) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    if not products:
        with open(path, "w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=list(asdict(Product()).keys())).writeheader()
        return
    fieldnames = list(asdict(products[0]).keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in products:
            writer.writerow(asdict(p))


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# category is genuinely empty" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME products and then stopped early —
# a page-load timeout, or a challenge, on page 3 of 10. The output file is
# still written (throwing away three good pages would be worse), but it is
# not a complete picture of the category, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products
# that disappeared from the catalogue. See write_run_meta.
EXIT_PARTIAL = 6

# Exit code for a run that never GOT the page: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering 4xx/5xx with
# something that is not the listing.
#
# This is the gap the 2026-09-11 audit found, and it is the same bug class as
# the one above it: every one of those used to return EXIT_NO_PRODUCTS, so a
# dead proxy, a network flap and a genuinely empty category were one value to
# an automated caller. Those want three different responses — retry the same
# exit, change exit, accept the answer — and the caller had no way to choose.
#
# 5 rather than a new number: the family exit-code contract already reserves
# it for "the transport failed" (scraper_api_client has used it for a Scraper
# API error since it was written), and the browser engines simply had no way
# to say the same thing. Widening it from "remote API error" to "the fetch
# failed" keeps ONE meaning per code across the family — see CHANGELOG.
#
# Deliberately NOT applied when products were gathered: a timeout on page 7
# of 10 is a PARTIAL run (exit 6, output written), which is already right.
# This only decides what a run holding nothing reports.
EXIT_FETCH_FAILED = 5

# Exit code for Selenium's driver-startup watchdog: chromedriver could not be
# BUILT within the timeout, so no page was ever requested. 124 because that is
# what `timeout(1)` uses and what a harness already understands.
#
# Named here rather than spelled 124 inside selenium_scraper because it is
# part of the documented contract: the canary's exit-code table explained it
# as "the page never became ready", which is the wrong cause entirely, and a
# magic number in one engine is how a table comes to describe something else.
# Unreachable from the other engines, which have no separate driver to start.
EXIT_DRIVER_TIMEOUT = 124


# Stop reasons that mean the run never obtained the page, as opposed to
# obtaining it and finding nothing on it. Kept as data next to the exit code
# they map to, so an engine cannot invent a reason that silently falls
# through to "no products" — the failure this list exists to prevent.
FETCH_FAILURE_STOP_REASONS = ("page_load_timeout", "proxy_unusable",
                              "http_error")


def stop_reason_for(*, load_failed: bool, blocked_by: Optional[str],
                    http_status: Optional[int] = None,
                    proxy_failure: Optional[str] = None) -> str:
    """The one place that names why a page did not yield content.

    Shared by the engines for the same reason finish_run() is: three copies
    of this triage drift, and the drift is silent — one engine reporting a
    dead proxy as a timeout while its twin calls it a block, on the same
    page. Keyword-only so adding a signal later cannot silently re-bind an
    existing caller's positional argument.

    Ordered by how much each signal PROVES, not by how cheap it is to test
    (CLAUDE.md §17): naming the vendor that refused us is a stronger
    statement than "the status was 403", which is stronger than "it timed
    out", so the specific reason wins over the general one.
    """
    if blocked_by:
        return f"blocked_{blocked_by}"
    if proxy_failure:
        return "proxy_unusable"
    if http_status is not None and http_status >= 400:
        return "http_error"
    if load_failed:
        return "page_load_timeout"
    return "completed"


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    product row: this describes the RUN, not the product, and repeating it
    across 96 identical rows would both bloat the output and change the
    schema every consumer of this project already parses.

    diff_runs.py reads it to refuse an assortment comparison between runs
    that are not both complete — the failure mode it exists to prevent is a
    partial run's un-fetched pages being reported as delisted products.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             *, run_id: Optional[str] = None,
             started_at: Optional[float] = None,
             rows: Optional[List[Product]] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — products were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    finished = datetime.now(timezone.utc)
    meta = {
        "source": "farfetch.com",
        "run_id": run_id or new_run_id(),
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": finished.isoformat(),
    }
    if started_at is not None:
        meta["started_at"] = datetime.fromtimestamp(
            started_at, timezone.utc).isoformat()
        meta["duration_s"] = round(finished.timestamp() - started_at, 1)
    if rows is not None:
        meta["quality"] = quality_metrics(rows)
    return meta


def new_run_id() -> str:
    """A short id for one run.

    Exists so a log line, a sidecar and a support request can be tied
    together — "the run that failed" is not identifying when a scraper is on
    a schedule. Short and random rather than a hash of the arguments: two
    runs of the same command ARE different runs, and that is the thing being
    identified.
    """
    return uuid.uuid4().hex[:12]


def quality_metrics(products: List[Product]) -> dict:
    """Coverage of the columns that are allowed to be null, as fractions.

    In the sidecar rather than only in the canary, because a consumer needs
    it for the same reason the canary does: a run can return the right NUMBER
    of rows with a column silently empty, and "96 products" says nothing
    about whether their prices rendered. Previously this was computed only
    inside .github/canary_check.py, so every other consumer had to recompute
    it — or, in practice, not notice.

    Fractions, not counts: a count has to be read against the row total to
    mean anything, and a fraction is what a threshold compares against.
    Rounded to three places so a sidecar diff does not churn on noise.
    """
    total = len(products)
    if not total:
        return {"rows": 0}

    def share(pred) -> float:
        return round(sum(1 for p in products if pred(p)) / total, 3)

    return {
        "rows": total,
        "priced": share(lambda p: p.price is not None),
        "with_currency": share(lambda p: p.currency is not None),
        "with_title": share(lambda p: bool(p.title)),
        "with_brand": share(lambda p: bool(p.brand)),
        "with_image": share(lambda p: bool(p.image_url)),
        "with_sku": share(lambda p: p.sku is not None),
        "discounted": share(lambda p: p.discount_pct is not None),
        # The one that is provenance rather than coverage: how much of the
        # price data was confirmed against a rendered tile instead of taken
        # from JSON-LD alone. A drop here is how a snapshot-taken-too-early
        # run announces itself.
        "dom_confirmed_price": share(
            lambda p: p.price_source == "jsonld+dom"),
    }


def save(products: List[Product], out_prefix: str, fmt: str,
         allow_empty: bool = False) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when products were written, EXIT_NO_PRODUCTS when there were
    none. Callers are expected to exit with it.

    On zero products, nothing is written at all unless `allow_empty`. Two
    reasons, and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not products and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(products, f"{out_prefix}.json")
        print(f"[+] Saved {len(products)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(products, f"{out_prefix}.csv")
        print(f"[+] Saved {len(products)} products -> {out_prefix}.csv")
    return 0 if products else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. See playwright_scraper.py, which now falls back to the ?page=
# convention rather than trusting the selector to decide.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products")


def finish_run(products: List[Product], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               run_id: Optional[str] = None,
               started_at: Optional[float] = None,
               webhook: Optional[str] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the product file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    rc = save(products, out_prefix, fmt, allow_empty=allow_empty)
    wrote_output = bool(products) or allow_empty

    status = "complete" if (products and complete) else (
        "partial" if products else "failed")
    # Built ALWAYS, written only beside a file that exists. The sidecar rule
    # is unchanged — a "failed" sidecar next to the previous run's still-intact
    # good output would contradict it — but the webhook needs the same summary
    # for exactly the runs that write nothing, which are the ones somebody
    # wants to be told about.
    meta = run_meta(
        status=status, stop_reason=stop_reason,
        pages_requested=pages_requested, pages_completed=pages_completed,
        pages_failed=pages_failed,
        start_url=start_url, final_url=final_url, products=len(products),
        run_id=run_id, started_at=started_at, rows=products)
    if wrote_output:
        write_run_meta(out_prefix, meta)

    rc = _finish_code(products, rc, blocked, stop_reason, complete,
                      out_prefix, pages_completed, pages_requested)
    if webhook:
        notify.send(webhook, meta, rc)
    return rc


def _finish_code(products, rc, blocked, stop_reason, complete,
                 out_prefix, pages_completed, pages_requested) -> int:
    """The exit code alone, so finish_run can notify after deciding it."""
    if not products:
        # Nothing gathered at all, and the three reasons are not the same
        # answer. Ordered by how much each proves: a named vendor outranks a
        # transport failure, which outranks "we got the page and it was
        # empty" — the only one of the three that is really EXIT_NO_PRODUCTS.
        if blocked:
            return EXIT_BLOCKED
        if stop_reason in FETCH_FAILURE_STOP_REASONS:
            print(f"[!] The page was never fetched ({stop_reason}) — this is "
                  f"exit {EXIT_FETCH_FAILED}, NOT an empty category "
                  f"(exit {EXIT_NO_PRODUCTS}). Nothing can be concluded about "
                  f"the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view of the "
              f"category — see {out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
