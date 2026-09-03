#!/usr/bin/env python3
"""
farfetch-scraper — Selenium edition
=======================================

Same feature set as the Playwright / Selenium versions — see
playwright_scraper.py for the full docstring on strategy and known
Farfetch quirks.

KNOWN LIMITATION, confirmed in a previous single-site scraper in this same
family: Selenium's `debuggerAddress` capability was designed for a LOCAL,
UNAUTHENTICATED debug port. It does NOT forward a `user:pass` embedded in
a remote CDP URL — connecting to an authenticated remote antidetect/Scraping
Browser this way fails with `SessionNotCreatedException: cannot connect to
chrome at <host>:<port> from chrome not reachable`. Use
playwright_scraper.py or puppeteer_scraper.py for that use case; this
script's --cdp-endpoint is best-effort and works fine against a local,
unauthenticated debug port.

Usage
-----
    python selenium_scraper.py \\
        --url "https://www.farfetch.com/shopping/kids/items.aspx" \\
        --pages 1 --format both

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          plus webdriver-manager ONLY if you let this script launch its own
          Chrome; --cdp-endpoint doesn't need it.
"""

import argparse
import logging
import os
import re
import signal
import sys
import tempfile
import time
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.support.ui import WebDriverWait

# webdriver_manager is imported LAZILY, inside the local-launch branch of
# build_driver(). It is only needed to download a matching chromedriver for a
# browser we launch ourselves — the --cdp-endpoint path attaches to an
# already-running browser and never touches it.
#
# It used to be imported here, at module level. That made a package the
# --cdp-endpoint path has no use for into a hard requirement for running the
# file at all: a real test run died on `ModuleNotFoundError:
# webdriver_manager` before reaching the CDP connection, so the thing the run
# was actually meant to measure went unmeasured.

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS, RECAPTCHA_DISCOVERY_JS)
from product_parser import parse_products, SELECTORS
from output_writer import save
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

# Printed on startup, not just stored: a stale checkout is otherwise silent
# about being stale — the option you just read about simply does not exist.
BUILD = "2026-08-25.9"

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


# ---------------------------------------------------------------------------
# Session-creation timeout
# ---------------------------------------------------------------------------
#
# Observed on a real run: with --cdp-endpoint pointing at an authenticated
# remote CDP host, this script does not raise SessionNotCreatedException
# promptly — it HANGS. Selenium's `debuggerAddress` makes chromedriver poll the
# debug host, and against a remote endpoint that never completes a usable
# handshake that polling just keeps going. The documented failure mode for this
# path is a clean SessionNotCreatedException; getting there can take longer
# than anyone is willing to sit and watch, and an unbounded wait in a test
# harness is indistinguishable from a broken harness.
#
# Two independent bounds, because they cover different layers:
#   1. RemoteConnection.set_timeout() — caps the HTTP wait for the newSession
#      response. Wrapped in try/except because it's a class-level API that has
#      moved around between Selenium versions.
#   2. SIGALRM around the whole of build_driver() — catches anything below the
#      HTTP layer too: chromedriver spawning, Selenium Manager resolving or
#      downloading a driver, DNS. POSIX only, so it's feature-detected.
#
# The budget is a ceiling, not a stopwatch: a signal can only be delivered when
# the interpreter regains control, so a blocking call already inside C code
# overshoots a little. Measured against a socket that accepts and never speaks,
# an 8s budget tripped at ~14s. That is fine for the purpose — the point is
# that it terminates with a useful message, not that it terminates on the
# exact second.
# Two numbers, because the two paths wait for different things. On the remote
# path a long wait is pure waste — chromedriver is polling a host it can never
# authenticate against, and more seconds change nothing. Locally it is launching
# a REAL browser with a fresh profile, which legitimately takes time: the local run that finally passed spent 48 seconds in session creation. A 60s ceiling
# would have failed it on a slower machine and we would have called Selenium
# broken for the third time. Cheap insurance.
DEFAULT_DRIVER_TIMEOUT_REMOTE = 60
DEFAULT_DRIVER_TIMEOUT_LOCAL = 150
DEFAULT_DRIVER_TIMEOUT = DEFAULT_DRIVER_TIMEOUT_REMOTE  # back-compat alias


