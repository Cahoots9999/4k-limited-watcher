import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# 4K LIMITED EDITION WATCHER v5
# ============================================================

VERSION = "5.0"

DATA_FILE = Path("data/products.json")
OUTPUT_DIR = Path("public")
FEED_FILE = OUTPUT_DIR / "4k-limited.xml"

USER_AGENT = (
    "Mozilla/5.0 (compatible; 4K-Limited-Watcher/5.0)"
)

REQUEST_TIMEOUT = 12

IMUSIC_URL = "https://imusic.se/movies"

IMUSIC_WORKERS = 10
MAX_IMUSIC_PRODUCTS = 500

# Ginza intentionally disabled for now.
GINZA_ENABLED = False


# ============================================================
# EDITION RULES
# ============================================================

# These indicate that the product is actually a limited /
# collector type edition.
#
# IMPORTANT:
# "Steelbook" by itself is NOT enough.
#
# Accepted examples:
#   Limited Edition
#   Limited Steelbook
#   Deluxe Limited Edition
#   Collector's Edition
#   Collector's Steelbook
#   Limited Collector's Edition
#
# Rejected examples:
#   Steelbook edition
#   Standard edition
#   Nordic edition
#   Photocards edition
#   2026 Mixes edition

LIMITED_PATTERNS = [
    r"\blimited\s+edition\b",
    r"\blimited\s+steelbook\b",
    r"\blimited\s+collector'?s?\s+edition\b",
    r"\blimited\s+collector'?s?\s+steelbook\b",
    r"\bdeluxe\s+limited\s+edition\b",
    r"\bdeluxe\s+limited\s+steelbook\b",
]

COLLECTOR_PATTERNS = [
    r"\bcollector'?s?\s+edition\b",
    r"\bcollector'?s?\s+steelbook\b",
    r"\bcollector'?s?\s+edition\s+steelbook\b",
]


# ============================================================
# HTTP
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": USER_AGENT,
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
# 4K DETECTION
# ============================================================

def is_4k_text(text):
    text = clean(text).lower()

    patterns = [
        r"\b4k\s+ultra\s+hd\b",
        r"\b4k\s+uhd\b",
        r"\b4k\b",
        r"\bultra\s+hd\b",
        r"\buhd\s+blu-ray\b",
    ]

    return any(
        re.search(
            pattern,
            text,
        )
        for pattern in patterns
    )


# ============================================================
# LIMITED / COLLECTOR DETECTION
# ============================================================

def edition_is_limited(text):
    """
    Returns True only when the text explicitly indicates
    a limited or collector edition.

    Ordinary Steelbook is intentionally NOT enough.
    """

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
    """
    Returns a human-readable reason for why the edition matched.
    """

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
# TITLE DETECTION
# ============================================================

def get_title_candidates(soup):
    candidates = []

    selectors = [
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
    ]

    for selector in selectors:
        tag = soup.select_one(
            selector
        )

        if tag:
            content = clean(
                tag.get(
                    "content",
                    "",
                )
            )

            if content:
                candidates.append(
                    content
                )

    for tag in soup.find_all("h1"):
        text = clean(
            tag.get_text(
                " ",
                strip=True,
            )
        )

        if text:
            candidates.append(
                text
            )

    if soup.title:
        title = clean(
            soup.title.get_text()
        )

        if title:
            candidates.append(
                title
            )

    unique = []

    for candidate in candidates:
        if candidate not in unique:
            unique.append(
                candidate
            )

    return unique


def get_product_title(soup):
    candidates = get_title_candidates(
        soup
    )

    if candidates:
        return candidates[0]

    return "Unknown title"


# ============================================================
# PRODUCT PAGE INFORMATION
# ============================================================

def find_media_text(soup):
    parts = []

    # Table rows.
    for row in soup.find_all("tr"):
        text = clean(
            row.get_text(
                " ",
                strip=True,
            )
        )

        lower = text.lower()

        if (
            "media" in lower
            or "format" in lower
            or "4k ultra hd" in lower
            or "4k uhd" in lower
        ):
            parts.append(
                text
            )

    # Elements containing explicit 4K information.
    for element in soup.find_all(
        string=re.compile(
            r"4K|Ultra HD|UHD",
            re.IGNORECASE,
        )
    ):
        text = clean(
            element.parent.get_text(
                " ",
                strip=True,
            )
            if element.parent
            else str(element)
        )

        if text:
            parts.append(
                text
            )

    return clean(
        " ".join(parts)
    )


