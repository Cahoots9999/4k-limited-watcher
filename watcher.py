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
# CONFIG
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (compatible; 4K-Limited-Watcher/4.0)"
)

DATA_FILE = Path("data/products.json")
OUTPUT_DIR = Path("public")
FEED_FILE = OUTPUT_DIR / "4k-limited.xml"

GINZA_URLS = [
    "https://www.ginza.se/Film/Kommande/4K/546",
]

IMUSIC_URL = "https://imusic.se/movies"

IMUSIC_WORKERS = 10
MAX_IMUSIC_PRODUCTS = 500

REQUEST_TIMEOUT = 12
GINZA_RETRIES = 2

FEED_RETENTION_DAYS = 180


# ============================================================
# IMPORTANT KEYWORDS
# ============================================================

# These indicate that the actual product is a special edition.
#
# "edition" by itself is NOT enough.
# This prevents things such as "Nordic edition",
# "Photocards edition", etc. from being accepted.

SPECIAL_EDITION_KEYWORDS = [
    "limited edition",
    "limited steelbook",
    "steelbook",
    "collector's edition",
    "collectors edition",
    "collector edition",
    "collectors",
    "collector's",
    "deluxe limited edition",
    "deluxe edition",
    "limited mediabook",
    "mediabook",
]

# Optional extra wording which often occurs in actual
# limited releases.
SPECIAL_EDITION_PATTERNS = [
    r"\blimited\b",
    r"\bsteelbook\b",
    r"\bcollector(?:'s|s)?\b",
    r"\bmediabook\b",
    r"\bdeluxe\b",
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
    """
    Detect explicit 4K/UHD wording.

    We deliberately do NOT treat "Ultra HD" alone as enough
    unless it appears in a product/media context.
    """

    text = clean(text).lower()

    patterns = [
        r"\b4k ultra hd\b",
        r"\b4k uhd\b",
        r"\b4k\b",
        r"\bultra hd\b",
        r"\buhd blu-ray\b",
    ]

    return any(
        re.search(
            pattern,
            text,
        )
        for pattern in patterns
    )


# ============================================================
# EDITION DETECTION
# ============================================================

def is_special_edition(text):
    """
    IMPORTANT:
    This function is intentionally stricter than v3.

    "edition" by itself is NOT accepted.

    Examples accepted:
        Limited Edition
        Limited Steelbook
        Steelbook
        Collector's Edition
        Deluxe Edition
        Mediabook

    Examples rejected:
        Nordic edition
        Photocards edition
        Standard edition
        2026 Mixes edition
    """

    text = clean(text).lower()

    # Direct phrases first.
    for keyword in SPECIAL_EDITION_KEYWORDS:
        if keyword in text:
            return True

    # Then controlled regex patterns.
    for pattern in SPECIAL_EDITION_PATTERNS:
        if re.search(
            pattern,
            text,
            re.IGNORECASE,
        ):
            return True

    return False


# ============================================================
# PREORDER
# ============================================================

def has_preorder(text):
    text = clean(text).lower()

    preorder_words = [
        "förboka",
        "förbeställ",
        "pre-order",
        "preorder",
        "pre order",
    ]

    return any(
        word in text
        for word in preorder_words
    )


# ============================================================
# PRODUCT TITLE EXTRACTION
# ============================================================

def get_title_candidates(soup):
    """
    Return likely product-title strings.

    iMusic sometimes presents the title in slightly different
    ways, e.g.

        Suspiria Limited Edition

    or

        4k Ultra Hd
        Innerspace [limited Edition]

    We therefore inspect metadata + headings instead of relying
    on one single HTML element.
    """

    candidates = []

    # OpenGraph title.
    for selector in [
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
    ]:
        tag = soup.select_one(
            selector
        )

        if tag:
            content = clean(
                tag.get(
                    "content",
                    ""
                )
            )

            if content:
                candidates.append(
                    content
                )

    # H1 is usually the most important source.
    for tag in soup.find_all(
        "h1"
    ):
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

    # H2/H3 can contain the actual product title
    # on some iMusic pages.
    for tag in soup.find_all(
        ["h2", "h3"]
    ):

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

    # Browser title as fallback.
    if soup.title:
        title = clean(
            soup.title.get_text()
        )

        if title:
            candidates.append(
                title
            )

    # Remove duplicates while preserving order.
    unique = []

    for candidate in candidates:
        if candidate not in unique:
            unique.append(
                candidate
            )

    return unique


def find_special_edition_title(
    soup
):
    """
    Find a title which itself identifies the product as
    a special/limited edition.

    This is the key improvement over v3.
    """

    candidates = get_title_candidates(
        soup
    )

    for candidate in candidates:

        if is_special_edition(
            candidate
        ):
            return candidate

    return ""


# ============================================================
# MEDIA INFORMATION
# ============================================================

def find_media_text(soup):
    """
    Try to find the media/format section.

    iMusic product pages normally contain text similar to:

        Media | Film 4K Ultra HD
        4K UHD Blu-ray

    We use this as confirmation that the actual product
    is 4K.
    """

    media_parts = []

    # Look for labels mentioning Media.
    for element in soup.find_all(
        string=re.compile(
            r"^\s*Media\s*$",
            re.IGNORECASE,
        )
    ):

        parent = element.parent

        if parent:
            parent_text = clean(
                parent.parent.get_text(
                    " ",
                    strip=True,
                )
                if parent.parent
                else parent.get_text(
                    " ",
                    strip=True,
                )
            )

            if parent_text:
                media_parts.append(
                    parent_text
                )

    # Also inspect tables.
    for row in soup.find_all(
        "tr"
    ):

        row_text = clean(
            row.get_text(
                " ",
                strip=True,
            )
        )

        if (
            "media" in row_text.lower()
            or "4k ultra hd"
            in row_text.lower()
            or "4k uhd"
            in row_text.lower()
        ):
            media_parts.append(
                row_text
            )

    return " ".join(
        media_parts
    )


def product_is_4k(
    soup,
    title,
    url,
):
    """
    4K must be confirmed by a product-level signal.

    We accept:
      - 4K in the actual product title
      - 4K in the product URL/title metadata
      - explicit 4K media information

    We do NOT simply search the entire page for "4K",
    because related products can contain 4K references.
    """

    title_lower = title.lower()
    url_lower = url.lower()

    # Strongest signal: actual title.
    if is_4k_text(
        title_lower
    ):
        return True

    # URL generated by iMusic often contains the actual
    # format, e.g. "...-4k-ultra-hd".
    if (
        "4k-ultra-hd"
        in url_lower
        or "4k-uhd"
        in url_lower
    ):
        return True

    # Explicit Media field.
    media_text = find_media_text(
        soup
    )

    if is_4k_text(
        media_text
    ):
        return True

    return False


# ============================================================
# DATE
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

    # 19 oktober 2026
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


def extract_release_date(
    text
):
    patterns = [
        r"releasedatum\s+(.{1,50})",
        r"release\s+date\s+(.{1,50})",
        r"release\s+(.{1,50})",
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
        r"EAN/UPC\s+([0-9]{8,14})",
        r"EAN\s+([0-9]{8,14})",
        r"UPC\s+([0-9]{8,14})",
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
# iMUSIC PRODUCT
# ============================================================

def looks_like_imusic_product(
    url
):
    return (
        "/movies/"
        in url.lower()
    )


def parse_imusic_product(
    url
):
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

        # ----------------------------------------------------
        # Find the actual edition title.
        # ----------------------------------------------------

        product_title = (
            find_special_edition_title(
                soup
            )
        )

        # If there is no limited/steelbook/etc. indication
        # in the product title itself, reject it.
        if not product_title:

            return None

        # ----------------------------------------------------
        # Confirm 4K at product level.
        # ----------------------------------------------------

        if not product_is_4k(
            soup,
            product_title,
            url,
        ):

            return None

        # ----------------------------------------------------
        # Must currently be preorderable.
        # ----------------------------------------------------

        if not has_preorder(
            page_text
        ):

            return None

        # ----------------------------------------------------
        # Release date.
        # ----------------------------------------------------

        release_date = (
            extract_release_date(
                page_text
            )
        )

        # Do not include already released products.
        if release_date:

            today = datetime.now(
                timezone.utc
            ).date()

            if (
                release_date.date()
                < today
            ):

                return None

        # ----------------------------------------------------
        # Extract product information.
        # ----------------------------------------------------

        return {
            "source": "iMusic",
            "title": product_title,
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
            "is_preorder": True,
            "last_seen": (
                datetime.now(
                    timezone.utc
                ).isoformat()
            ),
        }

    except Exception:
        return None


# ============================================================
# iMUSIC DISCOVERY
# ============================================================

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

    urls = list(
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
# iMUSIC PARALLEL CHECK
# ============================================================

def parse_imusic(
    urls
):

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
                    f"iMusic product error: "
                    f"{exc}"
                )

            if (
                completed % 25 == 0
                or completed
                == len(futures)
            ):

                print(
                    f"iMusic checked "
                    f"{completed}/"
                    f"{len(futures)}"
                )

    # Remove duplicates.
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
        f"iMusic matching products: "
        f"{len(results)}"
    )

    return results


# ============================================================
# GINZA
# ============================================================

def fetch_ginza():

    print("")
    print(
        "========== GINZA =========="
    )

    for url in GINZA_URLS:

        for attempt in range(
            1,
            GINZA_RETRIES + 1,
        ):

            try:

                print(
                    f"Ginza: trying "
                    f"{url} "
                    f"(attempt {attempt})"
                )

                html = get_html(
                    url
                )

                print(
                    "Ginza: page downloaded."
                )

                return html

            except Exception as exc:

                print(
                    f"Ginza attempt "
                    f"{attempt} failed: "
                    f"{exc}"
                )

                if (
                    attempt
                    < GINZA_RETRIES
                ):

                    time.sleep(
                        2
                    )

    print(
        "Ginza: unable to download "
        "4K/Kommande page."
    )

    return None


def parse_ginza():

    html = fetch_ginza()

    if not html:

        print(
            "Ginza: 0 products "
            "(site unreachable)."
        )

        return []

    # Ginza is deliberately left as-is for now.
    # We will improve this separately once GitHub Actions
    # can actually reach the site.

    print(
        "Ginza parser skipped for now."
    )

    return []


# ============================================================
# DATABASE
# ============================================================

def product_id(
    product
):

    ean = product.get(
        "ean"
    )

    if ean:

        return (
            f"{product['source']}:"
            f"{ean}"
        )

    return (
        f"{product['source']}:"
        f"{product['url']}"
    )


def merge_products(
    state,
    discovered,
):
    now = datetime.now(
        timezone.utc
    ).isoformat()

    new_items = []

    # IDs for products that are valid RIGHT NOW.
    current_ids = set()

    for product in discovered:

        pid = product_id(
            product
        )

        current_ids.add(pid)

        # ----------------------------------------------------
        # New product
        # ----------------------------------------------------

        if pid not in state:

            product[
                "first_seen"
            ] = now

            product[
                "preorder_first_seen"
            ] = now

            product[
                "published"
            ] = True

            state[pid] = product

            new_items.append(
                product
            )

           print(
                f"NEW: "
                f"{product['source']} - "
                f"{product['title']}"
            )

            print(
                f"MATCH: "
                f"{product['title']}"
            )

            continue

        # ----------------------------------------------------
        # Existing product
        # ----------------------------------------------------

        old = state[pid]

        old_preorder = old.get(
            "is_preorder",
            False,
        )

        new_preorder = product.get(
            "is_preorder",
            False,
        )

        first_seen = old.get(
            "first_seen",
            now,
        )

        preorder_first_seen = old.get(
            "preorder_first_seen",
            "",
        )

        old.update(
            product
        )

        old[
            "first_seen"
        ] = first_seen

        old[
            "published"
        ] = True

        # Product became preorderable.
        if (
            not old_preorder
            and new_preorder
        ):

            old[
                "preorder_first_seen"
            ] = (
                preorder_first_seen
                or now
            )

            new_items.append(
                old
            )

            print(
                f"NEW PREORDER: "
                f"{old['source']} - "
                f"{old['title']}"
            )

        # Show every current match.
        print(
            f"MATCH: "
            f"{old['title']}"
        )

    # --------------------------------------------------------
    # Unpublish products that no longer match.
    #
    # IMPORTANT:
    # We only do this for iMusic.
    # Ginza is being handled separately.
    # --------------------------------------------------------

    unpublished = 0

    for pid, product in state.items():

        if product.get(
            "source"
        ) != "iMusic":

            continue

        if pid not in current_ids:

            if product.get(
                "published",
                True,
            ):

                product[
                    "published"
                ] = False

                unpublished += 1

                print(
                    f"UNPUBLISHED: "
                    f"{product.get('title', 'Unknown')}"
                )

    if unpublished:

        print(
            f"Products removed from "
            f"RSS: {unpublished}"
        )

    return (
        state,
        new_items,
    )


# ============================================================
# RSS
# ============================================================

def xml_escape(
    text
):

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


def make_description(
    product
):

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

    return (
        f"<strong>{source}</strong><br>"
        f"4K Ultra HD<br>"
        f"Limited / Special Edition<br>"
        f"Förbokning<br>"
        f"Release: "
        f"{release or 'Ej angivet'}"
        f"<br>"
        f"Pris: "
        f"{price or 'Ej angivet'}"
    )


def make_feed(
    state
):

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    cutoff = (
        datetime.now(
            timezone.utc
        ).timestamp()
        - FEED_RETENTION_DAYS
        * 86400
    )

    items = []

    for product in state.values():

        if not product.get(
            "published"
        ):

            continue

        # Only iMusic for now.
        # Ginza will be added later.
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

        if (
            dt.timestamp()
            >= cutoff
        ):

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
            "Kommande 4K Limited Editions, "
            "Steelbooks och specialutgåvor "
            "från iMusic."
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
                    "</pubDate>"
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
        f"RSS items: "
        f"{len(items)}"
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
        "4K LIMITED EDITION WATCHER v4"
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
    # Ginza
    # --------------------------------------------------------

    # We still attempt Ginza so we can see if access starts
    # working, but it does not affect the iMusic watcher.
    parse_ginza()

    # --------------------------------------------------------
    # iMusic
    # --------------------------------------------------------

    imusic_urls = (
        discover_imusic_urls()
    )

    imusic_products = (
        parse_imusic(
            imusic_urls
        )
    )

    # --------------------------------------------------------
    # Merge
    # --------------------------------------------------------

    discovered = (
        imusic_products
    )

    print("")
    print(
        f"Total matching products: "
        f"{len(discovered)}"
    )

    state, new_items = (
        merge_products(
            state,
            discovered,
        )
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    save_state(
        state
    )

    make_feed(
        state
    )

    print("")
    print(
        "=" * 60
    )
    print(
        f"New items this run: "
        f"{len(new_items)}"
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
