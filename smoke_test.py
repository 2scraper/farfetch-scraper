#!/usr/bin/env python3
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for the shared core
(product_parser.py + output_writer.py + captcha_solver.py detection).

Run this FIRST, before touching a real browser or farfetch.com, to confirm
your Python environment and the parsing/output logic are working:

    python3 smoke_test.py

Exits non-zero on any failure so it's CI-friendly.
"""

import json
import os
import re
import builtins
import inspect
import sys
import tempfile

from product_parser import parse_products, category_from_url
from output_writer import save, dedupe_by_sku, Product
from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections)
from diff_runs import diff_products

SAMPLE_LISTING_HTML = """
<html><body>
<div class="grid">
  <a href="/shopping/kids/marni-kids-logo-print-cotton-t-shirt-item-29998189.aspx">
    <img alt="Marni Kids logo-print cotton T-shirt" src="https://cdn.farfetch.com/img1.jpg">
    <span>Marni Kids</span><span>logo-print cotton T-shirt</span><span>$79</span>
  </a>
  <a href="/shopping/kids/diesel-kids-stibogi-drawstring-shorts-set-item-99999.aspx">
    <img alt="Diesel Kids Stibogi drawstring shorts set" src="https://cdn.farfetch.com/img2.jpg">
    <span>Diesel Kids</span><span>Stibogi drawstring shorts set</span>
    <span>$160</span><span>$80</span><span>$64</span><span>-50%</span><span>-20%</span>
  </a>
  <!-- noise: a link matching the -item- URL shape but with no price, sharing
       a grid parent with real products — must NOT steal a sibling's data -->
  <a href="/shopping/kids/some-brand-sizing-guide-item-1.aspx">Sizing guide</a>
</div>
</body></html>
"""

# Confirmed live on 2026-08-24 from a European exit IP: farfetch.com
# geo-redirects the same category URL to /de/, renders prices as "125 €"
# (symbol AFTER the number) and localises product names. A $-only price
# regex returns ZERO products on this page while the page visibly shows 96.
# This fixture pins the multi-currency + symbol-position handling.
SAMPLE_EUR_LISTING_HTML = """
<html><body>
<div class="grid">
  <a href="/de/shopping/kids/philosophy-di-lorenzo-serafini-kids--item-36418586.aspx">
    <img alt="многослойный свитер с вышитым логотипом" src="https://cdn.farfetch.com/a.jpg">
    <span>Philosophy Di Lorenzo Serafini Kids</span><span>125 €</span>
  </a>
  <a href="/de/shopping/kids/zadig-voltaire-kids-printed-t-shirt-item-32980992.aspx">
    <img alt="printed T-shirt" src="https://cdn.farfetch.com/b.jpg">
    <span>Zadig &amp; Voltaire Kids</span><span>65 €</span><span>46 €</span><span>-30%</span>
  </a>
  <!-- EU decimal convention: 1.234,56 means one thousand two hundred -->
  <a href="/de/shopping/kids/some-brand-coat-item-11112222.aspx">
    <img alt="wool coat" src="https://cdn.farfetch.com/c.jpg">
    <span>Some Brand</span><span>1.234,56 €</span>
  </a>
</div>
</body></html>
"""

# Captured live from farfetch.com's sign-up modal on 2026-08-24 in a real
# browser. The <captcha-widget data-sitekey=...> element that the August 12
# capture relied on is GONE: the modal now loads api.js?render=explicit and
# configures the widget in JS, into a bare <div id="register-captcha">. No
# data-sitekey, no inline grecaptcha.execute, no grecaptcha.render appears in
# the served HTML — so the static detector finds nothing on a page that
# definitely has an active reCAPTCHA. This fixture pins that regression so
# the runtime path can never quietly stop being the thing that saves it.
SAMPLE_FARFETCH_2026_08_24_MODAL_HTML = """
<html><body>
<div role="dialog">
  <captcha-widgets></captcha-widgets>
  <div id="register-captcha" class="g-recaptcha ltr-1r5gb7q emc8ck80">
    <div class="grecaptcha-badge" data-style="bottomright"></div>
    <iframe title="reCAPTCHA" src="https://recaptcha.net/recaptcha/api2/anchor?ar=1&amp;k=6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT&amp;co=x&amp;size=invisible"></iframe>
  </div>
  <script src="https://recaptcha.net/recaptcha/api.js?render=explicit"></script>
</div>
</body></html>
"""

# What RECAPTCHA_DISCOVERY_JS actually returned from that live page.
LIVE_DISCOVERY_FARFETCH = {
    "found": True,
    "sitekey": "6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT",
    "size": "invisible",
    "action": None,
    "enterprise": False,
    "containerId": "register-captcha",
    "badge": True,
    "challengeFrame": True,   # a bframe iframe was present -> v2-invisible
    "scripts": ["https://recaptcha.net/recaptcha/api.js"],
    # The decisive signal: api.js was loaded with render=explicit, which per
    # Google's docs is the v2 pattern. v3 loads render=<SITE_KEY>.
    "renderParam": "explicit",
    "hints": ["sitekey from ___grecaptcha_cfg"],
}

# Same widget without an interactive challenge frame — v3's normal look.
LIVE_DISCOVERY_V3 = dict(LIVE_DISCOVERY_FARFETCH, challengeFrame=False,
                          action="signup", size=None,
                          renderParam="6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT",
                          hints=["sitekey from iframe k= param"])

# A plain v2 checkbox.
LIVE_DISCOVERY_V2_CHECKBOX = dict(LIVE_DISCOVERY_FARFETCH, size="normal",
                                   challengeFrame=True, badge=False,
                                   renderParam="explicit")

# Captured through 2captcha's Scraping Browser (zone country-th) on
# 2026-08-24. Same site, same modal, same day as the European-IP capture
# above — and materially different: here Farfetch's own <captcha-widget>
# element IS rendered, declaring data-version="v3", WHILE the page also
# loads api.js?render=explicit and registers an invisible client with a
# bframe challenge frame. The two signals coexist and contradict each other.
# So neither detector alone is trustworthy, and the site serves more than one
# variant of this modal depending on where you come from.
SAMPLE_FARFETCH_SCRAPING_BROWSER_MODAL_HTML = """
<html><body>
<div role="dialog">
  <captcha-widget data-captcha-type="recaptcha" data-widget-id="0" data-version="v3" data-sitekey="6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT" data-action="null" data-callback="reCaptchaWidgetCallback0" data-enterprise="false" data-container-id="register-captcha"></captcha-widget>
  <captcha-widgets></captcha-widgets>
  <div id="register-captcha" class="g-recaptcha">
    <div class="grecaptcha-badge" data-style="bottomright"></div>
    <iframe title="reCAPTCHA" src="https://recaptcha.net/recaptcha/api2/bframe?k=6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT&amp;size=invisible"></iframe>
  </div>
  <script src="https://recaptcha.net/recaptcha/api.js?render=explicit"></script>
