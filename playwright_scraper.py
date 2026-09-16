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
import os
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
                            INJECT_TOKEN_JS)
from product_parser import (parse_products, SELECTORS, detect_bot_challenge,
                            describe_block, page_url,
                            count_product_links)
from product_detail_parser import parse_product_detail
from output_writer import (dedupe_by_sku, finish_run, stop_reason_for,
                           COMPLETE_STOP_REASONS, new_run_id,
                           ProductVariant)
from run_state import Checkpoint
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config
from arg_types import positive_int, nonneg_int, nonneg_float

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
    # The response status for the document itself, where the driver exposes
    # one (Playwright and pyppeteer return a Response from goto(); Selenium
    # has no equivalent without a CDP session, so it stays None there and the
    # HTML markers carry the whole job — see CLAUDE.md §8 on needing a
    # STRUCTURAL secondary signal for exactly that case).
    http_status: Optional[int] = None
    # Chromium's own error text when the EXIT was unusable
    # (ERR_PROXY_CONNECTION_FAILED and friends), as opposed to the site being
    # slow. Recorded rather than folded into load_failed because the two want
    # opposite responses — another try at the same exit vs. a different exit —
    # and the run metadata should say which one happened.
    proxy_failure: Optional[str] = None
    # The page was served, carried product links, and the parser still
    # returned nothing. That is not an empty category — it is this repo's
    # bug, and the two deserve opposite responses from whoever reads the run.
    parse_drift: bool = False

    @property
    def http_error(self) -> bool:
        """The edge answered, with an error. Whatever came back is not the
        listing, even when no vendor marker names who refused us — an
        unrecognised 403 body is still a 403."""
        return self.http_status is not None and self.http_status >= 400

    @property
    def ok(self) -> bool:
        return (not self.load_failed and self.blocked_by is None
                and not self.http_error)


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

    # Detected is not the same as blocking. This site carries a reCAPTCHA in
    # its sign-up modal that has nothing to do with the catalogue, so a
    # detection on a page whose products are already present means the
    # challenge is not standing between us and anything. Counting the links
    # is instant — no wait_for_function, no 20s — which is why the check can
    # sit here rather than forcing the readiness wait to run first (doing
    # that would cost 20 wasted seconds on a page the captcha genuinely
    # gates, where solving first is what makes the products appear).
    if getattr(args, "solve_captcha", "when-blocked") == "when-blocked":
        visible = len(page.query_selector_all(ITEM_LINK_SELECTOR))
        if visible > MIN_CARD_MATCHES:
            logger.info("%s detected via %s, but %d product links are already "
                        "on the page — not solving it. The catalogue is not "
                        "what this challenge is guarding. Pass "
                        "--solve-captcha always to solve it anyway.",
                        challenge.kind, challenge.source, visible)
            return

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)

    # A captcha this run cannot solve must not take the run down with it. The
    # products may well be readable anyway, and a traceback in place of them
    # is strictly worse than a warning: the missing key raised RuntimeError
    # out of solve_recaptcha, through here, and out of scrape().
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds. Pass --twocaptcha-key or set TWOCAPTCHA_KEY if "
                       "the run comes back blocked (exit 3).")
        return
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds. If it was in fact blocking, "
                     "the run will report that as exit 3.", e)
        return

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
    http_status = None

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it.
        # One network flap on page 12 of 50 used to break the loop, and
        # scraper_api_client.py has had --retries all along — the same
        # transient deserved the same treatment here.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                # goto() hands back the Response for the document itself.
                # It was being discarded, and with it the single most
                # reliable signal a refusing edge gives us: Farfetch's
                # "Access Denied" page arrives under HTTP 403 (measured
                # 2026-09-14), while every marker on it was unknown to the
                # parser. Taking the status first is CLAUDE.md §8.
                resp = session.page.goto(url, wait_until="domcontentloaded",
                                         timeout=60000)
                http_status = resp.status if resp else None
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

        if http_status is not None and http_status >= 400:
            # An error status is not a page that has yet to paint, so the
            # readiness wait below can only expire. Skipping it is worth
            # doing rather than tidy: the wait is 20s, and it was being
            # spent on every attempt of every page of a blocked run —
            # 20s x --retries x --proxy-block-retries x --pages of nothing.
            logger.warning("HTTP %d for %s — the edge answered with an error, "
                           "so this is not the listing. Not waiting for "
                           "products to paint.", http_status, url)
        else:
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

    outcome.http_status = http_status
    outcome.proxy_failure = exit_failed

    if load_failed:
        if exit_failed:
            logger.error("Gave up loading %s: the exit is unusable (%s). That "
                         "is a dead proxy, not a slow site — another try at "
                         "the same address cannot help.", url, exit_failed)
        else:
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
        logger.error("Blocked by %s before parsing (%d bytes) — "
                     "saved to %s%s. This is exit 3, distinct from a genuinely "
                     "empty category (exit 4).", describe_block(html, vendor), len(html), debug_html,
                     f" (tried {block_retries + 1} exit(s))" if block_retries else "")
        outcome.blocked_by = vendor
        return outcome

    if outcome.http_error:
        # An error status whose body carries no marker we recognise. We still
        # know this is not the listing, and saying "0 products" about it
        # would be a claim about the catalogue that this run cannot support.
        # Reported separately from `blocked` precisely because we CANNOT name
        # who refused us — "never present a guess as a fact" (CLAUDE.md §8).
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        logger.error("HTTP %d for %s and nothing on the page names a known "
                     "bot-check vendor — saved %d bytes to %s. Reported as a "
                     "fetch failure, not an empty category.",
                     outcome.http_status, url, len(html), debug_html)
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
        linked = count_product_links(html)
        if linked >= MIN_CARD_MATCHES:
            # The distinction the audit asked for. A page that LINKS to 18
            # products and parses to 0 is a broken parser, not an empty
            # category, and reporting it as "no products" sends the reader to
            # check the URL instead of the JSON-LD.
            outcome.parse_drift = True
            logger.error("PARSER DRIFT: the page links to %d product(s) and "
                         "the parser extracted 0. This is not an empty "
                         "category — the markup or the JSON-LD shape has "
                         "changed. Saved what the browser saw to %s and %s.",
                         linked, debug_html, debug_png)
        else:
            logger.warning("0 products parsed, and the page links to %d "
                           "product(s) — consistent with an empty or filtered "
                           "category, or a hub URL. Saved what the browser "
                           "actually saw to %s and %s. Open the .png to see "
                           "it.", linked, debug_html, debug_png)

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


