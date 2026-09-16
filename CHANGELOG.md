# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit (rather than a
library with a stable API) reasonably can — a patch bump means "fixes", not
a promise that every flag and exit code is contractually frozen.

## [0.5.1] — 2026-09-16

### Fixed

- **Eight occurrences of a banned product name shipped in the three
  engines**, including in the `--cdp-endpoint` help text a user reads
  (`"…or any antidetect browser that exposes a CDP URL"`) and in
  `selenium_scraper.py`'s module docstring. The naming rule is that the
  product is the **2Captcha Scraping Browser API**; everything else is "a
  remote browser". This repo was the only one of the family's seventeen with
  the phrase in shipped code.
- **The check that exists to prevent exactly that had three holes**, and the
  third is why the first two survived:
  - it banned the two compound forms of the name but not the bare two-word
    phrase — the gap the eight occurrences went through;
  - matching was case-sensitive, patched by listing one capitalised variant
    by hand, which covers exactly the casings someone thought of;
  - `smoke_test.py` was excluded from the scan **wholesale**, so the file
    most likely to pick up a stray phrase by copy-paste was the one file
    nobody scanned. It is now scanned like any other, which works because
    the phrases are assembled from pieces rather than written out.

  The scan is also anchored to this file's directory rather than to the
  working directory, and asserts a floor on how many files it saw: it used
  `glob("*.py")` relative to CWD, so running the suite from anywhere else
  scanned nothing and passed. A check that can quietly scan zero files is
  not a check.

  Verified by control: reintroducing the phrase into an engine — in a
  capitalisation the old list would have missed — turns the suite red.

## [0.5.0] — 2026-09-15

> **Read this before upgrading.** Two things change for an existing caller.
>
> **A run that gathered nothing and never got the page now exits 5**, where
> it used to exit 4. If your automation branches on 4 meaning "anything went
> wrong", it needs the new row — 4 now means only "the page was fetched and
> held nothing", which is the one case that says something about the
> catalogue. See the exit-code table in the README.
>
> **A blocked run now exits 3 where many of them used to exit 4.** Akamai's
> refusal page was not recognised as a block, so every run refused by the
> edge reported an empty category. Nothing about your setup changed; the
> reports were wrong before.
>
> Everything else is additive: `--mode detail`, `--resume`, `--webhook`,
> console scripts, and a container image on GHCR.

This release is the work from a third-party audit taken on v0.4.2, plus what
running the code against the live site turned up while working through it.
The audit's own P0 was five items; all five are done, and two of its
suggested fixes turned out not to work as written (see the first entry
below). Of P1, four of six are done and two are declined with reasons in the
PR. Of P2, three of four.

### Fixed

- **Akamai's "Access Denied" page was not recognised as a block.** Every
  request to farfetch.com from a datacentre address returns 318–426 bytes
  under HTTP 403 — a title, a refusal sentence, and an `errors.edgesuite.net`
  reference. It carries none of the challenge markers this parser knew, which
  describe Akamai's *challenge* page, so `detect_bot_challenge()` returned
  `None` and a plainly blocked run exited **4**, telling the caller the
  category was empty. Reproduced live on 2026-09-14 (exit 4) and re-run after
  the fix (exit 3).

  Two details are pinned by fixtures because both are easy to get wrong:

  - The same page reaches the parser in **two spellings**. Akamai
    entity-escapes the punctuation on the wire
    (`errors&#46;edgesuite&#46;net`), while a browser parses it and
    `page.content()` serialises it back out plain. A literal
    `errors.edgesuite.net` marker therefore matches the three browser engines
    and silently misses `scraper_api_client` — measured 0 occurrences in the
    raw form. Entities are normalised before matching.
  - Only `errors.edgesuite.net` is matched, never a bare `edgesuite`:
    edgesuite.net is also an ordinary Akamai *asset* domain, and a marker
    that fires on a good page is worse than no marker. Counted at zero on all
    seven real-capture fixtures in the suite.

- **A run that never got the page reported "0 products".** A navigation
  timeout, a dead or unauthenticated proxy and a genuinely empty category
  were one value to an automated caller, which wants three different
  responses. They are now `5`, `5` and `4` respectively, and a dead exit is
  recorded as `proxy_unusable` rather than as a timeout.

  `5` rather than a new code: the contract already reserved it for a failed
  transport, and the browser engines simply had no way to say so.
  `scraper_api_client`'s `EXIT_API_ERROR` is now an alias of the shared
  constant, so there is one definition of 5 instead of two that can drift.