def product_is_4k(
    soup,
    title,
    url,
    page_text,
):
    if is_4k_text(
        title
    ):
        return True

    media_text = find_media_text(
        soup
    )

    if is_4k_text(
        media_text
    ):
        return True

    # URL fallback.
    url_lower = url.lower()

    if (
        "4k-ultra-hd" in url_lower
        or "4k-uhd" in url_lower
        or "/4k/" in url_lower
    ):
        return True

    # Last-resort page check.
    # We require actual 4K wording somewhere on the page.
    if is_4k_text(
        page_text
    ):
        return True

    return False


# ============================================================
# PREORDER / AVAILABILITY DETECTION
# ============================================================

def detect_preorder(soup, page_text):
    """
    Determine whether the product is currently available
    for preorder.

    We deliberately distinguish this from merely being a
    future release.

    The function checks visible page text and common button/
    availability elements.
    """

    lower = page_text.lower()

    # Strong positive indicators.
    positive_patterns = [
        r"\bpre-?order\b",
        r"\bpre order\b",
        r"\bförbeställ\b",
        r"\bförboka\b",
        r"\bförhandsbeställ\b",
        r"\bbeställ nu\b",
        r"\bboka nu\b",
    ]

    for pattern in positive_patterns:
        if re.search(
            pattern,
            lower,
            re.IGNORECASE,
        ):
            return True

    # Inspect buttons and links specifically.
    for tag in soup.find_all(
        ["button", "a", "input"]
    ):
        text = clean(
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

        combined = (
            f"{text} {value}"
        ).lower()

        for pattern in positive_patterns:
            if re.search(
                pattern,
                combined,
                re.IGNORECASE,
            ):
                return True

    return False


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

    # YYYY-MM-DD
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

    # DD/MM/YYYY
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
        r"release\s*date\s*[:\-]?\s*(.{1,50})",
        r"release\s*[:\-]?\s*(.{1,50})",
        r"releasedatum\s*[:\-]?\s*(.{1,50})",
        r"utgivningsdatum\s*[:\-]?\s*(.{1,50})",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            date = parse_date(
                match.group(1)
            )

            if date:
                return date

    return parse_date(
        text
    )


# ============================================================
# EAN / PRICE
# ============================================================

def extract_ean(text):
    patterns = [
        r"EAN/UPC\s*[:\-]?\s*([0-9]{8,14})",
        r"\bEAN\s*[:\-]?\s*([0-9]{8,14})",
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


# ============================================================
# iMUSIC PRODUCT PARSER
# ============================================================

def parse_imusic_product(url):
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

        # ----------------------------------------------------
        # 1. Must actually be 4K.
        # ----------------------------------------------------

        if not product_is_4k(
            soup,
            title,
            url,
            page_text,
        ):
            return None

        # ----------------------------------------------------
        # 2. Must actually be Limited / Collector.
        # ----------------------------------------------------

        if not edition_is_limited(
            title
        ):
            # The title is the strongest source, but some
            # sites put edition information elsewhere.
            #
            # Only use page text if it is close to the product
            # metadata. This prevents generic site text from
            # turning ordinary Steelbooks into matches.
            edition_text = ""

            for tag in soup.find_all(
                ["h1", "h2", "h3", "li", "tr"]
            ):
                text = clean(
                    tag.get_text(
                        " ",
                        strip=True,
                    )
                )

                if edition_is_limited(
                    text
                ):
                    edition_text = text
                    break

            if not edition_text:
                return None

        else:
            edition_text = title

        # ----------------------------------------------------
        # 3. Current preorder status.
        # ----------------------------------------------------

        is_preorder = detect_preorder(
            soup,
            page_text,
        )

        # ----------------------------------------------------
        # 4. Release date.
        # ----------------------------------------------------

        release_date = (
            extract_release_date(
                page_text
            )
        )

        # ----------------------------------------------------
        # 5. Build product.
        # ----------------------------------------------------

        now = datetime.now(
            timezone.utc
        ).isoformat()

        return {
            "source": "iMusic",
            "title": title,
            "url": url,
            "ean": extract_ean(
                page_text
            ),
            "release_date": (
                release_date.strftime(
                    "%Y-%m-%d"
                )
                if release_date
                else ""
            ),
            "price": extract_price(
                page_text
            ),
            "is_4k": True,
            "is_limited": True,
            "edition_type": edition_reason(
                edition_text
            ),
            "is_preorder": is_preorder,
            "last_seen": now,
            "currently_available": is_preorder,
        }

    except Exception as exc:
        print(
            f"iMusic product error "
            f"{url}: {exc}"
        )

        return None


# ============================================================
# iMUSIC DISCOVERY
# ============================================================

def looks_like_imusic_product(url):
    return (
        "/movies/"
        in url.lower()
    )


def discover_imusic_urls():
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

    urls = set()

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

        urls.add(
            url
        )

    urls = sorted(
        urls
    )

    print(
        f"iMusic product URLs found: "
        f"{len(urls)}"
    )

    return urls[
        :MAX_IMUSIC_PRODUCTS
    ]


# ============================================================
# iMUSIC CHECK
# ============================================================

def parse_imusic(urls):
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
                url,
            ): url
            for url in urls
        }

        for future in as_completed(
            futures
        ):
            completed += 1

            try:
                product = (
                    future.result()
                )

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
    # Deduplicate.
    # --------------------------------------------------------

    unique = {}

    for product in results:
        pid = product_id(
            product
        )

        unique[pid] = product

    results = list(
        unique.values()
    )

    print(
        f"iMusic relevant products: "
        f"{len(results)}"
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
        f"iMusic currently preorderable: "
        f"{preorder_count}"
    )

    return results