def _fetch_pages_concurrently(args, pool, specs, concurrency: int,
                              checkpoint=None):
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
                            # Written from the COLLECTING side, inside the
                            # lock that already serialises results — not from
                            # each worker, which would have several threads
                            # rewriting one file.
                            if checkpoint is not None and outcome.ok:
                                checkpoint.record(page_num, outcome.products,
                                                  outcome.final_url)
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



def _crawl_details(session, args, listing_rows, checkpoint):
    """Open each listed product and return one row per size.

    Sequential, deliberately, for a first cut: detail mode multiplies a run's
    requests by the number of products on a page (~18 here), and raising that
    again with workers is a decision that wants a live measurement behind it
    rather than a default.

    The URLs come from the LISTING ROWS rather than from the page's HTML, so
    the crawl and the listing parser cannot disagree about what counts as a
    product — and the listing rows are already de-duplicated, which matters
    because a tile links to its product twice.

    Returns (rows, failures, attempted).
    """
    urls, seen = [], set()
    for row in listing_rows:
        if row.url and row.url not in seen:
            seen.add(row.url)
            urls.append(row.url)

    total = len(urls)
    if args.max_products and total > args.max_products:
        logger.warning("Listing has %d products; --max-products %d caps this "
                       "run. The cap is recorded in the run metadata, so a "
                       "truncated crawl does not read as a complete one.",
                       total, args.max_products)
        urls = urls[:args.max_products]

    logger.info("Detail mode: opening %d product page(s)%s.", len(urls),
                f" of {total}" if len(urls) != total else "")

    rows, failures = [], []
    for i, url in enumerate(urls, start=1):
        if checkpoint.has(i):
            continue
        if i > 1:
            time.sleep(args.delay)
        try:
            resp = session.page.goto(url, wait_until="domcontentloaded",
                                     timeout=60000)
            status = resp.status if resp else None
            if status is not None and status >= 400:
                logger.warning("Product %d/%d: HTTP %d for %s — skipping.",
                               i, len(urls), status, url)
                failures.append(url)
                continue
            session.page.wait_for_timeout(1500)
            html = session.page.content()
        except (PWTimeout, PWError) as e:
            reason = _proxy_failure(e)
            logger.warning("Product %d/%d failed (%s) — skipping.", i,
                           len(urls), reason or type(e).__name__)
            failures.append(url)
            continue

        vendor = detect_bot_challenge(html)
        if vendor:
            logger.error("Product %d/%d: blocked by %s — skipping.", i,
                         len(urls), describe_block(html, vendor))
            failures.append(url)
            continue

        variants = parse_product_detail(html, session.page.url,
                                        category=args.category)
        if not variants:
            # The parser already said why. Recorded as a failure rather than
            # as "this product has no sizes", which is what an empty list
            # would otherwise quietly mean.
            failures.append(url)
            continue

        logger.info("Product %d/%d: %d size(s) — %s", i, len(urls),
                    len(variants), (variants[0].title or "")[:60])
        rows.extend(variants)
        checkpoint.record(i, variants, session.page.url)

    return rows, failures, len(urls)


