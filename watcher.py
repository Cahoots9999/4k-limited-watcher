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
    "Mozilla/5.0 (compatible; 4K-Limited-Watcher/3.0)"
)

DATA_FILE = Path("data/products.json")
OUTPUT_DIR = Path("public")
FEED_FILE = OUTPUT_DIR / "4k-limited.xml"

GINZA_URLS = [
    "https://www.ginza.se/Film/Kommande/4K/546",
    "https://www.ginza.se/4K%20UHD%20%2B%20Blu-ray/Kommande/4K/592",
]

IMUSIC_URL = "https://imusic.se/movies"

# iMusic can have hundreds of products.
# We check them in parallel instead of one by one.
IMUSIC_WORKERS = 10

# Keep this reasonably high so we don't miss new releases.
MAX_IMUSIC_PRODUCTS = 500

# Network settings.
REQUEST_TIMEOUT = 12
GINZA_RETRIES = 2

# RSS history.
FEED_RETENTION_DAYS = 180


# ============================================================
# KEYWORDS
# ============================================================

LIMITED_KEYWORDS = [
    "limited",
    "steelbook",
    "collector",
    "collector's",
    "collectors",
    "special edition",
    "deluxe edition",
    "deluxe",
    "limited edition",
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
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_url(url):
    parsed = urlparse(url)

    return parsed._replace(
        fragment="",
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
        print(f"Could not load database: {exc}")
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
# FILTERS
# ============================================================

def is_4k(text):
    text = text.lower()

    return any(
        re.search(
            pattern,
            text,
        )
        for pattern in [
            r"\b4k\b",
            r"4k ultra hd",
            r"4k uhd",
            r"ultra hd",
            r"uhd blu-ray",
        ]
    )


def is_limited(text):
    text = text.lower()

    return any(
        keyword in text
        for keyword in LIMITED_KEYWORDS
    )


def has_preorder(text):
    text = text.lower()

    return any(
        word in text
        for word in [
            "förboka",
            "förbeställ",
            "pre-order",
            "preorder",
            "pre order",
        ]
    )


def looks_like_imusic_product(url):
    return "/movies/" in url.lower()


# ============================================================
# DATE / PRODUCT INFO
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

    text = clean(text).lower()

    # 2026-10-19
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

    # 19/10/2026
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

    return parse_date(text)


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
            return f"{match.group(1)} kr"

    return ""


def extract_title(soup):
    h1 = soup.find("h1")

    if h1:
        title = clean(
            h1.get_text(
                " ",
                strip=True,
            )
        )

        if title:
            return title

    if soup.title:
        return clean(
            soup.title.get_text()
        )

    return "Okänd titel"


# ============================================================
# GINZA
# ============================================================

def fetch_ginza():
    """
    Ginza can sometimes reject/timeout GitHub runners.
    Try a couple of known 4K/Kommande pages.
    """

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
                    url,
                    timeout=REQUEST_TIMEOUT,
                )

                print(
                    "Ginza: page downloaded."
                )

                return html

            except Exception as exc:
                print(
                    f"Ginza attempt {attempt} "
                    f"failed: {exc}"
                )

                if attempt < GINZA_RETRIES:
                    time.sleep(2)

    print(
        "Ginza: unable to download "
        "4K/Kommande page."
    )

    return None


def parse_ginza():
    print("")
    print("========== GINZA ==========")

    html = fetch_ginza()

    if not html:
        print(
            "Ginza: 0 products "
            "(site unreachable from GitHub)."
        )

        return []

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    page_text = text_from_soup(
        soup
    )

    if not is_4k(page_text):
        print(
            "Ginza warning: page did not "
            "appear to contain 4K data."
        )

    results = []
    seen = set()

    # Ginza product links.
    for anchor in soup.find_all(
        "a",
        href=True,
    ):

        href = anchor.get("href")

        if not href:
            continue

        url = absolute_url(
            GINZA_URLS[0],
            href,
        )

        text = clean(
            anchor.get_text(
                " ",
                strip=True,
            )
        )

        if not url:
            continue

        # Ginza product pages normally use /product/.
        if "/product/" not in url.lower():
            continue

        # The link/title itself often contains the edition.
        # Look around the link as well.
        surrounding_text = clean(
            anchor.parent.get_text(
                " ",
                strip=True,
            )
            if anchor.parent
            else text
        )

        combined = (
            text
            + " "
            + surrounding_text
        )

        if not is_4k(combined):
            continue

        if not is_limited(combined):
            continue

        if url in seen:
            continue

        seen.add(url)

        results.append(
            {
                "source": "Ginza",
                "title": text or "Okänd titel",
                "url": url,
                "ean": "",
                "release_date": "",
                "price": "",
                "is_4k": True,
                "is_limited": True,
                "is_preorder": True,
                "last_seen": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            }
        )

    print(
        f"Ginza matching products: "
        f"{len(results)}"
    )

    return results


# ============================================================
# iMUSIC DISCOVERY
# ============================================================

def discover_imusic_urls():
    print("")
    print("====== iMUSIC DISCOVERY ======")

    try:
        html = get_html(
            IMUSIC_URL
        )
    except Exception as exc:
        print(
            f"iMusic catalogue failed: {exc}"
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
        href = anchor.get("href")

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

        urls.add(url)

    urls = list(urls)

    print(
        f"iMusic product URLs found: "
        f"{len(urls)}"
    )

    # Don't unnecessarily request an unlimited number.
    return urls[
        :MAX_IMUSIC_PRODUCTS
    ]


# ============================================================
# iMUSIC PRODUCT
# ============================================================

def parse_imusic_product(url):
    try:
        html = get_html(
            url,
            timeout=REQUEST_TIMEOUT,
        )

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        text = text_from_soup(
            soup
        )

        title = extract_title(
            soup
        )

        # Must be 4K.
        if not is_4k(text):
            return None

        # Must be limited/special.
        if not is_limited(
            title + " " + text
        ):
            return None

        # Must currently be a preorder.
        if not has_preorder(text):
            return None

        release_date = extract_release_date(
            text
        )

        # Don't include already released items.
        if release_date:
            today = datetime.now(
                timezone.utc
            ).date()

            if release_date.date() < today:
                return None

        return {
            "source": "iMusic",
            "title": title,
            "url": url,
            "ean": extract_ean(text),
            "release_date": (
                release_date.strftime(
                    "%Y-%m-%d"
                )
                if release_date
                else ""
            ),
            "price": extract_price(
                text
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


def parse_imusic(urls):
    print("")
    print("======== iMUSIC CHECK ========")

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
                product = future.result()

                if product:
                    results.append(
                        product
                    )

            except Exception as exc:
                print(
                    f"iMusic product error: "
                    f"{exc}"
                )

            # Progress every 25 pages.
            if (
                completed % 25 == 0
                or completed == len(futures)
            ):
                print(
                    f"iMusic checked "
                    f"{completed}/"
                    f"{len(futures)}"
                )

    print(
        f"iMusic matching products: "
        f"{len(results)}"
    )

    return results


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

    for product in discovered:

        pid = product_id(
            product
        )

        # Brand new product.
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

            continue

        # Existing product.
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

        old.update(product)

        old[
            "first_seen"
        ] = first_seen

        # It became preorderable.
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

            old[
                "published"
            ] = True

            new_items.append(
                old
            )

            print(
                f"NEW PREORDER: "
                f"{old['source']} - "
                f"{old['title']}"
            )

    return state, new_items


# ============================================================
# RSS
# ============================================================

def xml_escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
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

        if not product.get(
            "published"
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
            "Ginza + iMusic"
            "</title>"
        ),
        (
            "<link>"
            "https://www.ginza.se/"
            "</link>"
        ),
        (
            "<description>"
            "Kommande 4K Limited Editions "
            "och Steelbooks från Ginza "
            "och iMusic."
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
                f"<title>{title}</title>",
                f"<link>{url}</link>",
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
        "\n".join(chunks),
        encoding="utf-8",
    )

    print("")
    print(
        f"RSS items: {len(items)}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print("=" * 60)
    print(
        "4K LIMITED EDITION WATCHER v3"
    )
    print("=" * 60)

    state = load_state()

    print(
        f"Existing database entries: "
        f"{len(state)}"
    )

    # --------------------------------------------------------
    # Ginza
    # --------------------------------------------------------

    ginza_products = parse_ginza()

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
        ginza_products
        + imusic_products
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
    print("=" * 60)
    print(
        f"New items this run: "
        f"{len(new_items)}"
    )

    print(
        f"Database size: "
        f"{len(state)}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()
