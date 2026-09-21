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

import argparse
import contextlib
import io
import json
import logging
import os
import re
import builtins
import inspect
import subprocess
import sys
import tempfile

import ast as _ast

import captcha_solver as _cs
from bs4 import BeautifulSoup
from product_parser import parse_products, category_from_url, detect_bot_challenge
import csv

from output_writer import (save, dedupe_by_sku, Product, finish_run, run_meta,
                           write_csv, ProductVariant,
                           stop_reason_for, new_run_id, quality_metrics,
                           EXIT_NO_PRODUCTS, EXIT_BLOCKED, EXIT_PARTIAL,
                           EXIT_FETCH_FAILED, EXIT_DRIVER_TIMEOUT,
                           COMPLETE_STOP_REASONS, FETCH_FAILURE_STOP_REASONS)
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

# Captured live from www.farfetch.com on 2026-09-14, from a datacentre
# address the site refuses. This is the ENTIRE response body under HTTP 403 —
# 318 bytes as the browser serialises it, 406 as the wire delivers it. Not a
# challenge: there is no widget, no sitekey and nothing to solve, which is
# why every marker in BOT_CHALLENGE_MARKERS missed it and a blocked run
# reported "0 products" (exit 4) instead of "blocked" (exit 3).
#
# Only the Akamai reference id is edited (it is issued per request, so a real
# one would pin nothing and go stale immediately). Everything else — the
# casing, the stray space after the <h1>, the entity escaping in the raw
# form, the blank lines before </body> — is verbatim, because the whole point
# of a capture is that it is not what someone would have written by hand.

# As the three BROWSER engines see it: page.content() serialises the parsed
# DOM, so the entities come back out as ordinary punctuation.
SAMPLE_AKAMAI_DENIED_DOM_HTML = """<html><head>
<title>Access Denied</title>
</head><body>
<h1>Access Denied</h1>

You don't have permission to access "http://www.farfetch.com/shopping/kids/items.aspx" on this server.<p>
Reference #18.11111111.1111111111.11111111
</p><p>https://errors.edgesuite.net/18.11111111.1111111111.11111111</p>


</body></html>"""

# As scraper_api_client (and any plain HTTP client) sees it: Akamai escapes
# the punctuation, so "errors.edgesuite.net" and "Reference #" are simply not
# present as strings. Both of those were the audit's suggested markers.
SAMPLE_AKAMAI_DENIED_RAW_HTML = """<HTML><HEAD>
<TITLE>Access Denied</TITLE>
</HEAD><BODY>
<H1>Access Denied</H1>

You don't have permission to access "http&#58;&#47;&#47;www&#46;farfetch&#46;com&#47;shopping&#47;kids&#47;items&#46;aspx" on this server.<P>
Reference&#32;&#35;18&#46;11111111&#46;1111111111&#46;11111111
<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;11111111&#46;1111111111&#46;11111111</P>
</BODY>
</HTML>"""

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


# Cut from real pages captured 2026-09-15 through a DE exit. Only what the
# parser reads was kept — the two JSON-LD blocks and the composition markup —
# which is also the scrub: the uuids these pages carry live in their
# analytics and config scripts and are not part of what was cut. `hasVariant`
# is trimmed to two sizes so the fixture stays readable; every value is
# otherwise verbatim, down to the build-hash class names on the composition
# block, which are exactly what a parser must NOT anchor on.
#
# Verified before committing: the trimmed fixture parses to the SAME value
# for every field of every retained row as the untrimmed 260 KB original. A
# fixture that does not is pinning something the site never sent.
#
# A DETAIL page publishes ProductGroup; a LISTING page publishes
# ItemList/Product. That is the difference that makes a second parser
# necessary, and porting the listing parser here would return zero products
# in silence.
SAMPLE_DETAIL_FULL_PRICE_HTML = r"""<html><body>
<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@type": "ProductGroup",
 "name": "Baumwoll-T-Shirt mit Ami de Coeur",
 "image": [
  {
   "@type": "ImageObject",
   "contentUrl": "https://cdn-images.farfetch-contents.com/36/89/92/89/36899289_69172521_1000.jpg?ov=true",
   "description": "AMI Paris Baumwoll-T-Shirt mit Ami de Coeur | Weiß"
  },
  {
   "@type": "ImageObject",
   "contentUrl": "https://cdn-images.farfetch-contents.com/36/89/92/89/36899289_69623678_1000.jpg?ov=true",
   "description": "AMI Paris Baumwoll-T-Shirt mit Ami de Coeur | Klassisches T-Shirt"
  }
 ],
 "description": "AMI Paris Baumwoll-T-Shirt mit Ami de Coeur | Weiß | kastiger Schnitt | runder Kragen | Ami de Coeur Prägung und Zierstich auf der Brust | farblich abgestimmte AMI-Stickerei hinten | Bio-Baumwolle | Bio-Baumwolle | T-Shirts für Teen Girls | Tops für Teen Girls | Kleidung für Teen Girls | T-Shirts für Teen Boys | Tops | Teen Boys | Klassisches T-Shirt | Tops | Kleidung für Mädchen | Klassisches T-Shirt | Tops | Kleidung für Jungen | Kinder",
 "productGroupID": "36899289",
 "color": "Weiß",
 "brand": {
  "@type": "Brand",
  "name": "AMI Paris"
 },
 "itemCondition": "https://schema.org/NewCondition",
 "variesBy": [
  "https://schema.org/size"
 ],
 "hasVariant": [
  {
   "@type": "Product",
   "sku": "36899289-19",
   "name": "AMI Paris Baumwoll-T-Shirt mit Ami de Coeur | 4 Jahre",
   "size": "4 Jahre",
   "image": "https://cdn-images.farfetch-contents.com/36/89/92/89/36899289_69172521_1000.jpg?ov=true",
   "offers": {
    "@type": "Offer",
    "url": "https://www.farfetch.com/de/shopping/kids/ami-paris-baumwoll-t-shirt-mit-ami-de-coeur-item-36899289.aspx?lang=de-DE&size=19",
    "availability": "https://schema.org/InStock",
    "hasMerchantReturnPolicy": {
     "@type": "MerchantReturnPolicy",
     "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
     "merchantReturnDays": 30,
     "returnMethod": "https://schema.org/ReturnByMail",
     "returnFees": "https://schema.org/FreeReturn",
     "applicableCountry": [
      "DE"
     ]
    },
    "priceSpecification": [
     {
      "@type": "UnitPriceSpecification",
      "price": 60,
      "priceCurrency": "EUR"
     }
    ]
   }
  },
  {
   "@type": "Product",
   "sku": "36899289-21",
   "name": "AMI Paris Baumwoll-T-Shirt mit Ami de Coeur | 6 Jahre",
   "size": "6 Jahre",
   "image": "https://cdn-images.farfetch-contents.com/36/89/92/89/36899289_69172521_1000.jpg?ov=true",
   "offers": {
    "@type": "Offer",
    "url": "https://www.farfetch.com/de/shopping/kids/ami-paris-baumwoll-t-shirt-mit-ami-de-coeur-item-36899289.aspx?lang=de-DE&size=21",
    "availability": "https://schema.org/InStock",
    "hasMerchantReturnPolicy": {
     "@type": "MerchantReturnPolicy",
     "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
     "merchantReturnDays": 30,
     "returnMethod": "https://schema.org/ReturnByMail",
     "returnFees": "https://schema.org/FreeReturn",
     "applicableCountry": [
      "DE"
     ]
    },
    "priceSpecification": [
     {
      "@type": "UnitPriceSpecification",
      "price": 60,
      "priceCurrency": "EUR"
     }
    ]
   }
  }
 ],
 "url": "https://www.farfetch.com/de/shopping/kids/ami-paris-baumwoll-t-shirt-mit-ami-de-coeur-item-36899289.aspx"
}
</script>
<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@type": "BreadcrumbList",
 "itemListElement": [
  {
   "@type": "ListItem",
   "position": 1,
   "item": {
    "@id": "/de/shopping/kids/items.aspx",
    "name": "Kids"
   }
  },
  {
   "@type": "ListItem",
   "position": 2,
   "item": {
    "@id": "/de/shopping/kids/designer-ami-paris/items.aspx",
    "name": "AMI Paris"
   }
  },
  {
   "@type": "ListItem",
   "position": 3,
   "item": {
    "@id": "/de/shopping/kids/designer-ami-paris/boys-clothing-3/items.aspx",
    "name": "Kleidung für Jungen"
   }
  },
  {
   "@type": "ListItem",
   "position": 4,
   "item": {
    "@id": "/de/shopping/kids/designer-ami-paris/t-shirts-3/items.aspx",
    "name": "Klassisches T-Shirt"
   }
  }
 ]
}
</script>
<h4 class="ltr-2pfgen-Body-BodyBold" data-component="BodyBold">Zusammensetzung</h4><p class="ltr-4y8w0i-Body" data-component="Body"><span class="ltr-4y8w0i-Body" data-component="Body">Bio-Baumwolle 100%</span></p>
</body></html>"""

# The same shape, discounted. Two UnitPriceSpecification entries per variant:
# the one without a priceType is what is paid, the StrikethroughPrice one is
# what it was. 45 against 90 — the whole chain, published, which is why this
# parser needs no DOM price overlay.
SAMPLE_DETAIL_SALE_HTML = r"""<html><body>
<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@type": "ProductGroup",
 "name": "Pullover mit Logo-Stickerei",
 "image": [
  {
   "@type": "ImageObject",
   "contentUrl": "https://cdn-images.farfetch-contents.com/32/48/54/56/32485456_62556294_1000.jpg?ov=true",
   "description": "Marni Kids Pullover mit Logo-Stickerei | Grau"
  },
  {
   "@type": "ImageObject",
   "contentUrl": "https://cdn-images.farfetch-contents.com/32/48/54/56/32485456_62556268_1000.jpg?ov=true",
   "description": "Marni Kids Pullover mit Logo-Stickerei | Gestricktes Top"
  },
  {
   "@type": "ImageObject",
   "contentUrl": "https://cdn-images.farfetch-contents.com/32/48/54/56/32485456_62571423_1000.jpg?ov=true",
   "description": "Marni Kids Pullover mit Logo-Stickerei | Tops"
  },
  {
   "@type": "ImageObject",
   "contentUrl": "https://cdn-images.farfetch-contents.com/32/48/54/56/32485456_62556313_1000.jpg?ov=true",
   "description": "Marni Kids Pullover mit Logo-Stickerei | Kleidung für Baby Boys"
  }
 ],
 "description": "Marni Kids Pullover mit Logo-Stickerei | Grau | Grau | Logo-Stickerei | runder Ausschnitt | lange Ärmel | Baumwolle | Gestricktes Top | Tops | Kleidung für Baby Girls | Gestricktes Top | Tops | Kleidung für Baby Boys | Kinder",
 "productGroupID": "32485456",
 "color": "Grau",
 "brand": {
  "@type": "Brand",
  "name": "Marni Kids"
 },
 "itemCondition": "https://schema.org/NewCondition",
 "variesBy": [
  "https://schema.org/size"
 ],
 "hasVariant": [
  {
   "@type": "Product",
   "sku": "32485456-19",
   "name": "Marni Kids Pullover mit Logo-Stickerei | 3-6 M.",
   "size": "3-6 M.",
   "image": "https://cdn-images.farfetch-contents.com/32/48/54/56/32485456_62556294_1000.jpg?ov=true",
   "offers": {
    "@type": "Offer",
    "url": "https://www.farfetch.com/de/shopping/kids/marni-kids-pullover-mit-logo-stickerei-item-32485456.aspx?lang=de-DE&size=19",
    "availability": "https://schema.org/InStock",
    "hasMerchantReturnPolicy": {
     "@type": "MerchantReturnPolicy",
     "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
     "merchantReturnDays": 30,
     "returnMethod": "https://schema.org/ReturnByMail",
     "returnFees": "https://schema.org/FreeReturn",
     "applicableCountry": [
      "DE"
     ]
    },
    "priceSpecification": [
     {
      "@type": "UnitPriceSpecification",
      "price": 45,
      "priceCurrency": "EUR"
     },
     {
      "@type": "UnitPriceSpecification",
      "price": 90,
      "priceCurrency": "EUR",
      "priceType": "https://schema.org/StrikethroughPrice"
     }
    ]
   }
  },
  {
   "@type": "Product",
   "sku": "32485456-20",
   "name": "Marni Kids Pullover mit Logo-Stickerei | 6-9 M.",
   "size": "6-9 M.",
   "image": "https://cdn-images.farfetch-contents.com/32/48/54/56/32485456_62556294_1000.jpg?ov=true",
   "offers": {
    "@type": "Offer",
    "url": "https://www.farfetch.com/de/shopping/kids/marni-kids-pullover-mit-logo-stickerei-item-32485456.aspx?lang=de-DE&size=20",
    "availability": "https://schema.org/InStock",
    "hasMerchantReturnPolicy": {
     "@type": "MerchantReturnPolicy",
     "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
     "merchantReturnDays": 30,
     "returnMethod": "https://schema.org/ReturnByMail",
     "returnFees": "https://schema.org/FreeReturn",
     "applicableCountry": [
      "DE"
     ]
    },
    "priceSpecification": [
     {
      "@type": "UnitPriceSpecification",
      "price": 45,
      "priceCurrency": "EUR"
     },
     {
      "@type": "UnitPriceSpecification",
      "price": 90,
      "priceCurrency": "EUR",
      "priceType": "https://schema.org/StrikethroughPrice"
     }
    ]
   }
  }
 ],
 "url": "https://www.farfetch.com/de/shopping/kids/marni-kids-pullover-mit-logo-stickerei-item-32485456.aspx"
}
</script>
<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@type": "BreadcrumbList",
 "itemListElement": [
  {
   "@type": "ListItem",
   "position": 1,
   "item": {
    "@id": "/de/shopping/kids/items.aspx",
    "name": "Kids"
   }
  },
  {
   "@type": "ListItem",
   "position": 2,
   "item": {
    "@id": "/de/shopping/kids/marni-kids/items.aspx",
    "name": "Marni Kids"
   }
  },
  {
   "@type": "ListItem",
   "position": 3,
   "item": {
    "@id": "/de/shopping/kids/marni-kids/baby-boy-clothing-5/items.aspx",
    "name": "Kleidung für Baby Boys"
   }
  },
  {
   "@type": "ListItem",
   "position": 4,
   "item": {
    "@id": "/de/shopping/kids/marni-kids/knitwear-5/items.aspx",
    "name": "Gestricktes Top"
   }
  }
 ]
}
</script>
<h4 class="ltr-2pfgen-Body-BodyBold" data-component="BodyBold">Zusammensetzung</h4><p class="ltr-4y8w0i-Body" data-component="Body"><span class="ltr-4y8w0i-Body" data-component="Body">Baumwolle 100%</span></p>
</body></html>"""


