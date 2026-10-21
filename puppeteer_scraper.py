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
import time
from urllib.parse import urlparse

from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            SolveBudget,
                            INJECT_TOKEN_JS, RECAPTCHA_DISCOVERY_JS)
from product_parser import (parse_products, SELECTORS, detect_bot_challenge,
                            describe_block, page_url,
                            count_product_links)
from output_writer import (dedupe_by_sku, finish_run, new_run_id,
                           stop_reason_for, EXIT_FETCH_FAILED,
                           write_failed_attempt)
from proxy_pool import mask as mask_proxy
from proxy_pool import (redact_url_credentials, describe_cdp_failure,
                        cdp_failure_is_transient, CDP_CONNECT_ATTEMPTS,
                        CDP_CONNECT_PAUSE_S,
                        precheck_proxy, robots_url, precheck_message,
                        ProxyPrecheckError, PROXY_PRECHECK_STOP_REASON,
                        should_block_media, pyppeteer_block_media)
import env_config
import proxy_meter
from arg_types import positive_int, nonneg_float

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer bundled
    Chromium ships, and a UA claiming an older Chrome than what the JS
    engine, WebGL strings and TLS ClientHello all actually report is itself a
    mismatch a fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


ITEM_LINK_SELECTOR = SELECTORS["item_link"]
# Ordered most-durable first. `<link rel="next">` in <head> is a W3C/SEO
# convention rather than a build artefact, and on 2026-09-07 it was the ONLY
# one of these that farfetch.com actually served: the three anchor selectors
# below (which this project shipped with) matched nothing at all, so a
# multi-page run silently returned page 1 and reported success. Kept anyway —
# they cost nothing and the visible UI may come back — but the standards-based
# one leads, and product_parser.page_url() backs all of them up.
NEXT_PAGE_SELECTOR = ("link[rel='next'], a[data-testid='pagination-next'], "
                      "a[rel='next'], li.pagination-next a")
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


async def handle_captcha_if_present(page, args, budget=None) -> None:
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

    # Detected is not the same as blocking — see playwright_scraper.py for the
    # full reasoning. Counting the links is instant, so this costs nothing.
    if getattr(args, "solve_captcha", "when-blocked") == "when-blocked":
        visible = len(await page.querySelectorAll(ITEM_LINK_SELECTOR))
        if visible > MIN_CARD_MATCHES:
            logger.info("%s detected via %s, but %d product links are already "
                        "on the page — not solving it. Pass --solve-captcha "
                        "always to solve it anyway.",
                        challenge.kind, challenge.source, visible)
            return

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)

    # A captcha this run cannot solve must not take the run down with it.
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds. If it was blocking, the run reports exit 3.")
        return
    # Checked BEFORE the purchase, against an allowance the caller holds
    # for the whole page, so retries and exit rotations cannot each buy
    # another token. Without a caller's budget, this call alone may buy one.
    if not (budget if budget is not None else SolveBudget()).take():
        return
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return

    await page.evaluate(f"({INJECT_TOKEN_JS})", token)
    logger.info("Token injected. Reloading page to continue.")
    await asyncio.sleep(1.5)
    await page.reload({"waitUntil": "domcontentloaded", "timeout": 60000})


CDP_CONNECT_TIMEOUT_S = 30


