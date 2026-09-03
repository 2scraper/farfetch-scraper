#!/usr/bin/env python3
"""
farfetch-scraper — Playwright edition (primary engine)
==========================================================

Scrapes Farfetch (farfetch.com) kids category/hub pages: title, price,
original price, discount %, rating (best-effort), image, URL.

Works against ANY Farfetch category/hub page (Kids, Women, Men, Sale,
brand pages, etc.) — no category-specific logic. Product tiles are found
by URL pattern (`-item-<digits>.aspx`), not by CSS class, since Farfetch's
exact class names weren't available to reverse-engineer at write time (see
product_parser.py's module docstring for why that's the more durable choice
anyway).

Features
--------
  * Playwright (Chromium) with a real browser context.
  * Optional rotating proxy support via 2Captcha proxies (2captcha.com/proxy,
    also sold under the 2prx.com name — same product, same gateways).
  * Connect to an existing antidetect/Scraping Browser via --cdp-endpoint
    instead of launching a bundled Chromium.
  * If a reCAPTCHA challenge is detected on ANY page (not just one specific
    URL — this check runs after every navigation), it is classified (v3 /
    v2-invisible / v2-checkbox) and solved through 2captcha (--twocaptcha-key).
    Over the Scraping Browser API you often need none of this: Captcha.setAutoSolve
    can clear the challenge inside the browser before this code gets a turn.
  * Output: JSON, CSV, or both.

Usage
-----
    python playwright_scraper.py \\
        --url "https://www.farfetch.com/shopping/kids/items.aspx" \\
        --pages 1 \\
        --format both \\
        --cdp-endpoint "ws://user:pass@cb.2captcha.com:9222"

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium
          && playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, RECAPTCHA_DISCOVERY_JS)
from product_parser import parse_products, SELECTORS
from output_writer import save
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ITEM_LINK_SELECTOR = SELECTORS["item_link"]
NEXT_PAGE_SELECTOR = "a[data-testid='pagination-next'], a[rel='next'], li.pagination-next a"

# How many product-link matches must appear before we treat the page as
# "actually loaded" rather than a lucky single match on an unrelated link
# that happens to share the URL shape (e.g. a "sizing guide" link). Learned
# the hard way on a previous single-site scraper in this family: waiting
# for just ONE match resolves in ~2s on an unrelated element, long before
# the real grid renders, giving a fast false-positive "ready" state.
MIN_CARD_MATCHES = 5


def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises `Page.content: Unable to retrieve content because the
    page is navigating and changing the content` if the document swaps under
    it. On farfetch.com that is not an edge case: the site geo-redirects
    client-side, so a snapshot taken right after goto() can land exactly on
    the swap. A live run lost an entire test to it — an unhandled exception out
    of the captcha check, before a single product was parsed.

    Retries briefly and returns None if the page won't hold still, so the
    caller can skip captcha detection instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — skipping "
                               "captcha detection for this navigation.", attempts)
                return None
            logger.info("Page is navigating (client-side redirect?) — retrying "
                        "content() in %dms (%d/%d).", pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def handle_captcha_if_present(page, args) -> None:
    """Runs after EVERY navigation, for ANY page — not scoped to one URL.

    Two detectors, in order:
      1. the static-HTML one, which is cheap and catches the old
         `<captcha-widget data-sitekey=...>` / inline-`grecaptcha.execute`
         markup;
      2. the runtime one, which reads the live page's reCAPTCHA client
         config. Needed because as of 2026-08-24 farfetch.com configures the
         widget purely in JS — no sitekey appears in the served HTML at all,
         so detector 1 finds nothing on a page that definitely has a
         reCAPTCHA. See captcha_solver.py for the full write-up.
    """
    html = _content_when_settled(page)
    if html is None:
        # Couldn't get a stable snapshot — skip captcha detection for this
        # navigation rather than taking the whole run down. The next
        # navigation gets another chance, and the product parse below reads
        # its own copy of the DOM.
        return
    # BOTH detectors run, always — not static-then-fallback. On farfetch.com
    # they disagree on the same page (see reconcile_detections), and the
    # static one is the less trustworthy of the two, so short-circuiting on
    # it would send the wrong parameters to 2captcha.
    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    token = solve_recaptcha(challenge, args.twocaptcha_key,
                           api_version=args.captcha_api,
                           min_score=args.min_score)
    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)


def scrape(args) -> None:
    all_products = []

    with sync_playwright() as pw:
        if args.cdp_endpoint:
            # Connect to an already-running browser (antidetect/Scraping
            # Browser) instead of launching Playwright's bundled Chromium.
            logger.info("Connecting to existing browser over CDP: %s", _mask_credentials(args.cdp_endpoint))
            # Explicit timeout. Playwright defaults to 30s here, but stating it
            # makes the contract visible next to the pyppeteer twin, which has
            # no connect timeout at all and had to grow one by hand after a
            # ten-minute hang. A Scraping Browser session that is still held
            # answers with HTTP 500 rather than stalling, so this mostly guards
            # against the endpoint going quiet.
            browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
            # Reuse the antidetect browser's existing context so its
            # fingerprint/session/proxy settings stay intact.
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            # RETROACTIVE ADDITION (added after this project was originally
            # delivered — found via 2captcha's own Scraping Browser API docs while
            # building a later project in this family:
            # https://2captcha.com/scraper/browser-api/api). A real,
            # documented CDP domain (`Captcha.setAutoSolve` / `Captcha.solve`)
            # that solves reCAPTCHA, Cloudflare Turnstile, and more,
            # automatically — but must be explicitly enabled per session.
            # Tried first when --cdp-endpoint is set; this script's own
            # regex-based detect+solve logic below still runs as a fallback
            # if this CDP domain isn't supported by whatever browser
            # --cdp-endpoint actually points at.
            try:
                cdp_session = context.new_cdp_session(page)
                cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
                cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
                cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
                cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
                cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
                logger.info("2captcha Scraping Browser API Captcha.setAutoSolve enabled — reCAPTCHA, "
                            "Turnstile, and other supported types will be solved automatically "
                            "if this --cdp-endpoint is a 2captcha Scraping Browser API session.")
            except Exception as e:
                logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                            "relying on this script's own detect+solve logic instead.", e)
        else:
            launch_kwargs = {"headless": args.headless}
            if args.proxy:
                parsed = urlparse(args.proxy)
                launch_kwargs["proxy"] = {
                    "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                    "username": parsed.username,
                    "password": parsed.password,
                }
                logger.info("Using 2Captcha proxy: %s:%s", parsed.hostname, parsed.port)

            browser = pw.chromium.launch(**launch_kwargs)
            # Only override the UA when we launched our own bundled Chromium.
            # Forcing a UA on a page reached via --cdp-endpoint mismatches
            # the antidetect browser's real TLS/JS fingerprint on purpose-
            # matched values — a mistake that broke a previous run in this
            # family with an Akamai "Access Denied."
            ctx_kwargs = {"user_agent": USER_AGENT, "locale": "en-US"}
            init_script = None
            if args.fingerprint:
                # Only meaningful on this branch. Over --cdp-endpoint the Scraping Browser
                # browser already has its own fingerprint, and layering a second
                # one on top produces a mismatch rather than better cover.
                from fingerprint_client import (get_fingerprint,
                                                playwright_context_kwargs,
                                                playwright_init_script)
                fp = get_fingerprint(args.twocaptcha_key,
                                     tags=args.fp_tags, country=args.fp_country)
                ctx_kwargs.update(playwright_context_kwargs(fp))
                init_script = playwright_init_script(fp)
                logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

            context = browser.new_context(**ctx_kwargs)
            if init_script:
                # Must be installed on the context, before any page script runs.
                context.add_init_script(init_script)
            page = context.new_page()

        url = args.url
        for page_num in range(1, args.pages + 1):
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except PWTimeout:
                logger.error("Timeout loading %s — skipping.", url)
                break

            handle_captcha_if_present(page, args)

            # Don't wait for network idle (retail sites never go fully
            # quiet) and don't accept a single selector match as "ready"
            # (see MIN_CARD_MATCHES comment above).
            try:
                page.wait_for_function(
                    f"document.querySelectorAll({ITEM_LINK_SELECTOR!r}).length > {MIN_CARD_MATCHES}",
                    timeout=20000,
                )
                page.wait_for_timeout(1000)
            except PWTimeout:
                logger.warning("No product markers appeared within 20s — "
                                "parsing whatever loaded (may be a bot-check/consent page).")

            html = page.content()

            # Dumping on success, not only on failure: a run can return the
            # right NUMBER of products with a field silently unpopulated, and
            # then the only way to tell a parsing bug from a too-early snapshot
            # is to inspect the exact bytes the parser was given.
            if args.dump_html:
                dump_path = (args.dump_html if args.pages == 1
                             else f"{args.dump_html}.page{page_num}")
                with open(dump_path, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info("Saved the snapshot the parser sees to %s "
                            "(%d bytes).", dump_path, len(html))

            products = parse_products(html, page.url, category=args.category)
            logger.info("Parsed %d products from page %d.", len(products), page_num)

            # A discounted listing where nothing came back discounted is the
            # signature of a snapshot taken before prices render. Say so at the
            # point it happens rather than leaving a column quietly empty.
            if products and not any(p.original_price for p in products):
                logger.debug("No product on page %d carried an original_price. "
                             "Expected on a full-price category; on a sale page "
                             "it means the prices had not rendered yet — re-run "
                             "with --dump-html to check.", page_num)

            if not products:
                debug_html = f"{args.out}_page{page_num}_debug.html"
                debug_png = f"{args.out}_page{page_num}_debug.png"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    page.screenshot(path=debug_png, full_page=True)
                except Exception as e:
                    logger.warning("Could not capture screenshot: %s", e)
                logger.warning("0 products parsed — saved what the browser actually saw to "
                                "%s and %s. Open the .png to see it.", debug_html, debug_png)

            all_products.extend(products)

            if page_num < args.pages:
                next_link = page.query_selector(NEXT_PAGE_SELECTOR)
                if not next_link:
                    logger.info("No further pagination link found — stopping early.")
                    break
                href = next_link.get_attribute("href")
                if not href:
                    break
                url = href if href.startswith("http") else page.url.split("?")[0] + href
                time.sleep(args.delay)

        if args.cdp_endpoint:
            page.close()  # leave the antidetect browser app itself running
        else:
            browser.close()

    return save(all_products, args.out, args.format, allow_empty=args.allow_empty)


def parse_args():
    p = argparse.ArgumentParser(description="Farfetch scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="Farfetch category/hub/search listing URL. Required, unless "
                        "FARFETCH_URL is set in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--pages", type=int, default=1, help="Number of listing pages to crawl")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="farfetch_products", help="Output file prefix")
    p.add_argument("--proxy", default=None, help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 (2captcha.com/proxy)")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 products were found. Off by "
                        "default so a failed run can't overwrite a good result with "
                        "an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint API and "
                        "apply it to the launched browser. Needs --twocaptcha-key. Ignored "
                        "with --cdp-endpoint, where the Scraping Browser supplies its own.")
    p.add_argument("--fp-tags", default="Windows,Chrome,Desktop",
                   help="Fingerprint filter tags (default: Windows,Chrome,Desktop)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to your proxy's "
                        "exit country — a US fingerprint on a German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current JSON API "
                        "(api.2captcha.com/createTask); v1 is the legacy in.php/res.php "
                        "pair. Default v2, with an automatic one-shot fallback to v1.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 — "
                        "the API only accepts these three). Ignored for v2 widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                    help="Connect to an already-running browser over CDP instead of launching "
                         "Playwright's bundled Chromium, e.g. ws://user:pass@host:port "
                         "e.g. the Scraping Browser API endpoint, or any antidetect "
                         "browser that exposes a CDP URL. "
                         "--proxy and --headless/--headful are ignored when this is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure. Useful when the product count is right "
                        "but a column comes back empty — see TROUBLESHOOTING.md.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url / --out from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and FARFETCH_URL is not set in the environment "
                "or in .env. Use a FILTERED category URL — the bare hub carries "
                "no product data.")
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API uses the "
                     "same key, though it's a separate subscription from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the Scraping Browser "
                       "supplies its own fingerprint, and stacking a second one on top "
                       "creates a mismatch rather than better cover.")
    try:
        sys.exit(scrape(args))
    except KeyboardInterrupt:
        sys.exit(1)