# The SAME product as SAMPLE_DETAIL_FULL_PRICE_HTML (item 36899289), captured
# on a US exit rather than a DE one, so the cross-locale claims are pinned
# against real bytes from both markets rather than against one market and an
# assumption about the other.
#
# What this fixture is FOR: the variant sku is identical across markets while
# every human-readable field is not. A cross-market comparison therefore joins
# on sku, and `size` is display text.
SAMPLE_DETAIL_US_HTML = r"""<html><body>
<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@type": "ProductGroup",
 "name": "cotton t-shirt with Ami de Coeur",
 "image": [
  {
   "@type": "ImageObject",
   "contentUrl": "https://cdn-images.farfetch-contents.com/36/89/92/89/36899289_69172521_1000.jpg",
   "description": "AMI Paris cotton t-shirt with Ami de Coeur | White"
  },
  {
   "@type": "ImageObject",
   "contentUrl": "https://cdn-images.farfetch-contents.com/36/89/92/89/36899289_69623678_1000.jpg",
   "description": "AMI Paris cotton t-shirt with Ami de Coeur | Boys T-Shirts"
  }
 ],
 "description": "AMI Paris cotton t-shirt with Ami de Coeur | White | boxy fit | round collar | Ami de Coeur embossed and topstitched on chest | tonal Ami embroidery under back neckline | organic cotton | Organic Cotton | Teen T-Shirts | Teen Tops | Teen Girl Clothing | Teen T-shirts | Tops | Teen Boy Clothing | Girls T-Shirts | Tops | Girls Clothing | Boys T-Shirts | Boys Tops | Boys Clothing | Kids",
 "productGroupID": "36899289",
 "color": "White",
 "brand": {
  "@type": "Brand",
  "name": "AMI Paris"
 },
 "itemCondition": "https://schema.org/NewCondition",
 "variesBy": [
  "https://schema.org/size"
 ],
 "hasVariant": [
  {
   "@type": "Product",
   "sku": "36899289-19",
   "name": "AMI Paris cotton t-shirt with Ami de Coeur | 4 yrs",
   "size": "4 yrs",
   "image": "https://cdn-images.farfetch-contents.com/36/89/92/89/36899289_69172521_1000.jpg",
   "offers": {
    "@type": "Offer",
    "url": "https://www.farfetch.com//shopping/kids/ami-paris-cotton-t-shirt-with-ami-de-coeur-item-36899289.aspx?lang=en-US&size=19",
    "availability": "https://schema.org/InStock",
    "hasMerchantReturnPolicy": {
     "@type": "MerchantReturnPolicy",
     "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
     "merchantReturnDays": 30,
     "returnMethod": "https://schema.org/ReturnByMail",
     "returnFees": "https://schema.org/FreeReturn",
     "applicableCountry": [
      "US"
     ]
    },
    "priceSpecification": [
     {
      "@type": "UnitPriceSpecification",
      "price": 90,
      "priceCurrency": "USD"
     }
    ]
   }
  },
  {
   "@type": "Product",
   "sku": "36899289-21",
   "name": "AMI Paris cotton t-shirt with Ami de Coeur | 6 yrs",
   "size": "6 yrs",
   "image": "https://cdn-images.farfetch-contents.com/36/89/92/89/36899289_69172521_1000.jpg",
   "offers": {
    "@type": "Offer",
    "url": "https://www.farfetch.com//shopping/kids/ami-paris-cotton-t-shirt-with-ami-de-coeur-item-36899289.aspx?lang=en-US&size=21",
    "availability": "https://schema.org/InStock",
    "hasMerchantReturnPolicy": {
     "@type": "MerchantReturnPolicy",
     "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
     "merchantReturnDays": 30,
     "returnMethod": "https://schema.org/ReturnByMail",
     "returnFees": "https://schema.org/FreeReturn",
     "applicableCountry": [
      "US"
     ]
    },
    "priceSpecification": [
     {
      "@type": "UnitPriceSpecification",
      "price": 90,
      "priceCurrency": "USD"
     }
    ]
   }
  }
 ],
 "url": "https://www.farfetch.com//shopping/kids/ami-paris-cotton-t-shirt-with-ami-de-coeur-item-36899289.aspx"
}
</script>
<script type="application/ld+json">
{
 "@context": "https://schema.org",
 "@type": "BreadcrumbList",
 "itemListElement": [
  {
   "@type": "ListItem",
   "position": 1,
   "item": {
    "@id": "/shopping/kids/items.aspx",
    "name": "Kids Home"
   }
  },
  {
   "@type": "ListItem",
   "position": 2,
   "item": {
    "@id": "/shopping/kids/designer-ami-paris/items.aspx",
    "name": "AMI Paris"
   }
  },
  {
   "@type": "ListItem",
   "position": 3,
   "item": {
    "@id": "/shopping/kids/designer-ami-paris/boys-clothing-3/items.aspx",
    "name": "Boys Clothing"
   }
  },
  {
   "@type": "ListItem",
   "position": 4,
   "item": {
    "@id": "/shopping/kids/designer-ami-paris/t-shirts-3/items.aspx",
    "name": "Boys T-Shirts"
   }
  }
 ]
}
</script>
<h4 class="ltr-2pfgen-Body-BodyBold" data-component="BodyBold">Composition</h4><p class="ltr-4y8w0i-Body" data-component="Body"><span class="ltr-4y8w0i-Body" data-component="Body">Organic Cotton 100%</span></p>
</body></html>"""


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    return condition


def _kwonly_only(fn) -> bool:
    """True if `fn` takes no positional parameters at all.

    A shared helper called from three engines is exactly where a positional
    argument goes wrong quietly: tokopedia-scraper's classify(html, status,
    url) was called as classify(html, url=...) by two of its three engines
    and both crashed on their FIRST fetch, invisible to import, --help,
    compileall and 400+ green assertions (CLAUDE.md §17). Keyword-only
    parameters make that shape impossible to write.
    """
    import inspect
    return all(p.kind is inspect.Parameter.KEYWORD_ONLY
               for p in inspect.signature(fn).parameters.values())


# Skips and their engine names are accumulated ACROSS sections, so they are
# module state rather than something threaded through every signature. Only
# these: a section that needs anything else makes it.
#
# A suite that silently skips part of itself and still says "all passed" is
# the same defect as code reporting success without checking that what it
# wanted actually happened — which is why they are reported at the end rather
# than merely collected.
_skips = []
_skipped_engines = set()

# The repo this suite is checking. Was recomputed inside several sections
# when they all lived in one function and could see each other's locals.
_repo_root = os.path.dirname(os.path.abspath(__file__))


# Fixtures several sections assert against. Computed once at import, as they
# were when this was one function — they are pure, and recomputing them per
# section would be three copies of the same arrangement pretending to be
# three tests.
live = detect_recaptcha_in_page(lambda _js: LIVE_DISCOVERY_FARFETCH,
                                page_url="https://www.farfetch.com/")
v3 = detect_recaptcha_in_page(lambda _js: LIVE_DISCOVERY_V3, page_url="https://x/")
v2 = detect_recaptcha_in_page(lambda _js: LIVE_DISCOVERY_V2_CHECKBOX, page_url="https://x/")
products = parse_products(SAMPLE_LISTING_HTML,
                          "https://www.farfetch.com/shopping/kids/items.aspx",
                          category="Kids")

import scraper_api_client as _sac  # noqa: E402 — after the fixtures it reads

# playwright is optional; the suite must pass with no engine installed at all.
try:
    import playwright_scraper as _ps
except ImportError:
    _ps = None


def check_runtime_recaptcha_detection_added_2026_08_24(ok: bool) -> bool:
    """runtime reCAPTCHA detection (added 2026-08-24)"""
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

    ok &= check("api.js render=<sitekey> classified as v3",
                v3 is not None and v3.kind == "recaptcha_v3" and v3.is_v3)
    ok &= check("runtime detector carries the action through", v3 is not None and v3.action == "signup")

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

    return ok


def check_reconciling_two_detectors_that_disagree(ok: bool) -> bool:
    """reconciling two detectors that disagree"""
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

    return ok


def check_api_v2_task_objects_must_match_the_documented_ty(ok: bool) -> bool:
    """API v2 task objects must match the documented types"""
    # --- API v2 task objects must match the documented types ---------------
    return ok


def check_discounted_prices_the_dom_overlay(ok: bool) -> bool:
    """discounted prices: the DOM overlay"""
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

        # KNOWN LIMITATION, pinned deliberately    #
    # ---- KNOWN LIMITATION, pinned deliberately -----------------------------
    # The overlay assumes every price in a tile belongs to ONE discount chain,
    # which is measured behaviour for this site: Farfetch prints original /
    # sale / final and publishes only the middle one, so min() is the price a
    # customer pays. An INSTALLMENT price in the same tile would break that
    # assumption — min() would take the per-payment figure.
    #
    # Not defended against, on purpose. Checked against a live 106-tile
    # capture (2026-09-07): zero tiles contained an installment marker and
    # zero had a min more than 2.5x below the next price. The page does
    # mention Klarna and "Raten", but only in the footer and a translation
    # bundle, outside any tile's scope. Guarding it would mean either chasing
    # wording across every locale, or a ratio threshold that would reject the
    # real 60%+ discounts this site runs constantly.
    #
    # So this test does not assert the RIGHT answer — it pins the current one,
    # so that if Farfetch ever moves installments into a tile, the change
    # surfaces here as a deliberate decision rather than as "why are our
    # discounts 81%".
    _INSTALMENT = ('<html><body><script type="application/ld+json">'
                   + json.dumps({"@type": "ItemList", "itemListElement": [
                       {"@type": "Product", "name": "coat",
                        "offers": {"price": 150, "priceCurrency": "EUR",
                                   "url": "/de/shopping/kids/x-item-70000001.aspx"}}]})
                   + '</script>'
                     '<div><div><a href="/de/shopping/kids/x-item-70000001.aspx">'
                     '<p>200 &euro;</p><p>150 &euro;</p>'
                     '<p>oder 4 Zahlungen von 37,50 &euro;</p>'
                     '</a></div></div></body></html>')
    _inst = parse_products(_INSTALMENT, _SALE_URL)[0]
    ok &= check("KNOWN LIMITATION: an installment price inside a tile would be "
                "taken as the product price (min of the chain). Not seen on "
                "this site in a live 106-tile check; pinned so a change is "
                "noticed rather than silent",
                _inst.price == 37.5 and _inst.discount_pct == 81.2)

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

        # price_source    #
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
    return ok




def check_category_label(ok: bool) -> bool:
    """category label"""
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

    return ok


def check_sample_selection(ok: bool) -> bool:
    """sample selection"""
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

    return ok


def check_credential_loading(ok: bool) -> bool:
    """credential loading"""
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

    return ok


def check_v2_createtask_gettaskresult_round_trip_mocked(ok: bool) -> bool:
    """v2 createTask/getTaskResult round trip (mocked)"""
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

        # 2captcha payload must match the variant (legacy v1)    #
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
    return ok




def check_sign_up_modal_selectors(ok: bool) -> bool:
    """sign-up modal selectors"""
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

    return ok


def check_empty_result_contract(ok: bool) -> bool:
    """empty-result contract"""
    # --- empty-result contract ---------------------------------------------
    # A run that finds nothing must not look like a successful run that found
    # nothing to sell, and must not overwrite last night's good file with `[]`.

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

        # An empty result is still a well-formed result. A zero-byte CSV makes
        # a consumer fail on read (no columns at all) instead of reading a
        # valid table with zero rows — the opposite of what the rest of this
        # module is careful about.
        import csv as _csv
        from dataclasses import asdict
        from output_writer import write_csv as _wcsv
        _empty_csv = os.path.join(tmp, "empty_rows.csv")
        _wcsv([], _empty_csv)
        with open(_empty_csv, newline="", encoding="utf-8") as f:
            _rdr = _csv.reader(f)
            _hdr = next(_rdr, None)
            _rest = list(_rdr)
        ok &= check("an empty CSV still carries the full header row, so it "
                    "parses as a table with zero rows rather than failing",
                    _hdr == list(asdict(Product()).keys()) and _rest == [])

    return ok


def check_blocked_vs_empty_exit_code(ok: bool) -> bool:
    """blocked-vs-empty exit code"""
    # --- blocked-vs-empty exit code -----------------------------------------
    # README documents exit 3 (blocked before parsing) as distinct from exit 4
    # (genuinely zero products), but nothing detected a bot-challenge page in
    # any of the three browser engines until this was shared from
    # scraper_api_client.py into product_parser.py — a category page sitting
    # behind Akamai/Cloudflare would previously reach parse_products, get 0
    # products back, and exit 4 exactly like an empty category, which is the
    # ambiguity the exit-code contract exists to prevent.
    from product_parser import BOT_CHALLENGE_MARKERS

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

    return ok


def check_akamai_s_refusal_page_the_2026_09_11_audit_s_p0(ok: bool) -> bool:
    """Akamai's REFUSAL page (the 2026-09-11 audit's P0)"""
    # --- Akamai's REFUSAL page (the 2026-09-11 audit's P0) ------------------
    # Both fixtures below are the real thing, captured 2026-09-14 from a
    # datacentre address that farfetch.com refuses: the reference id is the
    # only thing edited (it is per-request, so pinning a real one would be
    # noise). The page is 318-426 bytes, carries HTTP 403, and contains NONE
    # of the challenge markers above — so detect_bot_challenge returned None,
    # and a plainly blocked run reported exit 4, "this category is empty".
    #
    # The TWO fixtures are the point, not duplication. The same page reaches
    # the parser in two different spellings depending on transport, and a
    # marker can pass one while silently missing the other:
    from product_parser import detect_access_denied, describe_block

    ok &= check("detect_access_denied: the browser-DOM form of the refusal "
                "page is recognised",
                detect_access_denied(SAMPLE_AKAMAI_DENIED_DOM_HTML))
    ok &= check("detect_access_denied: the RAW-TRANSPORT form is recognised "
                "too — Akamai entity-escapes the punctuation, so a literal "
                "'errors.edgesuite.net' marker matches the browser engines "
                "and misses scraper_api_client entirely",
                detect_access_denied(SAMPLE_AKAMAI_DENIED_RAW_HTML))
    ok &= check("...and the raw form really is escaped, so that check is not "
                "quietly testing the same string twice",
                "errors.edgesuite.net" not in SAMPLE_AKAMAI_DENIED_RAW_HTML
                and "errors&#46;edgesuite&#46;net" in SAMPLE_AKAMAI_DENIED_RAW_HTML)
    ok &= check("detect_bot_challenge reports the refusal page as akamai, so "
                "it reaches EXIT_BLOCKED like any other block",
                detect_bot_challenge(SAMPLE_AKAMAI_DENIED_DOM_HTML) == "akamai"
                and detect_bot_challenge(SAMPLE_AKAMAI_DENIED_RAW_HTML) == "akamai")
    ok &= check("describe_block calls a refusal a refusal, not a challenge — "
                "a log naming a widget that is not there sends the reader "
                "looking for one",
                "refusal" in describe_block(SAMPLE_AKAMAI_DENIED_DOM_HTML, "akamai")
                and "challenge" in describe_block('<div class="cf-challenge">',
                                                  "cloudflare"))

    # The negative half, and the half that matters more: a marker that fires
    # on a good page is worse than no marker at all (CLAUDE.md §18, where a
    # bare "akamai" marker made tokopedia-scraper report every served page as
    # blocked). Every real-capture fixture in this suite is checked, not just
    # the one listing sample.
    for _name, _html in sorted((n, v) for n, v in list(globals().items())
                               if n.startswith("SAMPLE_") and n.endswith("HTML")
                               and "DENIED" not in n and isinstance(v, str)):
        ok &= check(f"detect_access_denied does NOT fire on {_name}",
                    not detect_access_denied(_html))

    # "edgesuite" on its own is an ordinary Akamai ASSET domain. Matching it
    # bare would report a site serving its own images from one as blocked on
    # every page — the exact shape of the §18 trap. Pinned so a future
    # broadening of the marker is a decision rather than a surprise.
    ok &= check("a page merely SERVED from an edgesuite asset host is not a "
                "refusal — only errors.edgesuite.net is",
                not detect_access_denied(
                    '<html><head><title>Kids</title></head><body>'
                    '<img src="https://cdn.a1937.edgesuite.net/x.jpg">'
                    '</body></html>'))

    return ok


