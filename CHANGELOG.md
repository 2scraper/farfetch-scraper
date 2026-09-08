# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit (rather than a
library with a stable API) reasonably can — a patch bump means "fixes", not
a promise that every flag and exit code is contractually frozen.

## [Unreleased]

### Added
- **`--solve-captcha when-blocked|always`** (default `when-blocked`) on all
  three browser engines. A detected challenge is not necessarily a blocking
  one: this site carries a reCAPTCHA in its sign-up modal, and solving it on
  a page whose products are already rendered spends a paid task on a
  challenge guarding nothing. `when-blocked` counts product links on the
  spot — no waiting, so the check is free — and only solves when the
  catalogue is not already readable. `always` keeps the previous behaviour
  for anyone who would rather spend a solve than risk missing content.
  - The check deliberately does not work by running the readiness wait
    first: on a page the captcha genuinely gates, that would burn 20 seconds
    before solving, and solving first is what makes the products appear.

### Fixed
- **A captcha the run cannot solve no longer takes the run down.** With no
  API key, `solve_recaptcha` raised `RuntimeError` out of the handler and out
  of `scrape()` — a traceback in place of products that were already on the
  page. Reproduced from an audit. Now a missing key, or any solver error, is
  a warning and the run continues; if the challenge really was blocking, that
  surfaces as exit 3 rather than as a crash.
- **Space-grouped thousands are read correctly.** `1 234 €` parsed as **234**
  — an order of magnitude off, silently. French, Russian and other locales
  group with a space, and a rendered page uses a no-break variant so the
  number does not wrap: plain space, NBSP (U+00A0) and narrow NBSP (U+202F)
  are all handled now. Space grouping requires full three-digit groups, so a
  size list beside a price (`5 yrs, 6 yrs 200 €`) cannot merge into one
  number.
- **Prefixed dollar symbols name their currency.** `HK$1,234` reported 1234
  **USD** — the wrong currency rather than a rounding error, and directly
  against this project's cross-country comparison use. `HK$`, `A$`, `C$`,
  `S$`, `NZ$`, `NT$`, `R$`, `AU$`, `CA$` and `US$` now map properly; a bare
  `$` still reads as USD, which is what it means on the US site.
- **Proxy credentials no longer reach a browser command line** in the
  pyppeteer and Selenium engines. Both appended the whole `--proxy` value to
  Chromium's `--proxy-server`, which becomes part of the browser process's
  argv — readable by anything that can run `ps` — and logged the URL
  verbatim on the next line. Now only `scheme://host:port` goes on the
  command line and the log is masked. pyppeteer additionally sends the
  credentials over CDP via `page.authenticate`, which is the supported way
  and actually works; Selenium warns that it dropped them, since Chromium's
  flag cannot authenticate at all and pretending otherwise is worse than
  saying so. (Playwright was already correct — 0.3.0.)
- **Three legal JSON-LD shapes that the parser mishandled**, all reproduced
  against the old code:
  - `"offers": null` raised `AttributeError` and killed the run. A default
    only applies to an ABSENT key, and explicit nulls occur in the wild.
  - `image` as an `ImageObject` (or a list of them) raised `KeyError` —
    a crash over a decorative field. Now reads `url`/`contentUrl` from any
    of the four shapes schema.org allows.
  - Products inside an `@graph` block were silently missed, so a site
    publishing that way would report an EMPTY category for what is really
    an unread format — the exact confusion the exit codes exist to prevent.

