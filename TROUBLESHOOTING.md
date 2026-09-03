# Troubleshooting

Ordered by how often each one actually happens.

## 0 products, exit 4

Nothing was written, so a previous good output file is still intact. The engine
also dumped `{out}_page1_debug.html` and `.png` next to itself — open them, they
answer this in one look.

| What the dump shows | Cause |
|---|---|
| A challenge or "verify you are human" page | Bot management. Use a browser engine (not a plain HTTP fetch), a residential IP, or a remote browser via `--cdp-endpoint`. |
| A real page, prices visible, still 0 rows | The JSON-LD path found nothing and the CSS fallback did not match. Check that product links still match `-item-<digits>.aspx`. |
| A real page in a different language, prices like `125 €` | Fine — that parses. If rows are still 0, it is not the locale. |
| A near-empty page | The hub URL. `/shopping/kids/items.aspx` has zero products in its JSON-LD; use a filtered category URL. |

## A local Selenium session will not start

Symptom: nothing happens for the whole `--driver-timeout`, then a message. The
timeout is a ceiling, not a diagnosis — the cause is almost always one of four
things, in this order.

**1. No browser on the machine.** chromedriver does not attach to a browser
locally, it *launches* one, and when there is none where it looks it waits rather
than reporting. Any Chromium-family build works:

```bash
ls -d /Applications/*.app ~/Applications/*.app 2>/dev/null | grep -i 'chrom\|brave\|edge\|arc'
mdfind -name 'Chrome.app' | head          # macOS

python3 selenium_scraper.py --url "$URL" \
  --chrome-binary '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

No Chrome at all? Playwright's bundled Chromium is a real browser on disk:

```bash
python3 -c "from playwright.sync_api import sync_playwright as p; s=p().start(); print(s.chromium.executable_path)"
```

**2. Driver and browser majors differ.** chromedriver drives that exact binary,
so the majors must match. The scraper compares them before spending the timeout
and stops with both numbers. Get a matching driver from
[Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/), or
pass `--disable-build-check` to try anyway — if the protocol really differs you
get an error naming the incompatibility, which beats a refusal to start.

**3. macOS quarantine.** A downloaded driver can be blocked with no visible
error: `xattr -d com.apple.quarantine <chromedriver>`.

**4. A stale chromedriver** from an earlier run still holding its port:
`pkill -f chromedriver`.

To see chromedriver's own words instead of our timeout, cut Selenium out:

```bash
<chromedriver> --port=9515 --verbose &
curl -s -X POST localhost:9515/session \
  -d '{"capabilities":{"alwaysMatch":{"browserName":"chrome"}}}'
```

Session creation legitimately takes ~50s when launching a fresh profile, so the
local budget defaults to 150s (60s with `--cdp-endpoint`, where waiting longer
is pointless).

## Selenium cannot reach an authenticated remote CDP endpoint

Not fixable at this layer, and worth understanding rather than retrying.
chromedriver implements `debuggerAddress` as exactly three steps: `GET
/json/version`, read `webSocketDebuggerUrl`, open that WebSocket. None of them
carries credentials, and there is no option to add them. Playwright and Puppeteer
take a whole `ws://user:pass@host:port` and authenticate on the upgrade.

Use `playwright_scraper.py` or `puppeteer_scraper.py` for a remote endpoint.
Selenium is fine against a browser it launches itself, or a local
`chrome --remote-debugging-port=9222`, where no auth is involved.

## Remote browser: `profile_locked`, `proxy_timeout`, HTTP 500

One live CDP connection per profile. A second connection to the same `-pid-` is
refused. Either wait for the first to finish, or use a different pid for parallel
work.

If a **brand-new** pid is also refused, the account may be out of profiles
(`ERROR_MAX_PROFILES`) — every distinct pid ever used created one. Rotating the
pid again makes that worse. Check the dashboard and remove profiles you no longer
need.

## The captcha is detected but never solved

Expected, and handled: keep the fallback path. Detection is not a promise of a
solve, so treat `Captcha.solveFinished` as the only success signal and let the
scraper's own solver (`--twocaptcha-key`) take over otherwise.

If a solve returns a token the site rejects, check the variant. Farfetch's own
markup labels its widget v3 while the loader it ships is v2-invisible, and the
parameter sets are not interchangeable. `captcha_solver.py` reconciles the two
and prefers the loader; a log line reports when they disagree.

## A sale page comes back with no discounts

`original_price` and `discount_pct` are `null` on every row, on a page where the
site plainly shows struck-through prices.

Farfetch's listing JSON-LD publishes only one price per product, and on a
discounted item it is the intermediate one — the sale price before a site-wide
promo, not the price at checkout. The parser therefore reads the rendered tiles
as well and takes the lowest number in each as `price` and the highest as
`original_price`. A run doing this logs one line:

```
Corrected prices on 95 of 96 products from the DOM (JSON-LD publishes the pre-promo price).
```

**If that line is absent, the correction did not run.** In order of likelihood:

1. **An older copy of the code.** The absence of the line is the tell.
2. **The tiles had not rendered.** The correction reads the DOM, so a page
   captured mid-paint keeps the JSON-LD prices. Re-run, and use `--dump-html` to
   inspect exactly what the parser was given.
3. **`tile_prices_overlay=False`** was passed to `parse_products()`.

A single row left uncorrected is normal and deliberate. When the JSON-LD price is
not one of the numbers in that product's tile, the two views disagree about which
product it is — usually an item listed by more than one boutique — and the row is
left alone rather than overwritten. It logs a warning naming the SKU.

## The discount percentage does not match the one on the page

`discount_pct` is computed from `original_price` and `price`, not read from the
"-45%" the page prints. On a two-stage discount that printed figure is only the
first stage: 245 -> 135 is the -45%, and the -20% promo on top of it takes the
price to 108, for -55.9% overall. The site also rounds its own display, so a tile
showing "-30%" on 65 -> 46 is really -29.2%.

The computed figure is what the buyer actually saves. If the two disagree by more
than a percentage point, the parser logs it at debug level.

## Dependency conflicts

Real, and safe to ignore in practice:

```
playwright  requires  pyee>=13,<14
pyppeteer   requires  pyee>=11,<12   urllib3>=1.25.8,<2.0.0
selenium    requires  urllib3[socks]>=2.6.3,<3.0
```

`pip check` reports both collisions. All four engines still import and run
together, because none of them exercises the incompatible parts. For a clean
environment, give each engine its own venv.

`requirements.txt` holds only `beautifulsoup4` and `requests`, so install it
normally. The engines live in `requirements-playwright.txt`,
`requirements-selenium.txt` and `requirements-puppeteer.txt` — install **one**.
Installing all three in one command is what triggers the conflicts above, and
pip may resolve them by downgrading something you wanted.

## Everything looks right but every URL is the category page

The parser is reading `node.url` instead of `offers.url`. Farfetch nests the real
product URL under `offers`, and no product carries `node.url` — title, brand and
price all still look correct, which is what makes this one expensive to find.
`smoke_test.py` pins the fixture that guards it.