async def _connect_remote(args):
    """connect() with a timeout, a short retry and a credential-free error.

    pyppeteer's connect() has no timeout of its own and waits on a future a
    rejected WebSocket handshake may never resolve, so it is bounded here.
    A refused connect returns None and the run reports exit 5 (content never
    obtained) instead of a traceback whose text carries the endpoint.
    """
    for attempt in range(1, CDP_CONNECT_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(
                connect(browserWSEndpoint=args.cdp_endpoint),
                timeout=CDP_CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            text = f"no answer within {CDP_CONNECT_TIMEOUT_S}s"
        except Exception as e:  # noqa: BLE001 — reported, never re-raised raw
            text = str(e) or type(e).__name__
        if cdp_failure_is_transient(text) and attempt < CDP_CONNECT_ATTEMPTS:
            logger.warning("CDP connect refused (%s) — retrying in %.0fs (%d/%d).",
                           redact_url_credentials(text).splitlines()[0],
                           CDP_CONNECT_PAUSE_S, attempt, CDP_CONNECT_ATTEMPTS)
            await asyncio.sleep(CDP_CONNECT_PAUSE_S)
            continue
        logger.error("%s", describe_cdp_failure(text))
        return None
    return None


async def scrape(args) -> int:
    # One id per run, logged here and written into the sidecar, so a
    # log line and an artefact can be tied together. "the run that
    # failed" is not identifying for a scraper on a schedule.
    run_id = new_run_id()
    started_at = time.time()
    logger.info("Run %s starting: %s", run_id, args.url)
    all_products = []
    seen_skus = set()
    blocked = False
    # See playwright_scraper.py for what these mean; kept identical across
    # the three engines on purpose so the status/exit code cannot differ by
    # which engine you happened to run.
    stop_reason = "completed"
    pages_completed = 0
    final_url = args.url

    # Meters what goes through a LOCAL proxy and stops at --proxy-budget-mb.
    proxy_meter.start(getattr(args, "proxy_budget_mb", None), active=bool(args.proxy) and not args.cdp_endpoint)
    if args.proxy and not args.cdp_endpoint:
        # One HEAD for robots.txt through the proxy BEFORE the browser
        # starts — see playwright_scraper.py. Only a gateway 407/402 stops.
        try:
            precheck_proxy(args.proxy, robots_url(args.url), timeout=20)
        except ProxyPrecheckError as exc:
            print(precheck_message(exc, "FARFETCH_PROXY", "farfetch.com"),
                  file=sys.stderr)
            write_failed_attempt(args.out, stop_reason=PROXY_PRECHECK_STOP_REASON,
                                 start_url=args.url, pages_requested=args.pages,
                                 transport_error="proxy pre-check: %s" % exc)
            return EXIT_FETCH_FAILED

    if args.cdp_endpoint:
        logger.info("Connecting to existing browser over CDP: %s", _mask_credentials(args.cdp_endpoint))
        browser = await _connect_remote(args)
        if browser is None:
            return EXIT_FETCH_FAILED
    else:
        launch_args = {
            "headless": args.headless,
            "args": ["--disable-blink-features=AutomationControlled", "--window-size=1366,900"],
        }
        if args.proxy:
            # Only scheme://host:port goes on the command line. The old code
            # appended the whole --proxy value, credentials included, which
            # put them in the browser process's argv for anything that can
            # run `ps` — and logged them verbatim on the next line. Unlike
            # Selenium, pyppeteer CAN authenticate properly: page.authenticate
            # below sends them over CDP instead, so nothing is lost by
            # stripping them here.
            proxy_parts = urlparse(args.proxy)
            proxy_port = f":{proxy_parts.port}" if proxy_parts.port else ""
            launch_args["args"].append(
                f"--proxy-server={proxy_parts.scheme}://{proxy_parts.hostname}{proxy_port}")
            logger.info("Using proxy %s", mask_proxy(args.proxy))
        browser = await launch(**launch_args)

    pages = await browser.pages()
    page = pages[0] if pages else await browser.newPage()

    if args.proxy and not args.cdp_endpoint:
        creds = urlparse(args.proxy)
        if creds.username or creds.password:
            # Over CDP (Network.setExtraHTTPHeaders' proper cousin), not on a
            # command line: this is the supported way to authenticate a proxy
            # in Chromium, and it keeps the password out of argv.
            await page.authenticate({"username": creds.username or "",
                                     "password": creds.password or ""})
            logger.info("Proxy credentials sent over CDP, not via the command line.")
    if args.proxy and not args.cdp_endpoint:
        await proxy_meter.current().attach_pyppeteer(page)  # --proxy-budget-mb
    if should_block_media(args, proxy_in_use=bool(args.proxy),
                          over_cdp=bool(args.cdp_endpoint)):
        # CDP Fetch with resource-type patterns: only images, media and fonts
        # are paused, and each is failed. setRequestInterception relies on a
        # CDP method current Chromium no longer implements.
        await pyppeteer_block_media(page)
        logger.info("Blocking images/media/fonts "
                    "(--load-media keeps them).")
    if not args.cdp_endpoint:
        # Only override the UA when we launched our own bundled Chromium —
        # see playwright_scraper.py for why this matters when connected via
        # --cdp-endpoint (mismatched fingerprint got a previous run flagged).
        # browser.version() returns CDP's raw product string, e.g.
        # "HeadlessChrome/124.0.6367.60" — take the real version number after
        # the slash rather than assuming a fixed one.
        raw_version = await browser.version()
        chromium_version = raw_version.rsplit("/", 1)[-1]
        await page.setUserAgent(_chrome_ua(chromium_version))
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
            if proxy_meter.exceeded():
                break  # --proxy-budget-mb is spent; finish_run names the stop
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            # Retry a navigation failure rather than ending the run on it —
            # see playwright_scraper.py for why.
            http_status = None
            for attempt in range(1, args.retries + 1):
                if proxy_meter.exceeded():
                    load_failed = True
                    break
                try:
                    # Keep the document's Response: its status is the one
                    # signal a refusing edge always gives, and without it an
                    # HTTP 403 with an unfamiliar body read as an empty
                    # category (exit 4) here while Playwright said exit 5.
                    resp = await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 60000})
                    http_status = resp.status if resp else None
                    load_failed = False
                    break
                except Exception:
                    load_failed = True
                    if attempt < args.retries:
                        pause = args.retry_delay * (2 ** (attempt - 1))
                        logger.warning("Timeout/error loading %s (attempt %d/%d) — "
                                       "retrying in %.1fs.", url, attempt,
                                       args.retries, pause)
                        await asyncio.sleep(pause)
            if load_failed:
                logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
                stop_reason = "page_load_timeout"
                break

            await handle_captcha_if_present(page, args, budget=SolveBudget())

            if http_status is not None and http_status >= 400:
                logger.warning("HTTP %d for %s — not waiting for products to "
                               "paint.", http_status, url)
            else:
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

            vendor = detect_bot_challenge(html)
            if vendor:
                debug_html = f"{args.out}_page{page_num}_debug.html"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    await page.screenshot({"path": f"{args.out}_page{page_num}_debug.png", "fullPage": True})
                except Exception as e:
                    logger.warning("Could not capture screenshot: %s", e)
                logger.error("Blocked by %s before parsing (%d bytes) — "
                             "saved to %s. This is exit 3, distinct from a genuinely "
                             "empty category (exit 4).", describe_block(html, vendor), len(html), debug_html)
                blocked = True
                stop_reason = f"blocked_{vendor}"
                break

            if http_status is not None and http_status >= 400:
                # An error status whose body names no vendor we recognise.
                # Still not the listing, and "0 products" about it would be a
                # claim about the catalogue this run cannot support — the
                # same reading the Playwright engine gives it.
                debug_html = f"{args.out}_page{page_num}_debug.html"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.error("HTTP %d for %s and nothing on the page names a "
                             "known bot-check vendor — saved %d bytes to %s. "
                             "Reported as a fetch failure, not an empty "
                             "category.", http_status, url, len(html), debug_html)
                stop_reason = stop_reason_for(load_failed=False, blocked_by=None,
                                              http_status=http_status)
                break

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
                linked = count_product_links(html)
                if linked >= MIN_CARD_MATCHES:
                    # A page that LINKS to products and parses to none is a
                    # broken parser, not an empty category — named as such,
                    # as the Playwright engine names it.
                    logger.error("PARSER DRIFT: the page links to %d product(s) "
                                 "and the parser extracted 0. Saved what the "
                                 "browser saw to %s and %s.", linked,
                                 debug_html, debug_png)
                    stop_reason = stop_reason_for(load_failed=False,
                                                  blocked_by=None,
                                                  parse_drift=True)
                    break
                logger.warning("0 products parsed — saved what the browser actually saw to "
                                "%s and %s.", debug_html, debug_png)

            fresh = dedupe_by_sku(products, seen_skus)
            if len(fresh) < len(products):
                logger.info("Dropped %d duplicate product(s) already seen on an earlier page.",
                            len(products) - len(fresh))
            all_products.extend(fresh)
            pages_completed = page_num
            final_url = page.url

            # See playwright_scraper.py: a data-based terminating condition,
            # not a selector-based one.
            if page_num > 1 and not fresh:
                logger.info("Page %d added no products not already seen — "
                            "treating that as the end of the listing.", page_num)
                stop_reason = "no_new_products"
                break

            if page_num < args.pages:
                next_href = await page.evaluate(
                    f"""() => {{
                        const el = document.querySelector("{NEXT_PAGE_SELECTOR}");
                        return el ? el.href : null;
                    }}"""
                )
                if next_href:
                    url = next_href
                else:
                    url = page_url(page.url, page_num + 1)
                    logger.warning(
                        "No pagination link matched — falling back to the "
                        "?page= URL convention (%s). If this repeats, the "
                        "site's markup has probably changed.", url)
                await asyncio.sleep(args.delay)
    finally:
        if args.cdp_endpoint:
            await page.close()  # leave the remote browser running
            await browser.disconnect()
        else:
            await browser.close()

    return finish_run(all_products, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      run_id=run_id, started_at=started_at,
                      webhook=args.webhook,
                      pages_requested=args.pages, pages_completed=pages_completed,
                      start_url=args.url, final_url=final_url)