class DriverTimeout(Exception):
    pass


def _install_session_timeout(seconds: int):
    """Bound the HTTP-level wait for newSession. Returns a description.

    Selenium 4.4x deprecated `RemoteConnection.set_timeout()` in favour of
    ClientConfig, and on 4.47 calling it raises
    `'NoneType' object has no attribute 'timeout'` because there is no client
    config to mutate yet. So try ClientConfig first and keep the old call only
    as a fallback for older Selenium.
    """
    try:
        from selenium.webdriver.remote.client_config import ClientConfig
        # Not all versions accept a bare timeout; construct defensively.
        ClientConfig(remote_server_addr="http://localhost", timeout=seconds)
        return f"ClientConfig timeout available ({seconds}s)"
    except Exception:  # noqa: BLE001 — fall through to the legacy API
        pass
    try:
        from selenium.webdriver.remote.remote_connection import RemoteConnection
        RemoteConnection.set_timeout(seconds)
        return f"RemoteConnection timeout set to {seconds}s (legacy API)"
    except Exception as e:  # noqa: BLE001
        return (f"no HTTP-level timeout available ({type(e).__name__}) — relying on "
                f"the SIGALRM ceiling alone")


# Set by the SIGALRM handler. Needed because raising from a signal handler is
# not enough on its own: the alarm fires inside third-party code (Selenium
# Manager runs a subprocess and waits on it), which catches broadly and
# re-raises as its own exception type. On a real run the DriverTimeout was
# swallowed and resurfaced as NoSuchDriverException, so `except DriverTimeout`
# never matched and the test exited 1 with a 40-line traceback instead of 124
# with the one-line explanation. The flag survives that laundering.
_timed_out = {"hit": False, "seconds": None}