# ============================================================
# PRODUCT ID
# ============================================================

def product_id(product):
    """
    Prefer EAN because the URL/title can change.

    Fall back to URL if no EAN is available.
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
# DATABASE MERGE
# ============================================================

def merge_products(
    state,
    discovered,
):
    now = datetime.now(
        timezone.utc
    ).isoformat()

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

        old_preorder_first_seen = (
            old.get(
                "preorder_first_seen",
                "",
            )
        )

        old_notified = bool(
            old.get(
                "preorder_notified",
                False,
            )
        )

        # Update product data.
        old.update(
            product
        )

        old[
            "first_seen"
        ] = old_first_seen

        # ----------------------------------------------------
        # TRANSITION:
        #
        # Was NOT preorderable
        # ->
        # IS preorderable
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

            # This is the important event.
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

            print(
                "STILL PREORDERABLE: "
                f"{old['title']}"
            )

        # ----------------------------------------------------
        # NO LONGER PREORDERABLE
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
    # Products that disappeared from the current catalogue.
    #
    # We DON'T delete them from the database.
    # We simply remove them from RSS.
    # --------------------------------------------------------

    disappeared = 0

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

                disappeared += 1

                print(
                    "REMOVED FROM RSS: "
                    f"{product.get('title', 'Unknown')}"
                )

    if disappeared:
        print(
            f"Products removed because "
            f"they disappeared: "
            f"{disappeared}"
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
    source = xml_escape(
        product.get(
            "source",
            "",
        )
    )

    price = xml_escape(
        product.get(
            "price",
            "",
        )
    )

    release = xml_escape(
        product.get(
            "release_date",
            "",
        )
    )

    edition_type = xml_escape(
        product.get(
            "edition_type",
            "",
        )
    )

    return (
        f"<strong>{source}</strong><br>"
        f"4K Ultra HD<br>"
        f"Limited / Collector edition"
        f"<br>"
        f"Typ: {edition_type}"
        f"<br>"
        f"Förbokning aktiv"
        f"<br>"
        f"Release: "
        f"{release or 'Ej angivet'}"
        f"<br>"
        f"Pris: "
        f"{price or 'Ej angivet'}"
    )


def make_feed(state):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    items = []

    for product in state.values():

        # RSS ONLY contains active preorders.
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

        if product.get(
            "source"
        ) != "iMusic":
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

        items.append(
            (
                dt,
                product,
            )
        )

    items.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    now = datetime.now(
        timezone.utc
    )

    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        (
            "<title>"
            "4K Limited Editions – "
            "iMusic"
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

    for dt, product in items:

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

        description = (
            make_description(
                product
            )
        )

        pub_date = dt.strftime(
            "%a, %d %b %Y "
            "%H:%M:%S GMT"
        )

        chunks.extend(
            [
                "<item>",
                (
                    f"<title>"
                    f"{title}"
                    f"</title>"
                ),
                (
                    f"<link>"
                    f"{url}"
                    f"</link>"
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
                    f"</pubDate>"
                ),
                "</item>",
            ]
        )

    chunks.extend(
        [
            "</channel>",
            "</rss>",
        ]
    )

    FEED_FILE.write_text(
        "\n".join(
            chunks
        ),
        encoding="utf-8",
    )

    print(
        f"RSS items: {len(items)}"
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
    # iMUSIC DISCOVERY
    # --------------------------------------------------------

    imusic_urls = (
        discover_imusic_urls()
    )

    # --------------------------------------------------------
    # iMUSIC PRODUCT CHECK
    # --------------------------------------------------------

    imusic_products = (
        parse_imusic(
            imusic_urls
        )
    )

    # --------------------------------------------------------
    # CURRENT DISCOVERY
    # --------------------------------------------------------

    print("")
    print(
        "================================"
    )

    print(
        f"Relevant products discovered: "
        f"{len(imusic_products)}"
    )

    # --------------------------------------------------------
    # MERGE / STATUS CHANGES
    # --------------------------------------------------------

    state, new_preorders = (
        merge_products(
            state,
            imusic_products,
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
