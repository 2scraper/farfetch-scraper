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
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, RECAPTCHA_DISCOVERY_JS)
from product_parser import (parse_products, SELECTORS, detect_bot_challenge,
                            page_url)
from output_writer import dedupe_by_sku, finish_run
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")

def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chromium
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all actually report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page of a listing produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes. Two reasons, and the second is the point:
    dedupe that mutates a running set inside the loop makes the OUTPUT depend
    on the order pages happen to arrive in — fine while that order is fixed,
    wrong the moment pages are fetched concurrently, because which page
    "claims" a duplicate sku (and so which `scraped_at` the row carries)
    would vary between runs of the same command. Merging afterwards in page
    order is deterministic regardless of arrival order.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


def _plan_page_urls(page, args, page_one_url: str) -> Optional[List[str]]:
    """URLs for pages 2..N, decided once from page 1, or None to chain.

    Following the site's own next-link one page at a time is correct but
    strictly sequential: the address of page 5 is not knowable until page 4
    has been fetched. Constructing `?page=N` up front removes that chain —
    which is what makes fetching pages independently (and later,
    concurrently) possible at all.

    It is only safe when the site's own link AGREES with the convention, so
    that is checked rather than assumed: if page 1's next-link is not what
    `page_url()` would build for page 2, pagination is carrying something the
    convention cannot reproduce (a cursor, a token, a filter id) and the
    caller must keep chaining link to link. Returns None in that case.
    """
    if args.pages < 2:
        return None

    constructed = page_url(page_one_url, 2)
    next_link = page.query_selector(NEXT_PAGE_SELECTOR)
    href = next_link.get_attribute("href") if next_link else None

    if href:
        advertised = _resolve_pagination_url(page_one_url, href)
        if _same_url(advertised, constructed):
            logger.info("Pagination follows the ?page=N convention (page 2 "
                        "link matches the constructed URL) — planning pages "
                        "2-%d up front.", args.pages)
        else:
            logger.info("The site's own next-page link (%s) is not what the "
                        "?page= convention would build (%s) — following its "
                        "links one page at a time instead. Pages cannot be "
                        "fetched independently for this listing.",
                        advertised, constructed)
            return None
    else:
        logger.warning(
            "No pagination link matched %s on page 1 — falling back to the "
            "?page= URL convention. If this repeats, the site's markup has "
            "probably changed and NEXT_PAGE_SELECTOR needs updating.",
            NEXT_PAGE_SELECTOR)

    return [page_url(page_one_url, n) for n in range(2, args.pages + 1)]


def _next_url_from_page(page, args, page_num: int) -> str:
    """Next page's URL from the site's own link, falling back to ?page=N.

    Only used when pagination could not be planned up front. A missing link
    must not end the run: pagination resting entirely on DOM selectors is a
    silent-success failure waiting to happen (see #11), so the convention
    backs it up and the DATA decides when to stop.
    """
    next_link = page.query_selector(NEXT_PAGE_SELECTOR)
    href = next_link.get_attribute("href") if next_link else None
    if href:
        return _resolve_pagination_url(page.url, href)
    return page_url(page.url, page_num + 1)


def _same_url(a: str, b: str) -> bool:
    """URL equality that ignores query-parameter ORDER, which carries no meaning."""
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.netloc, pa.path.rstrip("/")) == \
           (pb.scheme, pb.netloc, pb.path.rstrip("/")) and \
           sorted(parse_qsl(pa.query)) == sorted(parse_qsl(pb.query))


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different exit — retrying it unchanged just
    spends the retry budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    Factored out of scrape() so a proxy rotation can tear the whole browser
    down and call this again. Swapping the proxy under a live session would
    be cheaper and wrong: cookies a bot manager issued against one exit,
    replayed from another, are a stronger signal than either address alone.
    A rotation therefore means a genuinely fresh browser — new cookie jar,
    new storage — which is what an ordinary user on a different network
    looks like.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    # Only override the UA when we launched our own bundled Chromium.
    # Forcing a UA on a page reached via --cdp-endpoint mismatches
    # the antidetect browser's real TLS/JS fingerprint on purpose-
    # matched values — a mistake that broke a previous run in this
    # family with an Akamai "Access Denied."
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": "en-US"}
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
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale. It also gives a worker thread a single object to own: with
    Playwright's sync API, a browser and everything reachable from it belong
    to the thread that created them, so each worker builds its own.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        A rotation must not reuse the session: see _launch_local for why
        carrying cookies across exits is worse than either exit alone. On a
        remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
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
    # regex-based detect+solve logic still runs as a fallback if this CDP
    # domain isn't supported by whatever --cdp-endpoint actually points at.
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
    return browser, context, page


