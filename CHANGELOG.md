# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit (rather than a
library with a stable API) reasonably can — a patch bump means "fixes", not
a promise that every flag and exit code is contractually frozen.

## [Unreleased]

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