- **A broken parser reported itself as an empty category.** A page that was
  served, that links to eighteen products, and that parses to zero rows is
  this repo's bug — but it exited 4 with the same message as a genuinely thin
  category, which sends the reader to check the URL instead of the JSON-LD.

  `stop_reason` is now `parse_drift` in that case, the log says so in as many
  words, and the canary fails on it by name. The exit code deliberately stays
  4: the catalogue question really was answered, and inventing a seventh code
  would diverge from the family contract. What changes is that the run says
  WHOSE fault it is.

  Not a hypothetical — the CSS fallback drops a product link whose tile
  yields no price text, so a tile-scoping failure turns a full page into no
  rows. That is the "junk-link data theft" shape this family has hit before,
  seen from the other side. Both directions are pinned: a full-but-unparseable
  page sets the flag, an empty one does not, and a page that parses fine does
  not either.

- **The canary interpreted exit codes it no longer matched.** Its table had
  no entry for 5 or 6, so a fetch failure was announced as an unknown code,
  and it explained 124 as "the page never became ready" — which is wrong
  twice: 124 is Selenium's watchdog for *chromedriver failing to start*, and
  the canary runs Playwright. Nothing executed that table, so it drifted
  silently. It now lives in `.github/canary_check.py`, which imports the
  constants and is asserted against them by the offline suite.

- **The canary's proxy secret was a commented-out `--proxy` line**, so
  enabling it meant setting a secret *and* remembering to edit the workflow —
  and would have put a credential in `argv`. It goes through the environment
  `env_config.py` already reads.

### Changed

- **The canary is two signals instead of one.** `reachability-no-proxy` runs
  free every day and reports a *block* as a skip with a notice rather than a
  failure: from a GitHub runner — a datacentre address — a refusal says
  something about the address, not about Farfetch, and a check that is red
  every morning is one everybody learns to ignore. Everything else still
  fails it. `production-like` is the authoritative signal, runs only when
  `FARFETCH_PROXY` is set, skips with a notice when it is not, and is strict
  about a block too, because from a residential exit a block *is* news.

- **An error status skips the readiness wait.** Playwright was discarding the
  `Response` that `goto()` returns, and with it the most reliable signal a
  refusing edge gives. It now records the status; a 4xx/5xx is not a page
  waiting to paint, so the 20-second wait for product markers — previously
  spent on every attempt of every page of a blocked run — is skipped. A 4xx
  whose body names no known vendor is reported as a fetch failure rather than
  as a block: we know it is not the listing, but not who refused us.

- Selenium's `124` is now the named `EXIT_DRIVER_TIMEOUT` rather than a magic
  number, which is how the canary came to describe it as something else.

- All three engines describe a block through one shared `describe_block()`,
  so a refusal is no longer logged as a "challenge page" — wording that sends
  the reader looking for a widget that is not there.

- **Every numeric CLI flag is range-checked**, and one of them was doing real
  damage. `--retries 0` was accepted by all three browser engines, and the
  attempt loop is `range(1, retries + 1)` — so zero attempts means
  `page.goto()` is never called. The run parsed `about:blank` (39 bytes,
  against 559 for the same URL with `--retries 1`) and exited 4: "the page
  was fetched and held nothing". A wrong answer about the catalogue, reached
  by typing a number.

  Validators live in `arg_types.py` as argparse `type=` callables, so argparse
  produces the usage message and exit 2 itself, before a browser launches.
  Zero stays allowed where it names a real behaviour (`--delay 0`,
  `--retry-delay 0`, `--proxy-block-retries 0`) and is refused where it names
  none. `--min-score` becomes `choices=[0.3, 0.7, 0.9]` — the API accepts
  exactly those three — and the Scraper API's `--timeout` is bounded to the
  1-120 the API documents.

- **A missing chromedriver exited 1 (crash) rather than 2 (setup).** It is
  something the operator fixes in one command, and `cli_entry.py` answers the
  equivalent question — an engine's driver library absent — with 2. The two
  should not disagree about the same kind of problem.

- **Three engines declared `def scrape(args) -> None`** while returning an
  exit code that `main()` passes straight to `sys.exit`. Harmless at runtime,
  and an annotation a reader would have trusted. Found by mypy, which is now
  part of CI.

### Added