def _resolve_pagination_url(base_url: str, href: str) -> str:
    """Resolve a pagination link's raw href against the page it came from.

    Playwright's get_attribute("href") returns the raw HTML attribute,
    unresolved — unlike the DOM .href property Puppeteer/Selenium read for
    the same purpose in this project, which the browser resolves for you.
    A hand-rolled "startswith('http') else base + href" got this wrong for
    an absolute-path href once the base URL's own query string was
    stripped first: base ".../items.aspx?page=1" + href
    "/shopping/kids/.../items.aspx?page=2" pasted into one broken, doubled
    URL. urljoin handles every shape correctly instead — absolute,
    protocol-relative, absolute-path, and page-relative hrefs alike.
    """
    return urljoin(base_url, href)

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


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a challenge page, a dead exit are all recorded on the outcome
    instead. What the run should do about them differs between the sequential
    and concurrent paths, so that decision belongs to the caller rather than
    to a raised exception unwinding through it.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried from a DIFFERENT exit.
    # Zero without a pool: there is nowhere else to go, and a bare retry from
    # the same address just burns it further.
    block_retries = args.proxy_block_retries if (pool and len(pool) > 1) else 0
    html, vendor, load_failed = None, None, False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it.
        # One network flap on page 12 of 50 used to break the loop, and
        # scraper_api_client.py has had --retries all along — the same
        # transient deserved the same treatment here.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter let it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list.
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        handle_captcha_if_present(session.page, args)

        # Don't wait for network idle (retail sites never go fully
        # quiet) and don't accept a single selector match as "ready"
        # (see MIN_CARD_MATCHES comment above).
        try:
            session.page.wait_for_function(
                f"document.querySelectorAll({ITEM_LINK_SELECTOR!r}).length > {MIN_CARD_MATCHES}",
                timeout=20000,
            )
            session.page.wait_for_timeout(1000)
        except PWTimeout:
            logger.warning("No product markers appeared within 20s — "
                            "parsing whatever loaded (may be a bot-check/consent page).")

        html = session.page.content()
        vendor = detect_bot_challenge(html)
        if not vendor:
            break

        # Blocked. A different exit is the one thing that plausibly changes
        # the outcome — the address is what was scored, so retrying from it
        # unchanged would only confirm the block.
        if block_attempt < block_retries:
            logger.warning("Blocked by %s on page %d from %s — retrying "
                           "from another exit (%d/%d).", vendor, page_num,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"blocked by {vendor} on page {page_num}")
            session.relaunch()

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    # Dumping on success, not only on failure: a run can return the
    # right NUMBER of products with a field silently unpopulated, and
    # then the only way to tell a parsing bug from a too-early snapshot
    # is to inspect the exact bytes the parser was given.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by a %s challenge page before parsing (%d bytes) — "
                     "saved to %s%s. This is exit 3, distinct from a genuinely "
                     "empty category (exit 4).", vendor, len(html), debug_html,
                     f" (tried {block_retries + 1} exit(s))" if block_retries else "")
        outcome.blocked_by = vendor
        return outcome

    products = parse_products(html, session.page.url, category=args.category)
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
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 products parsed — saved what the browser actually saw to "
                        "%s and %s. Open the .png to see it.", debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset. Two things fall out of that, both wanted:

      * Workers start on distinct exits, which is the point of running
        several — N workers all leaving from one address is just a faster
        way to burn that address.
      * No shared mutable state between threads, so rotation needs no lock.
        A worker that gets blocked can still walk the rest of the pool on
        its own.

    Its exit stays put for the worker's lifetime otherwise: the invariant
    from _launch_local is that a SESSION must not change address mid-flight,
    and a worker is one session.
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one
    across threads is not an option even if it were desirable.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a page comes back with no products at all — the end of the
    # listing. Without it, asking for 50 pages of a 5-page category would
    # fetch 45 empty ones. Workers check it before taking more work, so at
    # most (concurrency - 1) extra pages are in flight when it trips.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args, _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and not outcome.products:
                            logger.info("[%s] page %d returned no products — "
                                        "treating that as the end of the listing "
                                        "and stopping dispatch.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted (a worker died, or dispatch
    # stopped at the end of the listing). Not reported as failed pages: they
    # were not tried, and claiming otherwise would overstate the damage.
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> None:
    # One entry per page attempted, merged after the loop rather than folded
    # into shared state during it — see PageOutcome for why that ordering
    # matters more than it looks.
    outcomes: List[PageOutcome] = []
    seen_skus = set()
    blocked = False
    # Why the loop ended. "completed" means every requested page was
    # fetched; "pagination_exhausted" means the site itself ran out of pages
    # (also a complete result — there was nothing more to get). Anything else
    # is an early stop, and the run is only a partial view of the category.
    stop_reason = "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.cdp_endpoint:
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, and several workers would collide on it "
                           "(profile_locked). Use several pids instead, one run "
                           "each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster way "
                           "to get that address scored than to gather data. Pass "
                           "--proxy-file to spread the load.", concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit "
                        "for its lifetime, which is the same spread without a "
                        "browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each). Make sure the machine has the "
                           "memory for it.", concurrency, concurrency)

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            # Page 1 is always fetched on its own: its content is what decides
            # whether pages 2..N can be addressed independently at all.
            first = _fetch_one_page(session, args, pool, 1, args.url)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            else:
                seen_skus.update(p.sku for p in first.products if p.sku is not None)
                planned = _plan_page_urls(session.page, args, first.final_url)

                if args.pages > 1 and concurrency > 1 and planned is None:
                    logger.warning("--concurrency %d requested, but this listing's "
                                   "pagination cannot be addressed independently "
                                   "(see above) — falling back to one page at a "
                                   "time.", concurrency)
                    concurrency = 1

                if args.pages > 1 and concurrency > 1:
                    # Close the page-1 browser before starting workers: it has
                    # done its job, and holding it open would cost one more
                    # browser than asked for.
                    session.close()
                    specs = [(n, planned[n - 2]) for n in range(2, args.pages + 1)]
                    logger.info("Fetching pages 2-%d across %d workers%s.",
                                args.pages, concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency)
                    outcomes.extend(rest)

                    failed = [o for o in rest if not o.ok]
                    if failed:
                        worst = min(failed, key=lambda o: o.page_num)
                        stop_reason = ("page_load_timeout" if worst.load_failed
                                       else f"blocked_{worst.blocked_by}")
                        blocked = any(o.blocked_by for o in rest)
                    elif exhausted:
                        stop_reason = "no_new_products"
                    elif unattempted:
                        # Should not happen without a failure or exhaustion,
                        # but say so rather than reporting a complete run.
                        stop_reason = "pages_unattempted"
                    session = None  # already closed
                else:
                    url = (planned[0] if planned else
                           _next_url_from_page(session.page, args, 1))
                    for page_num in range(2, args.pages + 1):
                        # A new exit per page is what actually spreads a run's
                        # volume, and it costs a browser relaunch: see
                        # _launch_local for why carrying the session across
                        # exits would defeat the point.
                        if pool and pool.rotates_per_page():
                            pool.advance(f"per-page rotation, page {page_num}")
                            session.relaunch()

                        outcome = _fetch_one_page(session, args, pool, page_num, url)
                        outcomes.append(outcome)
                        if not outcome.ok:
                            stop_reason = ("page_load_timeout" if outcome.load_failed
                                           else f"blocked_{outcome.blocked_by}")
                            blocked = outcome.blocked_by is not None
                            break

                        # Whether this page contributed anything not already
                        # seen. Kept as a running check because the condition is
                        # inherently sequential — "new" only means anything
                        # relative to the pages before it. The authoritative
                        # dedupe happens once, after the loop, in page order.
                        fresh_count = sum(1 for p in outcome.products
                                          if p.sku is None or p.sku not in seen_skus)
                        seen_skus.update(p.sku for p in outcome.products
                                         if p.sku is not None)

                        # A page past the first that contributes nothing new
                        # means the end of the catalogue — or that pagination is
                        # looping back on itself. Either way there is nothing
                        # further to fetch, and this is the honest terminating
                        # condition: it is a property of the DATA, not of a CSS
                        # selector that may have been renamed.
                        if not fresh_count:
                            logger.info("Page %d added no products not already "
                                        "seen — treating that as the end of the "
                                        "listing.", page_num)
                            stop_reason = "no_new_products"
                            break

                        if page_num < args.pages:
                            url = (planned[page_num - 1] if planned else
                                   _next_url_from_page(session.page, args, page_num))
                            time.sleep(args.delay)
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    # At one page at a time the two are identical, which is the point: this
    # is what keeps the output byte-for-byte the same while removing the
    # dependency on arrival order that concurrency would otherwise introduce.
    all_products = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_sku(oc.products, merged_seen)
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d product(s) already seen on an "
                        "earlier page.", oc.page_num, len(oc.products) - len(fresh))
        all_products.extend(fresh)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    return finish_run(all_products, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages,
                      start_url=args.url, final_url=final_url)


def parse_args():
    p = argparse.ArgumentParser(description="Farfetch scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="Farfetch category/hub/search listing URL. Required, unless "
                        "FARFETCH_URL is set in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--pages", type=int, default=1, help="Number of listing pages to crawl")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1 — "
                        "unchanged sequential behaviour). Each worker runs its "
                        "own browser and holds its own proxy exit, so N>1 "
                        "without --proxy-file just sends N times the traffic "
                        "from one address. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). A "
                        "single network flap mid-run should not end a 50-page "
                        "job; the pause between attempts doubles each time.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="farfetch_products", help="Output file prefix")
    p.add_argument("--proxy", default=None, help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 (2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and blank "
                        "lines skipped) to rotate across. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit for every page — this is what spreads volume, "
                        "and it relaunches the browser each time so the session "
                        "does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not "
                        "all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back as a bot-challenge, retry it from "
                        "this many OTHER exits before giving up (default 2). "
                        "Needs a pool of more than one; ignored otherwise.")
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
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(1)
