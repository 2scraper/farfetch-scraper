# farfetch-scraper

[![tests](https://github.com/2scraper/farfetch-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/farfetch-scraper/actions/workflows/tests.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue?style=flat-square)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Playwright · Selenium · Puppeteer](https://img.shields.io/badge/engines-Playwright%20%C2%B7%20Selenium%20%C2%B7%20Puppeteer-orange?style=flat-square)](#engines)
[![Runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen?style=flat-square)](#do-you-need-a-paid-service-for-this)

A working scraper for **farfetch.com** listing pages — category, hub, search and
brand. Not a client for a hosted API: the code in this repo opens the site,
handles the page itself, and writes JSON and CSV. Run it on your own machine, on
your own IP, and read every line of what it does.

**Clone and run.** No account, no API key, no signup — a browser it launches
itself clears Farfetch's bot management on a clean residential IP, and a
filtered category page yields 96 products. Optional 2Captcha integrations
(solving, managed browser, proxies, fingerprints — all paid, each billed
separately) are there for when you outgrow that; see
[do you need a paid service for this](#do-you-need-a-paid-service-for-this).

```bash
git clone https://github.com/2scraper/farfetch-scraper && cd farfetch-scraper
pip install -r requirements.txt
pip install -r requirements-playwright.txt && playwright install chromium

python3 smoke_test.py          # offline checks, ~1s, no network, no key

python3 playwright_scraper.py \
  --url "https://www.farfetch.com/shopping/kids/girls-clothing-4/items.aspx" \
  --pages 1 --format both --out girls_clothing
```

Output shape: [`sample_output.json`](sample_output.json) /
[`sample_output.csv`](sample_output.csv) — three rows cut from a real run, so you
can see the exact fields before installing anything.

If a run returns 0 products, the usual cause is a hub URL rather than a filtered
category URL — see [Troubleshooting](TROUBLESHOOTING.md).

---

## Contents

- [What people use it for](#what-people-use-it-for)
- [Do you need a paid service for this](#do-you-need-a-paid-service-for-this)
- [Engines](#engines)
- [Configuration](#configuration)
- [Flags](#flags) · [Exit codes](#exit-codes)
- [Output](#output)
- [Using 2Captcha](#using-2captcha)
- [How the parser works](#how-the-parser-works)
- [Site-specific behaviour](#site-specific-behaviour)
- [Contributing](CONTRIBUTING.md) · [Troubleshooting](TROUBLESHOOTING.md) · [Legal](#legal)

---

## What people use it for

- **Price and discount monitoring** — `price`, `original_price` and
  `discount_pct` per SKU. Re-run on a schedule and diff on `sku`.
- **Assortment tracking** for a brand or category — what is listed, and what
  disappeared since the last run.
- **Availability** — `in_stock` per product.
- **Resale premium checks** — Farfetch retail price against a marketplace price
  for the same item.
- **Currency and market comparison** — the same category priced from a different
  country, by changing the exit IP rather than the URL. Farfetch keys currency
  and language off the exit IP; see [Geo-redirect](#site-specific-behaviour).

Listing-level fields only. This repo does not open product pages, so no sizes,
materials, colour variants or descriptions — those live one level deeper.

---

## Do you need a paid service for this?

Straight answer, because it decides whether you should keep reading:

**For one category page, once — no.** An ordinary local Chromium on a clean
residential IP cleared Akamai Bot Manager silently and returned all 96 products,
with no key and no proxy. That is measured, not assumed. Anyone telling you the
first page is impassable is selling something.

**At volume, or from a specific country, or when a challenge does appear — yes,
and the honest list is short.** What a paid service actually buys:

| You need | Why your own setup runs out | What covers it |
|---|---|---|
| Many requests per hour | Behavioural scoring degrades as one address repeats. Your own IP is the one you cannot rotate. | [Proxies](#3-proxies) |
| A specific country's prices | Farfetch decides currency and language from the exit IP. Your address gives you exactly one market. | [Proxies](#3-proxies) or the [Scraping Browser API](#2-scraping-browser-api) `country-` segment |
| A challenge that does not clear | A real browser usually clears it; when it does not, something has to answer. | [Captcha solving](#1-captcha-solving), or the browser solving it in-session |
| No browser infrastructure to run | Managing Chromium, versions and concurrency is its own job. | [Scraping Browser API](#2-scraping-browser-api) |
| Consistent device identity | A default automation fingerprint is uniform and therefore distinctive. | [Fingerprints](#4-fingerprints) |

All four are 2Captcha products and all four are paid, each billed separately;
one API key covers them. Nothing in this repo requires any of them, and every
integration is a flag you can leave unset.

### Compared with a managed scraping API

Different tools, and the honest trade is not "open source is better":

| | This repo | A hosted scraping API |
|---|---|---|
| Cost to start | Nothing | A subscription, typically per-record or per-month |
| Runs offline / on your own machine | Yes | No |
| You can read and change the extraction | Yes — one parser file, ~500 lines | No |
| Someone else fixes it when the site changes | No, you or a PR | Yes, that is what you pay for |
| Concurrency and IP pool | Yours to build | Included |
| Vendor lock-in | None, MIT | The output schema is theirs |

If your team does not want to own a scraper, buy one. If you want to know exactly
what is being requested and how the data is derived, this is that.

---
---

## Engines

Same CLI, same parsing core, same output. Pick by how you want to reach a
browser.

| Script | Engine | Own browser | Remote browser over CDP |
|---|---|---|---|
| `playwright_scraper.py` | Playwright | ✅ recommended | ✅ |
| `puppeteer_scraper.py` | pyppeteer | ✅ | ✅ |
| `selenium_scraper.py` | Selenium | ✅ | ❌ see below |
| `scraper_api_client.py` | HTTP API, no local browser | — | ✅ via `--cdp-url` |

**Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
`connect_over_cdp` and Puppeteer's `browserWSEndpoint` take a full
`ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere to put
a password — it is not a generic "connect to CDP" option. Use Playwright or
pyppeteer for a remote endpoint; Selenium is fine against a browser it launches
itself, or a local `--remote-debugging-port`.

**Farfetch listing pages need a rendered browser.** The product data is in
JSON-LD the page builds client-side, and the product tiles render client-side
too. A plain HTTP fetch of the HTML — no browser — is not enough on this site,
whichever client performs it. Use a browser engine, or the Scraper API client
with `--cdp-url` pointed at a browser.

```bash
# your own browser
python3 playwright_scraper.py --url "$URL" --pages 1 --out girls

# a remote browser over CDP
python3 playwright_scraper.py --url "$URL" --pages 1 --out girls \
  --cdp-endpoint "ws://USER:PASS@HOST:9222"
```

---

## Configuration

Credentials go in a `.env` file next to the scripts, not on the command line:

```bash
cp .env.example .env
$EDITOR .env
python3 env_config.py     # reports what got picked up, without printing secrets
```

| Variable | Backs | Needed for |
|---|---|---|
| `TWOCAPTCHA_KEY` | `--twocaptcha-key`, `--key` | Solving, fingerprints, `scraper_api_client.py` |
| `FARFETCH_CDP_ENDPOINT` | `--cdp-endpoint`, `--cdp-url` | Reaching a remote browser |
| `FARFETCH_PROXY` | `--proxy` | A proxy for a locally launched browser |
| `FARFETCH_URL` | `--url` | Convenience only |

Precedence, highest first: **explicit flag → exported environment variable →
`.env` → default.** An already-exported variable is never clobbered by the file,
so a CI secret keeps working.

`.env` is in `.gitignore`. There is no new dependency for this —
`env_config.py` parses the file itself, and defers to `python-dotenv` only if you
already have it.

**Prefer this over `--twocaptcha-key` on the command line.** A secret in `argv`
is readable by anything on the machine that can run `ps`, and it lands in your
shell history.

---

## Flags

The three browser engines share these:

| Flag | Default | Description |
|---|---|---|
| `--url` | *(required)* | Listing URL |
| `--pages` | `1` | Listing pages to paginate through |
| `--category` | derived from the URL | Free-text label written into every row |
| `--format` | `both` | `json`, `csv`, `both` |
| `--out` | `farfetch_products` | Output prefix, also used for debug dumps |
| `--delay` | `2.0` | Seconds between pages |
| `--cdp-endpoint` | – | Attach to a running browser instead of launching one |
| `--proxy` | – | Proxy for a self-launched browser; ignored with `--cdp-endpoint` |
| `--twocaptcha-key` | – | 2Captcha API key (or set `TWOCAPTCHA_KEY`) |
| `--captcha-api` | `v2` | `v2` (current JSON API) or `v1` (legacy `in.php`) |
| `--min-score` | `0.7` | reCAPTCHA v3 score to request — `0.3`, `0.7` or `0.9` only |
| `--allow-empty` | off | Write output even when 0 products were found |
| `--dump-html` | – | Save the exact HTML the parser was given, on success too |
| `--headless` / `--headful` | headless | Local browser only |

Playwright also takes `--fingerprint`, `--fp-tags`, `--fp-country`
([fingerprints](#4-fingerprints)). Selenium also takes `--chrome-binary`,
`--chromedriver`, `--disable-build-check` and `--driver-timeout` — see
[Troubleshooting](TROUBLESHOOTING.md) if a local Selenium run will not start.

`scraper_api_client.py` takes `--url`, `--key`, `--cdp-url`, `--wait-text`,
`--timeout`, `--retries`, `--dump-html`, `--out`, `--format`.

### Exit codes

A contract, not decoration — the harness and any pipeline can branch on these.

| Code | Meaning |
|---|---|
| `0` | Products written |
| `1` | Unhandled error |
| `2` | Bad usage |
| `3` | Blocked before parsing — a bot-check or challenge page |
| `4` | Ran fine, parsed **0 products** |
| `5` | Remote API returned an error |
| `124` | Self-imposed timeout expired |

**Exit 4 writes nothing.** A run that finds nothing leaves the previous output
file intact rather than replacing it with `[]`, because a consumer cannot tell an
empty category from a failed run. Pass `--allow-empty` when empty is the expected
answer; it writes the file and still exits 4.

---

## Output

```json
{
  "source": "farfetch.com",
  "scraped_at": "2026-08-26T22:40:31.652535+00:00",
  "url": "https://www.farfetch.com/de/shopping/kids/polo-ralph-lauren-kids-t-shirt-mit-polo-bear-print-item-35132321.aspx",
  "sku": "35132321",
  "title": "T-Shirt mit Polo Bear-Print",
  "brand": "POLO RALPH LAUREN KIDS",
  "price": 59.0,
  "currency": "EUR",
  "original_price": null,
  "discount_pct": null,
  "rating": null,
  "review_count": null,
  "in_stock": true,
  "image_url": "https://cdn-images.farfetch-contents.com/35/13/23/21/35132321_69402936_480.jpg",
  "category": "girls-clothing-4"
}
```

Fifteen columns, in that order, in both formats. Real rows are in [`sample_output.json`](sample_output.json) and
[`sample_output.csv`](sample_output.csv) — three products cut from an actual
run, not hand-written, so the field names there are the field names you get.

Three things about that row, because each looks like a bug and is not:

- **`currency` is EUR and the title is German.** That run exited in Europe.
  Farfetch keys currency, language and the URL locale off the exit IP, not off
  the URL you request — see [Geo-redirect](#site-specific-behaviour). The same
  category through a US address returns USD and English.
- **`original_price` and `discount_pct` are `null` here because that product
  was not discounted.** On a sale item all three price columns populate. Worth
  knowing how: Farfetch shows three prices per discounted tile — original 245 €,
  sale 135 €, final 108 € — and its JSON-LD publishes only the middle one. The
  parser reads the tile as well, so `price` is what a customer pays (108),
  `original_price` is the list price (245), and `discount_pct` is computed from
  those two rather than read from the "-45%" the page prints, which is only the
  first of two compounding discounts. Pass `tile_prices_overlay=False` to
  `parse_products()` for the raw JSON-LD figures instead.

  On a sale page this corrects nearly every row (95 of 96 on the run this was
  built against). A row is deliberately left alone when the JSON-LD price is not
  one of the numbers in its tile — the two views then disagree about which
  product it is, and overwriting a correct row is worse than leaving one
  uncorrected. It logs a warning naming the SKU when that happens.

- **`brand` casing is inconsistent** — 16 of 96 brands in that run were all-caps
  (`POLO RALPH LAUREN KIDS`), the rest mixed-case (`Bonpoint`). That is how the
  site stores them. No brand appears in two different casings, so grouping by
  `brand` is safe; normalising is left to you, since it would damage names like
  `DSQUARED2`.

`rating` and `review_count` are `null` on every row: the listing JSON-LD carries
no `aggregateRating` at all.

---

## Using 2Captcha

Four products, each optional and independently useful.

### 1. Captcha solving

[API docs](https://2captcha.com/api-docs) · `--twocaptcha-key` or
`TWOCAPTCHA_KEY`

Runs after **every** navigation, on any page — not scoped to one URL. Both
detectors always run: one over the static HTML, one in the live page over
`___grecaptcha_cfg`, and the results are reconciled.

That reconciliation matters more than it sounds. Farfetch's own wrapper element
declares `data-version="v3"`, while the Google loader the page actually ships is
`api.js?render=explicit` with `size: "invisible"` — the v2-invisible signature.
The parameters are not interchangeable:

| Variant | 2Captcha task | Parameters |
|---|---|---|
| v3 | `RecaptchaV3TaskProxyless` | `minScore` (0.3/0.7/0.9 only) + optional `pageAction` |
| v2 invisible | `RecaptchaV2TaskProxyless` | `isInvisible: true` |
| v2 checkbox | `RecaptchaV2TaskProxyless` | — |

v3 parameters sent for a v2-invisible widget buy a token the site rejects, so the
loader wins over the site's own label.

### 2. Scraping Browser API

[Product](https://2captcha.com/scraper/browser-api/) · `--cdp-endpoint`

Managed Chrome reached over a CDP WebSocket, with proxies and fingerprints
included:

```
ws://{login}-zone-scraping_browser-country-{cc}-pid-{profileId}:{password}@cb.2captcha.com:9222
```

| Segment | Meaning |
|---|---|
| `zone-scraping_browser` | Product zone |
| `country-us` | Exit country for the session. Farfetch keys currency and language off the exit IP, so this also decides those — as does a country-targeted `--proxy`. |
| `pid-p1` | Profile id: cookies and storage persist per profile |

**One live CDP connection per profile.** A second connection to the same `pid`
is rejected (`profile_locked`); use different pids for parallel sessions. Each
distinct pid creates a profile server-side and profiles are capped per account
(`ERROR_MAX_PROFILES`), so reuse them rather than minting one per run.

The browser can also solve challenges itself, before this project's own solver
gets a turn:

```python
cdp = context.new_cdp_session(page)
cdp.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
cdp.on("Captcha.detected",      lambda *_: ...)
cdp.on("Captcha.waitForSolve",  lambda *_: ...)
cdp.on("Captcha.solveFinished", lambda *_: ...)
cdp.on("Captcha.solveFailed",   lambda *_: ...)
```

Equivalents: `page.target.createCDPSession()` in pyppeteer,
`driver.execute_cdp_cmd(...)` in Selenium. All three engines enable it right
after connecting and fall back silently if the endpoint does not implement the
domain. Treat `solveFinished` as the success signal and keep the fallback path —
do not assume every detection completes.

Never set a user agent over `--cdp-endpoint`: it contradicts the fingerprint the
remote browser already presents, which is worse than not setting one.

### 3. Proxies

[Product](https://2captcha.com/proxy) · `--proxy`

Residential, premium, datacenter, ISP, mobile and SOCKS5, with country/state/city
targeting and configurable IP lifetime. Also sold as **2prx.com** — the same
product, not a second service.

```bash
python3 playwright_scraper.py --url "$URL" \
  --proxy "http://ACCOUNT:PASSWORD@HOST:9999"
```

Playwright receives server/username/password separately, so credentials never
reach a child process's command line. Two caveats: Selenium's `--proxy-server`
flag cannot carry credentials (use Selenium-Wire or an extension), and `--proxy`
is ignored with `--cdp-endpoint`, where the remote browser has its own.

**Match the proxy's country to your fingerprint's.** A US fingerprint arriving on
a German IP is a contradiction that is cheap to detect.

### 4. Fingerprints

[Product](https://2captcha.com/buy-fingerprints) · `--fingerprint`

```bash
python3 playwright_scraper.py --url "$URL" \
  --fingerprint --fp-tags "Windows,Chrome,Desktop" --fp-country us
```

`fingerprint_client.py` fetches one and applies it to a local Playwright context:
user agent, screen, locale, `navigator.platform` / `hardwareConcurrency` /
`deviceMemory`, and WebGL vendor/renderer on **both** `WebGLRenderingContext` and
`WebGL2RenderingContext` — patching only one leaves a mismatch easier to spot than
the original values. Responses are cached on disk, keyed on the filter set,
because the endpoint is billed per successful response with a per-minute cap.

Local browsers only. With `--cdp-endpoint` it is ignored: the remote browser
brings its own, and stacking two creates a contradiction.

It is a JS-level patch. A fingerprinter that cross-checks a claimed GPU against
real rendering output still wins — this raises the floor, it is not a disguise.

---

## How the parser works

`product_parser.py` does all extraction; `output_writer.py` holds the row model
and the JSON/CSV writers. Two paths, in order:

**1. JSON-LD (primary).** Reads `<script type="application/ld+json">`, walks
`ItemList` / `itemListElement` and pulls each `Product`. Handles both flat
Products and `ListItem`-wrapped entries.

**2. CSS + URL pattern (fallback).** Only if the first path yields nothing.
Anchors on `href` matching `-item-<digits>.aspx` — chosen because a URL pattern
outlives CSS class churn — then reads title, brand and prices from around the
matched link.

Three details that are easy to get wrong on this site:

- **The product URL is in `offers.url`, not `node.url`.** No product carries
  `node.url`. Read the wrong field and every row points at the category page
  while title, brand and price all look correct — which is what makes it hard to
  notice.
- **There is no `sku` field.** The product id is in the URL, so both paths
  recover it from `-item-(\d+)\.aspx`.
- **Widen to the parent only when it holds exactly one item link.** More than one
  means the search escaped into a shared grid wrapper, where a product can
  inherit its neighbour's data.

Prices parse `$ € £ ¥` in either position and both decimal conventions
(`1,234.56` and `1.234,56`, disambiguated by whichever separator comes last),
with `currency` set from the symbol found.

---

## Site-specific behaviour

**Geo-redirect.** Farfetch redirects on **exit IP**, and the URL you request has
no say in it. A European address gives `/de/` URLs, `125 €` with the symbol after
the number, and localised product names; a US address gives `$125` and English.

So pin the exit IP, whichever way you reach the site:

- **A proxy** — set the country (and state or city) in the proxy's own targeting,
  then pass it with `--proxy`. Works for any local browser, and for the remote one
  too if you override its proxy there.
  See [2captcha.com/proxy](https://2captcha.com/proxy).
- **The Scraping Browser URL** — the `country-` segment picks the exit country for
  that session, so `country-us` gives USD and English without touching anything
  else.

Both routes do the same job: they decide which country Farfetch thinks you are
in. Pick one per run rather than setting both to different countries.

**Use filtered category URLs, not the bare hub.** `/shopping/kids/items.aspx` has
one JSON-LD block of type `Organization` and **zero** products;
`/shopping/kids/girls-clothing-4/items.aspx` has all 96. A hub URL from a European
IP would have returned zero products with nothing obviously wrong.

**Bot management.** Category pages sit behind Akamai Bot Manager (`_abck`,
`bm_sz`). A real browser clears it silently — including an ordinary local
Chromium on a clean residential IP. That is worth knowing before reaching for
anything heavier: what a managed browser and proxies buy you is running at volume
from many addresses without burning your own, not access to the first page.

**How much of the page has rendered varies, and it affects the prices.** Two
measurements, both real: an early capture of a filtered category page had 96
products in the JSON-LD but only 18 product anchors in the DOM, while two
captures of a sale page had all of them — 108 anchors for 96 products, the extra
few being recommendation tiles.

This is why the JSON-LD path is primary: it carries every product regardless of
what has painted, and a DOM-only scraper would under-report on the first kind of
page. But the discount correction described in [Output](#output) reads the
rendered tiles, so on a page that has only partly painted, some rows keep the
JSON-LD price. If a sale page comes back with far fewer discounted rows than it
should, that is the reason — raise the wait or re-run, and use `--dump-html` to
see what the parser was actually given.

**The sign-up modal has its own captcha**, unrelated to Akamai — a Google
reCAPTCHA inside the modal. Nothing in this repo submits that form.

---

## Legal

MIT licensed — see [LICENSE](LICENSE).

Scrape responsibly: public catalogue data only, at a rate that does not degrade
the site. Read Farfetch's terms of service and the law in your jurisdiction
before running at volume. This project deliberately never submits the
registration form, and you should not either.