- **`--mode detail`: one row per SIZE.** The listing tells you a product
  exists; the product page tells you which sizes are in stock and what each
  costs. `--mode detail` fetches the listing as before, then opens each
  product and emits a `ProductVariant` row per size — keyed on the variant
  sku (`36899289-19`), with `product_id` grouping a product's sizes.

  The detail page publishes the **whole discount chain** as structured data,
  unlike the listing, which publishes the middle of it. So `price` and
  `original_price` are facts there, `discount_pct` is arithmetic, and no DOM
  price overlay is ported — it could not work anyway, because a detail page's
  DOM holds zero rendered price strings.

  `--max-products N` caps the crawl; a capped or partly-failed crawl is
  reported as `partial` with `max_products_reached` / `detail_pages_failed`
  rather than as a complete view.

  The sidecar now records `mode`, and `diff_runs.py` refuses to compare a
  listing run with a detail run — the one refusal `--force` does not override,
  because the two have different row shapes and different keys, so every line
  of that diff would be an artefact of the comparison.

  Measured on seven captured product pages rather than assumed: there are no
  ratings on a detail page (`aggregateRating` appears nowhere), no merchant or
  boutique, and no shipping details, so none of those became columns.

  Verified on a **second market**, which is what turns a guess about locales
  into a measurement. The same four products were captured on a DE and a US
  exit: the variant sku is byte-identical on both, while size, title, colour,
  composition and category are all localised — so a cross-market comparison
  joins on `sku` and `size` is display text. Prices are set per market rather
  than converted (60 EUR / 90 USD; 1020 EUR / 598 USD), which is worth knowing
  before anyone reads a cross-market difference as an arbitrage. Both markets
  are pinned by real fixtures.

- **`--resume`, and a checkpoint every multi-page run writes.** A run that
  died on page 17 of 20 used to start again at page 1.
  `<out>.progress.json` is written after every page and deleted by a run that
  completes; `--resume` continues from it.

  Not behind a flag on the first run, on purpose: nobody passes
  `--checkpoint` on the run that is about to be killed, and by then the pages
  are gone.

  Two refusals, both deliberate. A checkpoint written for a **different** URL,
  page count or category is refused with the difference named — resuming the
  wrong one merges two categories into one file, which looks like a successful
  scrape of something that was never scraped. And pages are only SKIPPED when
  pagination is addressable (`?page=N`): where the site chains next-links,
  page 17 is unreachable without fetching 16, so it says so rather than
  silently producing a run missing its middle. Changing `--retries`,
  `--proxy` or `--concurrency` between the two runs does not invalidate it —
  none of them changes what a page contains.

  Verified end-to-end against a local stand-in listing: run 1 hits a 503 on
  page 3 and keeps pages 1-2; run 2 with `--resume` requests only pages 1, 3
  and 4 — page 2 never appears in the server's access log.

- **Run id, timings and quality metrics in the sidecar.** `run_id` (logged on
  the first line too, so a log line and an artefact can be tied together),
  `started_at`, `duration_s`, and a `quality` block giving the coverage of
  every nullable column as a fraction. That last was previously computed only
  inside `.github/canary_check.py`, so every other consumer had to recompute
  it — or, in practice, not notice that a run returned the right NUMBER of
  rows with a column silently empty.

- **`--webhook URL`** POSTs the run summary plus the exit code when the run
  finishes. It fires on **failure too**, which is the main use: a run that
  gathers nothing deliberately writes no sidecar, so anything keyed on the
  sidecar is silent for exactly the runs worth an alert. It never fails the
  run, is bounded at 10s with no retries, and never logs the URL — most
  webhook URLs carry their token in the path, and `requests` puts the full URL
  into the text of every connection error. `FARFETCH_WEBHOOK` in `.env` is
  preferred over the flag, because argv is readable by anything that can run
  `ps`.

- **Console scripts.** `pip install .[playwright]` now produces
  `farfetch-scraper`, `farfetch-scraper-playwright`, `-selenium`,
  `-puppeteer`, `-api`, `-diff`, `-fingerprint` and `-env`.
  `python3 playwright_scraper.py ...` keeps working unchanged.

  Installing one engine still puts all three engine commands on PATH, so the
  other two now exit 2 with the pip line to run, via `cli_entry.py`. They used
  to print a `ModuleNotFoundError` traceback for a command the install itself
  had just created. The engines still import their drivers at module level —
  that is how the offline suite detects an absent engine, and moving those
  imports is how a sibling repo let CI run against a stub version.

- **Ruff and mypy in CI**, both narrowly configured, with the boundaries
  argued in `pyproject.toml`. Ruff is `F`/`E9`/`B`; its broader defaults
  produce 205-311 findings here and the largest groups demand Python 3.10+
  annotation syntax from a package that supports and tests 3.9 — advice that
  would break a supported version. mypy gates the six shared-core modules,
  which were already clean; the engines' 26/7/6 findings are pinned as a
  measured known limitation rather than half-guarded.

- **Dependabot**, weekly, for pip and github-actions. Linter pins moved into
  `requirements-dev.txt` so they are visible to it — a pin written inline in a
  workflow `run:` step is invisible to Dependabot and rots quietly.

### Changed

