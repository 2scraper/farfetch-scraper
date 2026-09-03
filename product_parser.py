"""
product_parser.py
-------------------
Extracts Product records from a Farfetch kids category/hub page.

Strategy, in order of preference:
  1. schema.org JSON-LD blocks (<script type="application/ld+json">) — used
     if Farfetch embeds per-product Product data there. Some large
     e-commerce sites do, some only embed page-level metadata (CollectionPage
     / ItemList without full Product entries) — this path is harmless either
     way, since step 2 catches whatever step 1 misses.
  2. URL-pattern + regex fallback. Farfetch product links follow a very
     distinctive, stable pattern:
         /shopping/kids/<brand-and-item-slug>-item-<digits>.aspx
     This is a far more durable anchor for "this is a product card" than
     guessing a CSS/utility class name, which is the mistake that cost the
     most time on a previous single-site scraper in this same family
     (Tailwind-class-based sites reuse generic layout classes everywhere,
     so class-only matching produces false positives). Once a product link
     is found, price/discount are extracted via regex over that element's
     own text — again, text-content regex survives a class-name/theme
     change that would silently break a CSS selector.

FIXED, and worth knowing why the fix looks the way it does: this site
publishes only ONE price per product in its listing JSON-LD, and on a
discounted item it is the INTERMEDIATE one. Measured on four products from
a /sale/all/ category:

    tile:     Originalpreis 245 / Sale-Preis 135 / Endpreis 108 EUR
              Sale-Discount -45%   Promo-Discount -20%
    JSON-LD:  offers.price = 135        <- the middle number

Verified live afterwards on a full /sale/all/ page: 96 products, 95 rows
corrected. The one skipped row is the guard working — sku 23538433 had a
JSON-LD price of 126 against tile prices of 133 and 101, so the two views
disagreed about the product and the row was left alone rather than
overwritten. That shape appears to be a product listed by more than one
boutique; it is rare (1 in 96) and worth a warning rather than a guess.

All four agreed: JSON-LD gave 135 / 65 / 60 / 426 where the tiles showed
108 / 52 / 48 / 341, and each pair of printed percentages compounded to the
implied discount within 0.1pp. So `parse_products` now runs a DOM pass over
a SUCCESSFUL JSON-LD parse (`_overlay_tile_prices`), matched by
-item-<digits>, and takes the lowest number in a tile as `price` and the
highest as `original_price`. `tile_prices_overlay=False` restores the raw
JSON-LD figures.

Two things that look like the obvious implementation and are wrong:

  - `line-through` does NOT mean "the old price". With a promo applied the
    SALE price is struck through too — three of three tiles inspected.
  - The price elements have no data-testid and their classes are
    build-generated hashes (`ltr-12ss2mb-Footnote e82sgrh11`). Neither
    survives a deploy.

Sorting the tile's numbers is stable against both, and against locale.

A related correction: `discount_pct` used to be read from the first "-NN%"
in the tile, which on a two-stage discount is the sale percentage rather
than what the buyer saves. It is now computed from the prices, with the
printed percentages kept only as a cross-check that logs on disagreement.

On three prices in one tile: the original build guessed this meant several
boutiques stocking the same item, each at its own discount. It does not.
"$160 $80 $64 -50% -20%" is ONE product's discount chain — 160 -50% -> 80,
then -20% -> 64 — the same shape later confirmed on four live products from
a /sale/all/ page. The guess happened to produce the right price (lowest is
what you pay, highest is the list price) but the wrong explanation, and the
wrong explanation sent the discount percentage to the first "-NN%" in the
text. Kept here because a plausible mechanism that predicts the observation
is not the same as the cause. Sizes and per-boutique detail are on the
product page; this scraper is listing-level.

No live browser was available to reverse-engineer Farfetch's actual CSS
classes when this was written (unlike the Kohl's build in this same
family, which WAS live-tested) — the URL-pattern approach above was chosen
specifically because it doesn't need that. If prices/titles come back
empty in your first real run, open the `{out}_page1_debug.html` dump the
engine writes on failure before assuming the whole approach is wrong — it
is far more likely a scoping tweak (see NOTE in _parse_css_fallback
below).
"""

import json
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from output_writer import Product