def check_page_content_mid_navigation(ok: bool) -> bool:
    """page.content() mid-navigation"""
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
        _skipped_engines.add("playwright")
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

        # Pagination must not depend ENTIRELY on NEXT_PAGE_SELECTOR. Rename one
        # data-testid upstream and every run would end after page 1 while
        # reporting a complete, successful result — the worst failure shape
        # there is, and one neither the offline suite nor a one-page canary
        # would notice. The engine now falls back to ?page=N and lets the data
        # decide when to stop, so this pins the fallback's presence.
        _src = open("playwright_scraper.py", encoding="utf-8").read()
        ok &= check("playwright: a missing pagination link falls back to the "
                    "?page= convention instead of ending the run",
                    "page_url(page.url, page_num + 1)" in _src
                    and 'stop_reason = "pagination_exhausted"' not in _src)
        ok &= check("playwright: a page adding no new skus stops the loop with "
                    "the data-based reason, not a selector-based one",
                    'stop_reason = "no_new_products"' in _src)

        # Phase 1 of concurrency work: page URLs are planned up front so a
        # page's address no longer depends on having fetched the one before
        # it. Only safe when the site's own link AGREES with the ?page=N
        # convention, so that is verified rather than assumed.
        _base = "https://www.farfetch.com/de/shopping/kids/x/items.aspx"
        ok &= check("_same_url ignores query-parameter ORDER, which carries no "
                    "meaning, but not a different param or path",
                    _ps._same_url(_base + "?a=1&page=2", _base + "?page=2&a=1")
                    and _ps._same_url(_base + "/", _base)
                    and not _ps._same_url(_base + "?page=2", _base + "?page=3")
                    and not _ps._same_url(_base + "?page=2",
                                          _base + "?page=2&cursor=abc"))

        class _FakePage:
            """Stands in for a Playwright page carrying one next-link."""
            def __init__(self, href): self._href = href
            def query_selector(self, _sel):
                if self._href is None:
                    return None
                href = self._href
                class _El:
                    def get_attribute(self, _n): return href
                return _El()

        class _PlanArgs:
            pages = 4

        ok &= check("page URLs are planned up front when the site's own next "
                    "link matches the ?page= convention",
                    _ps._plan_page_urls(_FakePage(_base + "?page=2"), _PlanArgs(), _base)
                    == [_base + "?page=2", _base + "?page=3", _base + "?page=4"])
        ok &= check("page URLs are still planned when NO next link is served "
                    "at all — which is what farfetch.com actually does",
                    _ps._plan_page_urls(_FakePage(None), _PlanArgs(), _base)
                    == [_base + "?page=2", _base + "?page=3", _base + "?page=4"])
        ok &= check("planning is REFUSED when the site's link carries something "
                    "the convention cannot reproduce (a cursor, a token) — the "
                    "run then chains link to link and cannot be parallelised",
                    _ps._plan_page_urls(
                        _FakePage(_base + "?page=2&cursor=opaque"),
                        _PlanArgs(), _base) is None)

        class _OnePage:
            pages = 1
        ok &= check("a single-page run plans nothing, since there is no page 2",
                    _ps._plan_page_urls(_FakePage(None), _OnePage(), _base) is None)

        # Merging in PAGE order rather than arrival order is what makes the
        # output independent of the order pages happen to finish — the
        # property concurrency needs and today's sequential run already has.
        _o1 = _ps.PageOutcome(page_num=1, url="u1")
        _o2 = _ps.PageOutcome(page_num=2, url="u2", load_failed=True)
        _o3 = _ps.PageOutcome(page_num=3, url="u3", blocked_by="akamai")
        ok &= check("a PageOutcome is 'ok' only when it neither failed to load "
                    "nor came back as a challenge page",
                    _o1.ok and not _o2.ok and not _o3.ok)
        ok &= check("playwright merges outcomes sorted by page number, not by "
                    "the order they finished",
                    "sorted(outcomes, key=lambda o: o.page_num)" in _src)
        ok &= check("playwright reports WHICH pages failed, not just how many "
                    "completed — a count stops being a description once pages "
                    "can fail out of order",
                    "pages_failed=failed_pages" in _src)

        # --- the checkpoint: resume without re-fetching ------------------
        # A run that dies on page 17 of 20 used to start again at page 1. The
        # checkpoint is written after EVERY page and always, never behind a
        # flag — nobody passes --checkpoint on the run that is about to be
        # killed, and by then the pages are gone.
        import run_state as _rs

        class _Args:
            def __init__(self, url="https://www.farfetch.com/shopping/kids/items.aspx",
                         pages=5, category="Kids", out="x"):
                self.url, self.pages, self.category, self.out = url, pages, category, out

        with tempfile.TemporaryDirectory() as _tmp:
            _pfx = os.path.join(_tmp, "run")
            _a = _Args(out=_pfx)
            _cp = _rs.Checkpoint(_pfx, _a)
            _p1 = [Product(sku="a", price=1.0), Product(sku="b", price=2.0)]
            _p2 = [Product(sku="c", price=3.0)]
            _cp.record(1, _p1, "https://x/1")
            _cp.record(2, _p2, "https://x/2")

            ok &= check("the checkpoint is on disk after each page, not at the "
                        "end — the run it exists for is the one that never "
                        "reaches the end",
                        os.path.exists(_rs.path_for(_pfx)))

            _cp2 = _rs.Checkpoint(_pfx, _Args(out=_pfx))
            _msg = _cp2.resume()
            ok &= check("...and a fresh Checkpoint restores those pages, in "
                        "page order, with their products intact",
                        _cp2.resumed_from == [1, 2]
                        and [p.sku for p in _cp2.products_in_page_order()]
                        == ["a", "b", "c"])
            ok &= check("...saying which pages it restored, rather than "
                        "resuming silently",
                        any("1-2" in m for m in _msg))

            # The whole risk of resume in one check. Mixing two categories
            # into one file looks like a successful scrape of something that
            # was never scraped — worse than any crash.
            _other = _rs.Checkpoint(_pfx, _Args(url="https://www.farfetch.com/shopping/women/items.aspx",
                                                out=_pfx))
            _m2 = _other.resume()
            ok &= check("a checkpoint for a DIFFERENT url is refused, and the "
                        "message names the difference",
                        _other.resumed_from == []
                        and any("DIFFERENT run" in m and "start_url" in m
                                for m in _m2))

            _diff_pages = _rs.Checkpoint(_pfx, _Args(pages=9, out=_pfx))
            ok &= check("...so is one for a different page count",
                        _diff_pages.resume() and _diff_pages.resumed_from == [])
            _diff_cat = _rs.Checkpoint(_pfx, _Args(category="Women", out=_pfx))
            ok &= check("...and one for a different category label",
                        _diff_cat.resume() and _diff_cat.resumed_from == [])

            # Retry/proxy settings are exactly what a person changes between
            # the crash and the retry. Refusing on those would refuse every
            # real resume.
            _a_retry = _Args(out=_pfx)
            _a_retry.retries, _a_retry.proxy = 99, "http://x:1"
            ok &= check("changing --retries or --proxy does NOT invalidate a "
                        "checkpoint — neither changes what a page contains",
                        _rs.identity(_a_retry) == _rs.identity(_a))

            _bumped = json.loads(open(_rs.path_for(_pfx), encoding="utf-8").read())
            _bumped["format_version"] = _rs.FORMAT_VERSION + 1
            with open(_rs.path_for(_pfx), "w", encoding="utf-8") as _f:
                json.dump(_bumped, _f)
            _oldfmt = _rs.Checkpoint(_pfx, _Args(out=_pfx))
            ok &= check("a checkpoint from a build whose Product had other "
                        "columns is refused, not fed to the dataclass",
                        _oldfmt.resume() and _oldfmt.resumed_from == [])

            with open(_rs.path_for(_pfx), "w", encoding="utf-8") as _f:
                _f.write('{"format_version": 1, "pages": {"1": ')   # truncated
            _trunc = _rs.Checkpoint(_pfx, _Args(out=_pfx))
            ok &= check("a truncated checkpoint (the process died mid-write) "
                        "is reported and skipped, never crashes the resume",
                        _trunc.resume() and _trunc.resumed_from == [])

            _cp.clear()
            ok &= check("a completed run deletes its checkpoint — a stale one "
                        "would offer to resume a run that is already done",
                        not os.path.exists(_rs.path_for(_pfx)))

            _single = _rs.Checkpoint(_pfx, _Args(pages=1, out=_pfx))
            _single.record(1, _p1)
            ok &= check("a single-page run writes no checkpoint at all — there "
                        "is no page to resume to",
                        not os.path.exists(_rs.path_for(_pfx)))

            _creds = _Args(url="https://user:secret@www.farfetch.com/x", out=_pfx)
            ok &= check("a URL carrying credentials is masked before it is "
                        "written to disk",
                        "secret" not in json.dumps(_rs.identity(_creds)))

        ok &= check("playwright only SKIPS a stored page when pagination is "
                    "addressable — page 17 is unreachable without 16 when the "
                    "site chains next-links, and silently not fetching it "
                    "would produce a run missing its middle",
                    "restorable and planned is None" in _src)
        ok &= check("a partial run KEEPS its checkpoint; only a complete one "
                    "clears it",
                    "if stop_reason in COMPLETE_STOP_REASONS and rows:"
                    in _src)

        # Phase 2: each worker owns a browser AND an exit for its lifetime.
        import proxy_pool as _pp_here
        _shared = _pp_here.ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
        _w = [_ps._worker_pool(_shared, i) for i in range(3)]
        ok &= check("each worker starts on a DIFFERENT exit — N workers all "
                    "leaving from one address is just a faster way to burn it",
                    [p.current for p in _w]
                    == ["http://a:1", "http://b:2", "http://c:3"])
        ok &= check("a worker can still walk the rest of the pool after a block, "
                    "wrapping through the exits the others started on",
                    _w[2].advance("blocked") == "http://a:1")
        ok &= check("worker pools are separate objects sharing no mutable state, "
                    "so rotation between threads needs no lock",
                    _w[0].current == "http://a:1" and _w[1].current == "http://b:2"
                    and _shared.current == "http://a:1"
                    and _shared.proxies is not _shared.proxies)
        ok &= check("more workers than exits still works — the pool wraps rather "
                    "than leaving a worker with nothing",
                    _ps._worker_pool(_pp_here.ProxyPool(["http://only:1"]), 5).current
                    == "http://only:1")
        ok &= check("no proxy pool means no worker pool, not a crash",
                    _ps._worker_pool(None, 0) is None)

        # The guardrails around raising concurrency, checked at the source
        # level because they are one-line decisions with no return value.
        ok &= check("concurrency defaults to 1, so the default run is exactly "
                    "the sequential one",
                    '"--concurrency", type=positive_int, default=1' in _src)
        ok &= check("raising concurrency without a proxy pool warns that every "
                    "worker leaves from the same address",
                    "with no proxy pool: every worker" in _src)
        ok &= check("concurrency is refused with --cdp-endpoint, where the "
                    "Scraping Browser allows one live connection per profile",
                    "--concurrency is ignored with --cdp-endpoint" in _src)
        ok &= check("a listing whose pagination cannot be planned falls back to "
                    "one page at a time instead of fetching wrong URLs fast",
                    "cannot be addressed independently" in _src)
        # Measured 2026-09-07: farfetch.com served NO anchor matching any of
        # the three selectors this project shipped with, but did serve
        # <link rel="next"> in <head>. A standards-based selector outlives a
        # build-generated attribute, so it has to stay in the list.
        ok &= check("every engine's NEXT_PAGE_SELECTOR includes the "
                    "standards-based link[rel=next], which is what this site "
                    "actually serves",
                    all("link[rel='next']" in
                        open(f, encoding="utf-8").read().split(
                            "NEXT_PAGE_SELECTOR = ")[1][:200]
                        for f in ("playwright_scraper.py", "puppeteer_scraper.py",
                                  "selenium_scraper.py")))

        # ---- captcha: detected is not the same as blocking ------------------
        # Reproduced from an audit: a page carrying products AND the sign-up
        # modal's hidden reCAPTCHA, run with no API key, raised RuntimeError
        # out of solve_recaptcha, through handle_captcha_if_present, and out
        # of scrape() — a traceback in place of products that were right
        # there. Separately, solving a modal widget when the catalogue is
        # already readable spends money on a challenge guarding nothing.
        class _CaptchaPage:
            """A page with `n_links` product links and a detectable captcha."""
            LIVE = {"found": True, "size": "invisible", "action": None,
                    "sitekey": "6LeifPcbAAAAAJaiPe_xgLTfnbdpEMAYJAAnVFJT",
                    "enterprise": False, "containerId": "register-captcha",
                    "hints": [], "badge": True, "challengeFrame": True,
                    "scripts": [], "renderParam": "explicit"}

            def __init__(self, n_links):
                self.n, self.reloaded, self.url = n_links, False, "https://x/"

            def content(self):
                return ('<html><div id="register-captcha" class="g-recaptcha">'
                        '<iframe title="reCAPTCHA" src="https://recaptcha.net/'
                        'recaptcha/api2/anchor?k=6LeifPcbAAAAAJaiPe_xgLTfnbdpEM'
                        'AYJAAnVFJT&size=invisible"></iframe></div><script src='
                        '"https://recaptcha.net/recaptcha/api.js?render=explicit"'
                        '></script></html>')

            def query_selector_all(self, _sel):
                return [None] * self.n

            def evaluate(self, _js, *_a):
                return dict(self.LIVE)

            def wait_for_timeout(self, _ms):
                pass

            def reload(self, **_kw):
                self.reloaded = True

        class _CapArgs:
            twocaptcha_key = None
            captcha_api = "v2"
            min_score = 0.7
            solve_captcha = "when-blocked"

        _cp = _CaptchaPage(96)
        _ca = _CapArgs()
        try:
            _ps.handle_captcha_if_present(_cp, _ca)
            _crashed = False
        except Exception:  # noqa: BLE001
            _crashed = True
        ok &= check("captcha: with products already on the page, the challenge "
                    "is NOT solved — it guards the sign-up modal, not the "
                    "catalogue, and solving it would spend a paid task on "
                    "nothing",
                    not _crashed and not _cp.reloaded)

        _cp0 = _CaptchaPage(0)
        try:
            _ps.handle_captcha_if_present(_cp0, _CapArgs())
            _crashed0 = False
        except Exception:  # noqa: BLE001
            _crashed0 = True
        ok &= check("captcha: no API key no longer raises out of the run — it "
                    "warns and lets the products (or the exit-3 block report) "
                    "speak for themselves",
                    not _crashed0 and not _cp0.reloaded)

        _cpa = _CaptchaPage(96)
        _caa = _CapArgs()
        _caa.solve_captcha = "always"
        try:
            _ps.handle_captcha_if_present(_cpa, _caa)
            _crasheda = False
        except Exception:  # noqa: BLE001
            _crasheda = True
        ok &= check("captcha: --solve-captcha always goes past the "
                    "already-readable check, for whoever would rather spend a "
                    "solve than risk missing content",
                    not _crasheda)

        _psrc_cap = open("playwright_scraper.py", encoding="utf-8").read()
        ok &= check("captcha: the readiness check counts links directly instead "
                    "of forcing the 20s wait_for_function to run first — which "
                    "would waste 20s on a page the captcha genuinely gates",
                    "page.query_selector_all(ITEM_LINK_SELECTOR)" in _psrc_cap)
        for _eng in ("puppeteer_scraper.py", "selenium_scraper.py"):
            _esrc_cap = open(_eng, encoding="utf-8").read()
            ok &= check(f"{_eng}: same captcha policy, so the engines cannot "
                        f"differ on whether a run crashes or pays",
                        'getattr(args, "solve_captcha", "when-blocked")' in _esrc_cap
                        and "cannot be "  in _esrc_cap
                        and '"--solve-captcha"' in _esrc_cap)

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
              "screen": {"width": 1920, "height": 1080,
                         "deviceScaleFactor": 1},
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
        # The device pixel ratio, which Playwright takes as its own option
        # and which was dropped on the floor until a live browser was
        # compared against the fingerprint: one stating 1.25 produced a
        # browser reporting `devicePixelRatio === 1`, an identity
        # contradicting itself on an axis a fingerprinter reads for free.
        ok &= check("fingerprint: the device scale factor is carried",
                    kw.get("device_scale_factor") == 1)
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

    return ok


def check_puppeteer_pyppeteer_ua_derived_from_the_real_lau(ok: bool) -> bool:
    """Puppeteer/pyppeteer: UA derived from the real launched version"""
    # ---- Puppeteer/pyppeteer: UA derived from the real launched version ----
    # Guarded the same way as the Playwright block above: importing
    # puppeteer_scraper pulls in pyppeteer, which is not installed in the
    # offline CI job on purpose.
    try:
        import puppeteer_scraper as _pup
    except ImportError as exc:
        _pup = None
        _skips.append(f"puppeteer_scraper UA checks (pyppeteer not installed: {exc.name})")
        _skipped_engines.add("pyppeteer")
    if _pup is not None:
        ok &= check("puppeteer_scraper._chrome_ua names the browser's REAL version, "
                    "not a hardcoded one that only ever drifts out of date",
                    _pup._chrome_ua("127.0.6533.17")
                    == "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/127.0.6533.17 Safari/537.36")

    return ok


