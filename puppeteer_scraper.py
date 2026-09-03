#!/usr/bin/env python3
"""
farfetch-scraper — Puppeteer edition (via pyppeteer)
========================================================

Same feature set as the Playwright / Selenium versions — see
playwright_scraper.py for the full docstring on strategy and known
Farfetch quirks. Kept in Python via Pyppeteer (Puppeteer's Python port).

Usage
-----
    python puppeteer_scraper.py \\
        --url "https://www.farfetch.com/shopping/kids/items.aspx" \\
        --pages 1 --format both \\
        --cdp-endpoint "ws://user:pass@cb.2captcha.com:9222"

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
Note: pyppeteer downloads its own bundled Chromium on first run, unless
you're using --cdp-endpoint (in which case it's never needed).
"""

import argparse
import asyncio
import logging
import sys

from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, RECAPTCHA_DISCOVERY_JS)
from product_parser import parse_products, SELECTORS
from output_writer import save
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ITEM_LINK_SELECTOR = SELECTORS["item_link"]
NEXT_PAGE_SELECTOR = "a[data-testid='pagination-next'], a[rel='next'], li.pagination-next a"
MIN_CARD_MATCHES = 5  # see playwright_scraper.py for why this must be >1, not just present


def _mask_credentials(url: str) -> str:
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


async def _content_when_settled(page, attempts: int = 4, pause: float = 0.7):
    """content() that tolerates a page mid-navigation — see the Playwright
    twin for why this is necessary on farfetch.com (client-side geo-redirect
    swapping the document out from under the snapshot)."""
    for attempt in range(1, attempts + 1):
        try:
            return await page.content()
        except Exception as e:  # noqa: BLE001 — pyppeteer raises plain Exception here
            if "navigat" not in str(e).lower() and "context" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — skipping "
                               "captcha detection for this navigation.", attempts)
                return None
            await asyncio.sleep(pause)
    return None


async def handle_captcha_if_present(page, args) -> None:
    """Runs after EVERY navigation, for ANY page — not scoped to one URL."""
    html = await _content_when_settled(page)
    if html is None:
        return
    # Both detectors, always — see reconcile_detections in captcha_solver.py.
    # detect_recaptcha_in_page takes a SYNC callable, so the async evaluate is
    # driven to completion here and handed over as a value.
    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = None
    try:
        info = await page.evaluate(f"({RECAPTCHA_DISCOVERY_JS})()", force_expr=True)
    except Exception as e:
        logger.debug("In-page reCAPTCHA discovery failed: %s", e)
        info = None
    if info:
        runtime_challenge = detect_recaptcha_in_page(lambda _js: info, page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    token = solve_recaptcha(challenge, args.twocaptcha_key,
                           api_version=args.captcha_api,
                           min_score=args.min_score)
    await page.evaluate(f"({INJECT_TOKEN_JS})", token)
    logger.info("Token injected. Reloading page to continue.")
    await asyncio.sleep(1.5)
    await page.reload({"waitUntil": "domcontentloaded", "timeout": 60000})


async def scrape(args) -> None:
    all_products = []

    if args.cdp_endpoint:
        logger.info("Connecting to existing browser over CDP: %s", _mask_credentials(args.cdp_endpoint))
        browser = await connect(browserWSEndpoint=args.cdp_endpoint)
    else:
        launch_args = {
            "headless": args.headless,
            "args": ["--disable-blink-features=AutomationControlled", "--window-size=1366,900"],
        }
        if args.proxy:
            launch_args["args"].append(f"--proxy-server={args.proxy}")
            logger.info("Using 2Captcha proxy: %s", args.proxy)
        browser = await launch(**launch_args)

    pages = await browser.pages()
    page = pages[0] if pages else await browser.newPage()
    if not args.cdp_endpoint:
        # Only override the UA when we launched our own bundled Chromium —
        # see playwright_scraper.py for why this matters when connected via
        # --cdp-endpoint (mismatched fingerprint got a previous run flagged).
        await page.setUserAgent(USER_AGENT)
    else:
        # RETROACTIVE ADDITION — see playwright_scraper.py in this same
        # project for the full story. Tried first when --cdp-endpoint is
        # set; this script's own regex-based logic runs as a fallback.
        try:
            cdp_session = await page.target.createCDPSession()
            await cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
            cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
            cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
            cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
            logger.info("2captcha Scraping Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:
            logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                        "relying on this script's own detect+solve logic instead.", e)

    try:
        url = args.url
        for page_num in range(1, args.pages + 1):
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            try:
                await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 60000})
            except Exception:
                logger.error("Timeout/error loading %s — skipping.", url)
                break

            await handle_captcha_if_present(page, args)

            try:
                await page.waitForFunction(
                    f"document.querySelectorAll('{ITEM_LINK_SELECTOR}').length > {MIN_CARD_MATCHES}",
                    {"timeout": 20000},
                )
                await asyncio.sleep(1)
            except Exception:
                logger.warning("No product markers appeared within 20s — "
                                "parsing whatever loaded (may be a bot-check/consent page).")

            html = await page.content()
            products = parse_products(html, page.url, category=args.category)
            logger.info("Parsed %d products from page %d.", len(products), page_num)

            if not products:
                debug_html = f"{args.out}_page{page_num}_debug.html"
                debug_png = f"{args.out}_page{page_num}_debug.png"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    await page.screenshot({"path": debug_png, "fullPage": True})
                except Exception as e:
                    logger.warning("Could not capture screenshot: %s", e)
                logger.warning("0 products parsed — saved what the browser actually saw to "
                                "%s and %s.", debug_html, debug_png)

            all_products.extend(products)

            if page_num < args.pages:
                next_href = await page.evaluate(
                    f"""() => {{
                        const el = document.querySelector("{NEXT_PAGE_SELECTOR}");
                        return el ? el.href : null;
                    }}"""
                )
                if not next_href:
                    logger.info("No further pagination link found — stopping early.")
                    break
                url = next_href
                await asyncio.sleep(args.delay)
    finally:
        if args.cdp_endpoint:
            await page.close()  # leave the remote/antidetect browser running
            await browser.disconnect()
        else:
            await browser.close()

    return save(all_products, args.out, args.format, allow_empty=args.allow_empty)


def parse_args():
    p = argparse.ArgumentParser(description="Farfetch scraper (Puppeteer/pyppeteer edition)")
    p.add_argument("--url", default=None,
                   help="Farfetch category/hub/search listing URL. Required, unless "
                        "FARFETCH_URL is set in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--pages", type=int, default=1, help="Number of listing pages to crawl")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="farfetch_products", help="Output file prefix")
    p.add_argument("--proxy", default=None, help="Proxy URL, e.g. http://HOST:9999 (2captcha.com/proxy)")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 products were found. Off by "
                        "default so a failed run can't overwrite a good result; exit "
                        "code is 4 either way.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current JSON API "
                        "(api.2captcha.com/createTask); v1 is the legacy in.php/res.php "
                        "pair. Default v2, with an automatic one-shot fallback to v1.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 — "
                        "the API only accepts these three). Ignored for v2 widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                    help="Connect to an already-running browser over CDP instead of launching "
                         "pyppeteer's bundled Chromium, e.g. ws://user:pass@host:port. "
                         "--proxy and --headless/--headful are ignored when this is set.")
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
    try:
        sys.exit(asyncio.run(scrape(args)))
    except KeyboardInterrupt:
        sys.exit(1)
