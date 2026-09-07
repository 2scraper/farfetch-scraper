"""
output_writer.py
-----------------
Shared product model + JSON/CSV writers used by all three scrapers.
"""

import csv
import json
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
    if not products:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("")
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
             products: int) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — products were gathered, then the run stopped early
      failed   — nothing was gathered at all
    """
    return {
        "source": "farfetch.com",
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
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
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted")


def finish_run(products: List[Product], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str) -> int:
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

    if wrote_output:
        status = "complete" if (products and complete) else (
            "partial" if products else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            start_url=start_url, final_url=final_url, products=len(products)))

    if not products:
        # Nothing gathered at all: a challenge outranks "empty category",
        # because it says something stood between the run and the content.
        return EXIT_BLOCKED if blocked else rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view of the "
              f"category — see {out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