logger = logging.getLogger(__name__)

SELECTORS = {
    # The one selector this parser actually depends on. Farfetch product
    # detail URLs all end in "-item-<digits>.aspx" — this has been stable
    # across Farfetch redesigns for years, unlike any single CSS class.
    "item_link": 'a[href*="-item-"]',
}

# Farfetch geo-redirects: the same category URL served to a European exit IP
# lands on /de/ with prices as "125 €", while a US exit IP gets "$125". A
# $-only price regex therefore returns ZERO products on any non-US locale —
# confirmed live on 2026-08-24 from a European IP, where the page rendered
# 96 products in "125 €" form. The symbol may lead or trail the number
# depending on locale, so both orders are matched.
#
# Note this only affects the CSS/URL-pattern fallback path. The JSON-LD path
# reads offers.price/priceCurrency as structured numbers and was never
# currency-sensitive.
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_PRICE_RE = re.compile(
    r"(?:([$€£¥])\s?([\d.,]+(?:[.,]\d{1,2})?)"      # symbol first: $125.00 / €125,00
    r"|([\d.,]+(?:[.,]\d{1,2})?)\s?([$€£¥]))"       # symbol last:  125,00 €
)
_DISCOUNT_RE = re.compile(r"-(\d{1,2})%")

# Every Farfetch product URL ends in "-item-<digits>.aspx", and those digits
# ARE the product id. Farfetch's listing JSON-LD carries no `sku`/`productID`
# field at all — verified across two separate live captures two weeks apart
# (2026-08-12 and 2026-08-24), so this is a standing gap, not a regression —
# which meant every row in every run so far had sku=None despite the id
# sitting in plain sight in the URL. Recover it from there.
_SKU_IN_URL_RE = re.compile(r"-item-(\d+)\.aspx")


def _sku_from_url(url: Optional[str]) -> Optional[str]:
    """Pull the product id out of a Farfetch product URL, if it's there."""
    if not url:
        return None
    m = _SKU_IN_URL_RE.search(url)
    return m.group(1) if m else None


def _prices_in(text: str):
    """Return ([amounts], currency_code_or_None) for all prices in `text`.

    Handles both symbol-first and symbol-last forms, and both decimal
    conventions: "1,234.56" (US) and "1.234,56" (EU). The disambiguation
    rule is 'whichever separator comes last is the decimal point' — which
    is correct for every locale Farfetch serves.
    """
    amounts, currency = [], None
    for m in _PRICE_RE.finditer(text):
        sym = m.group(1) or m.group(4)
        raw = m.group(2) or m.group(3)
        if currency is None:
            currency = _CURRENCY_SYMBOLS.get(sym)
        last_dot, last_comma = raw.rfind("."), raw.rfind(",")
        if last_dot > last_comma:
            norm = raw.replace(",", "")
        elif last_comma > last_dot:
            norm = raw.replace(".", "").replace(",", ".")
        else:
            norm = raw
        try:
            amounts.append(float(norm))
        except ValueError:
            continue
    return amounts, currency
_RATING_RE_COUNT_FIRST = re.compile(r"([\d,]+)\s*(?:user )?reviews?.*?([\d.]+)\s*out of 5", re.IGNORECASE)
_RATING_RE_RATING_FIRST = re.compile(r"([\d.]+)\s*out of 5.*?([\d,]+)\s*reviews?", re.IGNORECASE)


def _to_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"[\d,]+\.\d+|\d+", text.replace(",", ""))
    return float(m.group()) if m else None


def _parse_rating_from_aria(aria_label: str):
    """Best-effort only — Farfetch listing tiles may not show a rating at
    all (common on luxury/boutique marketplaces). Returns (None, None) if
    nothing matches; callers must treat rating as optional."""
    for pattern, order in ((_RATING_RE_COUNT_FIRST, "count_first"), (_RATING_RE_RATING_FIRST, "rating_first")):
        m = pattern.search(aria_label)
        if m:
            a, b = m.group(1), m.group(2)
            count, rating = (a, b) if order == "count_first" else (b, a)
            return _to_float(rating), int(count.replace(",", ""))
    return None, None