def check_selenium_chromedriver_on_the_local_path(ok: bool) -> bool:
    """Selenium: --chromedriver on the LOCAL path"""
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
            def execute_cdp_cmd(self, name, params=None):
                seen.setdefault("cdp", []).append((name, params))
                return {}
            # build_driver reads this to build a UA naming the browser's own
            # real version instead of a hardcoded one. Absent from this mock,
            # the whole check crashed — and it went unnoticed because selenium
            # was not installed anywhere it ran, including the engine-smoke
            # CI job, which installed only playwright and pyppeteer.
            capabilities = {"browserVersion": "127.0.6533.17"}

        # build_driver checks the path is a real, executable file, so this
        # needs to BE one. It used to point at smoke_test.py itself, which
        # meant running the suite chmod'ed a tracked file to 755 and left a
        # mode change in `git status` — a test must not mutate the working
        # tree. A throwaway temp file does the same job with no side effect.
        _fake_driver = tempfile.NamedTemporaryFile(
            prefix="fake-chromedriver-", delete=False)
        _fake_driver.close()
        os.chmod(_fake_driver.name, 0o755)

        class _Args:
            cdp_endpoint = None
            headless = True
            proxy = None
            disable_build_check = False
            chromedriver = _fake_driver.name

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
            _sel.build_driver(_Args())
        finally:
            builtins.__import__ = real_import
            _sel.Service, _sel.webdriver.Chrome = real_svc, real_chrome
            os.unlink(_fake_driver.name)

        ok &= check("selenium: --chromedriver is honoured on the LOCAL path too",
                    seen.get("path") == _Args.chromedriver and seen.get("built") is True)
        ok &= check("selenium: with --chromedriver given, webdriver_manager is never "
                    "imported (so it works behind an egress allowlist)",
                    blocked["hit"] is False)
        _ua_cmds = [p for n, p in seen.get("cdp", [])
                    if n == "Network.setUserAgentOverride"]
        ok &= check("selenium: the UA override names the version the driver "
                    "actually reported, not a hardcoded one",
                    len(_ua_cmds) == 1
                    and "Chrome/127.0.6533.17 " in _ua_cmds[0]["userAgent"])
    except ImportError:
        _skips.append("selenium --chromedriver local-path checks "
                      "(selenium not installed)")
        _skipped_engines.add("selenium")

    # A missing chromedriver is a SETUP problem, not a crash. It used to be
    # `raise SystemExit("...")`, which prints the message and exits 1 — the
    # code the contract reserves for "this program fell over". Confirmed live
    # on 2026-09-14: a machine with neither chromedriver nor webdriver-manager
    # got exit 1 for something the operator can fix in one command.
    #
    # Checked at the source level: reaching the real branch needs a machine
    # with no chromedriver, which is exactly the environment this suite cannot
    # assume. cli_entry answers the equivalent question (an engine's driver
    # LIBRARY absent) with 2 as well, and the two must not disagree about the
    # same kind of problem.
    _sel_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "selenium_scraper.py"),
                    encoding="utf-8").read()
    _driver_msg = "Launching a local Chrome needs a chromedriver"
    ok &= check("a missing chromedriver exits 2 (setup problem), not 1 "
                "(crash) — the same answer cli_entry gives for a missing "
                "driver library",
                _driver_msg in _sel_src
                and "raise SystemExit(2) from None" in _sel_src
                and f'raise SystemExit(\n                "{_driver_msg}' not in _sel_src)

    return ok


def check_selenium_two_live_local_failures_turned_into_tes(ok: bool) -> bool:
    """Selenium: two live local failures, turned into tests"""
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
        # The original defect was that this probe EXECUTED every candidate,
        # including ones that do not exist, and spent 10.5s doing it. The fix
        # is to test for the file first.
        #
        # This used to be asserted as "takes under 3 seconds", and that is a
        # PROXY for the behaviour rather than the behaviour: it passed for the
        # right reason almost always and failed on a loaded machine, which is
        # the worst kind of check — one that teaches people to re-run rather
        # than to look. Seen doing exactly that on 2026-09-15.
        #
        # Asserted directly instead: every candidate that is executed must
        # have been checked for existence first. The probe is handed a
        # candidate list of paths that cannot exist, and must spawn NOTHING.
        _spawned = []
        _orig_run = subprocess.run

        def _tracking_run(cmd, *a, **k):
            _spawned.append(cmd)
            return _orig_run(cmd, *a, **k)

        _orig_cands = _sel._chrome_candidates
        _sel._chrome_candidates = lambda: [
            "/nonexistent/Chrome", "/also/not/here/Chromium"]
        subprocess.run = _tracking_run
        try:
            _sel._local_chrome_version()
        finally:
            subprocess.run = _orig_run
            _sel._chrome_candidates = _orig_cands
        # NOT "spawns nothing": on macOS the probe legitimately asks Spotlight
        # where a browser is, which is a subprocess and is not the bug. The
        # bug was executing CANDIDATE PATHS that do not exist, so that is what
        # is asserted — no spawned command may name one.
        _executed = " ".join(str(c) for c in _spawned)
        ok &= check("selenium: a candidate path that does not exist is never "
                    "EXECUTED — the original probe ran every path it could "
                    "think of, with a 10s timeout each, and spent 10.5s "
                    "finding nothing",
                    "/nonexistent/Chrome" not in _executed
                    and "/also/not/here/Chromium" not in _executed)

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
        _skipped_engines.add("selenium")

    return ok


def check_cross_page_dedup_cross_run_diff(ok: bool) -> bool:
    """cross-page dedup + cross-run diff"""
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

    return ok