### Documented
- The README's geo-redirect claim is narrowed to what was actually measured:
  a fresh, cookie-less visit is redirected on exit IP. Farfetch also
  documents a customer-set *shopping location* with currency following the
  shipping destination, so exit IP decides the DEFAULT for a stateless
  scraper rather than being the site's only input. Readers are pointed at
  verifying the market in the output (`currency`, and the locale in the
  sidecar's `final_url`) instead of assuming.
- The price overlay's load-bearing assumption is now stated, and pinned by a
  test: every price in a tile is taken to belong to one discount chain, so an
  installment price inside a tile would be read as the product price. A live
  106-tile check found none — the page's Klarna/`Raten` text sits in the
  footer, outside any tile — so it is recorded as a known limitation rather
  than guarded with locale-chasing word lists or a ratio threshold that
  would reject this site's real 60%+ discounts.

## [0.4.0] — 2026-09-07

### Added
- **`--concurrency N`** (Playwright): fetch pages through N parallel workers.
  Defaults to 1, so the default run is exactly the sequential one. Measured
  on a live 4-page run: 57s at `--concurrency 3` against 98s sequential,
  with byte-identical output — same 333 products, same order, no field
  differing.
  - Each worker owns its own browser **and one proxy exit for its
    lifetime**. Not a shared browser (with Playwright's sync API a browser
    belongs to its creating thread) and not an exit that changes per page
    (the invariant from 0.3.0: a session must not change address
    mid-flight). Workers start on different exits and can walk the rest of
    the pool if one gets blocked; each holds its own pool object, so no
    locking is needed.
  - Warns when raised without `--proxy-file`, since N workers then send N
    times the traffic from one address.
  - Refused with `--cdp-endpoint`, where the Scraping Browser API allows one
    live connection per profile.
  - A listing whose pagination cannot be addressed independently (a cursor
    or token rather than `?page=N`) falls back to one page at a time.

### Changed
- **Page fetching restructured so pages no longer depend on each other**
  (groundwork for `--concurrency`; no behaviour change on its own, and no
  new flags). Three parts:
  - Page URLs are **planned up front** from page 1 instead of chaining each
    page's address off the previous page's next-link. Only done when the
    site's own link agrees with the `?page=N` convention — verified, not
    assumed, so a listing paginated with a cursor or token still chains
    link-to-link and says why.
  - Results are collected per page and **merged afterwards in page order**,
    rather than folded into a running dedupe set inside the loop. Dedupe
    that mutates shared state as it goes makes the output depend on the
    order pages arrive in — harmless while that order is fixed, wrong the
    moment pages are fetched concurrently.
  - `pages_failed` added to the run-metadata sidecar. `pages_completed`
    alone described the run only while pages were strictly ordered: "3 of
    10" could only mean 1-2-3. A count stops being a description once page 3
    can fail while 4 and 5 succeed.
- Verified byte-identical: a live 2-page run before and after produced 174
  products with an identical SKU order and zero differences in any field.

## [0.3.0] — 2026-09-07

### Added
- **Proxy rotation** (`proxy_pool.py`, Playwright engine). `--proxy` was a
  single static string applied once at launch — the shape of a demo, not of
  the thing proxies are bought for. Now: `--proxy-file` for a pool,
  `--proxy-rotate per-run|per-page`, `--proxy-shuffle`, and
  `--proxy-block-retries` to retry a challenged page from *other* exits.
  - A rotation **relaunches the browser** rather than swapping the proxy
    under a live session: cookies issued against one exit, replayed from
    another, are a stronger signal than either address alone.
  - An unusable exit (`ERR_PROXY_CONNECTION_FAILED`,
    `ERR_TUNNEL_CONNECTION_FAILED`, the auth variants) **rotates** instead of
    spending the retry budget on a proxy that will not answer. Found live: a
    dead proxy raises `PWError`, not `PWTimeout`, so it previously escaped as
    an unhandled traceback.
  - Credentials stay out of Chromium's argv (they go in Playwright's own
    `username`/`password` fields, never in `server`) and are masked in logs
    while host and port stay visible.
  - A malformed proxy list is rejected at load with the offending line named,
    exit 2 — not a connection failure on page 1 with nothing pointing at the
    cause.

## [0.2.1] — 2026-09-07

### Fixed
- **Multi-page runs silently returned page 1.** Measured live on
  2026-09-07: farfetch.com serves no anchor matching any of the three
  `NEXT_PAGE_SELECTOR` entries this project shipped with, so `--pages 3`
  fetched one page and exited 0 — a complete-looking run holding a third of
  the data. Three changes, layered:
  `<link rel="next">` (which the site *does* serve, and a standards-based
  signal rather than a build artefact) leads the selector list;
  `product_parser.page_url()` reconstructs `?page=N` when no selector
  matches at all; and the loop now terminates on a page that contributes no
  new `sku` — a property of the data — instead of on a missing link, a
  property of a selector. Verified: 263 unique products across 3 pages.
- A navigation timeout no longer ends the run on the first failure.
  `--retries` (default 3) with a doubling `--retry-delay` on every engine;
  `scraper_api_client.py` had retries all along, and the same transient
  deserved the same treatment in the browser engines.
- An empty CSV now carries its header row instead of being zero bytes, so
  `--allow-empty` output parses as a table with no rows rather than failing
  on read.
- A stale test mock hid a crash in the Selenium UA code: `engine-smoke` CI
  installed playwright and pyppeteer but **not** selenium, so every
  selenium-guarded check skipped in CI and the failure only surfaced when
  someone installed selenium locally. The job now installs all three engines
  and fails if *any* check group reports skipped.

### Changed
- The daily canary requests **3 pages, not 1** — with one page, pagination
  is never exercised, which is exactly how the bug above stayed invisible.
  It also now asserts `pages_completed`, `status == "complete"` and DOM
  price-confirmation coverage, not just a product count.

## [0.2.0] — 2026-09-07

First release verified against the live site: a real run of
`playwright_scraper.py` on the README's own known-good category URL from a
residential IP returned **96 products**, cleared Akamai with no proxy, no key
and no paid product, and confirmed the documented geo-redirect
(`.com/shopping` → `.com/de/shopping`, EUR, localised titles). Every earlier
release was fixture-verified only.

### Changed
- `Product` gained a `price_source` column (see below), so output is now
  **sixteen** columns rather than fifteen. Anything parsing the CSV header or
  asserting a column count needs updating.
