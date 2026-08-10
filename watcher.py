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
    "Mozilla/5.0 (compatible; 4K-Limited-Watcher/4.1)"
)

DATA_FILE = Path("data/products.json")
OUTPUT_DIR = Path("public")
FEED_FILE = OUTPUT_DIR / "4k-limited.xml"

# Ginza is intentionally left disabled for product parsing.
# We will handle Ginza separately later.
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
# SPECIAL EDITION KEYWORDS
# ============================================================

SPECIAL_EDITION_KEYWORDS = [
    "limited edition",
    "limited steelbook",
    "steelbook",
    "collector's edition",
    "collectors edition",
    "collector edition",
    "deluxe limited edition",
    "deluxe edition",
    "limited mediabook",
    "mediabook",
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
# SPECIAL EDITION DETECTION
# ============================================================

def is_special_edition(text):
    """
    Strict edition detection.

    Accepted examples:
        Limited Edition
        Limited Steelbook
        Steelbook
        Collector's Edition
        Deluxe Edition
        Mediabook

    Rejected examples:
        Nordic edition
        Photocards edition
        Standard edition
        2026 Mixes edition
    """

    text = clean(text).lower()

    for keyword in SPECIAL_EDITION_KEYWORDS:
        if keyword in text:
            return True

    return False


# ============================================================
# PREORDER DETECTION
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
# PRODUCT TITLE
# ============================================================

def get_title_candidates(soup):
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
                    "",
                )
            )

            if content:
                candidates.append(
                    content
                )

    # H1.
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

    # H2/H3 fallback.
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

    # Browser title fallback.
    if soup.title:
        title = clean(
            soup.title.get_text()
        )

        if title:
            candidates.append(
                title
            )

    # Remove duplicates.
    unique = []

    for candidate in candidates:
        if candidate not in unique:
            unique.append(
                candidate
            )

    return unique


def find_special_edition_title(soup):
    """
    Find a title that itself indicates a special edition.

    This is deliberately NOT based on the whole page.
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
# MEDIA / FORMAT INFORMATION
# ============================================================

def find_media_text(soup):
    media_parts = []

    # Look for explicit "Media" labels.
    for element in soup.find_all(
        string=re.compile(
            r"^\s*Media\s*$",
            re.IGNORECASE,
        )
    ):
        parent = element.parent

        if parent:
            if parent.parent:
                parent_text = clean(
                    parent.parent.get_text(
                        " ",
                        strip=True,
                    )
                )
            else:
                parent_text = clean(
                    parent.get_text(
                        " ",
                        strip=True,
                    )
                )

            if parent_text:
                media_parts.append(
                    parent_text
                )

    # Inspect table rows.
    for row in soup.find_all("tr"):
        row_text = clean(
            row.get_text(
                " ",
                strip=True,
            )
        )

        lower = row_text.lower()

        if (
            "media" in lower
            or "4k ultra hd" in lower
            or "4k uhd" in lower
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
    Confirm 4K at product level.

    We check:
      1. Product title
      2. Product URL
      3. Media information
    """

    title_lower = title.lower()
    url_lower = url.lower()

    # Actual product title.
    if is_4k_text(
        title_lower
    ):
        return True

    # iMusic URLs often contain this.
    if (
        "4k-ultra-hd" in url_lower
        or "4k-uhd" in url_lower
    ):
        return True

    # Explicit media information.
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

    # DD month YYYY
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

def looks_like_imusic_product(url):
    return (
        "/movies/"
        in url.lower()
    )


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

        # ----------------------------------------------------
        # 1. Find actual special-edition title.
        # ----------------------------------------------------

        product_title = (
            find_special_edition_title(
                soup
            )
        )

        if not product_title:
            return None

        # ----------------------------------------------------
        # 2. Confirm 4K at product level.
        # ----------------------------------------------------

        if not product_is_4k(
            soup,
            product_title,
            url,
        ):
            return None

        # ----------------------------------------------------
        # 3. Must be preorderable.
        # ----------------------------------------------------

        if not has_preorder(
            page_text
        ):
            return None

        # ----------------------------------------------------
        # 4. Release date.
        # ----------------------------------------------------

        release_date = (
            extract_release_date(
                page_text
            )
        )

        # If a release date is known and already passed,
        # don't treat it as a current preorder.
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
        # 5. Create product object.
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
                    f"iMusic product error: "
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

    # Remove duplicate products.
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
                    time.sleep(2)

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

    # Ginza is intentionally not parsed yet.
    print(
        "Ginza parser skipped for now."
    )

    return []


# ============================================================
# DATABASE
# ============================================================

def product_id(product):
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

    # Products that currently match the filters.
    current_ids = set()

    for product in discovered:
        pid = product_id(
            product
        )

        current_ids.add(
            pid
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
        # EXISTING PRODUCT
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

        # It currently matches, therefore it should
        # be visible in RSS.
        old[
            "published"
        ] = True

        # Product became preorderable again.
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

        print(
            f"MATCH: "
            f"{old['title']}"
        )

    # --------------------------------------------------------
    # UNPUBLISH OLD iMUSIC PRODUCTS
    # --------------------------------------------------------

    unpublished = 0

    for pid, product in state.items():

        # Only modify iMusic here.
        if product.get(
            "source"
        ) != "iMusic":
            continue

        # If it wasn't found in the current scan,
        # it no longer passes our filters.
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
            f"Products removed from RSS: "
            f"{unpublished}"
        )

    return (
        state,
        new_items,
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


def make_feed(state):
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

        # Only currently published products.
        if not product.get(
            "published",
            False,
        ):
            continue

        # Only iMusic for now.
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
        "4K LIMITED EDITION WATCHER v4.1"
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

    # Still tested, but intentionally not parsed.
    parse_ginza()

    # --------------------------------------------------------
    # iMUSIC
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
    # MERGE
    # --------------------------------------------------------

    discovered = imusic_products

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
    # SAVE
    # --------------------------------------------------------

    save_state(
        state
    )

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