def check_run_metadata_partial_runs_must_not_read_as_delis(ok: bool) -> bool:
    """run metadata: partial runs must not read as delistings"""
    # ---- run metadata: partial runs must not read as delistings -----------
    # A run cut short on page 3 of 10 is missing every product on pages
    # 4-10. Diffed against yesterday's full run, all of them came back as
    # `removed` — indistinguishable from "these products were delisted".
    # finish_run writes a sidecar recording that, and diff_runs refuses.
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

        # --- the 2026-09-11 audit's second P0 ----------------------------
        # A run that never GOT the page used to report EXIT_NO_PRODUCTS, so
        # a dead proxy, a network flap and a genuinely empty category were
        # one value to an automated caller — three situations wanting three
        # different responses (retry this exit / change exit / accept the
        # answer). Measured on the day of the audit: the .env proxy was dead
        # (curl: "Proxy CONNECT aborted" against any host), and the run
        # reported exit 4, "no products".
        for _reason in FETCH_FAILURE_STOP_REASONS:
            _f = os.path.join(tmp, f"fetchfail_{_reason}")
            rc_f = finish_run([], _f, "json", False, blocked=False,
                              stop_reason=_reason, pages_requested=1,
                              pages_completed=0, start_url="u", final_url="u")
            ok &= check(f"finish_run: '{_reason}' with nothing gathered exits "
                        f"EXIT_FETCH_FAILED (5), not EXIT_NO_PRODUCTS (4) — "
                        f"nothing can be concluded about the catalogue",
                        rc_f == EXIT_FETCH_FAILED)

        _e = os.path.join(tmp, "genuinely_empty")
        rc_e = finish_run([], _e, "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, start_url="u", final_url="u")
        ok &= check("finish_run: a page that WAS fetched and held nothing is "
                    "still EXIT_NO_PRODUCTS — widening 5 must not swallow "
                    "the one case 4 is actually for",
                    rc_e == EXIT_NO_PRODUCTS)

        # A fetch failure that still gathered pages is a PARTIAL run, not a
        # fetch failure: the output is written and exit 6 already says so.
        # This is the ordering inside finish_run, pinned.
        _fp = os.path.join(tmp, "partial_not_fetchfail")
        rc_fp = finish_run(_two, _fp, "json", False, blocked=False,
                           stop_reason="page_load_timeout", pages_requested=10,
                           pages_completed=2, start_url="u", final_url="u")
        ok &= check("finish_run: a timeout that still gathered products stays "
                    "EXIT_PARTIAL — exit 5 means 'we have nothing', and two "
                    "good pages is not nothing",
                    rc_fp == EXIT_PARTIAL)


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

    return ok


def check_run_metadata_id_timings_quality(ok: bool) -> bool:
    """run metadata: id, timings, quality"""
    # --- run metadata: id, timings, quality ------------------------------
    _rows = [Product(sku="a", price=1.0, currency="USD", title="t",
                     brand="b", image_url="i", price_source="jsonld+dom"),
             Product(sku="b", price=None, currency=None, title=None)]
    _q = quality_metrics(_rows)
    ok &= check("quality metrics are FRACTIONS, not counts — a count has to "
                "be read against the row total to mean anything, and a "
                "fraction is what a threshold compares against",
                _q["rows"] == 2 and _q["priced"] == 0.5
                and _q["dom_confirmed_price"] == 0.5
                and _q["with_currency"] == 0.5)
    ok &= check("...and an empty result reports rows=0 rather than dividing "
                "by it",
                quality_metrics([]) == {"rows": 0})
    ok &= check("two runs get two different run ids — two runs of the same "
                "command ARE different runs, which is the thing being "
                "identified",
                new_run_id() != new_run_id() and len(new_run_id()) == 12)

    return ok


def check_the_webhook(ok: bool) -> bool:
    """the webhook"""
    # --- the webhook ------------------------------------------------------
    # Three properties, each because the obvious version gets it wrong.
    import notify as _nf

    _hook = "https://hooks.slack.com/services/T00000000/B00000000/SECRETTOKEN"
    ok &= check("a webhook URL is described by scheme and host only — most "
                "carry their token in the PATH, so logging the URL publishes "
                "the credential",
                "SECRETTOKEN" not in _nf.describe(_hook)
                and "hooks.slack.com" in _nf.describe(_hook))

    class _Boom:
        def post(self, *a, **k):
            # requests puts the FULL url, query string included, into the text
            # of its connection errors. This is that, exactly.
            raise RuntimeError(f"Failed to establish a new connection: {_hook}")

    class _Rejects:
        status_code = 500

        def post(self, *a, **k):
            return self

    class _Accepts:
        status_code = 204
        sent = None

        def post(self, url, data=None, headers=None, timeout=None):
            _Accepts.sent = (url, json.loads(data.decode()), timeout)
            return self

    _real_import = builtins.__import__

    def _with_requests(stub):
        def _imp(name, *a, **k):
            if name == "requests":
                return stub
            return _real_import(name, *a, **k)
        return _imp

    for _stub, _expect, _label in (
            (_Boom(), False, "an unreachable endpoint"),
            (_Rejects(), False, "an endpoint answering HTTP 500"),
            (_Accepts(), True, "a working endpoint")):
        _buf = io.StringIO()
        _h = logging.StreamHandler(_buf)
        _nf.logger.addHandler(_h)
        builtins.__import__ = _with_requests(_stub)
        try:
            _delivered = _nf.send(_hook, {"status": "failed"}, 3)
        finally:
            builtins.__import__ = _real_import
            _nf.logger.removeHandler(_h)
        _logged = _buf.getvalue()
        ok &= check(f"webhook: {_label} never raises, and never puts the URL "
                    f"in the log",
                    _delivered is _expect and "SECRETTOKEN" not in _logged)

    ok &= check("webhook: the payload carries the exit code alongside the "
                "metadata — that is the field an alerting rule branches on, "
                "and it is not otherwise in the sidecar",
                _Accepts.sent is not None
                and _Accepts.sent[1]["exit_code"] == 3
                and _Accepts.sent[1]["status"] == "failed")
    ok &= check("webhook: the POST is bounded, so a hung endpoint cannot hold "
                "the process open after the data is on disk",
                _Accepts.sent[2] == _nf.TIMEOUT_S)
    ok &= check("webhook: no URL means no attempt at all",
                _nf.send(None, {}, 0) is False)

    # It fires on FAILURE too, which is the main use: finish_run deliberately
    # writes no sidecar for a run that gathered nothing, so a webhook keyed on
    # the sidecar would be silent for exactly the runs worth hearing about.
    _fired = {}

    def _capture(url, meta, rc):
        _fired["meta"], _fired["rc"] = meta, rc
        return True

    _real_send = _nf.send
    _nf.send = _capture
    try:
        with tempfile.TemporaryDirectory() as _t:
            _rc = finish_run([], os.path.join(_t, "n"), "json", False,
                             blocked=True, stop_reason="blocked_akamai",
                             pages_requested=1, pages_completed=0,
                             start_url="u", final_url="u",
                             webhook="https://example.invalid/hook")
    finally:
        _nf.send = _real_send
    ok &= check("webhook fires for a run that wrote NOTHING — the sidecar is "
                "deliberately absent there, so a webhook keyed on the sidecar "
                "would miss every run worth an alert",
                _fired.get("rc") == EXIT_BLOCKED
                and _fired["meta"]["status"] == "failed"
                and _fired["meta"]["stop_reason"] == "blocked_akamai")

    ok &= check("EXIT_FETCH_FAILED (5) is distinct from every other code in "
                "the contract",
                EXIT_FETCH_FAILED == 5
                and EXIT_FETCH_FAILED not in (0, 1, 2, EXIT_BLOCKED,
                                              EXIT_NO_PRODUCTS, EXIT_PARTIAL))
    ok &= check("scraper_api_client's EXIT_API_ERROR is the SAME code, not a "
                "second 5 that can drift — one meaning per exit code across "
                "the family",
                _sac.EXIT_API_ERROR == EXIT_FETCH_FAILED)
    ok &= check("no fetch-failure stop reason is also a COMPLETE one — a run "
                "cannot both have failed to fetch and have seen everything",
                not set(FETCH_FAILURE_STOP_REASONS) & set(COMPLETE_STOP_REASONS))

    # stop_reason_for is the one place that names why a page yielded nothing.
    # Ordered by how much each signal PROVES (CLAUDE.md §17's
    # classification-order trap), so the specific reason outranks the general.
    ok &= check("stop_reason_for: a named vendor outranks a bare status — "
                "'blocked_akamai' says more than 'http_error'",
                stop_reason_for(load_failed=False, blocked_by="akamai",
                                http_status=403) == "blocked_akamai")
    ok &= check("stop_reason_for: a dead exit is reported as such, not as a "
                "timeout — they want opposite responses",
                stop_reason_for(load_failed=True, blocked_by=None,
                                proxy_failure="ERR_PROXY_CONNECTION_FAILED")
                == "proxy_unusable")
    ok &= check("stop_reason_for: an error status with no recognised marker "
                "is still not the listing",
                stop_reason_for(load_failed=False, blocked_by=None,
                                http_status=503) == "http_error")
    ok &= check("stop_reason_for: a plain timeout stays a timeout",
                stop_reason_for(load_failed=True, blocked_by=None) ==
                "page_load_timeout")
    # PARSER DRIFT, the last piece of the audit's P0 item 3. A page that
    # LINKS to eighteen products and parses to zero is this repo's bug, not an
    # empty category, and the two send a reader to opposite places. The
    # scenario is real: the CSS fallback drops a product link whose tile
    # yields no price text, so a scoping failure turns a full page into no
    # rows — the "junk-link data theft" shape seen from the other side.
    from product_parser import count_product_links

    _full_unparseable = "<html><body>" + "".join(
        f'<a href="/shopping/kids/b-item-{1000 + i}.aspx">Item {i}</a>'
        for i in range(8)) + "</body></html>"
    _genuinely_empty = '<html><body><div class="grid"></div></body></html>'
    _parses_fine = "<html><body>" + "".join(
        f'<a href="/shopping/kids/b-item-{2000 + i}.aspx">'
        f'<span>Brand</span><span>Item {i}</span><span>${10 + i}</span></a>'
        for i in range(8)) + "</body></html>"

    ok &= check("count_product_links counts DISTINCT products by id, not "
                "anchors — a tile links to its product twice, so counting "
                "anchors makes a threshold mean half what it says",
                count_product_links(
                    '<a href="/x-item-1.aspx"><img></a>'
                    '<a href="/x-item-1.aspx">t</a>'
                    '<a href="/y-item-2.aspx">o</a>') == 2)
    ok &= check("parse drift is a REAL case, not a hypothetical: a page full "
                "of product links with no parseable price yields 0 rows, "
                "because the fallback drops a tile it can find no price in",
                count_product_links(_full_unparseable) == 8
                and parse_products(_full_unparseable, "https://x/") == [])
    ok &= check("...and it is distinguishable: an empty category has no "
                "product links at all",
                count_product_links(_genuinely_empty) == 0)
    ok &= check("...and the signal is not always on — a page that parses "
                "fine has links AND rows",
                count_product_links(_parses_fine) == 8
                and len(parse_products(_parses_fine, "https://x/")) == 8)
    ok &= check("stop_reason_for names it, and ranks it LAST: everything "
                "above says the page never arrived, this one says it arrived "
                "and we failed to read it",
                stop_reason_for(load_failed=False, blocked_by=None,
                                parse_drift=True) == "parse_drift"
                and stop_reason_for(load_failed=True, blocked_by=None,
                                    parse_drift=True) == "page_load_timeout")
    ok &= check("parse_drift is NOT a fetch failure — the catalogue question "
                "really was answered, so the exit code stays 4; what changes "
                "is that the sidecar names it as OUR bug",
                "parse_drift" not in FETCH_FAILURE_STOP_REASONS
                and "parse_drift" not in COMPLETE_STOP_REASONS)

    ok &= check("stop_reason_for: a 200 that loaded fine is 'completed' — the "
                "helper must not invent a failure",
                stop_reason_for(load_failed=False, blocked_by=None,
                                http_status=200) == "completed")
    ok &= check("stop_reason_for is keyword-only, so adding a signal later "
                "cannot silently re-bind an existing caller's argument",
                _kwonly_only(stop_reason_for))

    return ok


def check_json_ld_shapes_that_are_legal_but_were_not_handl(ok: bool) -> bool:
    """JSON-LD shapes that are legal but were not handled"""
    # ---- JSON-LD shapes that are legal but were not handled ----------------
    # All of these are valid schema.org and all were reproduced against the
    # old parser: the first two CRASHED the run (a null and an ImageObject),
    # the third returned zero products silently. A crash over a decorative
    # field, or an "empty category" that is really an unread format, is worse
    # than a missing image.
    def _ld(node_extra, sku):
        node = {"@type": "Product", "name": "x",
                "offers": {"price": 10, "priceCurrency": "EUR",
                           "url": f"/shopping/kids/a-item-{sku}.aspx"}}
        node.update(node_extra)
        return ('<html><body><script type="application/ld+json">'
                + json.dumps({"@type": "ItemList", "itemListElement": [node]})
                + "</script></body></html>")

    _LDU = "https://www.farfetch.com/shopping/kids/items.aspx"
    ok &= check("JSON-LD with an explicit \"offers\": null parses instead of "
                "raising — a default only applies to an ABSENT key, and nulls "
                "occur in the wild",
                len(parse_products(_ld({"offers": None}, "90000001"), _LDU)) == 1)
    ok &= check("an empty offers list is treated as no offer, not an IndexError",
                len(parse_products(_ld({"offers": []}, "90000002"), _LDU)) == 1)
    ok &= check("image as an ImageObject yields its url (it used to raise "
                "KeyError and kill the run over a decorative field)",
                parse_products(_ld({"image": {"@type": "ImageObject",
                                              "url": "http://i/1.jpg"}},
                                   "90000003"), _LDU)[0].image_url
                == "http://i/1.jpg")
    ok &= check("image as a LIST of ImageObjects yields the first usable url, "
                "including via contentUrl",
                parse_products(_ld({"image": [{"@type": "ImageObject",
                                               "contentUrl": "http://i/2.jpg"}]},
                                   "90000004"), _LDU)[0].image_url
                == "http://i/2.jpg")
    ok &= check("image: null yields None rather than raising",
                parse_products(_ld({"image": None}, "90000005"),
                               _LDU)[0].image_url is None)
    ok &= check("an explicit \"aggregateRating\": null parses",
                len(parse_products(_ld({"aggregateRating": None}, "90000006"),
                                   _LDU)) == 1)
    _GRAPH = ('<html><body><script type="application/ld+json">'
              + json.dumps({"@context": "https://schema.org", "@graph": [
                  {"@type": "WebPage", "name": "not a product"},
                  {"@type": "Product", "name": "in graph",
                   "offers": {"price": 10, "priceCurrency": "EUR",
                              "url": "/shopping/kids/a-item-90000007.aspx"}}]})
              + "</script></body></html>")
    ok &= check("products inside an @graph block are found — the other standard "
                "way schema.org is published; missing them reported an EMPTY "
                "category for what was really an unread format",
                [p.sku for p in parse_products(_GRAPH, _LDU)] == ["90000007"])

    return ok


def check_proxy_credentials_must_not_reach_a_browser_comma(ok: bool) -> bool:
    """proxy credentials must not reach a browser command line"""
    # ---- proxy credentials must not reach a browser command line -----------
    # Chromium's --proxy-server becomes part of the browser process's argv,
    # readable by anything that can run `ps`. Playwright got this right via
    # its own username/password fields; the other two engines appended the
    # whole --proxy value, credentials included, and logged it verbatim.
    _SECRET_PROXY = "http://myuser:s3cr3t@gate.example.com:9999"
    for _engine in ("puppeteer_scraper.py", "selenium_scraper.py"):
        _esrc = open(_engine, encoding="utf-8").read()
        ok &= check(f"{_engine}: the proxy URL is no longer logged verbatim",
                    'logger.info("Using 2Captcha proxy: %s", args.proxy)' not in _esrc)
        ok &= check(f"{_engine}: --proxy-server is built from scheme/host/port "
                    f"only, so credentials cannot reach the browser's argv",
                    "--proxy-server={args.proxy}" not in _esrc
                    and "parsed.hostname" in _esrc or "proxy_parts.hostname" in _esrc)

    return ok


def check_proxy_pool_and_rotation(ok: bool) -> bool:
    """proxy pool and rotation"""
    # ---- proxy pool and rotation -------------------------------------------
    # `--proxy` was one static string applied once at launch: the shape of a
    # demo, not of the thing proxies are bought for. These pin the rules that
    # replaced it, offline — no browser, no network.
    import proxy_pool as _pp

    for _bad, _why in (("host:9999", "a bare host:port has no scheme"),
                       ("ftp://h:1", "ftp is not a proxy scheme"),
                       ("http://", "no host at all"),
                       ("socks5://u:p@h:1", "Chromium cannot authenticate SOCKS5, "
                                            "so credentials would be dropped")):
        try:
            _pp.parse_proxy_line(_bad)
            _raised = False
        except _pp.ProxyError:
            _raised = True
        ok &= check(f"proxy list rejects {_bad!r} — {_why}", _raised)

    ok &= check("proxy list skips blank lines and # comments rather than "
                "treating them as entries",
                _pp.parse_proxy_line("  ") is None
                and _pp.parse_proxy_line("# a comment") is None
                and _pp.parse_proxy_line("http://a:1") == "http://a:1")

    ok &= check("a proxy URL is logged with credentials masked but host and "
                "port intact — which exit was used is the point of the log, "
                "and is not the secret",
                _pp.mask("http://user:secret@gate.example.com:9999")
                == "http://***:***@gate.example.com:9999"
                and "secret" not in _pp.mask("http://user:secret@gate.example.com:9999"))

    # Credentials must go in Playwright's own fields, never in `server`:
    # `server` becomes a Chromium command-line switch, so a user:pass left
    # there lands in the browser process's argv for anything running `ps`.
    _pwx = _pp.to_playwright("http://user:secret@gate.example.com:9999")
    ok &= check("Playwright proxy dict keeps credentials out of `server`, "
                "which becomes a browser command-line argument",
                _pwx["server"] == "http://gate.example.com:9999"
                and "secret" not in _pwx["server"]
                and _pwx["username"] == "user" and _pwx["password"] == "secret")

    _pool = _pp.ProxyPool(["http://a:1", "http://b:2", "http://c:3"],
                          rotate="per-page")
    _seq = [_pool.current]
    for _i in range(4):
        _seq.append(_pool.advance(f"test {_i}"))
    ok &= check("rotation walks the pool in order and wraps around rather than "
                "exhausting — a 3-exit pool across 50 pages is legitimate",
                _seq == ["http://a:1", "http://b:2", "http://c:3",
                         "http://a:1", "http://b:2"]
                and _pool.rotations == 4)

    _single = _pp.ProxyPool(["http://only:1"])
    ok &= check("rotating a single-exit pool stays put and warns instead of "
                "pretending it moved",
                _single.advance("blocked") == "http://only:1"
                and _single.rotations == 0)

    ok &= check("per-run is the default rotation mode — a session that changes "
                "address mid-flight is more suspicious than one that does not",
                _pp.ProxyPool(["http://a:1"]).rotate == "per-run"
                and not _pp.ProxyPool(["http://a:1"]).rotates_per_page()
                and _pp.ProxyPool(["http://a:1"], rotate="per-page").rotates_per_page())

    with tempfile.TemporaryDirectory() as tmp:
        _pf = os.path.join(tmp, "proxies.txt")
        with open(_pf, "w", encoding="utf-8") as f:
            f.write("# exits\nhttp://a:1\n\n  http://b:2  \n")
        ok &= check("a proxy file loads its entries, ignoring comments, blanks "
                    "and surrounding whitespace",
                    _pp.load_proxy_file(_pf) == ["http://a:1", "http://b:2"])

        class _PArgs:
            proxy = "http://single:1"
            proxy_file = _pf
            proxy_rotate = "per-page"
            proxy_shuffle = False
        _from = _pp.from_args(_PArgs())
        ok &= check("--proxy-file wins over --proxy when both are given, rather "
                    "than silently picking one",
                    len(_from) == 2 and _from.current == "http://a:1")

        _bad = os.path.join(tmp, "empty.txt")
        with open(_bad, "w", encoding="utf-8") as f:
            f.write("# only a comment\n\n")
        try:
            _pp.load_proxy_file(_bad)
            _raised = False
        except _pp.ProxyError:
            _raised = True
        ok &= check("a proxy file with no usable entries is an error, not an "
                    "empty pool that fails later at connect time", _raised)

    class _NoProxyArgs:
        proxy = None
        proxy_file = None
        proxy_rotate = "per-run"
    ok &= check("no --proxy and no --proxy-file means no pool at all (the "
                "unchanged default path)",
                _pp.from_args(_NoProxyArgs()) is None)

    # The engine must relaunch the browser on rotation rather than swapping the
    # proxy under a live session: cookies issued against one exit, replayed
    # from another, are a stronger signal than either address alone.
    _psrc = open("playwright_scraper.py", encoding="utf-8").read()
    ok &= check("playwright relaunches the browser when rotating exits, so the "
                "session does not follow the IP around",
                "session.relaunch()" in _psrc
                and "self.browser.close()" in _psrc)
    ok &= check("a blocked page is retried from a DIFFERENT exit — retrying the "
                "same address only confirms the block",
                'pool.advance(f"blocked by {vendor} on page {page_num}")' in _psrc)

    # A dead proxy raises PWError (net::ERR_PROXY_CONNECTION_FAILED), NOT
    # PWTimeout. Catching only the latter let it escape as a traceback —
    # observed live against an unreachable exit, which is the likeliest
    # failure the first time anyone points --proxy-file at a real list. And a
    # proxy-level failure wants a DIFFERENT exit, not a retry of the same one.
    if _ps is not None:
        ok &= check("a Chromium proxy failure is recognised as such, so it "
                    "rotates to another exit instead of spending the retry "
                    "budget on a proxy that will not answer",
                    _ps._proxy_failure(Exception(
                        "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://x/"))
                    == "ERR_PROXY_CONNECTION_FAILED"
                    and _ps._proxy_failure(Exception(
                        "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x/"))
                    == "ERR_TUNNEL_CONNECTION_FAILED")
        ok &= check("an ordinary timeout is NOT mistaken for a proxy failure — "
                    "it deserves a retry from the same exit",
                    _ps._proxy_failure(Exception("Timeout 60000ms exceeded")) == ""
                    and _ps._proxy_failure(Exception("net::ERR_NAME_NOT_RESOLVED")) == "")

    return ok


def check_product_detail_pages(ok: bool) -> bool:
    """product detail pages: one row per size"""
    from product_detail_parser import (parse_product_detail, composition,
                                       labelled_blocks, discount_pct)

    full = parse_product_detail(SAMPLE_DETAIL_FULL_PRICE_HTML, "https://x/")
    sale = parse_product_detail(SAMPLE_DETAIL_SALE_HTML, "https://x/")

    # THE finding this parser exists for. A detail page publishes
    # ProductGroup where a listing publishes ItemList/Product, so the listing
    # parser returns zero here — silently, which is the dangerous part.
    listing_on_detail = parse_products(SAMPLE_DETAIL_FULL_PRICE_HTML,
                                       "https://x/")
    ok &= check("the LISTING parser finds nothing on a detail page — it reads "
                "ItemList/Product and a detail page publishes ProductGroup, "
                "which is why this is a second parser and not a flag",
                listing_on_detail == [])
    ok &= check("...and the detail parser does find it",
                len(full) == 2 and len(sale) == 2)

    ok &= check("one row per SIZE, keyed on the variant sku — the id that is "
                "actually unique; product_id groups them",
                [r.sku for r in full] == ["36899289-19", "36899289-21"]
                and {r.product_id for r in full} == {"36899289"})
    ok &= check("sizes are read as the site states them, localised and in "
                "more than one convention on a single locale",
                [r.size for r in full] == ["4 Jahre", "6 Jahre"]
                and [r.size for r in sale] == ["3-6 M.", "6-9 M."])

    # Values, not coverage: a column can be 100% populated and wrong.
    r = full[0]
    ok &= check("title, brand and colour come off the group, not the variant",
                r.title == "Baumwoll-T-Shirt mit Ami de Coeur"
                and r.brand == "AMI Paris" and r.color == "Weiß")
    ok &= check("the category is the breadcrumb PATH — the names are nested "
                "under `item`, and reading element['name'] gives None on "
                "every entry, which looks like 'no breadcrumbs'",
                r.category == "Kids > AMI Paris > Kleidung für Jungen > "
                              "Klassisches T-Shirt")
    ok &= check("the row's url is the VARIANT's offer url, which carries the "
                "size — not the product url shared by every size",
                r.url.endswith("size=19") and full[1].url.endswith("size=21"))

    # The price chain, which is the reason no DOM overlay is ported here.
    s = sale[0]
    ok &= check("a discounted variant reads price AND original price as "
                "facts: the spec without a priceType is what is paid, the "
                "StrikethroughPrice one is what it was",
                s.price == 45.0 and s.original_price == 90.0
                and s.currency == "EUR")
    ok &= check("...and the discount is arithmetic from those two",
                s.discount_pct == 50.0)
    ok &= check("a FULL-PRICE variant has no original_price and no discount — "
                "None, not 0, which would read as 'measured, and it is zero'",
                r.original_price is None and r.discount_pct is None)
    ok &= check("discount_pct refuses a negative: an 'original' at or below "
                "the price means the two figures are not what they were "
                "taken for",
                discount_pct(100.0, 90.0) is None
                and discount_pct(100.0, 100.0) is None
                and discount_pct(90.0, 100.0) == 10.0)

    ok &= check("price_source says the figure came from the VARIANT's own "
                "chain, so diff_runs cannot compare a detail row with a "
                "listing row as though they were alike",
                {x.price_source for x in full + sale} == {"jsonld-variant"})

    # Composition is a labelled DOM block, and the label is localised while
    # the classes are build hashes.
    ok &= check("composition is read from the labelled block, not guessed out "
                "of the JSON-LD description blurb",
                r.composition == "Bio-Baumwolle 100%"
                and sale[0].composition == "Baumwolle 100%")
    # Checked over the parser's STRING LITERALS, docstrings excluded. The
    # docstring names this class precisely to say "do not anchor on it", and
    # a line-based grep flags that as a violation of the rule it states. What
    # matters is whether a class name is ever used to MATCH something.
    _pdp_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "product_detail_parser.py"),
                    encoding="utf-8").read()
    _pdp_tree = _ast.parse(_pdp_src)
    _docstrings = set()
    for _n in _ast.walk(_pdp_tree):
        if isinstance(_n, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                           _ast.ClassDef)):
            _d = _ast.get_docstring(_n, clean=False)
            if _d:
                _docstrings.add(_d)
    _literals = [n.value for n in _ast.walk(_pdp_tree)
                 if isinstance(n, _ast.Constant) and isinstance(n.value, str)
                 and n.value not in _docstrings]
    ok &= check("...by its heading text, so a renamed build-hash class cannot "
                "break it — the fixture carries the real class names and no "
                "string the parser MATCHES on contains one",
                "ltr-2pfgen" in SAMPLE_DETAIL_FULL_PRICE_HTML
                and not any("ltr-" in lit for lit in _literals))
    ok &= check("an unlabelled page leaves composition empty rather than "
                "picking up the wrong block",
                composition(BeautifulSoup(
                    "<html><h4>Versand</h4><p>3 Tage</p></html>",
                    "html.parser")) is None)
    ok &= check("labelled_blocks returns every label->value pair, so the next "
                "field somebody wants is already there",
                labelled_blocks(BeautifulSoup(
                    "<html><h4>Versand</h4><p>3 Tage</p></html>",
                    "html.parser")) == {"versand": "3 Tage"})

    # --- the same product on a second market -------------------------
    us = parse_product_detail(SAMPLE_DETAIL_US_HTML, "https://x/")
    ok &= check("the variant SKU is identical across markets — which is what "
                "a cross-market comparison can join on",
                [x.sku for x in us] == [x.sku for x in full]
                == ["36899289-19", "36899289-21"])
    ok &= check("...while the size LABEL is translated, so it is display text "
                "and not a key: the DE '4 Jahre' is the US '4 yrs'",
                [x.size for x in full] == ["4 Jahre", "6 Jahre"]
                and [x.size for x in us] == ["4 yrs", "6 yrs"])
    ok &= check("title, colour, composition and category are all localised "
                "too — none of them identifies a product across markets",
                us[0].title != r.title and us[0].color != r.color
                and us[0].composition != r.composition
                and us[0].category != r.category)
    ok &= check("...and the composition label differs per locale, which is "
                "why it is matched from a SET of headings rather than one "
                "string ('Zusammensetzung' vs 'Composition')",
                r.composition == "Bio-Baumwolle 100%"
                and us[0].composition == "Organic Cotton 100%")
    ok &= check("prices are set per market, not converted — the same product "
                "is 60 EUR and 90 USD, so a cross-market difference is "
                "pricing rather than arbitrage",
                r.price == 60.0 and r.currency == "EUR"
                and us[0].price == 90.0 and us[0].currency == "USD")

    ok &= check("images: the group's full set on every row, primary first",
                r.image_url.endswith("36899289_69172521_1000.jpg?ov=true")
                and len(r.image_urls.split(" | ")) == 2)

    # The JSON-LD shapes that are legal and have broken a naive parser here
    # before. Each is the real failure, not a hypothetical.
    ok &= check("a page with no ProductGroup returns [] and says so, rather "
                "than raising — but the CALLER must treat that as a failure",
                parse_product_detail("<html><body>nothing</body></html>",
                                     "https://x/") == [])
    ok &= check("'offers': null is handled — an explicit null is not a "
                "missing key, so a .get() default never applies to it",
                parse_product_detail(
                    '<html><script type="application/ld+json">'
                    '{"@type":"ProductGroup","name":"n","productGroupID":"1",'
                    '"hasVariant":[{"@type":"Product","sku":"1-1",'
                    '"size":"S","offers":null}]}</script></html>',
                    "https://x/")[0].price is None)
    ok &= check("a ProductGroup nested in @graph is found, not reported as an "
                "empty product",
                len(parse_product_detail(
                    '<html><script type="application/ld+json">'
                    '{"@graph":[{"@type":"ProductGroup","name":"n",'
                    '"productGroupID":"1","hasVariant":[{"@type":"Product",'
                    '"sku":"1-1","size":"S"}]}]}</script></html>',
                    "https://x/")) == 1)
    ok &= check("a product with NO size axis still emits a row rather than "
                "being dropped",
                len(parse_product_detail(
                    '<html><script type="application/ld+json">'
                    '{"@type":"ProductGroup","name":"n","productGroupID":"7"}'
                    '</script></html>', "https://x/")) == 1)
    ok &= check("one unparseable ld+json block does not cost the other one",
                len(parse_product_detail(
                    '<html><script type="application/ld+json">{oh no</script>'
                    '<script type="application/ld+json">'
                    '{"@type":"ProductGroup","name":"n","productGroupID":"1",'
                    '"hasVariant":[{"@type":"Product","sku":"1-1"}]}'
                    '</script></html>', "https://x/")) == 1)

    # KNOWN LIMITATION, pinned rather than half-guarded: all 38 variants
    # captured were InStock, so the negative case has never been seen on a
    # real page. The mapping is asserted on synthetic values so that a future
    # change to it is a decision.
    def _stock(value):
        return parse_product_detail(
            '<html><script type="application/ld+json">'
            '{"@type":"ProductGroup","name":"n","productGroupID":"1",'
            '"hasVariant":[{"@type":"Product","sku":"1-1","offers":'
            '{"availability":"https://schema.org/' + value + '"}}]}'
            '</script></html>', "https://x/")[0].in_stock

    ok &= check("in_stock: an unanticipated availability value reads as NOT "
                "available rather than silently as yes — allowlisted, not "
                "`!= OutOfStock`. NOTE: no real out-of-stock variant has been "
                "captured yet, so a False is less proven than a True",
                _stock("InStock") is True and _stock("OutOfStock") is False
                and _stock("SomethingNewSchemaOrgAdded") is False)

    # Columns that do NOT exist, each because it was looked for and not found.
    fields = set(ProductVariant().__dict__)
    ok &= check("no rating/review_count column: 0 occurrences of "
                "aggregateRating or ratingValue across seven captured detail "
                "pages, so they would be null on every row of every run",
                not fields & {"rating", "review_count"})
    ok &= check("no merchant or shipping column either — absent on 7 of 7 "
                "pages; and no return-policy column, which IS present but "
                "identical on all 38 variants, making it a line in the README",
                not fields & {"merchant", "boutique", "seller",
                              "shipping", "delivery", "return_days"})
    ok &= check("ProductVariant keeps Product's shared prefix, in order, so "
                "one column name means one thing across the family",
                [f for f in fields if f in set(Product().__dict__)]
                and list(ProductVariant().__dict__)[:4]
                == list(Product().__dict__)[:4])

    # --- the crawl that drives the parser ------------------------------
    # The sidecar has to say which kind of row the file holds: the repo used
    # to have one kind, so the repo implied it, and it no longer does.
    ok &= check("run_meta records the mode, defaulting to listing",
                run_meta(status="complete", stop_reason="completed",
                         pages_requested=1, pages_completed=1, start_url="u",
                         final_url="u", products=1)["mode"] == "listing"
                and run_meta(status="complete", stop_reason="completed",
                             pages_requested=1, pages_completed=1,
                             start_url="u", final_url="u", products=1,
                             mode="detail")["mode"] == "detail")

    # An empty run's CSV header must describe what the file was FOR. With two
    # row kinds, defaulting to Product would give an empty detail run a
    # listing header — columns describing something it does not contain.
    with tempfile.TemporaryDirectory() as _t:
        _lp = os.path.join(_t, "l.csv")
        _dp = os.path.join(_t, "d.csv")
        write_csv([], _lp)
        write_csv([], _dp, row_type=ProductVariant)
        _lh = next(csv.reader(open(_lp, encoding="utf-8")))
        _dh = next(csv.reader(open(_dp, encoding="utf-8")))
        ok &= check("an empty CSV still carries a header, and it is the "
                    "header of the row kind that run was for",
                    "rating" in _lh and "size" not in _lh
                    and "size" in _dh and "rating" not in _dh)

    # The checkpoint stores rows; it has to rebuild them as the right class.
    class _A:
        def __init__(self, mode="listing"):
            self.url, self.pages, self.category = "https://x/", 5, "Kids"
            self.out, self.mode = "x", mode

    import run_state as _rs2
    with tempfile.TemporaryDirectory() as _t:
        _pfx = os.path.join(_t, "r")
        _a = _A(mode="detail")
        _cp = _rs2.Checkpoint(_pfx, _a, row_type=ProductVariant)
        _cp.record(1, [ProductVariant(sku="1-19", size="4 Jahre", price=45.0)])
        _back = _rs2.Checkpoint(_pfx, _A(mode="detail"),
                                row_type=ProductVariant)
        _back.resume()
        ok &= check("a detail checkpoint rebuilds ProductVariant rows — "
                    "hardcoding Product raises TypeError on the first "
                    "unexpected key, AFTER the run has announced it is "
                    "resuming",
                    len(_back.pages.get(1, [])) == 1
                    and _back.pages[1][0].size == "4 Jahre")
        _wrong = _rs2.Checkpoint(_pfx, _A(mode="listing"))
        _msgs = _wrong.resume()
        ok &= check("a LISTING run refuses a detail checkpoint: the mode is "
                    "part of the identity, because the two hold rows of "
                    "different shapes",
                    _wrong.resumed_from == []
                    and any("DIFFERENT run" in m for m in _msgs))

    # The engine wiring, at the source level: these are one-line decisions
    # whose only observable effect is in a file the suite cannot produce
    # without a browser. Read here rather than reusing the copy another
    # section happens to hold — a section that depends on a sibling's local
    # is the coupling the split just removed.
    _eng_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "playwright_scraper.py"),
                    encoding="utf-8").read()
    ok &= check("the engine offers --mode listing|detail and caps a crawl "
                "with --max-products",
                '"--mode", choices=["listing", "detail"]' in _eng_src
                and '"--max-products"' in _eng_src)
    ok &= check("...and tells finish_run which mode and which row type, so "
                "the sidecar and an empty CSV both describe what the run was "
                "for",
                "mode=args.mode, row_type=row_type" in _eng_src)
    ok &= check("a capped crawl does not report itself as complete",
                'stop_reason = "max_products_reached"' in _eng_src)
    ok &= check("...nor does one whose product pages failed — the listing "
                "pages all succeeding says nothing about the product pages",
                'stop_reason = "detail_pages_failed"' in _eng_src)
    ok &= check("neither reason is in COMPLETE_STOP_REASONS",
                "max_products_reached" not in COMPLETE_STOP_REASONS
                and "detail_pages_failed" not in COMPLETE_STOP_REASONS)

    import diff_runs as _dr2
    ok &= check("diff_runs refuses to compare a listing run with a detail "
                "run, and --force does not apply — every line of that diff "
                "would be an artefact of the comparison",
                "_check_same_mode" in open(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "diff_runs.py"), encoding="utf-8").read()
                and hasattr(_dr2, "_check_same_mode"))
    return ok