- A JSON-LD offer with no `priceCurrency` now yields `currency: null` instead
  of a guessed `"USD"`, and the DOM discount-overlay no longer overwrites a
  currency the structured data stated explicitly.

### Fixed
- `.github/workflows/canary.yml` never recorded the scraper's exit code:
  GitHub Actions runs `run:` steps under `bash -e`, so a non-zero exit
  aborted the script before the line that captured it, leaving the whole
  exit-3-vs-4-vs-124 interpretation dead in exactly the cases it existed
  for. The canary also now sanity-checks the data it fetched (≥10 products,
  ≥90% with a non-null price) rather than treating "exit 0" as success.

### Added
- **`price_source` column** — says whether `price` is the DOM-confirmed
  figure a customer pays (`jsonld+dom`), structured data only and therefore
  possibly the pre-promo price (`jsonld`), or read off the tile with nothing
  to cross-check (`dom`). The same column previously held all three with no
  way to tell them apart.
- `diff_runs.py` reports a **`source_changed`** bucket: a price that differs
  while `price_source` also differs is not a site-side price change, just two
  snapshots that rendered differently. `--fail-on-change` ignores it.
- The parser logs **DOM price-confirmation coverage** per run, warning below
  90% — a low figure on a sale page means the snapshot predates the tiles.
- `sample_output.{json,csv}` regenerated from a live run (2026-09-07, 96
  products from a European exit IP), now including `price_source`. One of the
  three rows is a €1,020 product, which exercises the thousands-separator
  handling fixed in 0.1.1.
- **Run metadata sidecar** — every run that writes output also writes
  `<out>.meta.json` recording `status` (`complete`/`partial`/`failed`),
  `stop_reason`, pages requested vs. completed, and the start/final URL.
  A failed run writes no sidecar, so it cannot contradict the previous
  run's still-intact output.
- **Exit code 6 for a partial run** — a timeout or challenge partway
  through pagination still saves what it gathered, but no longer looks
  identical to a complete run. The site's own pagination running out still
  exits 0: there was nothing more to fetch.
- `diff_runs.py` **refuses an assortment diff** when either side's sidecar
  says the run was partial, since products on pages that were never fetched
  would otherwise be reported as `removed` (i.e. delisted). `--force` opts
  out.
- The CSS-fallback parser now recognises **3-letter ISO currency codes**
  (`AED 100`, `100 CHF`) in either position, matched against an allowlist of
  real ISO 4217 codes so a size chart (`XXL 100`) can't become a phantom
  price. Such tiles previously matched nothing and were dropped as "not a
  product", losing every product on those locales.

## [0.1.1] — 2026-09-07

### Fixed
- `solve_recaptcha()` crashed on any real captcha encounter: callers passed
  `api_version=`/`min_score=` it no longer accepted, and its body called a
  function (`_solve_with_2captcha`) that no longer existed. Restored
  dispatch to `_solve_with_2captcha_v1`/`_v2`.
- Thousands-separator bug in `product_parser._prices_in`: `$1,234` parsed as
  `1.234`. A single separator followed by exactly 3 digits is now read as a
  thousands grouping, not a decimal point.
- The DOM discount-overlay was overwriting a correct JSON-LD `priceCurrency`
  with a `$`→USD guess, corrupting AUD/CAD/SGD/HKD/NZD rows.
- Exit code 3 (blocked by a bot-challenge page) was unreachable from any of
  the three browser engines — a challenge page parsed to 0 products and
  exited 4, indistinguishable from a genuinely empty category. Shared
  `detect_bot_challenge` (moved from `scraper_api_client.py` into
  `product_parser.py`) across all four engines.
- All three browser engines hardcoded a `Chrome/124.0.0.0` user agent that
  only ever drifted from whatever Chromium was actually installed. Now built
  from each browser's own real launched version at runtime.
- Playwright's pagination concatenated a raw `get_attribute("href")` by hand
  instead of using `urljoin`, breaking on an absolute-path href.

### Added
- `engine-smoke` CI job: installs playwright + pyppeteer (packages only, no
  browser binaries) so engine-specific smoke checks run for real in CI
  instead of always silently skipping.

## [0.1.0] — 2026-09-07

Initial tagged release.

### Added
- Four scraper engines (Playwright, Selenium, pyppeteer, and a browserless
  2Captcha Scraper API client), sharing one parsing core and one JSON/CSV
  output writer.
- JSON-LD-primary / CSS-fallback parsing, with a DOM-tile overlay that
  corrects the discount price Farfetch's own JSON-LD under-reports.
- Optional 2Captcha integrations: captcha solving, the Scraping Browser API
  over `--cdp-endpoint`, proxies, and fingerprints.
- Cross-page dedup by `sku`, and `diff_runs.py` to diff two runs' output by
  `sku` (added/removed/changed).
- `.github/workflows/canary.yml`: a daily live run against a real category
  page, since the main `tests` workflow is deliberately offline-only.
- `pyproject.toml`, `tests/test_smoke.py` (pytest entry point), `Dockerfile`.
