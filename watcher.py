import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# 4K LIMITED EDITION WATCHER v5.1
# ============================================================

VERSION = "5.1"

DATA_FILE = Path("data/products.json")
OUTPUT_DIR = Path("public")
FEED_FILE = OUTPUT_DIR / "4k-limited.xml"

REQUEST_TIMEOUT = 12
IMUSIC_URL = "https://imusic.se/movies"

IMUSIC_WORKERS = 10
MAX_IMUSIC_PRODUCTS = 500

# Ginza is intentionally disabled for now.
GINZA_ENABLED = False


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 "
            "(compatible; 4K-Limited-Watcher/5.1)"
        ),
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
)


def get_html(url, timeout=REQUEST_TIMEOUT):
    response = session.get(
        url,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.text


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean(text):
    return re.sub(
        r"\s+",
        " ",
        text or "",
    ).strip()


def normalize_url(url):
    parsed = urlparse(url)

    return parsed._replace(
        fragment=""
    ).geturl()


def absolute_url(base, href):
    return normalize_url(
        urljoin(base, href)
    )


def text_from_soup(soup):
    return clean(
        soup.get_text(
            " ",
            strip=True,
        )
    )


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def load_state():
    if not DATA_FILE.exists():
        return {}

    try:
        return json.loads(
            DATA_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception as exc:
        print(
            f"Could not load database: {exc}"
        )
        return {}


def save_state(state):
    DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    DATA_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# PRODUCT TITLE
# ============================================================

def get_product_title(soup):
    candidates = []

    for selector in [
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
    ]:
        tag = soup.select_one(
            selector
        )

        if tag:
            value = clean(
                tag.get(
                    "content",
                    "",
                )
            )

            if value:
                candidates.append(
                    value
                )

    for tag in soup.find_all("h1"):
        value = clean(
            tag.get_text(
                " ",
                strip=True,
            )
        )

        if value:
            candidates.append(
                value
            )

    if soup.title:
        value = clean(
            soup.title.get_text()
        )

        if value:
            candidates.append(
                value
            )

    if candidates:
        return candidates[0]

    return "Unknown title"


# ============================================================
# EDITION DETECTION
# ============================================================

# IMPORTANT:
#
# Ordinary "Steelbook edition" is NOT enough.
#
# Accepted:
#   Limited edition
#   Limited Steelbook
#   Deluxe Limited Edition
#   Collector's edition
#   Collectors edition
#   Ultimate Collector's Steelbook
#
# Rejected:
#   Steelbook edition
#   Standard edition
#   Nordic edition
#   Photocards edition
#   2026 Mixes edition
#
# The text passed to this function must describe the CURRENT
# PRODUCT, not the whole website.

LIMITED_PATTERNS = [
    r"\blimited\s+edition\b",
    r"\blimited\s+steelbook\b",
    r"\bdeluxe\s+limited\s+edition\b",
    r"\bdeluxe\s+limited\s+steelbook\b",
    r"\blimited\s+collector'?s?\s+edition\b",
    r"\blimited\s+collector'?s?\s+steelbook\b",
]

COLLECTOR_PATTERNS = [
    r"\bcollector'?s?\s+edition\b",
    r"\bcollectors\s+edition\b",
    r"\bcollector'?s?\s+steelbook\b",
    r"\bultimate\s+collector'?s?\s+steelbook\b",
]


def edition_is_limited(text):
    text = clean(text).lower()

    for pattern in LIMITED_PATTERNS:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return True

    for pattern in COLLECTOR_PATTERNS:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return True

    return False


def edition_reason(text):
    text = clean(text).lower()

    for pattern in LIMITED_PATTERNS:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return "limited"

    for pattern in COLLECTOR_PATTERNS:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return "collector"

    return ""


# ============================================================
# FORMAT DETECTION
# ============================================================

def format_is_4k(text):
    """
    Detect 4K only from product-specific text.
    """

    text = clean(text).lower()

    patterns = [
        r"\b4k\s+ultra\s+hd\b",
        r"\b4k\s+uhd\b",
        r"\bultra\s+hd\b",
        r"\buhd\b",
    ]

    return any(
        re.search(
            pattern,
            text,
            re.IGNORECASE,
        )
        for pattern in patterns
    )


def format_is_blu_ray_only(text):
    text = clean(text).lower()

    if re.search(
        r"\bblu[\s-]?ray\b",
        text,
        re.IGNORECASE,
    ) and not format_is_4k(
        text
    ):
        return True

    return False


def format_is_dvd_only(text):
    text = clean(text).lower()

    if (
        re.search(
            r"\bdvd\b",
            text,
            re.IGNORECASE,
        )
        and not format_is_4k(text)
    ):
        return True

    return False


# ============================================================
# PRODUCT-SPECIFIC METADATA
# ============================================================

def get_meta_content(soup, property_name):
    tag = soup.find(
        "meta",
        attrs={
            "property": property_name
        },
    )

    if not tag:
        tag = soup.find(
            "meta",
            attrs={
                "name": property_name
            },
        )

    if not tag:
        return ""

    return clean(
        tag.get(
            "content",
            "",
        )
    )


def extract_product_metadata(soup):
    """
    Try to collect text that belongs to the current product.

    We deliberately do NOT use the complete page text here.
    """

    parts = []

    # Product title.
    title = get_product_title(
        soup
    )

    if title:
        parts.append(
            title
        )

    # OpenGraph description.
    description = get_meta_content(
        soup,
        "og:description",
    )

    if description:
        parts.append(
            description
        )

    # Product structured data.
    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        raw = script.string

        if not raw:
            continue

        try:
            data = json.loads(
                raw
            )
        except Exception:
            continue

        objects = (
            data
            if isinstance(data, list)
            else [data]
        )

        for obj in objects:
            if not isinstance(
                obj,
                dict,
            ):
                continue

            for key in [
                "name",
                "description",
                "category",
                "sku",
                "mpn",
            ]:
                value = obj.get(
                    key
                )

                if isinstance(
                    value,
                    str,
                ):
                    parts.append(
                        clean(value)
                    )

    return clean(
        " ".join(parts)
    )


# ============================================================
# DISCOVERY CARD TEXT
# ============================================================

def find_product_card(anchor):
    """
    Find a reasonably sized parent around a product link.

    This is used to obtain iMusic's own product-card text,
    where it commonly writes things like:

      4K Ultra HD Limited Steelbook edition
      4K Ultra HD Collector's edition
      Releasedatum ...
    """

    current = anchor

    for _ in range(6):
        if current is None:
            break

        text = clean(
            current.get_text(
                " ",
                strip=True,
            )
        )

        if 20 <= len(text) <= 1500:
            return text

        current = current.parent

    return ""


# ============================================================
# RELEASE DATE
# ============================================================

SWEDISH_MONTHS = {
    "januari": 1,
    "februari": 2,
    "mars": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "augusti": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}


def parse_date(text):
    if not text:
        return None

    text = clean(
        text
    ).lower()

    match = re.search(
        r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b",
        text,
    )

    if match:
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    match = re.search(
        r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b",
        text,
    )

    if match:
        try:
            return datetime(
                int(match.group(3)),
                int(match.group(2)),
                int(match.group(1)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    months = "|".join(
        SWEDISH_MONTHS.keys()
    )

    match = re.search(
        rf"\b(\d{{1,2}})\s+({months})\s+(20\d{{2}})\b",
        text,
    )

    if match:
        try:
            return datetime(
                int(match.group(3)),
                SWEDISH_MONTHS[
                    match.group(2)
                ],
                int(match.group(1)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    return None


def extract_release_date(text):
    patterns = [
        r"releasedatum\s*[:\-]?\s*(.{1,60})",
        r"release\s*date\s*[:\-]?\s*(.{1,60})",
        r"release\s*[:\-]?\s*(.{1,60})",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            result = parse_date(
                match.group(1)
            )

            if result:
                return result

    return None


# ============================================================
# PREORDER DETECTION
# ============================================================

def has_positive_preorder_text(text):
    text = clean(
        text
    ).lower()

    patterns = [
        r"\bpre[\s-]?order\b",
        r"\bpre[\s-]?beställ\b",
        r"\bförbeställ\b",
        r"\bförbeställning\b",
        r"\bförboka\b",
        r"\bförhandsbeställ\b",
    ]

    return any(
        re.search(
            pattern,
            text,
            re.IGNORECASE,
        )
        for pattern in patterns
    )


def has_purchase_cta(soup):
    """
    Look for actual product purchase controls.

    We intentionally do NOT treat arbitrary words such as
    "order" in descriptions as proof of preorder availability.
    """

    purchase_patterns = [
        r"\bpre[\s-]?order\b",
        r"\bpre[\s-]?beställ\b",
        r"\bförbeställ\b",
        r"\bförboka\b",
        r"\badd\s+to\s+cart\b",
        r"\badd\s+to\s+basket\b",
        r"\blägg\s+i\s+korg\b",
        r"\bköp\b",
        r"\bbeställ\b",
    ]

    for tag in soup.find_all(
        [
            "button",
            "a",
            "input",
        ]
    ):
        visible = clean(
            tag.get_text(
                " ",
                strip=True,
            )
        )

        value = clean(
            tag.get(
                "value",
                "",
            )
        )

        aria = clean(
            tag.get(
                "aria-label",
                "",
            )
        )

        combined = (
            f"{visible} "
            f"{value} "
            f"{aria}"
        ).lower()

        for pattern in purchase_patterns:
            if re.search(
                pattern,
                combined,
                re.IGNORECASE,
            ):
                return True

    return False


def detect_preorder(
    soup,
    page_text,
    release_date,
    card_text,
):
    """
    Determine whether the item is currently preorderable.

    Priority:

    1. Explicit preorder wording.
    2. Future release date + actual purchase CTA.
    3. Otherwise false.

    This prevents ordinary catalogue products from being
    interpreted as preorders merely because the page contains
    the word "order".
    """

    combined = clean(
        f"{card_text} {page_text}"
    )

    # Strongest signal.
    if has_positive_preorder_text(
        combined
    ):
        return True

    # If a future release date exists and there is a genuine
    # purchase control, this is very likely a preorder.
    if release_date:

        today = datetime.now(
            timezone.utc
        ).date()

        if release_date.date() > today:
            if has_purchase_cta(
                soup
            ):
                return True

    return False


# ============================================================
# PRICE / EAN
# ============================================================

def extract_price(text):
    patterns = [
        r"SEK\s*([0-9][0-9.,]*)",
        r"([0-9][0-9.,]*)\s*SEK",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return (
                f"{match.group(1)} kr"
            )

    return ""


def extract_ean(text):
    patterns = [
        r"\bEAN\s*[:\-]?\s*([0-9]{8,14})",
        r"\bEAN/UPC\s*[:\-]?\s*([0-9]{8,14})",
        r"\bUPC\s*[:\-]?\s*([0-9]{8,14})",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return match.group(1)

    return ""


# ============================================================
# iMUSIC DISCOVERY
# ============================================================

def looks_like_imusic_product(url):
    return (
        "/movies/"
        in url.lower()
    )


def discover_imusic_products():
    print("")
    print(
        "====== iMUSIC DISCOVERY ======"
    )

    try:
        html = get_html(
            IMUSIC_URL
        )
    except Exception as exc:
        print(
            f"iMusic catalogue failed: "
            f"{exc}"
        )
        return []

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    products = {}

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = anchor.get(
            "href"
        )

        if not href:
            continue

        url = absolute_url(
            IMUSIC_URL,
            href,
        )

        if not looks_like_imusic_product(
            url
        ):
            continue

        card_text = find_product_card(
            anchor
        )

        if not card_text:
            continue

        products[url] = {
            "url": url,
            "card_text": card_text,
        }

    result = list(
        products.values()
    )

    result.sort(
        key=lambda item: item["url"]
    )

    result = result[
        :MAX_IMUSIC_PRODUCTS
    ]

    print(
        f"iMusic product URLs found: "
        f"{len(result)}"
    )

    return result


# ============================================================
# iMUSIC PRODUCT PARSER
# ============================================================

def parse_imusic_product(item):
    url = item["url"]
    card_text = item.get(
        "card_text",
        "",
    )

    try:
        html = get_html(
            url
        )

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        page_text = text_from_soup(
            soup
        )

        title = get_product_title(
            soup
        )

        metadata = extract_product_metadata(
            soup
        )

        # ----------------------------------------------------
        # FORMAT
        # ----------------------------------------------------
        #
        # We use the product card + product metadata.
        # We NEVER use arbitrary unrelated page text to decide
        # whether this is 4K.
        #

        product_format_text = clean(
            f"{card_text} "
            f"{metadata}"
        )

        if not format_is_4k(
            product_format_text
        ):
            return None

        # Explicitly reject obvious Blu-ray/DVD-only products.
        if format_is_blu_ray_only(
            product_format_text
        ):
            return None

        if format_is_dvd_only(
            product_format_text
        ):
            return None

        # ----------------------------------------------------
        # LIMITED / COLLECTOR
        # ----------------------------------------------------
        #
        # Again, use product-specific metadata only.
        #

        edition_text = clean(
            f"{card_text} "
            f"{metadata}"
        )

        if not edition_is_limited(
            edition_text
        ):
            return None

        reason = edition_reason(
            edition_text
        )

        # ----------------------------------------------------
        # RELEASE DATE
        # ----------------------------------------------------

        release_date = (
            extract_release_date(
                card_text
            )
        )

        if not release_date:
            release_date = (
                extract_release_date(
                    page_text
                )
            )

        # ----------------------------------------------------
        # PREORDER
        # ----------------------------------------------------

        is_preorder = detect_preorder(
            soup,
            page_text,
            release_date,
            card_text,
        )

        # ----------------------------------------------------
        # PRODUCT DATA
        # ----------------------------------------------------

        return {
            "source": "iMusic",
            "title": title,
            "url": url,
            "ean": extract_ean(
                metadata
            ),
            "release_date": (
                release_date.strftime(
                    "%Y-%m-%d"
                )
                if release_date
                else ""
            ),
            "price": extract_price(
                card_text
            ),
            "is_4k": True,
            "is_limited": True,
            "edition_type": reason,
            "is_preorder": is_preorder,
            "last_seen": now_iso(),
            "currently_available": is_preorder,
        }

    except Exception as exc:
        print(
            f"iMusic product error "
            f"{url}: {exc}"
        )

        return None


# ============================================================
# iMUSIC CHECK
# ============================================================

def parse_imusic(items):
    print("")
    print(
        "======== iMUSIC CHECK ========"
    )

    results = []
    completed = 0

    with ThreadPoolExecutor(
        max_workers=IMUSIC_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                parse_imusic_product,
                item,
            ): item
            for item in items
        }

        for future in as_completed(
            futures
        ):
            completed += 1

            try:
                product = future.result()

                if product:
                    results.append(
                        product
                    )

            except Exception as exc:
                print(
                    f"iMusic parser error: "
                    f"{exc}"
                )

            if (
                completed % 25 == 0
                or completed == len(futures)
            ):
                print(
                    f"iMusic checked "
                    f"{completed}/"
                    f"{len(futures)}"
                )

    # --------------------------------------------------------
    # Deduplicate by URL first.
    # --------------------------------------------------------

    by_url = {}

    for product in results:
        url = normalize_url(
            product["url"]
        )

        by_url[url] = product

    results = list(
        by_url.values()
    )

    preorder_count = sum(
        1
        for product in results
        if product.get(
            "is_preorder",
            False,
        )
    )

    print(
        f"iMusic relevant products: "
        f"{len(results)}"
    )

    print(
        f"iMusic currently preorderable: "
        f"{preorder_count}"
    )

    return results


# ============================================================
# PRODUCT ID
# ============================================================

def normalize_title(title):
    title = clean(
        title
    ).lower()

    # Remove common non-identity information.
    title = re.sub(
        r"\[[^\]]+\]",
        "",
        title,
    )

    title = re.sub(
        r"\([^)]*4k[^)]*\)",
        "",
        title,
        flags=re.IGNORECASE,
    )

    title = re.sub(
        r"\b(4k|uhd|ultra hd)\b",
        "",
        title,
        flags=re.IGNORECASE,
    )

    return clean(
        title
    )


def product_id(product):
    """
    Prefer EAN.

    Otherwise use normalized URL. This is safer than trying
    to merge products solely from their title because iMusic
    can have multiple editions of the same film.
    """

    ean = clean(
        product.get(
            "ean",
            "",
        )
    )

    if ean:
        return (
            f"{product['source']}:EAN:{ean}"
        )

    return (
        f"{product['source']}:URL:"
        f"{normalize_url(product['url'])}"
    )


# ============================================================
# DATABASE / STATUS TRANSITIONS
# ============================================================

def merge_products(
    state,
    discovered,
):
    now = now_iso()

    new_preorders = []

    current_ids = set()

    print("")
    print(
        "======== STATUS CHANGES ========"
    )

    for product in discovered:

        pid = product_id(
            product
        )

        current_ids.add(
            pid
        )

        current_preorder = bool(
            product.get(
                "is_preorder",
                False,
            )
        )

        # ----------------------------------------------------
        # NEW PRODUCT
        # ----------------------------------------------------

        if pid not in state:

            product[
                "first_seen"
            ] = now

            product[
                "preorder_first_seen"
            ] = (
                now
                if current_preorder
                else ""
            )

            product[
                "published"
            ] = current_preorder

            product[
                "preorder_notified"
            ] = current_preorder

            state[pid] = product

            if current_preorder:

                new_preorders.append(
                    product
                )

                print(
                    "NEW PREORDER: "
                    f"{product['title']}"
                )

            else:

                print(
                    "NEW UPCOMING: "
                    f"{product['title']}"
                )

            continue

        # ----------------------------------------------------
        # EXISTING PRODUCT
        # ----------------------------------------------------

        old = state[pid]

        old_preorder = bool(
            old.get(
                "is_preorder",
                False,
            )
        )

        old_first_seen = old.get(
            "first_seen",
            now,
        )

        old_notified = bool(
            old.get(
                "preorder_notified",
                False,
            )
        )

        old_preorder_first_seen = (
            old.get(
                "preorder_first_seen",
                "",
            )
        )

        old.update(
            product
        )

        old[
            "first_seen"
        ] = old_first_seen

        # ----------------------------------------------------
        # UPCOMING -> PREORDER
        # ----------------------------------------------------

        if (
            not old_preorder
            and current_preorder
        ):

            old[
                "preorder_first_seen"
            ] = now

            old[
                "published"
            ] = True

            if not old_notified:

                old[
                    "preorder_notified"
                ] = True

                new_preorders.append(
                    old
                )

                print(
                    "NEW PREORDER: "
                    f"{old['title']}"
                )

            else:

                print(
                    "PREORDER ACTIVE: "
                    f"{old['title']}"
                )

        # ----------------------------------------------------
        # STILL PREORDERABLE
        # ----------------------------------------------------

        elif current_preorder:

            old[
                "published"
            ] = True

            old[
                "preorder_first_seen"
            ] = (
                old_preorder_first_seen
                or now
            )

            print(
                "STILL PREORDERABLE: "
                f"{old['title']}"
            )

        # ----------------------------------------------------
        # STILL UPCOMING
        # ----------------------------------------------------

        else:

            old[
                "published"
            ] = False

            print(
                "NOT YET PREORDERABLE: "
                f"{old['title']}"
            )

    # --------------------------------------------------------
    # Products no longer discovered.
    #
    # Keep history but remove from RSS.
    # --------------------------------------------------------

    for pid, product in state.items():

        if product.get(
            "source"
        ) != "iMusic":
            continue

        if pid not in current_ids:

            if product.get(
                "published",
                False,
            ):

                product[
                    "published"
                ] = False

                print(
                    "REMOVED FROM RSS: "
                    f"{product.get('title', 'Unknown')}"
                )

    return (
        state,
        new_preorders,
    )


# ============================================================
# RSS
# ============================================================

def xml_escape(text):
    return (
        str(text)
        .replace(
            "&",
            "&amp;",
        )
        .replace(
            "<",
            "&lt;",
        )
        .replace(
            ">",
            "&gt;",
        )
        .replace(
            '"',
            "&quot;",
        )
        .replace(
            "'",
            "&apos;",
        )
    )


def make_description(product):
    return (
        f"<strong>iMusic</strong><br>"
        f"4K Ultra HD<br>"
        f"Limited / Collector edition<br>"
        f"Typ: "
        f"{xml_escape(product.get('edition_type', ''))}"
        f"<br>"
        f"Förbokning aktiv<br>"
        f"Release: "
        f"{xml_escape(product.get('release_date', '') or 'Ej angivet')}"
        f"<br>"
        f"Pris: "
        f"{xml_escape(product.get('price', '') or 'Ej angivet')}"
    )


def make_feed(state):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    products = []

    for product in state.values():

        if product.get(
            "source"
        ) != "iMusic":
            continue

        if not product.get(
            "published",
            False,
        ):
            continue

        if not product.get(
            "is_preorder",
            False,
        ):
            continue

        if not product.get(
            "is_4k",
            False,
        ):
            continue

        if not product.get(
            "is_limited",
            False,
        ):
            continue

        timestamp = (
            product.get(
                "preorder_first_seen"
            )
            or product.get(
                "first_seen"
            )
        )

        if not timestamp:
            continue

        try:
            dt = datetime.fromisoformat(
                timestamp.replace(
                    "Z",
                    "+00:00",
                )
            )
        except Exception:
            continue

        products.append(
            (
                dt,
                product,
            )
        )

    products.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    now = datetime.now(
        timezone.utc
    )

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        (
            "<title>"
            "4K Limited Editions – iMusic"
            "</title>"
        ),
        (
            "<link>"
            "https://imusic.se/movies"
            "</link>"
        ),
        (
            "<description>"
            "Aktuella förbokningar av "
            "4K Limited och Collector "
            "Editions från iMusic."
            "</description>"
        ),
        (
            "<lastBuildDate>"
            + now.strftime(
                "%a, %d %b %Y "
                "%H:%M:%S GMT"
            )
            + "</lastBuildDate>"
        ),
    ]

    for dt, product in products:

        title = xml_escape(
            product.get(
                "title",
                "Okänd titel",
            )
        )

        url = xml_escape(
            product.get(
                "url",
                "",
            )
        )

        guid = xml_escape(
            product_id(
                product
            )
        )

        description = make_description(
            product
        )

        pub_date = dt.strftime(
            "%a, %d %b %Y "
            "%H:%M:%S GMT"
        )

        lines.extend(
            [
                "<item>",
                (
                    f"<title>{title}</title>"
                ),
                (
                    f"<link>{url}</link>"
                ),
                (
                    '<guid isPermaLink="false">'
                    f"{guid}"
                    "</guid>"
                ),
                (
                    "<description>"
                    f"{description}"
                    "</description>"
                ),
                (
                    "<pubDate>"
                    f"{pub_date}"
                    "</pubDate>"
                ),
                "</item>",
            ]
        )

    lines.extend(
        [
            "</channel>",
            "</rss>",
        ]
    )

    FEED_FILE.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    print(
        f"RSS items: "
        f"{len(products)}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print(
        "=" * 60
    )

    print(
        f"4K LIMITED EDITION WATCHER v{VERSION}"
    )

    print(
        "=" * 60
    )

    state = load_state()

    print(
        f"Existing database entries: "
        f"{len(state)}"
    )

    # --------------------------------------------------------
    # GINZA
    # --------------------------------------------------------

    if GINZA_ENABLED:
        print(
            "Ginza enabled."
        )
    else:
        print(
            "Ginza: skipped "
            "(handled later)."
        )

    # --------------------------------------------------------
    # DISCOVERY
    # --------------------------------------------------------

    discovery = (
        discover_imusic_products()
    )

    # --------------------------------------------------------
    # PRODUCT CHECK
    # --------------------------------------------------------

    products = parse_imusic(
        discovery
    )

    print("")
    print(
        "================================"
    )

    print(
        f"Relevant products discovered: "
        f"{len(products)}"
    )

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    state, new_preorders = (
        merge_products(
            state,
            products,
        )
    )

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    save_state(
        state
    )

    # --------------------------------------------------------
    # RSS
    # --------------------------------------------------------

    make_feed(
        state
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print("")
    print(
        "=" * 60
    )

    print(
        f"NEW PREORDERS THIS RUN: "
        f"{len(new_preorders)}"
    )

    for product in new_preorders:
        print(
            "  -> "
            f"{product['title']}"
        )

    print(
        f"Database size: "
        f"{len(state)}"
    )

    print(
        "=" * 60
    )


if __name__ == "__main__":
    main()