def parse_args():
    p = argparse.ArgumentParser(description="Farfetch scraper (Puppeteer/pyppeteer edition)")
    p.add_argument("--url", default=None,
                   help="Farfetch category/hub/search listing URL. Required, unless "
                        "FARFETCH_URL is set in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--pages", type=positive_int, default=1, help="Number of listing pages to crawl")
    p.add_argument("--delay", type=nonneg_float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--retries", type=positive_int, default=3,
                   help="Attempts per page load before giving up (default 3)")
    p.add_argument("--retry-delay", type=nonneg_float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="farfetch_products", help="Output file prefix")
    p.add_argument("--proxy", default=None, help="Proxy URL, e.g. http://HOST:9999 (2captcha.com/proxy)")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--load-media", action="store_true",
                   help="Load images, video and web fonts. By default they are "
                        "aborted while a LOCAL browser goes through --proxy "
                        "(no parser reads them, and a proxy bills per byte); "
                        "never blocked with --cdp-endpoint or with no proxy.")
    p.add_argument("--force-block-media", action="store_true", help=argparse.SUPPRESS)
    proxy_meter.add_budget_argument(p)
    p.add_argument("--webhook", default=None, metavar="URL",
                   help="POST the run summary (the same fields as the "
                        ".meta.json sidecar, plus the exit code) to this "
                        "URL when the run finishes — including when it "
                        "fails, which is the case worth being told about. "
                        "Never fails the run, never logged (the URL is "
                        "usually the credential). Prefer FARFETCH_WEBHOOK "
                        "in .env over this flag: argv is readable by "
                        "anything that can run ps.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 products were found. Off by "
                        "default so a failed run can't overwrite a good result; exit "
                        "code is 4 either way.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current JSON API "
                        "(api.2captcha.com/createTask); v1 is the legacy in.php/res.php "
                        "pair. Default v2, with an automatic one-shot fallback to v1.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a challenge "
                        "if the catalogue is not already readable. always: "
                        "solve whenever one is detected.")
    p.add_argument("--min-score", type=float, default=0.7,
                   choices=[0.3, 0.7, 0.9],
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


def main() -> int:
    """The entry point, as a callable — see playwright_scraper.main().

    asyncio.run() is inside main() rather than around it so the console
    script entry point is an ordinary sync callable, which is what a
    [project.scripts] target has to be.
    """
    args = parse_args()
    try:
        return asyncio.run(scrape(args))
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