def check_engine_flag_parity(ok: bool) -> bool:
    """the three engines' flag sets, against each other"""
    # The README used to say "Same CLI, same parsing core, same output" with
    # no qualification, and that was false: measured, Playwright carries 12
    # flags the others do not. Nine of those predate this batch
    # (--concurrency, --dump-html, --fingerprint, --fp-*, --proxy-*) and
    # three came with detail mode. Nobody noticed because nothing compared
    # them.
    #
    # Asserted in BOTH directions, which is the half that is usually missed:
    # a NEW unshared flag fails, and so does CLOSING a difference that the
    # README documents. The second matters because the exception list is the
    # documentation — a flag quietly gaining parity would leave the README
    # describing a limitation that no longer exists.
    import ast as _a

    # argparse receivers only. Counting every `.add_argument` catches Chrome
    # switches too — `options.add_argument("--no-sandbox")` is the same method
    # name on a different object — and inventing nine Selenium-only CLI flags
    # that do not exist is exactly the kind of wrong number this check is for.
    _parsers = {"p", "parser", "ap"}

    def _flags(name):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                name), encoding="utf-8").read()
        found = set()
        for node in _a.walk(_a.parse(src)):
            if (isinstance(node, _a.Call)
                    and isinstance(node.func, _a.Attribute)
                    and node.func.attr == "add_argument"
                    and isinstance(node.func.value, _a.Name)
                    and node.func.value.id in _parsers):
                for arg in node.args:
                    if (isinstance(arg, _a.Constant)
                            and str(arg.value).startswith("--")):
                        found.add(arg.value)
        return found

    _sets = {name: _flags(name) for name in
             ("playwright_scraper.py", "selenium_scraper.py",
              "puppeteer_scraper.py")}
    _shared = set.intersection(*_sets.values())

    # The documented exceptions, and why each one is not a bug. A flag that
    # is not here and not shared IS a bug.
    ENGINE_SPECIFIC = {
        "playwright_scraper.py": {
            # Playwright-only by design: the sync API ties a browser to its
            # creating thread, so the worker model is not portable as-is.
            "--concurrency",
            # Written for the primary engine and not yet ported. Named here
            # so the gap is a decision rather than an accident.
            "--dump-html", "--fingerprint", "--fp-tags", "--fp-country",
            "--proxy-file", "--proxy-rotate", "--proxy-shuffle",
            "--proxy-block-retries",
            "--mode", "--max-products", "--resume",
        },
        "selenium_scraper.py": {
            # Driver plumbing that only Selenium has: it launches a separate
            # chromedriver process, and the other two do not.
            "--chromedriver", "--chrome-binary", "--disable-build-check",
            "--driver-timeout",
        },
        "puppeteer_scraper.py": set(),
    }

    ok &= check(f"the three engines share a common flag set ({len(_shared)} "
                f"flags) — the CLI contract the README describes",
                len(_shared) >= 15 and "--url" in _shared
                and "--pages" in _shared and "--out" in _shared
                and "--webhook" in _shared)

    _undocumented = {}
    _closed = {}
    for name, flags in _sets.items():
        unshared = flags - _shared
        _undocumented[name] = sorted(unshared - ENGINE_SPECIFIC[name])
        _closed[name] = sorted(ENGINE_SPECIFIC[name] - unshared)

    ok &= check(f"...and every flag that is NOT shared is a documented "
                f"exception (undocumented: "
                f"{ {k: v for k, v in _undocumented.items() if v} or 'none'})",
                not any(_undocumented.values()))
    ok &= check(f"...in both directions: a flag that gained parity must be "
                f"removed from the exception list, or the README keeps "
                f"describing a limitation that is gone (stale: "
                f"{ {k: v for k, v in _closed.items() if v} or 'none'})",
                not any(_closed.values()))

    # And the README must not claim a parity that does not exist. It said
    # "Same CLI, same parsing core, same output" flat out.
    _readme = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "README.md"), encoding="utf-8").read()
    ok &= check("the README does not claim an unqualified identical CLI, "
                "because three flags are Playwright-only",
                "Same CLI, same parsing core, same output. Pick by" not in _readme)
    ok &= check("...and it names the engine-specific flags, so a reader "
                "picking an engine learns what they give up",
                "--mode" in _readme and "Playwright only" in _readme)
    return ok