def scrape(args) -> int:
    # One id per run, logged here and written into the sidecar, so a
    # log line and an artefact can be tied together. "the run that
    # failed" is not identifying for a scraper on a schedule.
    run_id = new_run_id()
    started_at = time.time()
    logger.info("Run %s starting: %s", run_id, args.url)
    # One entry per page attempted, merged after the loop rather than folded
    # into shared state during it — see PageOutcome for why that ordering
    # matters more than it looks.
    outcomes: List[PageOutcome] = []
    seen_skus = set()
    blocked = False

    # Written after every page, always — see run_state.py for why this is not
    # behind a flag. --resume reads it; a completed run deletes it.
    checkpoint = Checkpoint(args.out, args)
    if args.resume:
        for line in checkpoint.resume():
            logger.info("%s", line)
    elif checkpoint.enabled and os.path.exists(checkpoint.path):
        logger.info("A checkpoint from an earlier run is at %s. It will be "
                    "overwritten as this run progresses; pass --resume to "
                    "continue that run instead of restarting it.",
                    checkpoint.path)
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
                stop_reason = stop_reason_for(
                    load_failed=first.load_failed,
                    blocked_by=first.blocked_by,
                    http_status=first.http_status,
                    proxy_failure=first.proxy_failure,
                    parse_drift=first.parse_drift)
                blocked = first.blocked_by is not None
            else:
                seen_skus.update(p.sku for p in first.products if p.sku is not None)
                checkpoint.record(1, first.products, first.final_url)
                planned = _plan_page_urls(session.page, args, first.final_url)

                # Restored pages can only be SKIPPED when pages are
                # addressable. Page 17's URL is unknowable without visiting
                # 16 when pagination is a chain of links, so a resume there
                # would have to walk every page anyway — and quietly not
                # doing so would produce a run missing its middle. Say it
                # instead.
                restorable = [n for n in checkpoint.resumed_from if n >= 2]
                if restorable and planned is None:
                    logger.warning("--resume has %d stored page(s), but this "
                                   "listing's pagination is a chain of links "
                                   "rather than addressable URLs, so page N "
                                   "cannot be reached without fetching N-1. "
                                   "Re-fetching from page 2.", len(restorable))
                    restorable = []
                for n in restorable:
                    outcomes.append(PageOutcome(
                        page_num=n, url="(restored from checkpoint)",
                        final_url=checkpoint.final_urls.get(n),
                        products=checkpoint.pages[n]))
                    seen_skus.update(p.sku for p in checkpoint.pages[n]
                                     if p.sku is not None)
                skip = set(restorable)

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
                    specs = [(n, planned[n - 2]) for n in range(2, args.pages + 1)
                             if n not in skip]
                    logger.info("Fetching pages 2-%d across %d workers%s.",
                                args.pages, concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency, checkpoint)
                    outcomes.extend(rest)

                    failed = [o for o in rest if not o.ok]
                    if failed:
                        worst = min(failed, key=lambda o: o.page_num)
                        stop_reason = stop_reason_for(
                            load_failed=worst.load_failed,
                            blocked_by=worst.blocked_by,
                            http_status=worst.http_status,
                            proxy_failure=worst.proxy_failure,
                    parse_drift=worst.parse_drift)
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

                        if page_num in skip:
                            # Already held, and addressable, so the next URL
                            # is constructible without visiting this one.
                            url = planned[page_num - 1] if page_num < args.pages else url
                            continue

                        outcome = _fetch_one_page(session, args, pool, page_num, url)
                        outcomes.append(outcome)
                        if outcome.ok:
                            checkpoint.record(page_num, outcome.products,
                                              outcome.final_url)
                        if not outcome.ok:
                            stop_reason = stop_reason_for(
                                load_failed=outcome.load_failed,
                                blocked_by=outcome.blocked_by,
                                http_status=outcome.http_status,
                                proxy_failure=outcome.proxy_failure,
                    parse_drift=outcome.parse_drift)
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

    rows, row_type = all_products, None
    if args.mode == "detail":
        # A fresh session for the crawl. The listing session is closed by now
        # — and under --concurrency it was closed before the workers even
        # started — so reopening is the consistent choice rather than
        # threading one browser through two different phases.
        detail_cp = Checkpoint(args.out, args, row_type=ProductVariant)
        if args.resume:
            for line in detail_cp.resume():
                logger.info("%s", line)
        with sync_playwright() as pw:
            dsession = _BrowserSession(pw, args, pool,
                                       remote=bool(args.cdp_endpoint)).open()
            try:
                rows, detail_failures, attempted = _crawl_details(
                    dsession, args, all_products, detail_cp)
            finally:
                dsession.close()

        rows = detail_cp.products_in_page_order() or rows
        row_type = ProductVariant
        if detail_failures:
            # A crawl that lost products is not a complete view of them, and
            # the run must not claim to be. The listing pages all succeeding
            # says nothing about the product pages.
            logger.warning("%d of %d product page(s) yielded nothing — the "
                           "result covers the rest.", len(detail_failures),
                           attempted)
            if stop_reason in COMPLETE_STOP_REASONS:
                stop_reason = "detail_pages_failed"
        elif args.max_products and attempted >= args.max_products:
            # Truncation is not failure, but it is not completeness either.
            if stop_reason in COMPLETE_STOP_REASONS:
                stop_reason = "max_products_reached"

    rc = finish_run(rows, args.out, args.format, args.allow_empty,
                    blocked=blocked, stop_reason=stop_reason,
                      run_id=run_id, started_at=started_at,
                      webhook=args.webhook, mode=args.mode, row_type=row_type,
                    pages_requested=args.pages, pages_completed=len(ok_pages),
                    pages_failed=failed_pages,
                    start_url=args.url, final_url=final_url)

    # Only a run that saw everything drops its checkpoint. A partial or failed
    # run keeps it — that is the run --resume exists for, and deleting it here
    # would throw away the pages it did get.
    if stop_reason in COMPLETE_STOP_REASONS and rows:
        checkpoint.clear()
    elif checkpoint.enabled and checkpoint.pages:
        logger.info("Checkpoint kept at %s (%d page(s)) — re-run the same "
                    "command with --resume to continue from there.",
                    checkpoint.path, len(checkpoint.pages))
    return rc