def _leave_now(code: int):
    """Exit immediately, without waiting on interpreter shutdown.

    `raise SystemExit(124)` is not enough here, and a live run proved it: the
    timeout fired at 60s, both error lines were logged — and then the process
    sat for another four minutes until the outer harness SIGTERM'd it. Python
    joins non-daemon threads on the way out, and an interrupted Selenium
    Manager leaves a subprocess and pool threads behind that nobody is going to
    reap.

    The same shape of bug appeared in the pyppeteer diagnostic, for a different
    underlying reason (asyncio awaiting an uncancellable task). Both times the
    answer was already on screen while the process refused to leave, so both
    times the fix is the same: once the result is reported, go.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    logging.shutdown()
    os._exit(code)


def _driver_timeout_hit() -> bool:
    return _timed_out["hit"]


def _chrome_candidates():
    """Where a Chromium-family browser might be, widest-first.

    Widened after the second attempt: the first version only looked at
    /Applications/Google Chrome.app and three bare command names, found nothing
    on a machine that clearly runs a browser, and reported "could not find a
    local Chrome" — technically true and practically useless. A per-user
    install, Canary, Brave, Edge, Arc or Chromium are all Chromium-family and
    all drivable by chromedriver.
    """
    home = os.path.expanduser("~")
    apps = [
        "Google Chrome.app/Contents/MacOS/Google Chrome",
        "Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "Chromium.app/Contents/MacOS/Chromium",
        "Brave Browser.app/Contents/MacOS/Brave Browser",
        "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "Arc.app/Contents/MacOS/Arc",
    ]
    out = []
    for root in ("/Applications", os.path.join(home, "Applications")):
        out += [os.path.join(root, a) for a in apps]
    out += [
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/opt/pw-browsers/chromium",
    ]
    return out


def _local_chrome_version():
    """(major, full, path) for a Chromium-family browser on this machine, or None.

    Worth the effort because the alternative is a 60-second wait followed by a
    message about the wrong thing. chromedriver has to match the browser it
    launches, and Selenium Manager is not in the loop when --chromedriver is
    given, so nothing else checks this.

    Cheap on purpose. The first version spawned a subprocess per candidate with
    a 10s timeout each, including for paths that do not exist — a live run spent
    10.5 seconds here before printing anything. Existence is checked first now,
    and only real files are executed.
    """
    import shutil
    import subprocess

    paths = [c for c in _chrome_candidates() if os.path.isfile(c)]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        w = shutil.which(name)
        if w:
            paths.append(w)

    # macOS: ask Spotlight rather than guessing more paths. Bounded, and skipped
    # entirely if mdfind isn't there.
    if not paths and shutil.which("mdfind"):
        try:
            found = subprocess.run(
                ["mdfind", "-name", "Chrome.app"], capture_output=True,
                text=True, timeout=5).stdout.splitlines()
            for app in found[:5]:
                exe = os.path.join(app, "Contents", "MacOS",
                                   os.path.basename(app)[:-4])
                if os.path.isfile(exe):
                    paths.append(exe)
        except Exception:
            pass

    for c in paths:
        # 8s was not enough on a real Mac: `Google Chrome --version` took longer
        # than that, the call was abandoned, and the browser was reported
        # missing while sitting in /Applications. Log the reason rather than
        # silently moving on — a skipped candidate is information.
        try:
            out = subprocess.run([c, "--version"], capture_output=True, text=True,
                                 timeout=25).stdout.strip()
        except Exception as e:
            logger.info("Could not read a version from %s (%s) — skipping it.",
                        c, type(e).__name__)
            continue
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
        if m:
            return int(m.group(1)), out, c
    return None


def _binary_version(path):
    """major version reported by a chromedriver binary, or None."""
    import subprocess
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except Exception:
        return None, None
    m = re.search(r"(\d+)\.", out)
    return (int(m.group(1)) if m else None), out


def check_local_versions(args) -> None:
    """Compare chromedriver to the LOCAL Chrome before spending the budget.

    Added after a live local run: chromedriver 151 against this machine's Chrome, given via
    --chromedriver, produced a 60-second session-creation timeout and then a
    message about `debuggerAddress` — an option that run wasn't using. The
    mismatch is checkable in two subprocess calls, so check it and say so.
    """
    if args.cdp_endpoint or not args.chromedriver:
        return
    drv_major, drv_out = _binary_version(args.chromedriver)

    # If the caller already told us WHICH browser to launch, check that one.
    # Not doing this produced a run that warned "No Chromium-family browser
    # found on this machine" and then, three lines later, logged
    # "Chrome binary: /Applications/Google Chrome.app/...". The search was
    # hunting for something it had been handed.
    if getattr(args, "chrome_binary", None) and os.path.isfile(args.chrome_binary):
        maj, out = _binary_version(args.chrome_binary)
        local = (maj, out, args.chrome_binary) if maj else None
        if local is None:
            logger.info("Could not read a version from --chrome-binary %s — skipping "
                        "the comparison and letting chromedriver try.",
                        args.chrome_binary)
            return
    else:
        local = _local_chrome_version()
    if drv_out:
        logger.info("chromedriver reports: %s", drv_out)
    if local:
        logger.info("local browser reports: %s  (%s)", local[1], local[2])
    else:
        logger.warning(
            "No Chromium-family browser found on this machine (checked "
            "/Applications, ~/Applications, /usr/bin and Spotlight; Chrome, "
            "Chromium, Canary, Brave, Edge, Arc).\n"
            "  This is a check that could not confirm anything — NOT proof there "
            "is no browser. It has been wrong: a run warned this and then drove "
            "Chrome from /Applications successfully, because a --version call had "
            "timed out. Treat it as 'unverified', and if the session does time "
            "out, treat it as the first suspect: chromedriver LAUNCHES a browser "
            "locally, and with none where it looks it waits rather than "
            "reporting.\n"
            "  Find yours and pass it:\n"
            "    ls -d /Applications/*.app ~/Applications/*.app 2>/dev/null | "
            "grep -i 'chrom\\|brave\\|edge\\|arc'\n"
            "    --chrome-binary '/path/to/Google Chrome'\n"
            "  Playwright's bundled Chromium also works as a target (it is a real "
            "browser on disk); add --disable-build-check if its major differs:\n"
            "    python3 -c \"from playwright.sync_api import sync_playwright as p; "
            "s=p().start(); print(s.chromium.executable_path)\"")
        return
    if drv_major is None:
        return
    if drv_major == local[0]:
        return
    msg = (f"chromedriver major {drv_major} does not match local Chrome major "
           f"{local[0]}.")
    if args.disable_build_check:
        logger.warning("%s Continuing anyway because --disable-build-check was "
                       "passed; if it fails, this is the first thing to suspect.", msg)
        return
    raise SystemExit(
        f"{msg}\n"
        f"  chromedriver: {args.chromedriver}\n"
        f"  Chrome:       {local[2]}\n\n"
        f"chromedriver launches and drives that exact browser locally, so the majors\n"
        f"have to match. Options:\n"
        f"  * get chromedriver {local[0]}.x from "
        f"https://googlechromelabs.github.io/chrome-for-testing/\n"
        f"  * or drop --chromedriver and let webdriver-manager fetch a matching one\n"
        f"  * or pass --disable-build-check to try anyway (it may still fail)\n"
        f"Checked in two subprocess calls instead of costing you a 60s timeout.")


def _timeout_message() -> str:
    if not _timed_out.get("remote", True):
        return (f"Selenium did not finish creating a session within "
                f"{_timed_out['seconds']}s, launching a LOCAL Chrome. "
                f"--cdp-endpoint was not used, so nothing remote is involved. "
                f"In order of likelihood:\n"
                f"  * chromedriver's major version doesn't match the local Chrome "
                f"it launches. Check both:  chromedriver --version  and  "
                f"'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' "
                f"--version\n"
                f"  * Chrome isn't where chromedriver looks for it — pass "
                f"--chrome-binary, or launch chromedriver by hand to see its error;\n"
                f"  * on macOS, a quarantined driver binary can block silently: "
                f"xattr -d com.apple.quarantine <chromedriver>\n"
                f"  * a stale chromedriver from an earlier run still holds its port: "
                f"pkill -f chromedriver\n"
                f"Fastest way to see the real error is to cut Selenium out of it:\n"
                f"    <chromedriver> --port=9515 --verbose\n"
                f"    curl -s -X POST localhost:9515/session -d "
                f"'{{\"capabilities\":{{\"alwaysMatch\":{{\"browserName\":\"chrome\"}}}}}}'\n"
                f"chromedriver states the incompatibility outright; this wrapper only "
                f"sees a request that never answered.")
    return (f"Selenium did not finish creating a session within "
            f"{_timed_out['seconds']}s. Two things can cause this on the "
            f"--cdp-endpoint path, and both end here:\n"
            f"  * `debuggerAddress` does not forward URL-embedded credentials, "
            f"so chromedriver polls a host it can never authenticate against;\n"
            f"  * Selenium still needs a LOCAL chromedriver even when attaching "
            f"to a remote browser, and Selenium Manager can sit for a long time "
            f"trying to resolve or download one.\n"
            f"Check the traceback above for NoSuchDriverException to tell them "
            f"apart.\n"
            f"For an authenticated remote endpoint, use playwright_scraper.py or "
            f"puppeteer_scraper.py: both take a full ws://user:pass@host:port and "
            f"authenticate on the upgrade. Selenium is fine against a local debug "
            f"port (chrome --remote-debugging-port=9222), where no auth applies.")


def build_driver_with_timeout(args) -> webdriver.Chrome:
    seconds = args.driver_timeout
    if seconds is None:
        seconds = (DEFAULT_DRIVER_TIMEOUT_REMOTE if args.cdp_endpoint
                   else DEFAULT_DRIVER_TIMEOUT_LOCAL)
    logger.info("selenium_scraper build %s (has --disable-build-check, --chromedriver)", BUILD)
    logger.info("Session-creation budget: %ds (%s)", seconds,
                _install_session_timeout(seconds))
    _timed_out["remote"] = bool(args.cdp_endpoint)
    check_local_versions(args)

    if not hasattr(signal, "SIGALRM"):
        # Windows: the HTTP-level bound above is all we get.
        return build_driver(args)

    _timed_out["hit"] = False
    _timed_out["seconds"] = seconds
    _timed_out["remote"] = bool(args.cdp_endpoint)

    def _on_alarm(_signum, _frame):
        # Record it before raising: whatever catches this on the way out, the
        # flag still says what happened.
        _timed_out["hit"] = True
        raise DriverTimeout(_timeout_message())

    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(seconds)
    try:
        return build_driver(args)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def build_driver(args) -> webdriver.Chrome:
    options = Options()

    if args.cdp_endpoint:
        # BEST-EFFORT ONLY — see module docstring. Confirmed NOT to work
        # against an authenticated remote endpoint in a previous scraper
        # in this family; works against a local/unauthenticated debug port.
        parsed = urlparse(args.cdp_endpoint)
        host_port = parsed.netloc.split("@")[-1]  # drop user:pass@ if present
        is_local = (parsed.hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
        if is_local:
            # Pointed at a local shim or a local debug port: the credentials
            # warning below would be actively misleading here, since the whole
            # Loopback needs no credentials, so the warning below would be
            # actively misleading here.
            logger.info("Connecting via debuggerAddress to %s (loopback — a local "
                        "debug port or a local proxy).", host_port)
        else:
            logger.warning("Connecting via Selenium's debuggerAddress to %s — this "
                            "does NOT forward URL-embedded credentials. If your "
                            "endpoint requires auth, this will fail — use "
                            "playwright_scraper.py or puppeteer_scraper.py instead, "
                            "which authenticate on the WebSocket upgrade.", host_port)
        options.add_experimental_option("debuggerAddress", host_port)

        # chromedriver must match the browser it attaches to, and
        # `debuggerAddress` gives it no say in which browser that is. Selenium
        # Manager resolves a driver for whatever Chrome is installed LOCALLY,
        # which has nothing to do with the remote one — so on the remote path
        # an explicit, version-matched driver is usually required. Read the
        # remote browser's version from its own /json/version endpoint.
        if args.chromedriver:
            # Check the path before Selenium turns "file not found" into a
            # forty-line NoSuchDriverException. The docs' example path is a
            # placeholder, and a placeholder is exactly what gets pasted.
            if not os.path.isfile(args.chromedriver):
                raise SystemExit(
                    f"--chromedriver: not a file: {args.chromedriver}\n"
                    f"That looks like the placeholder from the docs. Download a "
                    f"chromedriver whose MAJOR version matches the browser it will "
                    f"drive, from\n"
                    f"  https://googlechromelabs.github.io/chrome-for-testing/\n"
                    f"and pass its real path.")
            if not os.access(args.chromedriver, os.X_OK):
                raise SystemExit(f"--chromedriver: not executable: {args.chromedriver}\n"
                                 f"  chmod +x {args.chromedriver}")
            logger.info("Using chromedriver at %s", args.chromedriver)
            service_args = []
            if args.disable_build_check:
                # chromedriver normally refuses to attach to a browser whose
                # major version differs from its own. That check assumes the
                # browser's self-reported version is meaningful — and a managed
                # antidetect browser's whole job includes spoofing what it
                # claims to be, so the version it reports may name nothing real.
                # This skips the comparison and lets the protocol decide. If the
                # protocol genuinely doesn't match you get an error naming the
                # actual incompatibility, which beats a refusal to start.
                service_args.append("--disable-build-check")
                logger.warning("--disable-build-check: skipping chromedriver's version "
                               "check. If the remote browser's protocol really differs, "
                               "expect a protocol error rather than a clean refusal.")
            driver = webdriver.Chrome(
                options=options,
                service=Service(executable_path=args.chromedriver,
                                service_args=service_args or None))
        else:
            logger.info("No --chromedriver given: Selenium Manager will resolve one from "
                        "the LOCAL Chrome, which may not match the remote browser. If this "
                        "fails, pass --chromedriver with a driver matching the remote "
                        "browser's major version.")
            driver = webdriver.Chrome(options=options)

        # RETROACTIVE ADDITION, best-effort — see playwright_scraper.py in
        # this same project for the full story.
        try:
            driver.execute_cdp_cmd("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
            logger.info("2captcha Scraping Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:
            logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                        "relying on this script's own detect+solve logic instead.", e)
        return driver

    # Named in the local timeout message, so it has to exist. chromedriver looks
    # for Chrome in a fixed set of places; if it lives elsewhere, the symptom is
    # a session that never gets created rather than "Chrome not found".
    if getattr(args, "chrome_binary", None):
        if not os.path.isfile(args.chrome_binary):
            raise SystemExit(f"--chrome-binary: not a file: {args.chrome_binary}")
        options.binary_location = args.chrome_binary
        logger.info("Chrome binary: %s", args.chrome_binary)

    # A unique profile per run. Chrome refuses to start on a user-data-dir that
    # another Chrome already holds ("probably user data directory is already in
    # use"), which is what you get from two runs at once or from a browser the
    # user already has open. chromedriver normally invents a temp dir, but not
    # when binary_location points at a build with a default profile in play —
    # hit exactly this while verifying a local run. Explicit beats implicit here.
    _profile = tempfile.mkdtemp(prefix="ff-selenium-")
    options.add_argument(f"--user-data-dir={_profile}")

    # Running as root means a container, and Chrome's sandbox needs privileges a
    # container usually doesn't grant. Never true on a normal desktop, so this
    # does not silently weaken anyone's local run.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        logger.info("Running as root — adding --no-sandbox (container-only path).")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1366,900")

    if args.proxy:
        # Selenium's basic --proxy-server flag doesn't support inline user:pass;
        # for authenticated 2Captcha proxies use a Selenium-Wire / extension
        # setup in production. Shown here in its simple unauthenticated form.
        options.add_argument(f"--proxy-server={args.proxy}")
        logger.info("Using 2Captcha proxy: %s", args.proxy)

    # --chromedriver is honoured on THIS path too, not just the remote one.
    # It used to be remote-only, which was wrong in a way that only shows up on
    # a machine that already has a driver: webdriver-manager would be required,
    # and would try to download a second copy of a binary sitting on disk —
    # failing outright behind an egress allowlist. If you have one, pass it.
    if args.chromedriver:
        if not os.path.isfile(args.chromedriver):
            raise SystemExit(f"--chromedriver: not a file: {args.chromedriver}")
        if not os.access(args.chromedriver, os.X_OK):
            raise SystemExit(f"--chromedriver: not executable: {args.chromedriver}\n"
                             f"  chmod +x {args.chromedriver}")
        logger.info("Using chromedriver at %s (local Chrome)", args.chromedriver)
        driver_args = ["--disable-build-check"] if args.disable_build_check else None
        service = Service(executable_path=args.chromedriver, service_args=driver_args)
    else:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
        except ImportError:
            raise SystemExit(
                "Launching a local Chrome needs a chromedriver. Either pass one you\n"
                "already have:\n"
                "    --chromedriver /path/to/chromedriver\n"
                "or install webdriver-manager so it can fetch a matching one:\n"
                "    pip install webdriver-manager\n"
                "(Neither is needed with --cdp-endpoint — that attaches to a browser\n"
                "that's already running.)")
        service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver


def handle_captcha_if_present(driver, args) -> None:
    """Runs after EVERY navigation, for ANY page — not scoped to one URL."""
    html = driver.page_source
    # Both detectors, always — see reconcile_detections in captcha_solver.py.
    # execute_script needs an explicit `return` and the arrow function
    # invoked; Playwright and pyppeteer accept a bare function, Selenium does
    # not.
    html_challenge = detect_recaptcha_v3(html, driver.current_url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=driver.current_url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    token = solve_recaptcha(challenge, args.twocaptcha_key,
                           api_version=args.captcha_api,
                           min_score=args.min_score)
    driver.execute_script(f"({INJECT_TOKEN_JS})(arguments[0]);", token)
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    driver.refresh()


def scrape(args) -> None:
    all_products = []
    try:
        driver = build_driver_with_timeout(args)
    except DriverTimeout as e:
        logger.error("%s", e)
        _leave_now(124)
    except Exception as e:  # noqa: BLE001
        # The alarm may have been laundered into someone else's exception type
        # on the way out (see _timed_out). If it was, report it as the timeout
        # it is rather than as whatever Selenium relabelled it.
        if _driver_timeout_hit():
            logger.error("%s", _timeout_message())
            logger.error("(surfaced as %s — Selenium re-raised the timeout as its own error)",
                         type(e).__name__)
            _leave_now(124)
        raise
    wait = WebDriverWait(driver, 20)

    try:
        url = args.url
        for page_num in range(1, args.pages + 1):
            logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
            driver.get(url)
            try:
                wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
            except TimeoutException:
                logger.error("Timeout loading %s — skipping.", url)
                break

            handle_captcha_if_present(driver, args)

            try:
                WebDriverWait(driver, 20).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, ITEM_LINK_SELECTOR)) > MIN_CARD_MATCHES
                )
                time.sleep(1)
            except TimeoutException:
                logger.warning("No product markers appeared within 20s — "
                                "parsing whatever loaded (may be a bot-check/consent page).")

            html = driver.page_source
            products = parse_products(html, driver.current_url, category=args.category)
            logger.info("Parsed %d products from page %d.", len(products), page_num)

            if not products:
                debug_html = f"{args.out}_page{page_num}_debug.html"
                debug_png = f"{args.out}_page{page_num}_debug.png"
                with open(debug_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    driver.save_screenshot(debug_png)
                except Exception as e:
                    logger.warning("Could not capture screenshot: %s", e)
                logger.warning("0 products parsed — saved what the browser actually saw to "
                                "%s and %s.", debug_html, debug_png)

            all_products.extend(products)

            if page_num < args.pages:
                try:
                    next_link = driver.find_element(By.CSS_SELECTOR, NEXT_PAGE_SELECTOR)
                    href = next_link.get_attribute("href")
                except NoSuchElementException:
                    logger.info("No further pagination link found — stopping early.")
                    break
                if not href:
                    break
                url = href
                time.sleep(args.delay)
    finally:
        if args.cdp_endpoint:
            driver.close()  # detach this tab only, leave the remote browser running
        else:
            driver.quit()

    return save(all_products, args.out, args.format, allow_empty=args.allow_empty)


def parse_args():
    p = argparse.ArgumentParser(description="Farfetch scraper (Selenium edition)")
    p.add_argument("--url", default=None,
                   help="Farfetch category/hub/search listing URL. Required, unless "
                        "FARFETCH_URL is set in the environment or in .env.")
    p.add_argument("--category", default=None, help="Label to tag output rows with. Defaults to the category segment of the URL, so the column is never empty just because the flag was omitted.")
    p.add_argument("--pages", type=int, default=1, help="Number of listing pages to crawl")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="farfetch_products", help="Output file prefix")
    p.add_argument("--proxy", default=None, help="Proxy URL, e.g. http://HOST:9999 (2captcha.com/proxy)")
    p.add_argument("--chrome-binary", default=None,
                   help="Path to the Chrome/Chromium binary to launch locally. Only "
                        "needed when it isn't in one of chromedriver's default "
                        "locations — otherwise the symptom is a session-creation "
                        "timeout, not a 'not found' error.")
    p.add_argument("--chromedriver", default=None,
                   help="Path to a chromedriver binary. Honoured on BOTH paths. "
                        "Locally it saves a webdriver-manager download of a binary you "
                        "already have (and is the only way to run behind an egress "
                        "allowlist). On the --cdp-endpoint path it is usually required: "
                        "the driver must match the REMOTE browser's major version, and "
                        "Selenium Manager only knows about your local Chrome.")
    p.add_argument("--disable-build-check", action="store_true",
                   help="Pass --disable-build-check to chromedriver, so it attaches "
                        "without comparing versions. Use when no driver matches the "
                        "remote browser's reported version, or when you suspect that "
                        "version is spoofed — which a managed antidetect browser may "
                        "well do.")
    p.add_argument("--driver-timeout", type=int, default=None,
                   help=f"Seconds to allow for creating the Selenium session before giving up. "
                        f"Default depends on the path: {DEFAULT_DRIVER_TIMEOUT_REMOTE}s with "
                        f"--cdp-endpoint (waiting longer there is pointless — chromedriver is "
                        f"polling a host it cannot authenticate against), "
                        f"{DEFAULT_DRIVER_TIMEOUT_LOCAL}s locally (launching a real browser "
                        f"with a fresh profile took 48s on a live run).")
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
                    help="Best-effort: attach to an existing browser via Selenium's "
                         "debuggerAddress. Does NOT forward URL-embedded credentials — "
                         "for authenticated remote endpoints use playwright_scraper.py "
                         "or puppeteer_scraper.py instead.")
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
        sys.exit(scrape(args))
    except KeyboardInterrupt:
        sys.exit(1)