def _parse_jsonld(html: str, base_url: str) -> List[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: List[Product] = []

    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        blocks = data if isinstance(data, list) else [data]
        for block in blocks:
            items = block.get("itemListElement") if isinstance(block, dict) else None
            candidates = items if items else [block]

            for entry in candidates:
                node = entry.get("item", entry) if isinstance(entry, dict) else entry
                if not isinstance(node, dict) or node.get("@type") not in ("Product", ["Product"]):
                    continue

                offers = node.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                agg_rating = node.get("aggregateRating", {}) or {}

                # Farfetch's own JSON-LD (confirmed live) nests the product
                # URL under offers.url, not directly on the Product node —
                # node.get("url") is simply absent there. Check both, in
                # that order, rather than assuming one convention.
                url_path = node.get("url") or offers.get("url") or ""

                resolved_url = urljoin(base_url, url_path) if url_path else base_url
                products.append(Product(
                    url=resolved_url,
                    # Farfetch's listing JSON-LD has no sku/productID at all,
                    # so this falls through to the URL every time in practice.
                    sku=node.get("sku") or node.get("productID") or _sku_from_url(resolved_url),
                    title=node.get("name"),
                    brand=(node.get("brand") or {}).get("name") if isinstance(node.get("brand"), dict) else node.get("brand"),
                    price=_to_float(str(offers.get("price"))) if offers.get("price") is not None else None,
                    currency=offers.get("priceCurrency", "USD"),
                    rating=_to_float(str(agg_rating.get("ratingValue"))) if agg_rating.get("ratingValue") else None,
                    review_count=int(agg_rating["reviewCount"]) if str(agg_rating.get("reviewCount", "")).isdigit() else None,
                    in_stock=("InStock" in str(offers.get("availability", ""))) if offers.get("availability") else None,
                    image_url=node.get("image") if isinstance(node.get("image"), str) else (node.get("image") or [None])[0],
                ))
    return products


def _parse_css_fallback(html: str, base_url: str, category: Optional[str]) -> List[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: List[Product] = []
    seen_urls = set()

    for a in soup.select(SELECTORS["item_link"]):
        href = a.get("href")
        if not href:
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen_urls:
            continue

        # NOTE / first thing to check if this comes back empty on a real run:
        # Farfetch sometimes splits one product into TWO adjacent <a> tags
        # sharing the same href (one wrapping the image, one wrapping the
        # brand/name/price text) rather than one tile. We scope to the link
        # itself first; if there's no price text inside it, we widen to its
        # immediate parent — but ONLY if that parent still contains exactly
        # this one product link. Widening past that would silently pull in
        # a sibling tile's price/image whenever this link happens to be
        # junk (e.g. a "Sizing guide" link matching the URL shape by
        # coincidence but sharing a grid container with real products) —
        # caught by a fixture test while building this, not live traffic.
        scope = a
        text = scope.get_text(" ", strip=True)
        if not _PRICE_RE.search(text) and scope.parent is not None:
            candidate = scope.parent
            if len(candidate.select(SELECTORS["item_link"])) == 1:
                scope = candidate
                text = scope.get_text(" ", strip=True)

        prices, currency = _prices_in(text)
        if not prices:
            continue  # not a product tile (e.g. a nav link that happens to match)
        seen_urls.add(full_url)

        price = min(prices)
        original_price = max(prices) if len(prices) > 1 else None
        # Computed from the prices, not read off the page: a tile can print two
        # compounding percentages and the first one is not the buyer's discount.
        discount_pct = _discount_from(price, original_price, text)

        img = scope.find("img")
        title = None
        if img and img.get("alt"):
            title = img["alt"].strip() or None
        if not title:
            # Strip the price/discount substrings out of the tile's own text
            # as a last-resort title — better than nothing, worse than alt text.
            cleaned = _PRICE_RE.sub("", text)
            cleaned = _DISCOUNT_RE.sub("", cleaned)
            title = cleaned.strip(" ·-") or None

        rating, review_count = (None, None)
        rating_el = scope.find(attrs={"aria-label": re.compile(r"out of 5|reviews?", re.IGNORECASE)})
        if rating_el:
            rating, review_count = _parse_rating_from_aria(rating_el.get("aria-label", ""))

        products.append(Product(
            url=full_url,
            sku=_sku_from_url(full_url),
            title=title,
            price=price,
            currency=currency or "USD",
            original_price=original_price,
            discount_pct=discount_pct,
            rating=rating,
            review_count=review_count,
            image_url=(img.get("src") or img.get("data-src")) if img else None,
            category=category,
        ))
    return products


# Path segments that are never a category: the site's own route prefixes, and
# the two-letter locale Farfetch inserts when it geo-redirects (`/de/shopping/...`).
# `all` is skipped too: on a sale URL like /shopping/women/sale/all/items.aspx
# the informative segment is `sale`, not the generic bucket after it.
_NOT_A_CATEGORY = {"shopping", "sets", "items.aspx", "all"}


def category_from_url(url: str):
    """Best-effort category label from a listing URL.

    `category` is a free-text label for grouping rows, and the page itself does
    not supply one — so before this existed it was `null` on every row unless
    the caller remembered `--category`. A column that is empty by default reads
    as a broken field rather than an optional one.

    The last path segment before `items.aspx` is the category:

        /shopping/kids/girls-clothing-4/items.aspx        -> girls-clothing-4
        /de/shopping/kids/girls-clothing-4/items.aspx     -> girls-clothing-4
        /shopping/kids/items.aspx                         -> kids

    The numeric suffix is kept deliberately. It is Farfetch's own category id,
    it is stable, and stripping it would silently merge two categories that
    differ only by id. A caller who wants a tidier label passes --category,
    which always wins.

    Returns None when nothing usable is in the path (a search URL, say), which
    leaves the field null exactly as before.
    """
    if not url:
        return None
    try:
        parts = [seg for seg in urlparse(url).path.split("/") if seg]
    except ValueError:
        return None

    if parts and parts[-1].lower().endswith(".aspx"):
        parts = parts[:-1]

    # Drop a leading two-letter locale, but only in first position — a category
    # is free to be two characters long anywhere else.
    if parts and len(parts[0]) == 2 and parts[0].isalpha():
        parts = parts[1:]

    parts = [seg for seg in parts if seg.lower() not in _NOT_A_CATEGORY]
    return parts[-1] if parts else None



# How far to widen from a product link when looking for its tile. Eight levels
# covers the observed markup; the loop stops earlier the moment a candidate
# holds more than one product link.
_MAX_TILE_WIDEN = 8


def _tile_scope(anchor):
    """The outermost ancestor of `anchor` that still holds exactly ONE product link.

    Stopping one level too late is the "junk-link data theft" failure: every
    tile then reports its neighbours' prices. Confirmed the hard way twice — in
    the original build, and again while diagnosing the price overlay, where a
    too-wide scope showed three different products with one product's prices.
    """
    best, node = anchor, anchor
    for _ in range(_MAX_TILE_WIDEN):
        node = node.parent
        if node is None or not hasattr(node, "select"):
            break
        if len(node.select(SELECTORS["item_link"])) != 1:
            break
        best = node
    return best


def _discount_from(price, original_price, text):
    """Percentage off, computed from the prices rather than read from the page.

    This site can show TWO percentages on one tile — a sale discount and a
    promo discount that compounds on top of it:

        Originalpreis 245 EUR  Sale-Preis 135 EUR  Endpreis 108 EUR
        Sale-Discount -45%     Promo-Discount -20%

    Reading the first percentage gives -45%, which is not the discount the
    buyer receives; 245 -> 108 is -55.9%, and -45% compounded with -20% is
    -56.0%. So the arithmetic on the prices is the trustworthy source and the
    printed percentages are only a cross-check. Verified on four products, all
    four agreeing to within 0.1pp.
    """
    if original_price and price is not None and original_price > price:
        computed = round((1 - price / original_price) * 100, 1)

        # Cross-check against whatever the page printed. A mismatch is worth a
        # log line rather than a silent override: it means either a third
        # discount stage or a price we misread.
        percents = [float(m) for m in _DISCOUNT_RE.findall(text)]
        if percents:
            remaining = 1.0
            for pct in percents:
                remaining *= (1 - pct / 100)
            stated = round((1 - remaining) * 100, 1)
            if abs(stated - computed) > 1.0:
                logger.debug(
                    "Discount mismatch: prices imply %.1f%%, the page's "
                    "percentages %s compound to %.1f%%. Using the prices.",
                    computed, percents, stated)
        return computed

    # No usable pair of prices. A single printed percentage is better than
    # nothing, but cannot be verified.
    match = _DISCOUNT_RE.search(text)
    return float(match.group(1)) if match else None


def tile_prices(html: str, base_url: str):
    """{sku: (amounts, currency, tile_text)} for every tile showing a price.

    Exists because farfetch.com's listing JSON-LD publishes ONE price per
    product, and on a discounted item it is the INTERMEDIATE one — the sale
    price before a site-wide promo, not the price at checkout. Confirmed live
    on four products from a /sale/all/ category: JSON-LD said 135 / 65 / 60 /
    426 where the tiles showed 108 / 52 / 48 / 341.

    Deliberately does NOT use line-through to identify the old price: when a
    promo applies the SALE price is struck through as well, on all four
    products sampled. Nor the class names, which are build-generated hashes
    (`ltr-12ss2mb-Footnote`) with no data-testid to fall back on. Sorting the
    numbers is stable against both.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {}

    for a in soup.select(SELECTORS["item_link"]):
        href = a.get("href")
        if not href:
            continue
        sku = _sku_from_url(urljoin(base_url, href))
        if not sku or sku in out:
            continue

        scope = _tile_scope(a)
        text = scope.get_text(" ", strip=True)
        amounts, currency = _prices_in(text)
        if amounts:
            out[sku] = (amounts, currency, text)

    return out


def _overlay_tile_prices(products, html: str, base_url: str):
    """Correct price / original_price / discount_pct from the DOM, in place.

    Only touches a product whose tile shows MORE than one price: a single price
    means no discount, and JSON-LD's figure is then correct and better trusted
    (it is structured data). Returns how many rows were corrected.
    """
    tiles = tile_prices(html, base_url)
    if not tiles:
        logger.debug("No priced tiles found in the DOM; leaving JSON-LD prices "
                     "as they are.")
        return 0

    corrected = 0
    for product in products:
        entry = tiles.get(product.sku)
        if entry is None:
            continue
        amounts, currency, text = entry
        if len(set(amounts)) < 2:
            continue

        low, high = min(amounts), max(amounts)

        # The JSON-LD price should be one of the tile's numbers. If it is not,
        # the two views disagree about which product this is — most likely the
        # tile scope is wrong — and overwriting would corrupt a correct row.
        if product.price is not None and not any(
                abs(product.price - amount) < 0.01 for amount in amounts):
            logger.warning(
                "sku %s: JSON-LD price %.2f is not among the tile's prices %s "
                "— leaving the row untouched.", product.sku, product.price,
                amounts)
            continue

        product.price = low
        product.original_price = high
        product.discount_pct = _discount_from(low, high, text)
        if currency:
            product.currency = currency
        corrected += 1

    if corrected:
        logger.info("Corrected prices on %d of %d products from the DOM "
                    "(JSON-LD publishes the pre-promo price).",
                    corrected, len(products))
    return corrected


def parse_products(html: str, base_url: str, category: Optional[str] = None,
                   tile_prices_overlay: bool = True) -> List[Product]:
    """Products from a listing page. JSON-LD first, CSS/URL patterns as fallback.

    `tile_prices_overlay` runs a DOM pass over a SUCCESSFUL JSON-LD parse to
    correct the prices. It defaults on because without it every discounted row
    carries the pre-promo price — see tile_prices() for the measurements. Pass
    False to get the raw JSON-LD figures, which is occasionally what you want
    when comparing against the site's own structured data.
    """
    # An explicit label always wins; otherwise derive one from the URL so the
    # column is populated by default. Note base_url is the URL the browser
    # ENDED on, so after a geo-redirect this reflects the page actually parsed.
    label = category or category_from_url(base_url)

    products = _parse_jsonld(html, base_url)
    if not products:
        # The fallback reads the same tiles directly, so its prices are already
        # right; no overlay needed on this path.
        return _parse_css_fallback(html, base_url, label)

    for p in products:
        p.category = label
    if tile_prices_overlay:
        _overlay_tile_prices(products, html, base_url)
    return products