def parse_args():
    p = argparse.ArgumentParser(description="Farfetch scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="Farfetch category/hub/search listing URL. Required, unless "
                        "FARFETCH_URL is set in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--pages", type=positive_int, default=1, help="Number of listing pages to crawl")
    p.add_argument("--delay", type=nonneg_float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=positive_int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1 — "
                        "unchanged sequential behaviour). Each worker runs its "
                        "own browser and holds its own proxy exit, so N>1 "
                        "without --proxy-file just sends N times the traffic "
                        "from one address. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=positive_int, default=3,
                   help="Attempts per page load before giving up (default 3). A "
                        "single network flap mid-run should not end a 50-page "
                        "job; the pause between attempts doubles each time.")
    p.add_argument("--retry-delay", type=nonneg_float, default=2.0,
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
    p.add_argument("--proxy-block-retries", type=nonneg_int, default=2,
                   help="When a page comes back as a bot-challenge, retry it from "
                        "this many OTHER exits before giving up (default 2). "
                        "Needs a pool of more than one; ignored otherwise.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--mode", choices=["listing", "detail"], default="listing",
                   help="listing (default): one row per product, read from "
                        "the category page. detail: open each product page "
                        "and emit one row per SIZE, with per-size price, "
                        "strikethrough price and availability. Detail costs "
                        "one extra request per product, so a 3-page run of "
                        "~18 products a page is ~54 more fetches — see "
                        "--max-products.")
    p.add_argument("--max-products", type=nonneg_int, default=0, metavar="N",
                   help="In --mode detail, stop after N product pages "
                        "(0 = no limit, the default). A cap is reported in "
                        "the run metadata, so a truncated crawl never reads "
                        "as a complete one.")
    p.add_argument("--resume", action="store_true",
                   help="Continue a run that stopped early, using the "
                        "<out>.progress.json checkpoint every multi-page run "
                        "writes. Pages already held are not fetched again. "
                        "Refused if the checkpoint is for a different URL, "
                        "page count or category. Only skips pages when the "
                        "listing's pagination is addressable (?page=N) — a "
                        "chain of next-links has to be walked in order.")
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
                        "default so a failed run can't overwrite a good result with "
                        "an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint API and "
                        "apply it to the launched browser. Needs --twocaptcha-key. Ignored "
                        "with --cdp-endpoint, where the Scraping Browser supplies its own.")
    # ONE OS-family tag, not a list — and this default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop",
    # which the fingerprint API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation.
    #
    # fingerprint_client.py's own --tags help has said so all along; the
    # engines' default contradicted it. Measured against the live API on
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`,
    # `Chrome` and `Desktop` each 400.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to your proxy's "
                        "exit country — a US fingerprint on a German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current JSON API "
                        "(api.2captcha.com/createTask); v1 is the legacy in.php/res.php "
                        "pair. Default v2, with an automatic one-shot fallback to v1.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a challenge "
                        "if the catalogue is not already readable — this site "
                        "carries a reCAPTCHA in its sign-up modal that guards "
                        "nothing we want. always: solve whenever one is "
                        "detected, which is the safer choice if you would "
                        "rather spend a solve than risk missing content that "
                        "only appears afterwards.")
    p.add_argument("--min-score", type=float, default=0.7,
                   choices=[0.3, 0.7, 0.9],
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


def main() -> int:
    """The entry point, as a callable rather than a module-level block.

    It was inline under `if __name__ == "__main__"`, which meant two things:
    the console script declared in pyproject had nothing to point at, and the
    offline suite could only ever test the helpers underneath it — the exact
    gap CLAUDE.md §10 names ("test the public entry point, not only its
    internals"), which is how a signature once drifted away from its callers
    with every check still green.
    """
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API uses the "
                     "same key, though it's a separate subscription from solving).")
        return 2
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the Scraping Browser "
                       "supplies its own fingerprint, and stacking a second one on top "
                       "creates a mismatch rather than better cover.")
    try:
        return scrape(args)
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        return 2
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