- **`engine-smoke` is a matrix of one venv per engine**, each installed from
  its own requirements files, with `pip check` as a real gate and an assertion
  that the installed version satisfies the pin. It used to be
  `pip install playwright pyppeteer selenium` into a single environment, with
  a comment saying `pip check` would complain and that this was "expected and
  harmless". It is not: the three pin mutually unsatisfiable versions of
  `pyee` and `urllib3`, so pip resolves the conflict by reaching for whatever
  it can — on a sibling repo that meant pyppeteer 0.0.25, a stub, against a
  requirements file asking for >=1.0.2, with CI green throughout.

  Each leg also installs the package and checks the console scripts, including
  that the two engines it did NOT install explain themselves.

- **`pytest` reports 291 results instead of 1.** `tests/test_smoke.py` runs
  the suite once and turns each `[PASS]`/`[FAIL]` line into its own pytest
  result, so `-k` selects a check and a failure names it. Splitting
  `smoke_test.py` itself into thematic pytest modules was deliberately not
  done: the single-file design is an explicit invariant, and a second copy of
  the checks is what this wrapper exists to avoid.

- `smoke_test.py` prints a machine-readable `SKIPPED_ENGINES:` line, which is
  what lets each matrix leg assert that ITS engine ran rather than grepping
  prose.

- **`smoke_test.py`'s `main()` was one 2,650-line function.** It is now a
  preamble plus 27 section functions, one per the banner comments already in
  the file. `python3 smoke_test.py` and `pytest` behave identically; the file
  count, the runner and the no-pytest-required property are unchanged.

  Splitting into separate pytest modules — the other half of what the audit
  proposed — is still not done, and the reason is now measured rather than
  asserted. The attempt showed the sections are not independent: 133 names
  leaked across the boundaries. Most were things that belonged at module
  scope anyway, but a real remainder was one fixture built once and asserted
  across three sections, so those were merged back rather than forced apart.
  A module split would need either a second copy of the checks or the loss of
  `python3 smoke_test.py` — which runs with no pytest installed, and pytest
  is not in `requirements.txt`.

  The split is verified behaviour-preserving by diffing every check LABEL
  before and after: 315 before, 315 after, none lost, none added. That
  mattered — the first attempt left a `return ok` in the middle of a merged
  body, which made 7 checks dead code and passed as 308 of 315. Four new
  checks guard the shape: a floor on section count, a ceiling on `main()`,
  every section called exactly once, and no section returning before its end.

### Testing

- Offline suite: **319 checks**, up from 232; `pytest` reports 321 results. New coverage includes the
  Dockerfile's COPY list against the entrypoint's import graph — a check
  CLAUDE.md §10 calls for after every repo in this family shipped an image
  that died on every invocation, and which did not exist here. It failed on
  its first run, catching a module added in this same batch.

## [0.4.3] — 2026-09-11

### Fixed

- **`fingerprint_client.py` could not read the key from `.env`.** `--key`
  defaulted to `os.environ.get("TWOCAPTCHA_KEY")` and only that, so a key put
  in `.env` — exactly as §3, the README and `.env.example` instruct — worked
  for every engine and failed HERE with "No API key". A documented mechanism
  not applied on one path, which is the shape of half the defects §16 lists.

  It now reads through `env_config.env_value`, calling `load_env()` itself
  because this is a standalone entry point that no engine has necessarily run
  first. Going through the loader rather than `os.environ` is measured rather
  than stylistic: with `TWOCAPTCHA_KEY=your_2captcha_api_key_here` exported,
  the old path sent the placeholder to the API and reported "Fingerprint API
  rejected the key (401) — note this is a separate subscription", sending the
  reader off to check a subscription they never needed; the loader says
  "still set to the placeholder from .env.example" instead.

  Found on a sibling repo's first live `--fingerprint` run, then checked
  across the family before patching, per §16: five repos had it and one had
  already fixed it. Pinned by a check verified to fail on the old code —
  including that the help string does not interpolate its default, which is
  one substring away from printing a live credential to anyone who types
  `--help`.

---

## [0.4.2] — 2026-09-11

### Fixed

- **`--fingerprint` dropped `deviceScaleFactor`, so the identity
  contradicted itself.** `playwright_context_kwargs` mapped the user agent,
  the locale, the timezone and the screen onto the browser context and
  ignored the scale factor the fingerprint API returns beside them. Measured
  2026-09-11 against the live API and a live browser: a fingerprint stating
  `deviceScaleFactor: 1.25` produced a browser reporting
  `window.devicePixelRatio === 1` — the paid identity saying one thing and
  the browser another, on every run, silently, on an axis any fingerprinter
  reads for free. Playwright takes it as its own context option, so the fix
  is to pass it; verified in a live browser both ways and pinned in the
  offline suite.

  Found while auditing a new sibling repo against the family notes. All five
  repos in this family had it.

---

## [0.4.1] — 2026-09-08

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