</div>
</body></html>
"""

SAMPLE_RECAPTCHA_HTML = """
<script src="https://www.google.com/recaptcha/api.js?render=6Lc_test_sitekey_123456789"></script>
<script>
grecaptcha.ready(function() {
  grecaptcha.execute('6Lc_test_sitekey_123456789', {action: 'signup'});
});
</script>
"""

# Confirmed live on farfetch.com's sign-up modal via DevTools inspection on
# 2026-08-12: the actual grecaptcha.execute() call lives inside a bundled
# JS file and never appears as inline script text at all — Farfetch
# instead renders a <captcha-widget> custom element carrying the config as
# HTML attributes. A version of detect_recaptcha_v3() that only checked for
# inline script text reported "no captcha" on this exact page despite one
# being genuinely present; this fixture pins the fix.
SAMPLE_FARFETCH_CAPTCHA_WIDGET_HTML = """
<captcha-widget data-captcha-type="recaptcha" data-widget-id="0" data-version="v3" data-sitekey="6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT" data-action="null" data-callback="reCaptchaWidgetCallback0" data-enterprise="false" data-container-id="register-captcha" data-binded-button-id="null" data-reset="true"></captcha-widget>
"""

# Confirmed live from farfetch.com on 2026-08-12: the product URL lives
# under offers.url, NOT directly on the Product node. An earlier version
# of _parse_jsonld only checked node["url"], which is absent here, and
# silently fell back to the listing page's own URL for every product —
# this fixture pins that exact bug so it can't come back unnoticed.
SAMPLE_FARFETCH_JSONLD_HTML = """
<html><body>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "ItemList",
  "url": "/shopping/kids/girls-clothing-4/items.aspx",
  "numberOfItems": 2,
  "itemListElement": [
    {
      "@type": "Product",
      "position": "1",
      "name": "logo T-shirt",
      "image": ["https://cdn-images.farfetch-contents.com/img1.jpg"],
      "brand": {"@type": "Brand", "name": "Diesel Kids"},
      "offers": {
        "@type": "Offer",
        "price": 42,
        "priceCurrency": "USD",
        "url": "/shopping/kids/diesel-kids-logo-t-shirt-item-33055894.aspx",
        "availability": "https://schema.org/InStock"
      }
    },
    {
      "@type": "Product",
      "position": "2",
      "name": "logo-print T-shirt",
      "image": ["https://cdn-images.farfetch-contents.com/img2.jpg"],
      "brand": {"@type": "Brand", "name": "Marni Kids"},
      "offers": {
        "@type": "Offer",
        "price": 65,
        "priceCurrency": "USD",
        "url": "/shopping/kids/marni-kids-logo-print-t-shirt-item-32485327.aspx",
        "availability": "https://schema.org/InStock"
      }
    }
  ]
}
</script>
</body></html>
"""


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    return condition


def main() -> int:
    ok = True

    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and still
    # says "all passed" is the same defect as code that reports success without
    # checking that what it wanted actually happened.
    _skips = []

    products = parse_products(SAMPLE_LISTING_HTML, "https://www.farfetch.com/shopping/kids/items.aspx", category="Kids")
    ok &= check("parser extracts exactly 2 real products (junk link excluded)", len(products) == 2)
    ok &= check("first product has correct title/price",
                products[0].title == "Marni Kids logo-print cotton T-shirt" and products[0].price == 79.0)
    ok &= check("three-price tile resolves to lowest price + highest original",
                products[1].price == 64.0 and products[1].original_price == 160.0)
    # This fixture was written as a "multiple boutiques" case. It is not: the
    # live site shows exactly this shape as ONE product's discount chain —
    # 160 -50% -> 80 -20% -> 64, matching the four real products measured on a
    # /sale/all/ page. The old expectation of 50.0 read only the FIRST
    # percentage, which is not the discount the buyer gets.
    ok &= check("discount_pct is the compounded discount (160->64 = 60%), not "
                "the first printed percentage (-50%)",
                products[1].discount_pct == 60.0)
    ok &= check("category label propagated", products[0].category == "Kids")
    ok &= check("junk 'sizing guide' link did not create a 3rd product or steal a sibling's data", len(products) == 2)

    farfetch_products = parse_products(SAMPLE_FARFETCH_JSONLD_HTML, "https://www.farfetch.com/shopping/kids/girls-clothing-4/items.aspx")
    ok &= check("Farfetch JSON-LD: product URL comes from offers.url, not the listing page",
                len(farfetch_products) == 2
                and farfetch_products[0].url == "https://www.farfetch.com/shopping/kids/diesel-kids-logo-t-shirt-item-33055894.aspx"
                and farfetch_products[1].url == "https://www.farfetch.com/shopping/kids/marni-kids-logo-print-t-shirt-item-32485327.aspx")
    ok &= check("Farfetch JSON-LD: brand parsed correctly", farfetch_products[0].brand == "Diesel Kids")

    # sku is recovered from the URL: Farfetch's listing JSON-LD carries no
    # sku/productID field at all (verified on two live captures two weeks
    # apart), so without this every row would have sku=None.
    ok &= check("JSON-LD: sku recovered from the -item-<digits>.aspx URL",
                farfetch_products[0].sku == "33055894" and farfetch_products[1].sku == "32485327")
    ok &= check("CSS fallback: sku recovered from the URL too",
                products[0].sku == "29998189" and products[1].sku == "99999")

    # EUR / symbol-after-number locale — a $-only regex scores 0 here.
    eur = parse_products(SAMPLE_EUR_LISTING_HTML, "https://www.farfetch.com/de/shopping/kids/girls-clothing-4/items.aspx")
    ok &= check("EUR locale: all 3 products parsed despite '125 €' form", len(eur) == 3)
    ok &= check("EUR locale: currency detected as EUR, not defaulted to USD",
                all(p.currency == "EUR" for p in eur))
    ok &= check("EUR locale: plain price parsed", eur[0].price == 125.0)
    # 65 -> 46 is 29.2%, and the tile prints "-30%" — the site rounds for
    # display. The computed figure is the discount actually received, so that is
    # what ships; the 1pp cross-check tolerance treats this as agreement and
    # logs nothing.
    ok &= check("EUR locale: discounted tile resolves low/high correctly, not inverted",
                eur[1].price == 46.0 and eur[1].original_price == 65.0)
    ok &= check("EUR locale: discount computed from prices (29.2%), not read "
                "from the site's rounded '-30%'",
                eur[1].discount_pct == 29.2)
    ok &= check("EUR locale: EU decimal convention '1.234,56' parsed as 1234.56",
                eur[2].price == 1234.56)

    # A single separator with no second one to disambiguate against is
    # ambiguous between "thousands grouping" and "decimal point". This
    # project supports exactly four currencies (_CURRENCY_SYMBOLS: USD, EUR,
    # GBP, JPY), none with a 3-digit decimal subunit, so 3 trailing digits
    # after the only separator present means thousands, not decimal —
    # getting this backwards previously turned "$1,234" into 1.234.
    from product_parser import _prices_in
    ok &= check("thousands separator: '$1,234' (US, no cents shown) is 1234, not 1.234",
                _prices_in("$1,234")[0] == [1234.0])
    ok &= check("thousands separator: '€1.234' (EU, no cents shown) is 1234, not 1.234",
                _prices_in("€1.234")[0] == [1234.0])
    ok &= check("thousands separator: '¥123,456' (JPY, no decimal subunit) is 123456",
                _prices_in("¥123,456")[0] == [123456.0])
    ok &= check("thousands separator: a lone separator with 2 trailing digits is still "
                "read as a decimal point, e.g. '$1,23' -> 1.23",
                _prices_in("$1,23")[0] == [1.23])
    ok &= check("thousands separator: repeated thousands groups, '$1,234,567' -> 1234567",
                _prices_in("$1,234,567")[0] == [1234567.0])

    # Some Farfetch markets print a 3-letter ISO code instead of a symbol.
    # A tile priced that way matched nothing before and was dropped as "not a
    # product tile" — losing every product on that locale rather than
    # reporting one with an unfamiliar currency.
    ok &= check("ISO currency code, code first: 'AED 100' -> 100 AED",
                _prices_in("AED 100") == ([100.0], "AED"))
    ok &= check("ISO currency code, code last: '100 CHF' -> 100 CHF",
                _prices_in("100 CHF") == ([100.0], "CHF"))
    ok &= check("ISO currency code carries the thousands/decimal handling too: "
                "'SAR 1,250.50' -> 1250.50 SAR",
                _prices_in("SAR 1,250.50") == ([1250.5], "SAR"))
    ok &= check("ISO currency code: a discounted tile's three prices all parse",
                _prices_in("AED 245 AED 135 AED 108")
                == ([245.0, 135.0, 108.0], "AED"))
    # The allowlist is the whole point: a bare [A-Z]{3} would turn a size
    # chart or a spec line into phantom prices.
    ok &= check("three capitals that are NOT a currency code are not a price: "
                "'XXL 100' yields nothing",
                _prices_in("XXL 100") == ([], None))
    ok &= check("a longer word starting with a real code is not matched: "
                "'SARAH 100' yields nothing",
                _prices_in("SARAH 100") == ([], None))

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "smoke_out")
        save(products, prefix, "both")
        ok &= check("JSON file written", os.path.isfile(prefix + ".json") and os.path.getsize(prefix + ".json") > 0)
        ok &= check("CSV file written", os.path.isfile(prefix + ".csv") and os.path.getsize(prefix + ".csv") > 0)

    challenge = detect_recaptcha_v3(SAMPLE_RECAPTCHA_HTML, "https://www.farfetch.com/account/signup")
    ok &= check("reCAPTCHA v3 detected with correct sitekey/action",
                challenge is not None and challenge.sitekey == "6Lc_test_sitekey_123456789" and challenge.action == "signup")

    no_challenge = detect_recaptcha_v3(SAMPLE_LISTING_HTML, "https://www.farfetch.com/shopping/kids/items.aspx")
    ok &= check("no false-positive captcha detection on clean page", no_challenge is None)

    widget_challenge = detect_recaptcha_v3(SAMPLE_FARFETCH_CAPTCHA_WIDGET_HTML, "https://www.farfetch.com/shopping/kids/items.aspx")
    ok &= check("reCAPTCHA v3 detected via Farfetch's <captcha-widget> custom-element format",
                widget_challenge is not None
                and widget_challenge.sitekey == "6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT"
                and widget_challenge.action == "verify")  # data-action="null" -> default

    # --- runtime reCAPTCHA detection (added 2026-08-24) ---------------------
    # First, pin the regression itself: the static detector finds NOTHING in
    # today's real modal markup. This is not a bug in the fixture.
    stale = detect_recaptcha_v3(SAMPLE_FARFETCH_2026_08_24_MODAL_HTML,
                                "https://www.farfetch.com/de/shopping/kids/items.aspx")
    ok &= check("static-HTML detector returns None on Farfetch's current modal markup "
                "(documents why the runtime path exists)", stale is None)

    live = detect_recaptcha_in_page(lambda _js: LIVE_DISCOVERY_FARFETCH,
                                    page_url="https://www.farfetch.com/de/shopping/kids/items.aspx")
    ok &= check("runtime detector recovers the sitekey the HTML never contained",
                live is not None and live.sitekey == "6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT")
    ok &= check("runtime detector marks source='runtime'", live is not None and live.source == "runtime")
    ok &= check("api.js render=explicit + size=invisible classified as v2-invisible, NOT v3 "
                "(render=explicit is the v2 pattern; v3 uses render=<sitekey>)",
                live is not None and live.kind == "recaptcha_v2_invisible"
                and live.is_invisible_v2 and not live.is_v3)

    v3 = detect_recaptcha_in_page(lambda _js: LIVE_DISCOVERY_V3, page_url="https://x/")
    ok &= check("api.js render=<sitekey> classified as v3",
                v3 is not None and v3.kind == "recaptcha_v3" and v3.is_v3)
    ok &= check("runtime detector carries the action through", v3 is not None and v3.action == "signup")

    v2 = detect_recaptcha_in_page(lambda _js: LIVE_DISCOVERY_V2_CHECKBOX, page_url="https://x/")
    ok &= check("size=normal classified as a v2 checkbox",
                v2 is not None and v2.kind == "recaptcha_v2")
    # No render param at all (older/unknown loader): fall back to size + frames.
    legacy_v3 = detect_recaptcha_in_page(
        lambda _js: dict(LIVE_DISCOVERY_FARFETCH, renderParam=None, challengeFrame=False),
        page_url="https://x/")
    ok &= check("no render param: invisible with no challenge frame still reads as v3",
                legacy_v3 is not None and legacy_v3.kind == "recaptcha_v3")
    legacy_inv = detect_recaptcha_in_page(
        lambda _js: dict(LIVE_DISCOVERY_FARFETCH, renderParam=None, challengeFrame=True),
        page_url="https://x/")
    ok &= check("no render param: invisible WITH a challenge frame reads as v2-invisible",
                legacy_inv is not None and legacy_inv.kind == "recaptcha_v2_invisible")

    ok &= check("runtime detector returns None when nothing is found",
                detect_recaptcha_in_page(lambda _js: {"found": False, "sitekey": None}) is None)
    ok &= check("runtime detector survives an evaluate that raises",
                detect_recaptcha_in_page(lambda _js: (_ for _ in ()).throw(RuntimeError("no page"))) is None)

    # --- reconciling two detectors that disagree ---------------------------
    # The real Scraping Browser capture: static markup claims v3, the loader
    # says v2-invisible. The loader has to win — it's what Google enforces.
    sb_html = detect_recaptcha_v3(SAMPLE_FARFETCH_SCRAPING_BROWSER_MODAL_HTML,
                                  "https://www.farfetch.com/th/shopping/kids/items.aspx")
    ok &= check("static detector reads the Scraping Browser capture as v3 "
                "(that's what Farfetch's own data-version says)",
                sb_html is not None and sb_html.kind == "recaptcha_v3"
                and sb_html.source == "html")

    sb_runtime = detect_recaptcha_in_page(lambda _js: LIVE_DISCOVERY_FARFETCH,
                                          page_url="https://www.farfetch.com/th/shopping/kids/items.aspx")
    reconciled = reconcile_detections(sb_html, sb_runtime)
    ok &= check("when the two disagree, the live loader wins over data-version",
                reconciled is not None and reconciled.kind == "recaptcha_v2_invisible"
                and reconciled.source == "runtime")

    # And the payload that follows from it must be the v2-invisible one, which
    # is the whole point of caring about the disagreement.
    ok &= check("reconciled challenge is not classified as v3",
                reconciled is not None and not reconciled.is_v3)

    # Agreement, and single-sided cases, must pass straight through.
    ok &= check("reconcile: only static found something -> use it",
                reconcile_detections(sb_html, None) is sb_html)
    ok &= check("reconcile: only runtime found something -> use it",
                reconcile_detections(None, sb_runtime) is sb_runtime)
    ok &= check("reconcile: neither found anything -> None",
                reconcile_detections(None, None) is None)

    v3_both = detect_recaptcha_in_page(lambda _js: LIVE_DISCOVERY_V3, page_url="https://x/")
    ok &= check("reconcile: agreement on v3 passes through as v3",
                reconcile_detections(sb_html, v3_both) is not None
                and reconcile_detections(sb_html, v3_both).kind == "recaptcha_v3")

    # --- API v2 task objects must match the documented types ---------------
    # ---- discounted prices: the DOM overlay ---------------------------------
    # This site's listing JSON-LD publishes ONE price per product, and on a
    # discounted item it is the INTERMEDIATE one — the sale price before a
    # site-wide promo. Measured live on four products from a /sale/all/ page:
    # JSON-LD said 135 / 65 / 60 / 426 where the tiles showed 108 / 52 / 48 /
    # 341, with the printed percentages compounding to match in all four cases.
    # So the parser runs a DOM pass over a SUCCESSFUL JSON-LD parse.
    def _real_tile(sku, brand, slug, orig, sale, final, p1, p2):
        return f"""
        <div class="grid-item"><div><div>
          <a href="/de/shopping/kids/{brand}-{slug}-item-{sku}.aspx">
            <img alt="{slug}">
            <p>Promotion</p><p>{brand}</p><p>{slug}</p>
            <p>Originalpreis {orig} &euro;</p><p>Sale-Preis {sale} &euro;</p>
            <p>Endpreis {final} &euro;</p>
            <p>Sale-Discount -{p1}%</p><p>Promo-Discount -{p2}%</p>
            <p>Verf&uuml;gbar in 6 yrs, 8 yrs</p>
          </a>
        </div></div></div>"""

    _REAL_SALE = [("30081580", "lanvin-enfant", "cardigan", 245, 135, 108, 45, 20),
                  ("33139701", "diesel-kids", "jogginganzug", 130, 65, 52, 50, 20),
                  ("30083116", "boss-kidswear", "jacke", 109, 60, 48, 45, 20),
                  ("32982230", "chloe-kids", "wickeltasche", 609, 426, 341, 30, 20)]

    _ld_items = ",".join(
        '{{"@type":"Product","name":"{slug}","brand":{{"name":"{brand}"}},'
        '"offers":{{"price":{sale},"priceCurrency":"EUR",'
        '"url":"/de/shopping/kids/{brand}-{slug}-item-{sku}.aspx",'
        '"availability":"https://schema.org/InStock"}}}}'.format(
            sku=r[0], brand=r[1], slug=r[2], sale=r[4])
        for r in _REAL_SALE)

    _SALE_HTML = ('<html><body><script type="application/ld+json">'
                  '{"@type":"ItemList","itemListElement":[' + _ld_items + ']}'
                  '</script>' + "".join(_real_tile(*r) for r in _REAL_SALE)
                  + "</body></html>")
    _SALE_URL = "https://www.farfetch.com/de/shopping/kids/sale/all/items.aspx"

    _fixed = {p.sku: p for p in parse_products(_SALE_HTML, _SALE_URL)}
    ok &= check("overlay: all four real products parsed",
                len(_fixed) == 4)
    ok &= check("overlay: price is the Endpreis a customer pays, not the "
                "intermediate Sale-Preis from JSON-LD",
                [_fixed[r[0]].price for r in _REAL_SALE] == [108.0, 52.0, 48.0, 341.0])
    ok &= check("overlay: original_price is the Originalpreis",
                [_fixed[r[0]].original_price for r in _REAL_SALE]
                == [245.0, 130.0, 109.0, 609.0])
    # Each of these matches the two printed percentages compounded, to within
    # 0.1pp: 45+20 -> 56.0, 50+20 -> 60.0, 45+20 -> 56.0, 30+20 -> 44.0.
    ok &= check("overlay: discount_pct is the compounded discount, agreeing "
                "with the percentages the site prints",
                [_fixed[r[0]].discount_pct for r in _REAL_SALE]
                == [55.9, 60.0, 56.0, 44.0])

    _raw = {p.sku: p for p in parse_products(_SALE_HTML, _SALE_URL,
                                             tile_prices_overlay=False)}
    ok &= check("tile_prices_overlay=False returns the raw JSON-LD figures, for "
                "comparing against the site's own structured data",
                _raw["30081580"].price == 135.0
                and _raw["30081580"].original_price is None)

    # $ is not always USD — AUD, CAD, SGD, HKD and NZD all render with a bare
    # $ too, and _CURRENCY_SYMBOLS can only ever guess "USD" for it. JSON-LD's
    # priceCurrency is the real, structured answer; the overlay must leave it
    # alone rather than clobbering it with that guess, which would silently
    # break the "compare prices across countries" use case for every non-US
    # market that happens to print $.
    _AUD_HTML = ('<html><body><script type="application/ld+json">'
                 '{"@type":"ItemList","itemListElement":[{"@type":"Product",'
                 '"name":"cardigan","brand":{"name":"Lanvin Enfant"},'
                 '"offers":{"price":135,"priceCurrency":"AUD",'
                 '"url":"/au/shopping/kids/lanvin-enfant-cardigan-item-30081580.aspx",'
                 '"availability":"https://schema.org/InStock"}}]}</script>'
                 + _real_tile("30081580", "lanvin-enfant", "cardigan", 245, 135, 108, 45, 20)
                    .replace("&euro;", "").replace("Originalpreis ", "$")
                    .replace("Sale-Preis ", "$").replace("Endpreis ", "$")
                 + "</body></html>")
    _aud = parse_products(_AUD_HTML, "https://www.farfetch.com/au/shopping/kids/sale/all/items.aspx")
    ok &= check("overlay corrects the price from a '$'-tile without touching "
                "priceCurrency (AUD stays AUD, not overwritten to USD)",
                len(_aud) == 1 and _aud[0].price == 108.0 and _aud[0].currency == "AUD")

    # A JSON-LD offer that genuinely omits priceCurrency must not be reported
    # as "USD" — that's a guess presented as a structured-data fact, worse
    # for a cross-country comparison than admitting the currency is unknown.
    _NO_CURRENCY_HTML = """
    <html><body><script type="application/ld+json">{"@type":"ItemList",
      "itemListElement":[{"@type":"Product","name":"no currency field",
        "offers":{"price":59,
                  "url":"/shopping/kids/x-item-55555556.aspx",
                  "availability":"https://schema.org/InStock"}}]}</script>
    </body></html>
    """
    _nc = parse_products(_NO_CURRENCY_HTML, "https://www.farfetch.com/shopping/kids/items.aspx")
    ok &= check("JSON-LD with no priceCurrency field yields currency=None, "
                "not a guessed 'USD'",
                len(_nc) == 1 and _nc[0].currency is None)
    ok &= check("Product()'s own currency default is None, not a guessed 'USD'",
                Product().currency is None)


    # A tile with ONE price means no discount. JSON-LD is structured data and is
    # the better source there, so the overlay must leave it alone rather than
    # setting original_price equal to price.
    _NO_DISCOUNT = """
    <html><body><script type="application/ld+json">{"@type":"ItemList",
      "itemListElement":[{"@type":"Product","name":"full price thing",
        "offers":{"price":59,"priceCurrency":"EUR",
                  "url":"/de/shopping/kids/x-item-44444444.aspx",
                  "availability":"https://schema.org/InStock"}}]}</script>
      <div><div><a href="/de/shopping/kids/x-item-44444444.aspx">
        <p>Brand</p><p>full price thing</p><p>59 &euro;</p></a></div></div>
    </body></html>
    """
    _nd = parse_products(_NO_DISCOUNT, _SALE_URL)
    ok &= check("overlay leaves an undiscounted product untouched (no "
                "original_price equal to price)",
                len(_nd) == 1 and _nd[0].price == 59.0
                and _nd[0].original_price is None and _nd[0].discount_pct is None)

    # If JSON-LD's price is not among the tile's numbers, the two views disagree
    # about which product this is — most likely a tile-scoping failure.
    # Overwriting would corrupt a row that was correct, so the row is skipped.
    _DISAGREE = """
    <html><body><script type="application/ld+json">{"@type":"ItemList",
      "itemListElement":[{"@type":"Product","name":"thing",
        "offers":{"price":999,"priceCurrency":"EUR",
                  "url":"/de/shopping/kids/x-item-55555555.aspx",
                  "availability":"https://schema.org/InStock"}}]}</script>
      <div><div><a href="/de/shopping/kids/x-item-55555555.aspx">
        <p>Originalpreis 245 &euro;</p><p>Endpreis 108 &euro;</p></a></div></div>
    </body></html>
    """
    _dis = parse_products(_DISAGREE, _SALE_URL)
    ok &= check("overlay refuses to overwrite when the JSON-LD price is not one "
                "of the tile's prices (scoping-failure guard)",
                _dis[0].price == 999.0 and _dis[0].original_price is None)

    # JSON-LD with no rendered tiles at all: the overlay must be a no-op, not a
    # crash and not a wipe.
    _LD_ONLY = """
    <html><body><script type="application/ld+json">{"@type":"ItemList",
      "itemListElement":[{"@type":"Product","name":"thing",
        "offers":{"price":70,"priceCurrency":"EUR",
                  "url":"/de/shopping/kids/x-item-66666666.aspx",
                  "availability":"https://schema.org/InStock"}}]}</script>
    </body></html>
    """
    _ldo = parse_products(_LD_ONLY, _SALE_URL)
    ok &= check("overlay is a no-op when the page has JSON-LD but no rendered "
                "tiles (how much of this site paints at load varies)",
                len(_ldo) == 1 and _ldo[0].price == 70.0)

    # ---- price_source ------------------------------------------------------
    # The same column used to hold two figures with different confidence —
    # the DOM-corrected price a customer pays, or the raw JSON-LD one
    # (pre-promo on a discounted item) when that tile had not rendered — with
    # nothing saying which. Two runs differing only in how much had painted
    # then produced a false "price changed" in diff_runs.py.
    ok &= check("price_source='jsonld+dom' when the tile corrected the price",
                all(p.price_source == "jsonld+dom" for p in _fixed.values()))
    ok &= check("price_source='jsonld+dom' on an undiscounted product too — a "
                "single-price tile CONFIRMS there is no discount, which is "
                "corroboration rather than a correction",
                _nd[0].price_source == "jsonld+dom")
    ok &= check("price_source stays 'jsonld' when the page has JSON-LD but no "
                "rendered tile for the product — that price may be pre-promo",
                _ldo[0].price_source == "jsonld")
    ok &= check("price_source stays 'jsonld' when the scoping guard refuses "
                "the overlay (JSON-LD price not among the tile's prices)",
                _dis[0].price_source == "jsonld")
    ok &= check("price_source='dom' on the CSS/URL fallback path, where there "
                "is no JSON-LD to cross-check against",
                all(p.price_source == "dom" for p in products))
    ok &= check("tile_prices_overlay=False leaves every row 'jsonld', since "
                "the DOM pass never ran",
                all(p.price_source == "jsonld" for p in _raw.values()))

    # The tile-scoping rule is what stops a product inheriting its neighbour's
    # prices. Two products in one grid wrapper, each with its own tile.
    _TWO_IN_GRID = """
    <html><body><script type="application/ld+json">{"@type":"ItemList",
      "itemListElement":[
        {"@type":"Product","name":"a","offers":{"price":135,"priceCurrency":"EUR",
          "url":"/de/shopping/kids/a-item-77777777.aspx","availability":"https://schema.org/InStock"}},
        {"@type":"Product","name":"b","offers":{"price":65,"priceCurrency":"EUR",
          "url":"/de/shopping/kids/b-item-88888888.aspx","availability":"https://schema.org/InStock"}}]}
      </script>
      <div class="grid">
        <div><div><a href="/de/shopping/kids/a-item-77777777.aspx">
          <p>Originalpreis 245 &euro;</p><p>Sale-Preis 135 &euro;</p><p>Endpreis 108 &euro;</p></a></div></div>
        <div><div><a href="/de/shopping/kids/b-item-88888888.aspx">
          <p>Originalpreis 130 &euro;</p><p>Sale-Preis 65 &euro;</p><p>Endpreis 52 &euro;</p></a></div></div>
      </div>
    </body></html>
    """
    _grid = {p.sku: p for p in parse_products(_TWO_IN_GRID, _SALE_URL)}
    ok &= check("tile scoping: neighbouring products do not inherit each "
                "other's prices (the 'junk-link data theft' failure)",
                _grid["77777777"].price == 108.0
                and _grid["77777777"].original_price == 245.0
                and _grid["88888888"].price == 52.0
                and _grid["88888888"].original_price == 130.0)

    # ---- category label -----------------------------------------------------
    # Before this existed, `category` was null on every row unless the caller
    # remembered --category. A column that is empty by default reads as a
    # broken field rather than an optional one.
    for _url, _want in [
        ("https://www.farfetch.com/shopping/kids/girls-clothing-4/items.aspx",
         "girls-clothing-4"),
        # after a geo-redirect the URL carries a locale segment
        ("https://www.farfetch.com/de/shopping/kids/girls-clothing-4/items.aspx",
         "girls-clothing-4"),
        ("https://www.farfetch.com/shopping/kids/items.aspx", "kids"),
        # on a sale URL the informative segment is `sale`, not the generic
        # bucket after it
        ("https://www.farfetch.com/de/shopping/women/sale/all/items.aspx", "sale"),
        ("https://www.farfetch.com/", None),
        ("", None),
    ]:
        ok &= check(f"category_from_url: {_url or '(empty)'} -> {_want}",
                    category_from_url(_url) == _want)

    # The numeric suffix is Farfetch's own category id and is kept on purpose:
    # stripping it would merge two categories that differ only by id.
    ok &= check("category_from_url keeps the numeric category id",
                category_from_url(
                    "https://www.farfetch.com/shopping/kids/girls-clothing-4/items.aspx"
                ).endswith("-4"))

    _derived = parse_products(
        SAMPLE_LISTING_HTML,
        "https://www.farfetch.com/de/shopping/kids/girls-clothing-4/items.aspx")
    ok &= check("parse_products derives category from the URL when none is given",
                bool(_derived) and all(p.category == "girls-clothing-4"
                                       for p in _derived))

    _explicit = parse_products(
        SAMPLE_LISTING_HTML,
        "https://www.farfetch.com/de/shopping/kids/girls-clothing-4/items.aspx",
        category="Kids")
    ok &= check("an explicit --category always beats the URL-derived label",
                bool(_explicit) and all(p.category == "Kids" for p in _explicit))

    # ---- sample selection ---------------------------------------------------
    # sample_output.json is three rows, so which three matters. A row type that
    # is rare in the run must not get a reserved slot: on a sale page the only
    # rows without an original_price tend to be the ones the price overlay
    # deliberately skipped, and putting one in a three-row sample presents a
    # known anomaly as the normal case. Caught on a real run — the sample came
    # out 2 of 3 discounted on a page that was 95 of 96.
    # make_sample.py is a maintainer tool and is not part of the published repo,
    # so these checks skip when it is absent. Note spec_from_file_location
    # returns a spec for a file that does not exist — only exec_module fails,
    # and it fails at the point of use rather than the point of the check. The
    # isfile guard is what actually decides this.
    import importlib.util as _ilu
    _ms_spec = (_ilu.spec_from_file_location("make_sample", "make_sample.py")
                if os.path.isfile("make_sample.py") else None)
    if _ms_spec and _ms_spec.loader:
        _ms = _ilu.module_from_spec(_ms_spec)
        _ms_spec.loader.exec_module(_ms)

        def _row(sku, original):
            return {"url": f"https://x/a-item-{sku}.aspx", "sku": sku,
                    "title": "thing", "price": 46.0, "original_price": original,
                    "discount_pct": 29.2 if original else None}

        _sale_run = [_row(f"3{i:07d}", 109.0) for i in range(95)] + [_row("23538433", None)]
        _picked = _ms.pick(_sale_run, 3)
        ok &= check("sample: on a 95/96-discounted run all three sampled rows "
                    "are discounted (no reserved slot for the outlier)",
                    all(r["original_price"] for r in _picked) and len(_picked) == 3)

        _full_run = [_row(f"4{i:07d}", None) for i in range(95)] + [_row("49999999", 65.0)]
        _picked = _ms.pick(_full_run, 3)
        ok &= check("sample: the reverse holds — a lone clearance item on a "
                    "full-price page does not claim a slot either",
                    not any(r["original_price"] for r in _picked))

        _mixed = ([_row(f"5{i:07d}", None) for i in range(50)]
                  + [_row(f"6{i:07d}", 65.0) for i in range(46)])
        _picked = _ms.pick(_mixed, 3)
        ok &= check("sample: when both kinds are common, both appear",
                    any(r["original_price"] for r in _picked)
                    and any(not r["original_price"] for r in _picked))

        ok &= check("sample: fabricated input is refused, so a placeholder "
                    "cannot be committed as a real run",
                    bool(_ms.looks_fabricated(
                        {"sku": "sample-product-123456", "title": "Sample Product"})))

    # ---- credential loading -------------------------------------------------
    # The .env.example sync check is the one that matters most here: a variable
    # documented in the example file that nothing reads is a setting which looks
    # configurable and is not — the same defect class as a flag that cannot
    # succeed. FARFETCH_OUT was exactly that and was removed.
    import env_config as _ec

    with tempfile.TemporaryDirectory() as _d:
        _envfile = os.path.join(_d, ".env")
        with open(_envfile, "w") as _f:
            _f.write('# comment\n'
                     'TWOCAPTCHA_KEY="fromfile"\n'
                     'export FARFETCH_PROXY=http://u:p@h:9999   # inline\n'
                     "FARFETCH_URL='https://example.test/x'\n"
                     'TWO_CAPTCHA_KEY=typo\n')

        _saved = {k: os.environ.get(k)
                  for k in ("TWOCAPTCHA_KEY", "FARFETCH_PROXY", "FARFETCH_URL")}
        try:
            for _k in _saved:
                os.environ.pop(_k, None)
            _ec.load_env(_envfile)
            ok &= check("env_config: parses comments, export, quotes and strips "
                        "an inline comment from an unquoted value",
                        _ec.env_value("TWOCAPTCHA_KEY") == "fromfile"
                        and _ec.env_value("FARFETCH_PROXY") == "http://u:p@h:9999")

            # An exported variable must win over the file, or a CI secret gets
            # clobbered by a .env someone forgot to delete.
            os.environ["TWOCAPTCHA_KEY"] = "fromshell"
            _ec.load_env(_envfile)
            ok &= check("env_config: an exported variable beats the .env file",
                        _ec.env_value("TWOCAPTCHA_KEY") == "fromshell")

            os.environ["TWOCAPTCHA_KEY"] = "your_2captcha_api_key_here"
            ok &= check("env_config: the .env.example placeholder is treated as "
                        "unset, not sent to the API as a key",
                        _ec.env_value("TWOCAPTCHA_KEY") is None)

            ok &= check("env_config: a mistyped key in .env is reported, not "
                        "silently ignored",
                        "TWO_CAPTCHA_KEY" in _ec.unknown_keys(_envfile))
        finally:
            for _k, _v in _saved.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v

    if os.path.isfile(".env.example"):
        _documented = {line.split("=", 1)[0].strip()
                       for line in open(".env.example", encoding="utf-8")
                       if line.strip() and not line.strip().startswith("#")
                       and "=" in line}
        _declared = set(_ec.ENV_KEYS)
        ok &= check(".env.example documents exactly the variables the code reads"
                    + ("" if _documented == _declared else
                       f" -> unread: {sorted(_documented - _declared)}, "
                       f"undocumented: {sorted(_declared - _documented)}"),
                    _documented == _declared)

    import captcha_solver as _cs
    from captcha_solver import _v2_task_for, CaptchaChallenge

    t_v3 = _v2_task_for(v3, 0.7)
    ok &= check("v2 API: v3 -> RecaptchaV3TaskProxyless with minScore + pageAction",
                t_v3["type"] == "RecaptchaV3TaskProxyless"
                and t_v3["minScore"] == 0.7 and t_v3["pageAction"] == "signup"
                and t_v3["websiteKey"] == v3.sitekey and "isInvisible" not in t_v3)

    t_inv = _v2_task_for(live, 0.7)
    ok &= check("v2 API: v2-invisible -> RecaptchaV2TaskProxyless with isInvisible",
                t_inv["type"] == "RecaptchaV2TaskProxyless"
                and t_inv.get("isInvisible") is True
                and "minScore" not in t_inv and "pageAction" not in t_inv)

    t_cb = _v2_task_for(v2, 0.7)
    ok &= check("v2 API: v2 checkbox -> RecaptchaV2TaskProxyless, no isInvisible",
                t_cb["type"] == "RecaptchaV2TaskProxyless" and "isInvisible" not in t_cb)

    # minScore is not free-form in v2: 0.3 / 0.7 / 0.9 only.
    ok &= check("v2 API: an out-of-range minScore snaps to a documented value",
                _v2_task_for(v3, 0.55)["minScore"] in (0.3, 0.7, 0.9)
                and _v2_task_for(v3, 0.95)["minScore"] == 0.9
                and _v2_task_for(v3, 0.1)["minScore"] == 0.3)

    # A placeholder action must NOT be sent as if it were real: v3 scores on it.
    no_action = CaptchaChallenge(kind="recaptcha_v3", sitekey=v3.sitekey,
                                 page_url="https://x/", action="verify")
    ok &= check("v2 API: the 'verify' placeholder action is omitted, not sent",
                "pageAction" not in _v2_task_for(no_action, 0.7))

    # --- v2 createTask/getTaskResult round trip (mocked) -------------------
    calls = []

    class _R:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    def _post(url, json=None, **kw):
        calls.append((url, json))
        if url.endswith("/createTask"):
            return _R({"errorId": 0, "taskId": 777})
        if url.endswith("/getTaskResult"):
            # first poll processing, then ready — exercises the loop
            n = sum(1 for u, _ in calls if u.endswith("/getTaskResult"))
            if n < 2:
                return _R({"errorId": 0, "status": "processing"})
            return _R({"errorId": 0, "status": "ready",
                       "solution": {"gRecaptchaResponse": "TOKEN_V2", "token": "TOKEN_V2"}})
        raise AssertionError(url)

    real_post, real_sleep = _cs.requests.post, _cs.time.sleep
    _cs.requests.post, _cs.time.sleep = _post, lambda *_: None
    try:
        token = _cs._solve_with_2captcha_v2("KEY", live, poll_interval=1)
    finally:
        _cs.requests.post, _cs.time.sleep = real_post, real_sleep

    ok &= check("v2 API: createTask -> poll -> token returned", token == "TOKEN_V2")
    ok &= check("v2 API: clientKey sent in both calls, taskId echoed back",
                all(c[1].get("clientKey") == "KEY" for c in calls)
                and calls[-1][1].get("taskId") == 777)
    ok &= check("v2 API: polling tolerates a 'processing' status before 'ready'",
                sum(1 for u, _ in calls if u.endswith("/getTaskResult")) == 2)

    # An API-level error must raise, not return an empty token.
    def _post_err(url, json=None, **kw):
        return _R({"errorId": 1, "errorCode": "ERROR_ZERO_BALANCE",
                   "errorDescription": "no funds"})
    _cs.requests.post = _post_err
    try:
        try:
            _cs._solve_with_2captcha_v2("KEY", live)
            raised = False
        except RuntimeError as e:
            raised = "ERROR_ZERO_BALANCE" in str(e)
    finally:
        _cs.requests.post = real_post
    ok &= check("v2 API: an errorId response raises with the error code", raised)

    # --- 2captcha payload must match the variant (legacy v1) ---------------
    captured = {}

    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"status": 1, "request": "TASKID"}

    def _fake_post(url, data=None, **kw):
        captured.clear(); captured.update(data or {})
        return _FakeResp()

    def _fake_get(url, params=None, **kw):
        class R:
            def json(self_inner): return {"status": 1, "request": "TOKEN123"}
        return R()

    real_post, real_get, real_sleep = _cs.requests.post, _cs.requests.get, _cs.time.sleep
    _cs.requests.post, _cs.requests.get, _cs.time.sleep = _fake_post, _fake_get, lambda *_: None
    try:
        _cs._solve_with_2captcha_v1("KEY", v3)
        v3_payload = dict(captured)
        _cs._solve_with_2captcha_v1("KEY", live)
        inv_payload = dict(captured)
    finally:
        _cs.requests.post, _cs.requests.get, _cs.time.sleep = real_post, real_get, real_sleep

    ok &= check("2captcha payload for v3 carries version/action/min_score",
                v3_payload.get("version") == "v3" and v3_payload.get("action") == "signup"
                and "min_score" in v3_payload and "invisible" not in v3_payload)
    ok &= check("2captcha payload for v2-invisible carries invisible=1 and NO v3 params",
                inv_payload.get("invisible") == 1 and "version" not in inv_payload
                and "action" not in inv_payload and "min_score" not in inv_payload)

    # --- solve_recaptcha() itself, the exact call the three scrapers make --
    # This signature drifted from its callers once already (missing
    # api_version/min_score, and a body that called a function which no
    # longer existed) with nothing here catching it — every check above
    # exercises the internal _solve_with_2captcha_v1/v2 helpers directly, never
    # the public function playwright_scraper.py etc. actually import and call.
    calls.clear()
    real_post, real_sleep = _cs.requests.post, _cs.time.sleep
    _cs.requests.post, _cs.time.sleep = _post, lambda *_: None
    try:
        token = _cs.solve_recaptcha(live, "KEY", api_version="v2", min_score=0.7)
    finally:
        _cs.requests.post, _cs.time.sleep = real_post, real_sleep
    ok &= check("solve_recaptcha(api_version='v2', min_score=...) — the exact keyword "
                "call every scraper makes — reaches the v2 createTask/getTaskResult "
                "path and returns a token",
                token == "TOKEN_V2")

    real_post, real_get, real_sleep = _cs.requests.post, _cs.requests.get, _cs.time.sleep
    _cs.requests.post, _cs.requests.get, _cs.time.sleep = _fake_post, _fake_get, lambda *_: None
    try:
        token_v1 = _cs.solve_recaptcha(live, "KEY", api_version="v1", min_score=0.7)
    finally:
        _cs.requests.post, _cs.requests.get, _cs.time.sleep = real_post, real_get, real_sleep
    ok &= check("solve_recaptcha(api_version='v1', min_score=...) reaches the legacy "
                "in.php/res.php path", token_v1 == "TOKEN123")

    try:
        _cs.solve_recaptcha(live, None)
        no_key_raised = False
    except RuntimeError:
        no_key_raised = True
    ok &= check("solve_recaptcha() with no API key raises before touching the network",
                no_key_raised)

    # --- sign-up modal selectors -------------------------------------------
    # The modal diagnostics are not part of the published scraper (they drive a
    # registration form, which this project never submits), so these checks only
    # run when they are present alongside it.
    try:
        import check_signup_captcha_pyppeteer as _pyp
    except ImportError:
        _pyp = None
    if _pyp is not None:
        MODAL_TESTIDS = ["userlogin", "slice-login-sign-up-tab",
                         "slice-login-register-name", "slice-login-recaptcha",
                         "slice-login-sign-up-form"]
        modal_html = "".join(f'<div data-testid="{t}"></div>' for t in MODAL_TESTIDS)
        soup_ids = re.findall(r'data-testid="([^"]+)"', modal_html)

        ok &= check("account-icon list tries the exact testid before any wildcard",
                    _pyp.ACCOUNT_ICON_CANDIDATES[0] == "[data-testid='userlogin']")
        ok &= check("register-tab list tries the real tab first, not "
                    "[data-testid*='register']",
                    _pyp.REGISTER_TAB_CANDIDATES[0]
                    == "[data-testid='slice-login-sign-up-tab']"
                    and "[data-testid*='register']" not in _pyp.REGISTER_TAB_CANDIDATES)

        # The trap itself: a *register* substring match hits the name input too,
        # so a list that leads with it can click a text field and call it a tab.
        register_matches = [t for t in soup_ids if "register" in t]
        ok &= check("substring 'register' matches more than one element in the real "
                    "modal (which is why it can't be the first choice)",
                    len(register_matches) >= 1
                    and "slice-login-register-name" in register_matches
                    and "slice-login-sign-up-tab" not in register_matches)

    # --- empty-result contract ---------------------------------------------
    # A run that finds nothing must not look like a successful run that found
    # nothing to sell, and must not overwrite last night's good file with `[]`.
    from output_writer import EXIT_NO_PRODUCTS

    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "empty_out")
        rc_empty = save([], prefix, "both")
        ok &= check("0 products returns a distinct exit code, not 0",
                    rc_empty == EXIT_NO_PRODUCTS and EXIT_NO_PRODUCTS != 0)
        ok &= check("0 products writes NO files, so a previous good result survives",
                    not os.path.exists(prefix + ".json") and not os.path.exists(prefix + ".csv"))

        # the previous-good-result case, concretely
        save(products, prefix, "both")
        before = open(prefix + ".json", encoding="utf-8").read()
        save([], prefix, "both")
        after = open(prefix + ".json", encoding="utf-8").read()
        ok &= check("an empty run leaves an existing output file untouched",
                    before == after and len(before) > 10)

        ok &= check("--allow-empty writes the empty file but still returns non-zero",
                    save([], prefix + "_ae", "json", allow_empty=True) == EXIT_NO_PRODUCTS
                    and os.path.exists(prefix + "_ae.json"))
        ok &= check("a non-empty save returns 0", save(products, prefix + "_ok", "json") == 0)

    # --- blocked-vs-empty exit code -----------------------------------------
    # README documents exit 3 (blocked before parsing) as distinct from exit 4
    # (genuinely zero products), but nothing detected a bot-challenge page in
    # any of the three browser engines until this was shared from
    # scraper_api_client.py into product_parser.py — a category page sitting
    # behind Akamai/Cloudflare would previously reach parse_products, get 0
    # products back, and exit 4 exactly like an empty category, which is the
    # ambiguity the exit-code contract exists to prevent.
    from output_writer import EXIT_BLOCKED
    from product_parser import detect_bot_challenge, BOT_CHALLENGE_MARKERS
    import scraper_api_client as _sac

    ok &= check("EXIT_BLOCKED (3) and EXIT_NO_PRODUCTS (4) are distinct codes",
                EXIT_BLOCKED == 3 and EXIT_BLOCKED != EXIT_NO_PRODUCTS)
    ok &= check("detect_bot_challenge: an Akamai challenge page is recognised",
                detect_bot_challenge('<div id="sec-if-cpt-container">...</div>') == "akamai")
    ok &= check("detect_bot_challenge: a Cloudflare challenge page is recognised",
                detect_bot_challenge('<div class="cf-challenge">...</div>') == "cloudflare")
    ok &= check("detect_bot_challenge: an ordinary listing page is not flagged",
                detect_bot_challenge(SAMPLE_LISTING_HTML) is None)
    ok &= check("scraper_api_client shares the same BOT_CHALLENGE_MARKERS, not "
                "a second copy that can drift out of sync",
                _sac.BOT_CHALLENGE_MARKERS is BOT_CHALLENGE_MARKERS)

    # --- page.content() mid-navigation --------------------------------------
    # Playwright raises when the document swaps under the snapshot, which
    # farfetch.com's client-side geo-redirect makes routine.
    #
    # Guarded like the Selenium blocks below: importing playwright_scraper pulls
    # in playwright itself, and the suite is meant to run with NO engine
    # installed — that is the whole point of calling it the offline suite. This
    # import was unguarded and passed locally for exactly the reason it should
    # not have: the engine happened to be installed on the machine running it.
    try:
        import playwright_scraper as _ps
    except ImportError as exc:
        _ps = None
        _skips.append(f"page.content() navigation-race checks "
                      f"(playwright not installed: {exc.name})")
    if _ps is not None:

        ok &= check("playwright_scraper._chrome_ua names the browser's REAL version, "
                    "not a hardcoded one that only ever drifts out of date",
                    _ps._chrome_ua("127.0.6533.17")
                    == "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/127.0.6533.17 Safari/537.36")

        # get_attribute("href") is the raw, unresolved HTML attribute — the
        # previous "startswith('http') else base + href" concatenation
        # mishandled an absolute-path href once the base URL's own query
        # string was stripped first, pasting two absolute paths into one
        # broken URL.
        _base = "https://www.farfetch.com/de/shopping/kids/girls-clothing-4/items.aspx?page=1"
        ok &= check("pagination: an absolute-path href replaces the base URL's path "
                    "and query, not concatenated onto it",
                    _ps._resolve_pagination_url(
                        _base, "/de/shopping/kids/girls-clothing-4/items.aspx?page=2")
                    == "https://www.farfetch.com/de/shopping/kids/girls-clothing-4/items.aspx?page=2")
        ok &= check("pagination: a full absolute href is used as-is",
                    _ps._resolve_pagination_url(_base, "https://www.farfetch.com/other?x=1")
                    == "https://www.farfetch.com/other?x=1")
        ok &= check("pagination: a page-relative href resolves against the base URL's path",
                    _ps._resolve_pagination_url("https://www.farfetch.com/de/shopping/kids/items.aspx",
                                                "sub/items.aspx?page=2")
                    == "https://www.farfetch.com/de/shopping/kids/sub/items.aspx?page=2")

        class _NavPage:
            """Raises the navigation error N times, then succeeds."""
            def __init__(self, fail_times): self.left = fail_times; self.waits = 0
            def content(self):
                if self.left > 0:
                    self.left -= 1
                    raise _ps.PWError("Page.content: Unable to retrieve content because "
                                      "the page is navigating and changing the content.")
                return "<html>settled</html>"
            def wait_for_timeout(self, ms): self.waits += 1

        p_ok = _NavPage(2)
        ok &= check("content() retries through a client-side redirect and succeeds",
                    _ps._content_when_settled(p_ok, attempts=4, pause_ms=0) == "<html>settled</html>"
                    and p_ok.waits == 2)

        p_bad = _NavPage(99)
        ok &= check("content() gives up with None instead of raising, so the run continues",
                    _ps._content_when_settled(p_bad, attempts=3, pause_ms=0) is None)

        class _OtherError:
            def content(self): raise _ps.PWError("Page.content: some unrelated failure")
            def wait_for_timeout(self, ms): pass
        raised = False
        try:
            _ps._content_when_settled(_OtherError(), attempts=2, pause_ms=0)
        except _ps.PWError:
            raised = True
        ok &= check("an unrelated Playwright error is NOT swallowed by the retry", raised)

        # --- fingerprint glue (2captcha Fingerprint API) ------------------------
        from fingerprint_client import playwright_context_kwargs, playwright_init_script

        FP = {"id": "fp_test", "country": "us",
              "screen": {"width": 1920, "height": 1080},
              "userAgent": {"value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/145.0.0.0"},
              "navigator": {"platform": "Win32", "hardwareConcurrency": 8, "deviceMemory": 8},
              "webgl": {"vendor": 'Google Inc. "quoted"', "renderer": "ANGLE (RTX 3060)"}}

        kw = playwright_context_kwargs(FP)
        ok &= check("fingerprint: user_agent and locale mapped onto the Playwright context",
                    kw["user_agent"].startswith("Mozilla/5.0 (Windows") and kw["locale"] == "en-US")
        ok &= check("fingerprint: viewport is smaller than the screen "
                    "(a viewport equal to screen size is itself a signal)",
                    kw["viewport"]["height"] < kw["screen"]["height"]
                    and kw["screen"]["width"] == 1920)

        js = playwright_init_script(FP)
        ok &= check("fingerprint: values are JSON-encoded, so a quote in an API string "
                    "cannot break out of the script",
                    '\\"quoted\\"' in js and "Google Inc. \"quoted\"" not in js)
        ok &= check("fingerprint: patches navigator.platform / hardwareConcurrency / deviceMemory",
                    "'platform'" in js and "'hardwareConcurrency'" in js and "'deviceMemory'" in js)
        ok &= check("fingerprint: patches BOTH WebGL1 and WebGL2 getParameter",
                    "WebGLRenderingContext" in js and "WebGL2RenderingContext" in js
                    and "37445" in js and "37446" in js)

        empty = playwright_init_script({})
        ok &= check("fingerprint: an empty fingerprint yields a script that patches nothing",
                    "null" in empty and "getParameter" in empty)

    # ---- Puppeteer/pyppeteer: UA derived from the real launched version ----
    # Guarded the same way as the Playwright block above: importing
    # puppeteer_scraper pulls in pyppeteer, which is not installed in the
    # offline CI job on purpose.
    try:
        import puppeteer_scraper as _pup
    except ImportError as exc:
        _pup = None
        _skips.append(f"puppeteer_scraper UA checks (pyppeteer not installed: {exc.name})")
    if _pup is not None:
        ok &= check("puppeteer_scraper._chrome_ua names the browser's REAL version, "
                    "not a hardcoded one that only ever drifts out of date",
                    _pup._chrome_ua("127.0.6533.17")
                    == "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/127.0.6533.17 Safari/537.36")

    # ---- Selenium: --chromedriver on the LOCAL path -----------------------
    # This was remote-only, which broke exactly the machine that already had a
    # driver: webdriver-manager was mandatory and would try to download a copy
    # of a binary sitting on disk. A local Selenium run (own Chrome, no CDP) is the
    # test that finally exercises this path, so pin the behaviour first.
    try:
        import selenium_scraper as _sel
        from selenium.webdriver.chrome.service import Service as _Svc

        seen = {}

        class _FakeService:
            def __init__(self, executable_path=None, service_args=None, **kw):
                seen["path"] = executable_path
                seen["args"] = service_args

        class _FakeChrome:
            def __init__(self, service=None, options=None, **kw):
                seen["built"] = True
            def execute_cdp_cmd(self, *a, **k):
                return {}

        class _Args:
            cdp_endpoint = None
            headless = True
            proxy = None
            disable_build_check = False
            chromedriver = os.path.abspath(__file__)   # a real, readable file

        real_svc, real_chrome = _sel.Service, _sel.webdriver.Chrome
        _sel.Service, _sel.webdriver.Chrome = _FakeService, _FakeChrome
        # Make an accidental webdriver_manager import fail loudly rather than
        # silently succeed on a machine that happens to have it installed.
        blocked = {"hit": False}
        real_import = builtins.__import__

        def _guard(name, *a, **k):
            if name.startswith("webdriver_manager"):
                blocked["hit"] = True
                raise AssertionError("webdriver_manager must NOT be imported "
                                     "when --chromedriver is given")
            return real_import(name, *a, **k)

        builtins.__import__ = _guard
        try:
            os.chmod(_Args.chromedriver, 0o755)
            _sel.build_driver(_Args())
        finally:
            builtins.__import__ = real_import
            _sel.Service, _sel.webdriver.Chrome = real_svc, real_chrome

        ok &= check("selenium: --chromedriver is honoured on the LOCAL path too",
                    seen.get("path") == _Args.chromedriver and seen.get("built") is True)
        ok &= check("selenium: with --chromedriver given, webdriver_manager is never "
                    "imported (so it works behind an egress allowlist)",
                    blocked["hit"] is False)
    except ImportError:
        _skips.append("selenium --chromedriver local-path checks "
                      "(selenium not installed)")

    # ---- Selenium: two live local failures, turned into tests -------------
    # A local run spent 60 seconds and then printed a message about `debuggerAddress`
    # on a run that never used --cdp-endpoint. Two separate defects: no version
    # pre-check, and a timeout message that assumed the remote path.
    try:
        import selenium_scraper as _sel

        class _A:
            cdp_endpoint = None
            chromedriver = "/tmp/fake-chromedriver"
            disable_build_check = False

        _orig_local = _sel._local_chrome_version
        _orig_binary = _sel._binary_version
        _sel._binary_version = lambda p: (151, "ChromeDriver 151.0.7922.138")
        _sel._local_chrome_version = lambda: (141, "Google Chrome 141.0.7390.65",
                                              "/Applications/Google Chrome.app")
        raised = ""
        try:
            _sel.check_local_versions(_A())
        except SystemExit as e:
            raised = str(e)
        ok &= check("selenium: a chromedriver/Chrome major mismatch stops BEFORE the "
                    "60s budget, naming both versions",
                    "151" in raised and "141" in raised and "chrome-for-testing" in raised)

        class _B(_A):
            disable_build_check = True
        went = True
        try:
            _sel.check_local_versions(_B())
        except SystemExit:
            went = False
        ok &= check("selenium: --disable-build-check downgrades the mismatch to a "
                    "warning instead of a stop", went)

        _sel._local_chrome_version = lambda: (151, "Google Chrome 151.0.7922.60", "/x")
        passed = True
        try:
            _sel.check_local_versions(_A())
        except SystemExit:
            passed = False
        ok &= check("selenium: matching majors pass the check silently", passed)

        class _C(_A):
            cdp_endpoint = "ws://127.0.0.1:9333"
        _sel._local_chrome_version = lambda: (141, "Google Chrome 141", "/x")
        skipped = True
        try:
            _sel.check_local_versions(_C())
        except SystemExit:
            skipped = False
        ok &= check("selenium: the local version check does NOT run on the "
                    "--cdp-endpoint path (the remote browser is not this one)", skipped)

        # the second attempt: the probe spent 10.5s and then reported no browser at all.
        # Both halves were defects — it only looked in three places, and it
        # executed candidates that don't exist.
        import time as _time
        _sel._local_chrome_version = _orig_local
        cands = _sel._chrome_candidates()
        ok &= check("selenium: the browser search covers ~/Applications, Canary, "
                    "Brave, Edge, Arc — not just /Applications/Google Chrome",
                    any("Applications" in c and c.startswith(os.path.expanduser("~"))
                        for c in cands)
                    and any("Canary" in c for c in cands)
                    and any("Brave" in c for c in cands)
                    and any("Edge" in c for c in cands))
        _t0 = _time.time()
        _sel._local_chrome_version()
        ok &= check("selenium: the browser search checks existence before spawning, "
                    "so it costs well under a second (was 10.5s live)",
                    _time.time() - _t0 < 3.0)

        # the run that finally passed: the check warned "no browser on this
        # machine" while --chrome-binary named Chrome in /Applications, and the
        # very next log line used it. The check was hunting for what it was
        # handed.
        class _D(_A):
            chrome_binary = os.path.abspath(__file__)
        calls = {"searched": False}
        _sel._local_chrome_version = lambda: (calls.__setitem__("searched", True), None)[1]
        _sel._binary_version = lambda p: ((151, "ChromeDriver 151.0.7922.138")
                                          if "chromedriver" in p
                                          else (151, "Google Chrome 151.0.7922.175"))
        passed = True
        try:
            _sel.check_local_versions(_D())
        except SystemExit:
            passed = False
        ok &= check("selenium: --chrome-binary is what gets version-checked, and the "
                    "machine-wide search is not run at all",
                    passed and calls["searched"] is False)

        _sel._binary_version = lambda p: ((151, "ChromeDriver 151")
                                          if "chromedriver" in p
                                          else (141, "Google Chrome 141"))
        caught = ""
        try:
            _sel.check_local_versions(_D())
        except SystemExit as e:
            caught = str(e)
        ok &= check("selenium: a mismatch against --chrome-binary still stops, naming "
                    "that binary", "151" in caught and "141" in caught)

        _sel._binary_version = _orig_binary
        ok &= check("selenium: the local session budget is larger than the remote one "
                    "(a passing live run spent 48s launching a browser)",
                    _sel.DEFAULT_DRIVER_TIMEOUT_LOCAL > _sel.DEFAULT_DRIVER_TIMEOUT_REMOTE
                    and _sel.DEFAULT_DRIVER_TIMEOUT_LOCAL >= 120)

        _sel._timed_out.update(seconds=60, remote=False)
        local_msg = _sel._timeout_message()
        _sel._timed_out.update(remote=True)
        remote_msg = _sel._timeout_message()
        ok &= check("selenium: the local-path timeout message never mentions "
                    "debuggerAddress (a live run printed exactly that)",
                    "debuggerAddress" not in local_msg and "--cdp-endpoint was not used" in local_msg)
        ok &= check("selenium: the local-path message names the version mismatch first "
                    "and gives a chromedriver-only reproduction",
                    "major version doesn't match" in local_msg and "--port=9515" in local_msg)
        ok &= check("selenium: the remote-path message still explains debuggerAddress",
                    "debuggerAddress" in remote_msg)
    except ImportError:
        _skips.append("selenium version-guard checks (selenium not installed)")

    # ---- cross-page dedup + cross-run diff --------------------------------
    # Pins the pagination bug the three browser engines all shared until this
    # was added: all_products.extend(products) with no seen-set, so a stale or
    # repeating NEXT_PAGE_SELECTOR link duplicated a row into the output.
    seen = set()
    page1 = [Product(sku="1", title="A"), Product(sku="2", title="B")]
    page2 = [Product(sku="2", title="B"), Product(sku="3", title="C")]
    fresh1 = dedupe_by_sku(page1, seen)
    fresh2 = dedupe_by_sku(page2, seen)
    ok &= check("dedupe_by_sku: first page passes through untouched",
                [p.sku for p in fresh1] == ["1", "2"])
    ok &= check("dedupe_by_sku: a sku repeated on a later page is dropped, "
                "a genuinely new one is kept",
                [p.sku for p in fresh2] == ["3"])
    no_sku = [Product(sku=None, title="fallback-parse miss")]
    ok &= check("dedupe_by_sku: a product with no sku is always kept — there is "
                "nothing to key a duplicate check on, so dropping it would be "
                "silent data loss rather than dedup",
                len(dedupe_by_sku(no_sku, seen)) == 1)

    # README sells "re-run on a schedule and diff on sku" for price monitoring
    # and assortment tracking; this pins the tool that actually does it.
    old_run = [
        {"sku": "10", "title": "Still listed, same price", "price": 50.0,
         "original_price": None, "discount_pct": None, "currency": "USD", "in_stock": True},
        {"sku": "11", "title": "Delisted since", "price": 30.0,
         "original_price": None, "discount_pct": None, "currency": "USD", "in_stock": True},
        {"sku": "12", "title": "Price dropped", "price": 100.0,
         "original_price": None, "discount_pct": None, "currency": "USD", "in_stock": True},
    ]
    new_run = [
        {"sku": "10", "title": "Still listed, same price", "price": 50.0,
         "original_price": None, "discount_pct": None, "currency": "USD", "in_stock": True},
        {"sku": "12", "title": "Price dropped", "price": 80.0,
         "original_price": 100.0, "discount_pct": 20.0, "currency": "USD", "in_stock": True},
        {"sku": "13", "title": "Newly listed", "price": 40.0,
         "original_price": None, "discount_pct": None, "currency": "USD", "in_stock": True},
    ]
    result = diff_products(old_run, new_run)
    ok &= check("diff_runs: a sku only in the new run is 'added'",
                [p["sku"] for p in result["added"]] == ["13"])
    ok &= check("diff_runs: a sku only in the old run is 'removed'",
                [p["sku"] for p in result["removed"]] == ["11"])
    ok &= check("diff_runs: an unchanged sku produces no 'changed' entry",
                "10" not in [c["sku"] for c in result["changed"]])
    ok &= check("diff_runs: price/original_price/discount_pct changing on a "
                "matched sku is reported with old and new values",
                result["changed"] == [{
                    "sku": "12", "title": "Price dropped",
                    "changes": {
                        "price": {"old": 100.0, "new": 80.0},
                        "original_price": {"old": None, "new": 100.0},
                        "discount_pct": {"old": None, "new": 20.0},
                    },
                }])
    dup_old = [{"sku": "20", "title": "dup"}, {"sku": "20", "title": "dup again"}]
    ok &= check("diff_runs: the second row of a duplicate sku within one file "
                "is counted as unmatchable rather than silently overwriting the first",
                diff_products(dup_old, [])["unmatchable_old"] == 1)

    # A price that differs while price_source ALSO differs is not a site-side
    # price change: one run read the DOM-corrected figure, the other the raw
    # JSON-LD one because that tile had not painted. Reporting it as `changed`
    # is a false alarm about the site.
    _src_old = [{"sku": "40", "title": "same product, different render",
                 "price": 130.0, "original_price": None, "discount_pct": None,
                 "currency": "EUR", "in_stock": True, "price_source": "jsonld"}]
    _src_new = [{"sku": "40", "title": "same product, different render",
                 "price": 52.0, "original_price": 130.0, "discount_pct": 60.0,
                 "currency": "EUR", "in_stock": True, "price_source": "jsonld+dom"}]
    _sc = diff_products(_src_old, _src_new)
    ok &= check("diff_runs: a price difference across a price_source change is "
                "reported as source_changed, NOT as a real price change",
                not _sc["changed"]
                and [c["sku"] for c in _sc["source_changed"]] == ["40"]
                and _sc["source_changed"][0]["price_source"]
                    == {"old": "jsonld", "new": "jsonld+dom"})
    # ...but a genuine change alongside it must still be reported.
    _src_new2 = [dict(_src_new[0], in_stock=False)]
    _sc2 = diff_products(_src_old, _src_new2)
    ok &= check("diff_runs: a non-price change (in_stock) is still reported as "
                "'changed' even when the price columns moved to source_changed",
                [c["sku"] for c in _sc2["changed"]] == ["40"]
                and list(_sc2["changed"][0]["changes"]) == ["in_stock"]
                and len(_sc2["source_changed"]) == 1)
    # Same source on both sides = a real price change, reported as such.
    _same_src = [dict(_src_new[0], price_source="jsonld")]
    _sc3 = diff_products(_src_old, _same_src)
    ok &= check("diff_runs: a price difference with the SAME price_source on "
                "both sides is a real change",
                [c["sku"] for c in _sc3["changed"]] == ["40"]
                and not _sc3["source_changed"])

    # ---- run metadata: partial runs must not read as delistings -----------
    # A run cut short on page 3 of 10 is missing every product on pages
    # 4-10. Diffed against yesterday's full run, all of them came back as
    # `removed` — indistinguishable from "these products were delisted".
    # finish_run writes a sidecar recording that, and diff_runs refuses.
    from output_writer import (finish_run, run_meta, EXIT_PARTIAL,
                               COMPLETE_STOP_REASONS)
    import diff_runs as _dr

    ok &= check("EXIT_PARTIAL (6) is distinct from 0, EXIT_BLOCKED and "
                "EXIT_NO_PRODUCTS",
                EXIT_PARTIAL == 6
                and EXIT_PARTIAL not in (0, EXIT_BLOCKED, EXIT_NO_PRODUCTS))
    ok &= check("pagination running out counts as a COMPLETE run — the site "
                "had nothing more to give, which is not an early stop",
                "pagination_exhausted" in COMPLETE_STOP_REASONS
                and "page_load_timeout" not in COMPLETE_STOP_REASONS)

    _two = [Product(sku="1", price=10.0), Product(sku="2", price=20.0)]
    with tempfile.TemporaryDirectory() as tmp:
        _c = os.path.join(tmp, "complete")
        rc_c = finish_run(_two, _c, "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, start_url="u", final_url="u")
        meta_c = json.load(open(_c + ".meta.json", encoding="utf-8"))
        ok &= check("finish_run: a complete run exits 0 and its sidecar says "
                    "status=complete",
                    rc_c == 0 and meta_c["status"] == "complete")

        _p = os.path.join(tmp, "partial")
        rc_p = finish_run(_two, _p, "json", False, blocked=False,
                          stop_reason="page_load_timeout", pages_requested=10,
                          pages_completed=2, start_url="u", final_url="v")
        meta_p = json.load(open(_p + ".meta.json", encoding="utf-8"))
        ok &= check("finish_run: a partial run still WRITES its products "
                    "(discarding good pages would be worse) but exits "
                    "EXIT_PARTIAL and records why",
                    rc_p == EXIT_PARTIAL
                    and os.path.exists(_p + ".json")
                    and meta_p["status"] == "partial"
                    and meta_p["stop_reason"] == "page_load_timeout"
                    and meta_p["pages_completed"] == 2
                    and meta_p["pages_requested"] == 10)

        _b = os.path.join(tmp, "blocked")
        rc_b = finish_run([], _b, "json", False, blocked=True,
                          stop_reason="blocked_akamai", pages_requested=1,
                          pages_completed=0, start_url="u", final_url="u")
        ok &= check("finish_run: a run that gathered nothing writes NO sidecar "
                    "— it would otherwise contradict the previous run's "
                    "still-intact output, which save() deliberately keeps",
                    rc_b == EXIT_BLOCKED and not os.path.exists(_b + ".meta.json"))

        # diff_runs must refuse a comparison involving the partial run.
        class _A:
            def __init__(self, old, new, force=False):
                self.old, self.new, self.force = old, new, force
        ok &= check("diff_runs refuses to compare when a run's sidecar says "
                    "'partial' — missing pages would be reported as delistings",
                    _dr._check_comparable(_A(_c + ".json", _p + ".json")) is False)
        ok &= check("diff_runs compares happily when both sidecars say 'complete'",
                    _dr._check_comparable(_A(_c + ".json", _c + ".json")) is True)
        ok &= check("diff_runs still works on output with NO sidecar at all "
                    "(files written before run metadata existed)",
                    _dr._check_comparable(_A(os.path.join(tmp, "nope.json"),
                                             os.path.join(tmp, "nope2.json"))) is True)

    # ---- naming and dead-feature guards ----------------------------------
    # Not testing behaviour — testing claims. Three separate rounds of work went
    # into naming the products correctly and removing a flag that could not
    # work, and every one of those is a string an editor can reintroduce without
    # anything failing. So fail here instead.
    import glob as _glob

    SHIPPED = sorted(set(_glob.glob("*.py")) | set(_glob.glob("*.sh"))
                     | set(_glob.glob("*.md")) | set(_glob.glob("*.txt"))
                     | set(_glob.glob("*.html")))
    BANNED = [
        # invented product names
        ("2scraper Antidetect Browser", "a product that does not exist under that name"),
        ("proprietary antidetect browser", "same — the browser is the Scraping Browser API"),
        # superseded product naming (Petr, 2026-08-26: it is the
        # "2Captcha Scraping Browser API"; there is no separate brand yet)
        ("cloud browser", "call it the Scraping Browser API"),
        ("Cloud browser", "call it the Scraping Browser API"),
        # a gateway host that was never real; 2prx.com is a synonym of
        # 2captcha.com/proxy, not a separate service with its own hostnames
        ("gate.2prx.com", "not a real gateway host"),
        # the removed flag
        ("--antidetect", "the flag was removed: its endpoint was a placeholder"),
        ("ANTIDETECT_LOCAL_API", "removed with the flag"),
    ]
    offenders = []
    for f in SHIPPED:
        try:
            body = open(f, encoding="utf-8").read()
        except Exception:
            continue
        for phrase, why in BANNED:
            for n, line in enumerate(body.splitlines(), 1):
                if phrase in line:
                    # This test names the phrases, so skip its own listing.
                    if f == "smoke_test.py" and "BANNED" in body[:body.index(line)][-2000:]:
                        continue
                    offenders.append(f"{f}:{n} {phrase!r} — {why}")
    # smoke_test.py holds the list itself; exclude it wholesale rather than
    # guessing which line is the data.
    offenders = [o for o in offenders if not o.startswith("smoke_test.py")]
    ok &= check("no shipped file names a product that does not exist, or the "
                "removed --antidetect flag"
                + ("" if not offenders else " -> " + "; ".join(offenders[:4])),
                not offenders)

    import captcha_solver as _cs
    ok &= check("solve_recaptcha no longer takes use_antidetect",
                "use_antidetect" not in inspect.signature(_cs.solve_recaptcha).parameters)
    ok &= check("the placeholder antidetect endpoint constant is gone",
                not hasattr(_cs, "ANTIDETECT_LOCAL_API"))

    print()
    if _skips:
        print(f"{len(_skips)} group(s) of checks SKIPPED — an optional engine "
              f"library is not installed here:")
        for line in _skips:
            print(f"  - {line}")
        print("Expected in CI, which installs no engine on purpose. Install one "
              "to exercise them.")
        print()

    if ok:
        print("All smoke tests passed. Core logic is sound — safe to move on to a real browser run.")
        return 0
    else:
        print("Some checks FAILED — fix these before running against a real browser/site.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
