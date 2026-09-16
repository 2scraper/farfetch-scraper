"""
product_detail_parser.py
------------------------
Extracts per-SIZE rows from a Farfetch PRODUCT page.

Separate from product_parser.py, which reads listings, because the two pages
publish different things in different shapes — and the most important
difference is the one that would silently produce nothing:

    listing page   <script type="application/ld+json"> ItemList / Product
    detail  page   <script type="application/ld+json"> ProductGroup

A detail page carries exactly two blocks, `ProductGroup` and
`BreadcrumbList`. Porting the listing parser here finds ZERO products, and
would do so quietly. Measured on ten pages captured 2026-09-15 from a DE
exit; see the captures' FINDINGS.md.

ONE ROW PER SIZE, not per product. Availability and price are published per
variant, which is the whole reason to open a detail page at all, and the
variant sku (`36899289-19`) is what is actually unique. That follows the
family rule for a second kind of row: dedupe on whatever is unique, not on
the product id.

    ProductGroup
      productGroupID  "36899289"
      name, brand{name}, color, description, image[ImageObject]
      variesBy: [https://schema.org/size]
      hasVariant: [ Product, ... ]      one per size
          sku, name, size, image
          offers.availability
          offers.priceSpecification[]   <- the price chain, see below

NO DOM PRICE OVERLAY HERE, and that is a decision rather than an omission.
`product_parser.py` reconciles the listing's JSON-LD price against the
rendered tile because the listing publishes the MIDDLE of a discount chain.
A detail page publishes the whole chain structurally:

    UnitPriceSpecification price=65  EUR                              <- paid
    UnitPriceSpecification price=130 EUR priceType=StrikethroughPrice <- was

Measured on three discounted products: 65/130, 33/65, 45/90. So price and
original_price are facts here and discount_pct is arithmetic. An overlay
could not work anyway: a detail page's DOM contains ZERO rendered price
strings — prices are painted by JS after load — so it would be dead code
that looks load-bearing.

COLUMNS THAT ARE NOT HERE, each because it was looked for and not found
across seven product pages:

    rating / review_count   no aggregateRating, no ratingValue, 0 occurrences
    merchant / boutique     no "Boutique", no "sold by", no storeId
    shipping / delivery     offers.shippingDetails absent
    return policy           present, but IDENTICAL on all 38 variants
                            (30 days, free, by mail) — a constant is a line
                            in the README, not a column

LOCALE: THE SKU IS STABLE, THE SIZE LABEL IS NOT. Measured by capturing the
same four product ids on DE and on US:

    sku          36899289-19   identical on both markets
    size         "4 Jahre"  ->  "4 yrs"
                 "3-6 M."   ->  "3-6 mth"
                 "10"       ->  "10"        (nothing to translate)
    title, color, composition, category   all translated
    color        "Nude"     ->  "Neutrals"  (a different taxonomy value,
                                             not a translation)
    price        60 EUR     ->  90 USD
                 1020 EUR   ->  598 USD

So a cross-market comparison joins on `sku`, and `size` is display text. The
prices are set per market rather than converted — 1020 EUR is about 1100 USD
and the US price is 598 — which is worth knowing before anyone reads a
cross-market difference as an arbitrage.

KNOWN LIMITATION, pinned rather than half-guarded: `in_stock` has never been
observed False on a real page. 100 variants across 19 products and two
markets, including an entire sale section, every one InStock. The size picker
was also opened in a live browser on one product: it showed exactly the sizes
the JSON-LD carried, no more.

Two explanations remain open, and they mean different things:

  a) everything happened to be in stock, or
  b) `hasVariant` lists only AVAILABLE sizes and a sold-out one is omitted
     rather than marked — which would make this column constant.

The page's own analytics event names include Product_OutOfStock_ClickNotifyMe,
so the UI plainly has a sold-out state; whether the structured data expresses
it is unresolved. The mapping is therefore an ALLOWLIST rather than
`!= OutOfStock`: a value nobody anticipated reads as not available rather
than silently as yes. Treat a False with more suspicion than a True until one
is captured.
"""

import json
import logging
import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from output_writer import ProductVariant

logger = logging.getLogger(__name__)

# schema.org values that mean "you can buy this right now". Anything else —
# OutOfStock, SoldOut, Discontinued, PreOrder, BackOrder — is not stock in
# hand. Listed explicitly rather than testing `!= OutOfStock`, so a value
# nobody anticipated reads as "not available" instead of silently as "yes".
_IN_STOCK = {
    "instock", "instoreonly", "limitedavailability", "onlineonly",
}