def check_the_suite_s_own_shape(ok: bool) -> bool:
    """the suite's own shape"""
    # main() was one 2,650-line function. The 2026-09-11 audit called that
    # out and it was right: a reader could not find a section without
    # scrolling, and a traceback only ever named `main`. It is now a preamble
    # plus one function per section.
    #
    # What is NOT done, and the reason is measured rather than preferred:
    # splitting into separate pytest modules. Doing so needs either a second
    # copy of these checks or the loss of `python3 smoke_test.py`, which runs
    # with no pytest installed — and pytest is not in requirements.txt, so a
    # fresh clone can verify the repo with nothing extra. tests/test_smoke.py
    # already turns every check below into its own pytest result.
    #
    # Guarded here because a 2,650-line function does not appear in one
    # commit; it accretes.
    _self = _ast.parse(open(__file__, encoding="utf-8").read())
    _fns = [n for n in _self.body if isinstance(n, _ast.FunctionDef)]
    _main = next((n for n in _fns if n.name == "main"), None)
    _sections = [n for n in _fns if n.name.startswith("check_")]
    _lines = {n.name: n.end_lineno - n.lineno for n in _fns}

    ok &= check("the suite is split into section functions, not one long "
                "main() — a 2,650-line function is what the audit found",
                len(_sections) >= 20)
    ok &= check(f"main() is a preamble plus calls, not the suite itself "
                f"({_lines.get('main')} lines)",
                _main is not None and _lines["main"] < 400)
    ok &= check("every section function is called from main() exactly once — "
                "a section nobody calls is a test suite quietly shrinking",
                all(open(__file__, encoding="utf-8").read().count(
                    f"ok = {n.name}(ok)") == 1 for n in _sections))

    # The merge that produced these functions once left a `return ok` in the
    # MIDDLE of a merged body, which made every check after it dead code: 308
    # of 315 silently stopped running, and only a count caught it. An early
    # return inside a section is the shape of that mistake.
    def _own_returns(fn):
        """Returns belonging to `fn` itself, not to anything nested in it.

        A section legitimately defines helper functions and classes, and
        their returns are theirs. Descending into them would flag every
        section that has one.
        """
        out = []
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                 _ast.ClassDef, _ast.Lambda)):
                continue
            if isinstance(node, _ast.Return):
                out.append(node)
            stack.extend(_ast.iter_child_nodes(node))
        return out

    _early = [n.name for n in _sections
              if any(r.lineno < n.end_lineno - 1 for r in _own_returns(n))]
    ok &= check(f"no section returns before its end, which is how a merge "
                f"turns the rest of a section into dead code "
                f"(offenders: {_early or 'none'})",
                not _early)
    return ok


def check_naming_and_dead_feature_guards(ok: bool) -> bool:
    """naming and dead-feature guards"""
    # ---- naming and dead-feature guards ----------------------------------
    # Not testing behaviour — testing claims. Three separate rounds of work went
    # into naming the products correctly and removing a flag that could not
    # work, and every one of those is a string an editor can reintroduce without
    # anything failing. So fail here instead.
    # Three holes were closed here on 2026-09-16, and the third is why the
    # first two survived:
    #
    #   * the list carried the two COMPOUND forms of the banned product
    #     name (the "2scraper ..." one and the "proprietary ..." one) but not
    #     the bare two-word phrase, which every sibling repo in this family
    #     bans. Eight occurrences of it shipped in the three engines through
    #     that gap, including in the --cdp-endpoint help text a user reads;
    #   * matching was case-sensitive, patched by listing one capitalised
    #     variant by hand beside its lowercase twin -- which works for
    #     exactly the casings someone thought of;
    #   * smoke_test.py was excluded from the scan WHOLESALE, so the file
    #     most likely to acquire a stray phrase by copy-paste was the one
    #     file nobody checked.
    #
    # All three are fixed the way woolworths-scraper does it: the phrases are
    # ASSEMBLED from pieces, so this file does not contain the literals it
    # forbids and can therefore be scanned like any other; matching is
    # case-insensitive; and the scan is anchored to this file's directory
    # with a floor on how many files it saw, because a check that can quietly
    # scan zero files is not a check.
    _ad = "anti" + "detect"
    BANNED = [
        (f"2scraper {_ad} browser", "a product that does not exist under that name"),
        (f"proprietary {_ad} browser", "the browser is the Scraping Browser API"),
        (f"{_ad} browser", "call it the Scraping Browser API"),
        (" ".join(["cloud", "browser"]), "call it the Scraping Browser API"),
        ("gate.2prx" + ".com", "not a real gateway host; 2prx.com is a synonym "
                               "of 2captcha.com/proxy"),
        (f"--{_ad}", "the flag was removed: its endpoint was a placeholder"),
        (f"{_ad.upper()}_LOCAL_API", "removed with the flag"),
    ]
    _root = os.path.dirname(os.path.abspath(__file__))
    SHIPPED = sorted(
        os.path.join(_root, n) for n in os.listdir(_root)
        if n.endswith((".py", ".sh", ".md", ".txt", ".html", ".yml", ".yaml",
                       ".toml", ".example")))
    ok &= check(f"the wording scan actually scanned something ({len(SHIPPED)} "
                f"files)", len(SHIPPED) > 20)
    offenders = []
    for f in SHIPPED:
        try:
            body = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        rel = os.path.relpath(f, _root)
        for n, line in enumerate(body.splitlines(), 1):
            low = line.lower()
            for phrase, why in BANNED:
                if phrase.lower() in low:
                    offenders.append(f"{rel}:{n} {phrase!r} - {why}")
    ok &= check("no shipped file names a product that does not exist, or the "
                f"removed --{_ad} flag"
                + ("" if not offenders else " -> " + "; ".join(offenders[:4])),
                not offenders)

    ok &= check("solve_recaptcha no longer takes use_antidetect",
                "use_antidetect" not in inspect.signature(_cs.solve_recaptcha).parameters)
    ok &= check("the placeholder local-API endpoint constant is gone",
                not hasattr(_cs, f"{_ad.upper()}_LOCAL_API"))

    # THE SHIPPED CI CHECKS RUN, AND PASS ON THIS REPO.
    #
    # `.github/ci_checks.py` was in this repo and invoked by NOTHING — not
    # CI, not this suite — while `tests.yml` carried an inline grep doing a
    # narrower version of the same job with its own allowlist. The inline one
    # matched only ws:// and wss://, so an `http://user:pass@` credential
    # would have sailed past CI; the shipped one, which does match http://,
    # meanwhile failed on this repo's own main because the documentation
    # placeholder in proxy_pool.py and the masking fixtures here were missing
    # from its allowlist.
    #
    # Two sources of truth, one dead and one with a hole. Running the shipped
    # one here as well means a failure shows up locally, before a push.
    _script = os.path.join(_repo_root, ".github", "ci_checks.py")
    ok &= check("ci_checks.py is present", os.path.exists(_script))
    if os.path.exists(_script):
        _proc = subprocess.run([sys.executable, _script, "--all"],
                               cwd=_repo_root, capture_output=True, text=True)
        ok &= check(f"ci_checks.py --all passes on this repo "
                    f"(exit {_proc.returncode})", _proc.returncode == 0)
        if _proc.returncode != 0:
            for _line in (_proc.stdout + _proc.stderr).strip().split("\n")[-12:]:
                print(f"        {_line}")
        _wf = open(os.path.join(_repo_root, ".github", "workflows",
                                "tests.yml"), encoding="utf-8").read()
        ok &= check("tests.yml runs the shipped check rather than an inline "
                    "copy",
                    "ci_checks.py --secret-check" in _wf
                    or "ci_checks.py --all" in _wf)
        ok &= check("...and carries no second, narrower inline credential "
                    "grep", "(ws|wss)://[^ " not in _wf)

    return ok


def check_canary_yml_the_exit_code_table_it_prints_must_be(ok: bool) -> bool:
    """canary.yml: the exit-code table it prints must be the engines'"""
    # --- canary.yml: the exit-code table it prints must be the engines' ----
    # The 2026-09-11 audit found the canary announcing "exit 124 — self-imposed
    # timeout" for a code no engine has ever returned, while having no entry
    # for 5 or 6 at all. Nothing executed that table, so it drifted silently
    # for months and a fetch failure was reported as an unknown code. It now
    # lives in a shipped script that imports the constants, and this pins both
    # halves: the table agrees with output_writer, and the workflow calls the
    # script instead of reimplementing a copy of it.
    _canary_script = os.path.join(_repo_root, ".github", "canary_check.py")
    ok &= check("canary_check.py is present", os.path.exists(_canary_script))
    if os.path.exists(_canary_script):
        sys.path.insert(0, os.path.join(_repo_root, ".github"))
        import canary_check as _cc
        from output_writer import EXIT_DRIVER_TIMEOUT

        _contract = {0, 1, 2, EXIT_BLOCKED, EXIT_NO_PRODUCTS,
                     EXIT_FETCH_FAILED, EXIT_PARTIAL, EXIT_DRIVER_TIMEOUT}
        ok &= check("the canary's exit-code table covers every code the "
                    "contract defines — no code can fall through to "
                    "'unexpected'",
                    _contract <= set(_cc.EXIT_MEANINGS))
        ok &= check("...and invents none: every key is a code some engine "
                    "really returns",
                    set(_cc.EXIT_MEANINGS) == _contract)
        ok &= check("124 is named, not a magic number — selenium_scraper uses "
                    "the shared constant, so the table cannot come to describe "
                    "something else (it used to say 'the page never became "
                    "ready'; it is chromedriver failing to START)",
                    EXIT_DRIVER_TIMEOUT == 124
                    and "_leave_now(124)" not in open(
                        os.path.join(_repo_root, "selenium_scraper.py"),
                        encoding="utf-8").read())
        # Output is swallowed while probing: check_exit_code PRINTS
        # "::error::..." lines, and this suite runs inside tests.yml, where
        # GitHub turns that prefix into a workflow annotation. A green run
        # that annotates itself with six errors is worse than no check.
        def _verdict(code, allow_block):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                return _cc.check_exit_code(code, allow_block=allow_block)

        ok &= check("a clean run is the only code the canary passes on when a "
                    "block is NOT excused",
                    _verdict(0, False) == 0
                    and all(_verdict(c, False) == 1
                            for c in (1, 2, EXIT_BLOCKED, EXIT_NO_PRODUCTS,
                                      EXIT_FETCH_FAILED, EXIT_PARTIAL)))
        ok &= check("--allow-block excuses a BLOCK and nothing else — a "
                    "datacentre refusal says nothing about the site, but 0 "
                    "products on a page that loaded still does",
                    _verdict(EXIT_BLOCKED, True) == 0
                    and all(_verdict(c, True) == 1
                            for c in (1, 2, EXIT_NO_PRODUCTS,
                                      EXIT_FETCH_FAILED, EXIT_PARTIAL)))

        _cwf = open(os.path.join(_repo_root, ".github", "workflows",
                                 "canary.yml"), encoding="utf-8").read()
        ok &= check("canary.yml calls the shipped script rather than carrying "
                    "its own copy of the table",
                    "canary_check.py --exit-code" in _cwf
                    and "case \"$code\" in" not in _cwf)
        # Comment lines are excluded on purpose: the header explains what the
        # old commented-out --proxy line was and why it is gone, and prose
        # about a flag cannot leak a credential. Only an executable line can.
        _cwf_code = [ln for ln in _cwf.splitlines()
                     if not ln.lstrip().startswith("#")]
        ok &= check("the canary never puts the proxy credential in argv — it "
                    "goes through the environment env_config.py already "
                    "reads (CLAUDE.md §3, §11)",
                    not any("--proxy" in ln for ln in _cwf_code)
                    and "FARFETCH_PROXY: ${{ secrets.FARFETCH_PROXY }}" in _cwf)
        ok &= check("the proxy-backed job SKIPS rather than fails when the "
                    "secret is absent — a check that is red every morning is "
                    "a check nobody reads",
                    "HAVE_PROXY: ${{ secrets.FARFETCH_PROXY != '' }}" in _cwf
                    and "::notice::Skipped" in _cwf)
        ok &= check("BOTH canary jobs exercise pagination (--pages 3): with "
                    "one page a dead next-link stays invisible, which is how "
                    "the silent-single-page bug survived once already",
                    sum(1 for ln in _cwf_code if "--pages 3" in ln) == 2)

    return ok


def check_numeric_flags_are_range_checked_in_every_cli(ok: bool) -> bool:
    """numeric flags are range-checked, in every CLI"""
    # --- numeric flags are range-checked, in every CLI ---------------------
    # `--retries 0` was accepted by all three browser engines, and the attempt
    # loop is `range(1, retries + 1)` — so zero attempts means page.goto() is
    # never called. The run parsed about:blank (39 bytes, measured 2026-09-14
    # against a page that returns 559 with --retries 1) and exited 4, "the
    # page was fetched and held nothing". A wrong answer reached by typing a
    # number, which is the bug class this repo cares about most.
    #
    # The validators live in arg_types.py and are attached as argparse
    # `type=` callables, so argparse produces the usage error and exit 2
    # itself, before a browser is launched. The risk that then remains is
    # DRIFT — a numeric flag added later with a bare `type=int`. This walks
    # every CLI's AST and closes it.
    import arg_types as _at

    _clis = ["playwright_scraper.py", "selenium_scraper.py",
             "puppeteer_scraper.py", "scraper_api_client.py",
             "diff_runs.py", "fingerprint_client.py"]
    _unguarded = []
    _guarded = 0
    for _name in _clis:
        _tree = _ast.parse(open(os.path.join(_repo_root, _name),
                                encoding="utf-8").read())
        for _node in _ast.walk(_tree):
            if not (isinstance(_node, _ast.Call)
                    and isinstance(_node.func, _ast.Attribute)
                    and _node.func.attr == "add_argument"):
                continue
            _flag = next((a.value for a in _node.args
                          if isinstance(a, _ast.Constant)
                          and isinstance(a.value, str)
                          and a.value.startswith("--")), None)
            _type = next((k.value for k in _node.keywords if k.arg == "type"),
                         None)
            if _flag is None or _type is None:
                continue
            # A bare `int`/`float` is the shape being hunted.
            if isinstance(_type, _ast.Name) and _type.id in ("int", "float"):
                if _flag not in _at.UNVALIDATED_OK:
                    _unguarded.append(f"{_name} {_flag}")
            elif (isinstance(_type, _ast.Name) and _type.id in _at.VALIDATORS):
                _guarded += 1
            elif (isinstance(_type, _ast.Call)
                  and isinstance(_type.func, _ast.Name)
                  and _type.func.id == "bounded_int"):
                _guarded += 1

    ok &= check(f"every numeric CLI flag is range-checked ({_guarded} guarded; "
                f"unguarded: {_unguarded or 'none'}) — a bare type=int is how "
                f"--retries 0 came to mean 'never fetch the page'",
                not _unguarded)
    ok &= check("...and at least one flag in each browser engine is actually "
                "guarded, so the walk above is finding call sites rather than "
                "quietly matching nothing",
                _guarded >= 15)

    # Zero is allowed exactly where it names a real behaviour and refused
    # where it names none. Pinned in both directions: a validator that
    # rejects everything would pass a one-sided check.
    for _fn, _good, _bad in (
            (_at.positive_int, ("1", "50"), ("0", "-1", "x")),
            (_at.nonneg_int, ("0", "3"), ("-1", "x")),
            (_at.nonneg_float, ("0", "0.0", "2.5"), ("-0.1", "x")),
            (_at.bounded_int(1, 120), ("1", "120", "60"), ("0", "121", "x"))):
        _name = getattr(_fn, "__name__", "?")
        _ok_good = all(_fn(v) is not None for v in _good)
        _ok_bad = True
        for v in _bad:
            try:
                _fn(v)
                _ok_bad = False
            except argparse.ArgumentTypeError:
                pass
        ok &= check(f"{_name}: accepts {_good} and refuses {_bad}",
                    _ok_good and _ok_bad)

    return ok