# The heading labels that introduce the composition block. Localised, so this
# is a set rather than a string — and a page whose label is not here simply
# leaves the column empty rather than picking up the wrong block.
_COMPOSITION_LABELS = {
    "zusammensetzung",      # de
    "composition",          # en, fr
    "composizione",         # it
    "composición",          # es
    "samenstelling",        # nl
    "skład",                # pl
    "состав",               # ru
}

_SIZE_IN_OFFER_URL = re.compile(r"[?&]size=([^&]+)")


# --------------------------------------------------------------------------
# JSON-LD plumbing. Every shape below is legal schema.org and at least three
# of them have broken a naive parser in this family before, so they are
# handled rather than assumed away.
# --------------------------------------------------------------------------

def _ld_blocks(soup: BeautifulSoup) -> List[dict]:
    """Every parseable ld+json object on the page, flattened.

    A block that does not parse is skipped with a warning rather than taking
    the page down: one malformed script must not cost the other one.
    """
    out: List[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Skipping an unparseable ld+json block (%s).", e)
            continue
        for node in (data if isinstance(data, list) else [data]):
            if isinstance(node, dict):
                out.append(node)
                # @graph, which is how some sites nest everything and which
                # cost this family an "empty category" report once.
                for sub in (node.get("@graph") or []):
                    if isinstance(sub, dict):
                        out.append(sub)
    return out


def _first_of_type(blocks: List[dict], wanted: str) -> Optional[dict]:
    for b in blocks:
        t = b.get("@type")
        types = t if isinstance(t, list) else [t]
        if wanted in types:
            return b
    return None


def _image_urls(node) -> List[str]:
    """Image URLs out of any of the four legal shapes.

    `image` may be a string, an ImageObject, or a list of either. Reading it
    as a string when it is an ImageObject is a KeyError; reading [0] when it
    is a string silently yields "h".
    """
    raw = node.get("image") if isinstance(node, dict) else node
    if raw is None:
        return []
    out = []
    for item in (raw if isinstance(raw, list) else [raw]):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            url = item.get("contentUrl") or item.get("url")
            if url:
                out.append(url)
    # Deduplicated in order: the same photo often appears at several widths
    # and the caller wants distinct pictures, not distinct URLs.
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _offer(node: dict) -> dict:
    """The offer for a variant, from the three shapes `offers` takes.

    `"offers": null` is the one that bites: an explicit null is not a missing
    key, so a `.get("offers", {})` default does not apply and the next
    attribute access raises.
    """
    raw = node.get("offers")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                return item
    return {}


def _prices(offer: dict):
    """(price, original_price, currency) for one variant.

    The chain lives in `priceSpecification`, which is a LIST even when it
    holds one entry. The entry without a priceType is what a customer pays;
    the one marked StrikethroughPrice is what it was. `price`/`priceCurrency`
    directly on the offer are read as a fallback, because that is the shape
    the listing uses and a page could legitimately use either.
    """
    price = original = currency = None

    specs = offer.get("priceSpecification")
    for spec in (specs if isinstance(specs, list) else [specs]):
        if not isinstance(spec, dict):
            continue
        amount = spec.get("price")
        if amount is None:
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        currency = currency or spec.get("priceCurrency")
        kind = str(spec.get("priceType") or "").rsplit("/", 1)[-1].lower()
        if kind == "strikethroughprice":
            original = amount
        elif price is None:
            price = amount

    if price is None and offer.get("price") is not None:
        try:
            price = float(offer["price"])
        except (TypeError, ValueError):
            price = None
    currency = currency or offer.get("priceCurrency")
    return price, original, currency


def discount_pct(price: Optional[float],
                 original: Optional[float]) -> Optional[float]:
    """What the buyer saves, or None when the two figures do not support it.

    None rather than 0 when there is no original, and None rather than a
    NEGATIVE when the "original" is not above the price — a sibling repo
    learned that one by reading a 30-day-low disclosure as a was-price and
    reporting a negative discount on an undiscounted product.
    """
    if price is None or original is None or original <= 0:
        return None
    if original <= price:
        return None
    return round((original - price) / original * 100, 1)


# --------------------------------------------------------------------------
# DOM plumbing: the facts the page states as labelled blocks.
# --------------------------------------------------------------------------

def labelled_blocks(soup: BeautifulSoup) -> Dict[str, str]:
    """Every `<h*>LABEL</h*><next>VALUE</next>` pair on the page.

    Anchored on the heading text, not on a class: the classes here are build
    hashes (`ltr-2pfgen-Body-BodyBold`) and churn on every deploy, while a
    heading that says "Zusammensetzung" is what the block is FOR.

    Returning all of them rather than hunting one keeps this useful when the
    next field somebody wants turns out to be labelled too.
    """
    out: Dict[str, str] = {}
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        label = heading.get_text(" ", strip=True)
        if not label:
            continue
        sib = heading.find_next_sibling()
        if sib is None:
            continue
        value = sib.get_text(" ", strip=True)
        if value and label.lower() not in out:
            out[label.lower()] = value
    return out


def composition(soup: BeautifulSoup) -> Optional[str]:
    """The material composition, e.g. "Bio-Baumwolle 100%".

    Present on 7 of 7 captured pages, as a labelled block and NOT in JSON-LD.
    It is also inside the JSON-LD `description`, but buried in a
    pipe-separated blurb among a dozen category strings — picking it out of
    there would be a guess presented as a fact.
    """
    blocks = labelled_blocks(soup)
    for label, value in blocks.items():
        if label in _COMPOSITION_LABELS:
            return value
    return None


def _category(blocks: List[dict]) -> Optional[str]:
    """The breadcrumb path, e.g. "Kids > AMI Paris > Kleidung für Jungen".

    The names are nested under `item`, not on the ListItem — reading
    `element["name"]` gives None on every entry, which looks like "no
    breadcrumbs" rather than like a wrong field.
    """
    crumbs = _first_of_type(blocks, "BreadcrumbList")
    if not crumbs:
        return None
    names = []
    for element in (crumbs.get("itemListElement") or []):
        if not isinstance(element, dict):
            continue
        item = element.get("item")
        name = (item.get("name") if isinstance(item, dict)
                else element.get("name"))
        if name:
            names.append(str(name))
    return " > ".join(names) or None


def _brand(node: dict) -> Optional[str]:
    raw = node.get("brand")
    if isinstance(raw, dict):
        return raw.get("name")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                return item["name"]
            if isinstance(item, str):
                return item
    return None


def _size(variant: dict) -> Optional[str]:
    """The size label, preferring the one the site states.

    `size` is localised ("4 Jahre", "3-6 M."). The offer URL carries a
    numeric code (`?size=19`) which is stable across locales but meaningless
    to a reader, so it is only a fallback for a variant that states no label.
    """
    label = variant.get("size")
    if isinstance(label, dict):
        label = label.get("name") or label.get("value")
    if label:
        return str(label).strip()
    offer_url = _offer(variant).get("url") or ""
    m = _SIZE_IN_OFFER_URL.search(offer_url)
    return m.group(1) if m else None


def parse_product_detail(html: str, url: str,
                         category: Optional[str] = None
                         ) -> List[ProductVariant]:
    """One ProductVariant per size, from a Farfetch product page.

    Returns [] when the page carries no ProductGroup — a challenge page, a
    404, or a markup change. Returning [] rather than raising is deliberate
    and matches the listing parser, but the CALLER must treat an empty list
    as a failure to be reported rather than as "this product has no sizes":
    fail loudly is the rule this codebase keeps relearning.
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks = _ld_blocks(soup)

    group = _first_of_type(blocks, "ProductGroup")
    if group is None:
        logger.warning("No ProductGroup in the JSON-LD of %s — a detail page "
                       "publishes one, so this is a challenge page, a 404, or "
                       "the markup has changed. (%d ld+json block(s) found.)",
                       url, len(blocks))
        return []

    title = group.get("name")
    brand = _brand(group)
    colour = group.get("color")
    product_id = group.get("productGroupID") or group.get("sku")
    images = _image_urls(group)
    comp = composition(soup)
    cat = category or _category(blocks)

    variants = group.get("hasVariant") or []
    if isinstance(variants, dict):
        variants = [variants]
    if not variants:
        # A product with no size axis at all. Not seen in the captures, but
        # legal, and dropping it would silently lose a product rather than
        # report one with an unknown size.
        logger.info("%s publishes no hasVariant — emitting a single row with "
                    "no size.", url)
        variants = [group]

    rows: List[ProductVariant] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        offer = _offer(variant)
        price, original, currency = _prices(offer)
        availability = str(offer.get("availability") or "").rsplit("/", 1)[-1]
        variant_images = _image_urls(variant) or images

        rows.append(ProductVariant(
            url=offer.get("url") or group.get("url") or url,
            sku=variant.get("sku") or product_id,
            title=title,
            brand=brand,
            price=price,
            currency=currency,
            original_price=original,
            discount_pct=discount_pct(price, original),
            in_stock=(availability.lower() in _IN_STOCK
                      if availability else None),
            image_url=variant_images[0] if variant_images else None,
            category=cat,
            # Not "jsonld" as on a listing: the figure here comes from the
            # VARIANT's own price chain, which is a stronger statement than
            # the listing's single mid-chain number. Naming it differently
            # keeps diff_runs from comparing the two as if they were alike.
            price_source="jsonld-variant",
            product_id=product_id,
            size=_size(variant),
            color=colour,
            composition=comp,
            image_urls=" | ".join(images) or None,
        ))

    return rows