def check_the_dockerfile_s_copy_list_vs_the_entrypoint_s_i(ok: bool) -> bool:
    """the Dockerfile's COPY list vs the entrypoint's import graph"""
    # --- the Dockerfile's COPY list vs the entrypoint's import graph -------
    # An explicit COPY list is right — the image should carry no test suite
    # and no stray .env — but it falls behind, and CI is the only thing that
    # builds the image. Every repo in this family has shipped an image that
    # died with ModuleNotFoundError on every invocation, --help included,
    # because one module was missing from that list (CLAUDE.md §10). This
    # check needs no Docker, and it is what catches a module added today.
    _dockerfile = open(os.path.join(_repo_root, "Dockerfile"),
                       encoding="utf-8").read()
    _copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*\.py)", _dockerfile))

    # Walk the entrypoint's imports transitively, keeping only modules that
    # are files in this repo.
    _local = {f[:-3] for f in os.listdir(_repo_root) if f.endswith(".py")}
    _needed, _queue = set(), ["playwright_scraper"]
    while _queue:
        _mod = _queue.pop()
        if _mod in _needed or _mod not in _local:
            continue
        _needed.add(_mod)
        _t = _ast.parse(open(os.path.join(_repo_root, _mod + ".py"),
                             encoding="utf-8").read())
        for _n in _ast.walk(_t):
            if isinstance(_n, _ast.Import):
                _queue += [a.name.split(".")[0] for a in _n.names]
            elif isinstance(_n, _ast.ImportFrom) and _n.level == 0 and _n.module:
                _queue.append(_n.module.split(".")[0])

    _missing = sorted(m for m in _needed if m + ".py" not in _copied)
    ok &= check(f"the Dockerfile COPYs every module its entrypoint imports "
                f"(needs {len(_needed)}; missing: {_missing or 'none'}) — a "
                f"module left out breaks the image on EVERY invocation, "
                f"--help included",
                not _missing)
    ok &= check("...and still carries no test suite or fixtures into the "
                "image",
                "smoke_test.py" not in _copied and "tests" not in _dockerfile)

    # pyproject lists the same flat modules. A new module missing here
    # installs a package whose console script cannot import itself.
    _pyproject = open(os.path.join(_repo_root, "pyproject.toml"),
                      encoding="utf-8").read()
    _declared = set(re.findall(r'^\s*"([a-z_]+)",\s*$', _pyproject, re.M))
    _undeclared = sorted(m for m in _needed if m not in _declared)
    ok &= check(f"pyproject's py-modules lists every module the entrypoint "
                f"imports (missing: {_undeclared or 'none'})",
                not _undeclared)

        # console scripts point at something that exists    #
    # --- console scripts point at something that exists -------------------
    # A [project.scripts] entry naming a missing module or a non-callable
    # installs perfectly happily and fails only when a user runs it — the
    # same "looks configured, is not" shape as the rest of this file. Checked
    # statically so it does not need an install.
    _scripts = dict(re.findall(r'^([a-z0-9-]+) = "([a-z_]+:[a-z_]+)"\s*$',
                               _pyproject, re.M))
    ok &= check(f"pyproject declares console scripts ({len(_scripts)} of them)",
                len(_scripts) >= 8)
    _bad_targets = []
    for _cmd, _target in sorted(_scripts.items()):
        _mod, _fn = _target.split(":")
        if _mod not in _declared:
            _bad_targets.append(f"{_cmd} -> {_mod} not in py-modules")
            continue
        _t = _ast.parse(open(os.path.join(_repo_root, _mod + ".py"),
                             encoding="utf-8").read())
        if not any(isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                   and n.name == _fn for n in _t.body):
            _bad_targets.append(f"{_cmd} -> {_target} is not a top-level def")
    ok &= check(f"every console script points at a real top-level function in "
                f"a declared module (bad: {_bad_targets or 'none'})",
                not _bad_targets)

    # The three engines go through cli_entry, not straight at their own
    # main(). Installing ONE extra — which is what the README says to do —
    # still puts all three commands on PATH, and running the other two used
    # to print a ModuleNotFoundError traceback for a command the install
    # itself created. cli_entry turns that into exit 2 and the pip line.
    ok &= check("the engine commands go through cli_entry, so a missing "
                "driver is a usage error naming the extra to install rather "
                "than a traceback",
                all(_scripts.get(f"farfetch-scraper-{_e}", "").startswith("cli_entry:")
                    for _e in ("playwright", "selenium", "puppeteer")))

    import cli_entry as _ce
    ok &= check("...and cli_entry knows every engine, with the right driver "
                "name for each — pyppeteer's module and its extra differ, "
                "which is why this is a map and not a string operation",
                _ce._ENGINES["puppeteer_scraper"] == ("pyppeteer", "puppeteer")
                and set(_ce._ENGINES) == {"playwright_scraper",
                                          "selenium_scraper",
                                          "puppeteer_scraper"})

    # Both halves of the triage, with the import stubbed so neither case
    # launches a browser. The engine's OWN driver missing is a usage error;
    # anything else must surface as itself, or a genuinely broken module gets
    # reported as "you forgot an extra" and the real cause is never seen.
    _real_import = _ce.importlib.import_module

    def _raise_missing(name):
        def _stub(_mod):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return _stub

    _ce.importlib.import_module = _raise_missing("pyppeteer")
    _buf = io.StringIO()
    with contextlib.redirect_stderr(_buf):
        _rc_missing_driver = _ce._run("puppeteer_scraper")
    _msg = _buf.getvalue()

    _ce.importlib.import_module = _raise_missing("bs4")
    try:
        _ce._run("puppeteer_scraper")
        _other = "swallowed"
    except ModuleNotFoundError as e:
        _other = e.name
    _ce.importlib.import_module = _real_import

    ok &= check("cli_entry: the engine's own driver missing is exit 2 with "
                "the pip line, not a traceback for a command the install "
                "itself created",
                _rc_missing_driver == 2
                and "pip install" in _msg and "puppeteer" in _msg)
    ok &= check("cli_entry: any OTHER missing module is re-raised unchanged — "
                "a broken import must never be blamed on a missing extra",
                _other == "bs4")

    # `--fp-tags` MUST DEFAULT TO ONE OS-FAMILY TAG. It shipped as
    # "Windows,Chrome,Desktop", which the fingerprint API rejects with HTTP
    # 400 — so --fingerprint failed on every invocation, while
    # fingerprint_client.py's own --tags help said ONE tag all along.
    # Measured against the live API on 2026-09-10: `Windows` succeeds, and
    # `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.
    import glob as _glob
    for _path in sorted(_glob.glob(os.path.join(_repo_root, "*_scraper.py"))):
        _m = re.search(r'--fp-tags"\s*,\s*default="([^"]*)"',
                       open(_path, encoding="utf-8").read())
        if _m is None:
            continue
        ok &= check(f"{os.path.basename(_path)}'s --fp-tags default is ONE "
                    f"tag the API accepts",
                    "," not in _m.group(1)
                    and _m.group(1) in ("Windows", "Microsoft Windows",
                                        "Android"))

    # HOW fingerprint_client FINDS ITS KEY, which is a separate defect from
    # which tags it sends. `--key` defaulted to `os.environ.get(
    # "TWOCAPTCHA_KEY")` alone, so a key put in `.env` — exactly as the
    # README and .env.example instruct — worked for every engine and failed
    # HERE with "No API key". A documented mechanism not applied on one path,
    # which is the shape of half the defects the family notes list. Found on
    # a sibling repo's first live --fingerprint run and confirmed present
    # here.
    import fingerprint_client as _fpc
    import env_config as _envc
    _fp_src = inspect.getsource(_fpc.main)
    ok &= check("fingerprint_client loads .env itself, rather than hoping an "
                "engine did", "env_config.load_env()" in _fp_src)
    ok &= check("...and reads the key through the family's loader",
                'env_config.env_value("TWOCAPTCHA_KEY")' in _fp_src)
    # Through `env_value` and NOT `os.environ.get`, because only the former
    # applies the placeholder rule. Measured both ways with
    # TWOCAPTCHA_KEY=your_2captcha_api_key_here exported: os.environ.get
    # sends the placeholder to the API and the run reports "Fingerprint API
    # rejected the key (401) — note this is a separate subscription", which
    # sends the reader to check a subscription they never needed.
    ok &= check("...not straight from os.environ, which skips the "
                "placeholder rule",
                'os.environ.get("TWOCAPTCHA_KEY")' not in _fp_src)
    _saved = os.environ.get("TWOCAPTCHA_KEY")
    try:
        os.environ["TWOCAPTCHA_KEY"] = "your_2captcha_api_key_here"
        _read_back = _envc.env_value("TWOCAPTCHA_KEY")
    finally:
        if _saved is None:
            os.environ.pop("TWOCAPTCHA_KEY", None)
        else:
            os.environ["TWOCAPTCHA_KEY"] = _saved
    ok &= check("a placeholder still reads as unset on this path",
                _read_back is None)
    # The default must never reach `--help`: argparse prints one only when
    # the help string asks for it, so this is one substring away from
    # printing a live credential to anyone who types --help.
    ok &= check("the --key help text does not interpolate its default",
                "%(default)s" not in _fp_src)
    return ok




def check_x_debug_header_is_redacted(ok):
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim: the API
    echoes back the task it ran, so a credentialed CDP endpoint's username
    and password went into the log.

    The fixtures are assembled from pieces, never written out whole, because
    this file is scanned by the credential check like every other one.
    """
    import scraper_api_client as sac
    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    gone = pw not in out and key not in out
    kept = ("cost=0.00145" in out and "cb.2captcha.com:9222" in out
            and "status=200" in out)
    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    both = s1 not in two and s2 not in two
    wired = ('logger.info("x-debug: %s", _redact_debug_header(debug))'
             in inspect.getsource(sac))
    ok &= check("x-debug: the credential and the key are gone", gone)
    ok &= check("x-debug: the cost, host and status survive", kept)
    ok &= check("x-debug: both credentials are masked, not just the first", both)
    ok &= check("x-debug: the log line calls the redactor", wired)
    return ok


def main() -> int:
    ok = True

    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and still
    # says "all passed" is the same defect as code that reports success without
    # checking that what it wanted actually happened.
    _skips = []
    # Which engine LIBRARIES were absent, as names rather than prose. The
    # engine-smoke CI job runs one venv per engine and has to assert that THIS
    # engine did not skip while the other two did — which a free-text line
    # cannot answer. Printed as one machine-readable line at the end.
    _skipped_engines = set()

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

    # The ?page=N fallback used when NEXT_PAGE_SELECTOR matches nothing.
    from product_parser import page_url
    ok &= check("page_url: adds ?page=N to a bare listing URL",
                page_url("https://www.farfetch.com/shopping/kids/x/items.aspx", 2)
                == "https://www.farfetch.com/shopping/kids/x/items.aspx?page=2")
    ok &= check("page_url: REPLACES an existing page param rather than "
                "appending a second one",
                page_url("https://www.farfetch.com/shopping/x/items.aspx?page=1", 3)
                == "https://www.farfetch.com/shopping/x/items.aspx?page=3")
    ok &= check("page_url: preserves the filters and sort order already in the "
                "URL — dropping them would silently scrape a different listing",
                page_url("https://www.farfetch.com/de/shopping/x/items.aspx?view=90&sort=3", 4)
                == "https://www.farfetch.com/de/shopping/x/items.aspx?view=90&sort=3&page=4")
    ok &= check("page_url: an existing param differing only in case is still "
                "replaced, not duplicated",
                page_url("https://www.farfetch.com/shopping/x/items.aspx?PAGE=7", 8)
                == "https://www.farfetch.com/shopping/x/items.aspx?page=8")

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

    # A space is the thousands separator in French, Russian and others, and a
    # rendered page uses a no-break variant so the number does not wrap. All
    # three forms appeared in an audit and all three parsed as 234, an order
    # of magnitude off, silently.
    ok &= check("space-grouped thousands: '1 234 €' is 1234, not 234",
                _prices_in("1 234 €") == ([1234.0], "EUR"))
    ok &= check("no-break space (U+00A0) groups thousands too — this is what a "
                "rendered page actually contains",
                _prices_in("1 234 €") == ([1234.0], "EUR"))
    ok &= check("narrow no-break space (U+202F) as well",
                _prices_in("1 234 €") == ([1234.0], "EUR"))
    ok &= check("space grouping combines with a decimal comma: "
                "'1 234,56 €' -> 1234.56",
                _prices_in("1 234,56 €") == ([1234.56], "EUR"))
    ok &= check("space grouping requires FULL groups of three digits, so a size "
                "list beside a price ('5 yrs, 6 yrs 200 €') does not merge into "
                "one number",
                _prices_in("Verfügbar in 5 yrs, 6 yrs 200 €")
                == ([200.0], "EUR"))

    # A bare "$" is genuinely ambiguous, but a PREFIXED one is not, and
    # reporting HK$1,234 as USD is the wrong currency rather than a rounding
    # error — directly against this project's cross-country comparison use.
    ok &= check("HK$ is HKD, not USD", _prices_in("HK$1,234") == ([1234.0], "HKD"))
    ok &= check("A$ is AUD and NT$ is TWD, and the prefix is tried before the "
                "bare '$' so it cannot be swallowed",
                _prices_in("A$99") == ([99.0], "AUD")
                and _prices_in("NT$1 500") == ([1500.0], "TWD"))
    ok &= check("a bare '$' still reads as USD — on the US site that is what it "
                "means, and JSON-LD supplies the real currency when the site "
                "publishes one",
                _prices_in("$1,234") == ([1234.0], "USD"))

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


    # Every section, in the order they were written. A section
    # takes the running verdict and returns it; nothing else is
    # threaded between them.
    ok = check_runtime_recaptcha_detection_added_2026_08_24(ok)
    ok = check_reconciling_two_detectors_that_disagree(ok)
    ok = check_api_v2_task_objects_must_match_the_documented_ty(ok)
    ok = check_discounted_prices_the_dom_overlay(ok)
    ok = check_category_label(ok)
    ok = check_sample_selection(ok)
    ok = check_credential_loading(ok)
    ok = check_v2_createtask_gettaskresult_round_trip_mocked(ok)
    ok = check_sign_up_modal_selectors(ok)
    ok = check_empty_result_contract(ok)
    ok = check_x_debug_header_is_redacted(ok)
    ok = check_blocked_vs_empty_exit_code(ok)
    ok = check_akamai_s_refusal_page_the_2026_09_11_audit_s_p0(ok)
    ok = check_page_content_mid_navigation(ok)
    ok = check_puppeteer_pyppeteer_ua_derived_from_the_real_lau(ok)
    ok = check_selenium_chromedriver_on_the_local_path(ok)
    ok = check_selenium_two_live_local_failures_turned_into_tes(ok)
    ok = check_cross_page_dedup_cross_run_diff(ok)
    ok = check_run_metadata_partial_runs_must_not_read_as_delis(ok)
    ok = check_run_metadata_id_timings_quality(ok)
    ok = check_the_webhook(ok)
    ok = check_json_ld_shapes_that_are_legal_but_were_not_handl(ok)
    ok = check_proxy_credentials_must_not_reach_a_browser_comma(ok)
    ok = check_proxy_pool_and_rotation(ok)
    ok = check_product_detail_pages(ok)
    ok = check_engine_flag_parity(ok)
    ok = check_the_suite_s_own_shape(ok)
    ok = check_naming_and_dead_feature_guards(ok)
    ok = check_canary_yml_the_exit_code_table_it_prints_must_be(ok)
    ok = check_numeric_flags_are_range_checked_in_every_cli(ok)
    ok = check_the_dockerfile_s_copy_list_vs_the_entrypoint_s_i(ok)

    print()
    if _skips:
        print(f"{len(_skips)} group(s) of checks SKIPPED — an optional engine "
              f"library is not installed here:")
        for line in _skips:
            print(f"  - {line}")
        print("Expected in the offline CI job, which installs no engine on "
              "purpose. Install one to exercise them.")
        print()
    # Always printed, including when empty, so a CI job can tell "no engine
    # skipped" from "this line was never reached".
    print(f"SKIPPED_ENGINES: {','.join(sorted(_skipped_engines))}")
    print()

    if ok:
        print("All smoke tests passed. Core logic is sound — safe to move on to a real browser run.")
        return 0
    else:
        print("Some checks FAILED — fix these before running against a real browser/site.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
