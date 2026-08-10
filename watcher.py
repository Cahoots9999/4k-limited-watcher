```python
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (compatible; 4K-Limited-Watcher/2.0; "
    "+https://github.com/)"
)

REQUEST_DELAY = 1.0

DATA_FILE = Path("data/products.json")
OUTPUT_DIR = Path("public")
FEED_FILE = OUTPUT_DIR / "4k-limited.xml"

GINZA_START_URL = "https://www.ginza.se/Film/Kommande/4K/546"
IMUSIC_START_URL = "https://imusic.se/movies"

MAX_LIST_PAGES = 8
MAX_PRODUCT_PAGES = 120
FEED_RETENTION_DAYS = 180


session = requests.Session()

session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
)


def get_html(url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return response.text


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_url(url):
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def absolute_url(base, href):
    return normalize_url(urljoin(base, href))


def load_state():
    if not DATA_FILE.exists():
        return {}

    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not load state: {exc}")
        return {}


def save_state(products):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(products, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def text_from_soup(soup):
    return clean(soup.get_text(" ", strip=True))


LIMITED_KEYWORDS = [
    "limited",
    "steelbook",
    "collector",
    "collector's",
    "collectors",
    "special edition",
    "deluxe edition",
    "deluxe",
    "restored limited",
    "limited edition",
]


def is_4k(text):
    text = text.lower()

    patterns = [
        r"\b4k\b",
        r"4k ultra hd",
        r"4k uhd",
        r"ultra hd",
        r"uhd blu-ray",
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def is_limited(text):
    text = text.lower()
    return any(keyword in text for keyword in LIMITED_KEYWORDS)


def has_preorder(text):
    text = text.lower()

    preorder_words = [
        "förboka",
        "förbeställ",
        "pre-order",
        "preorder",
        "pre order",
    ]

    return any(word in text for word in preorder_words)


def looks_like_ginza_product(url):
    return "/product/" in url.lower()


def looks_like_imusic_product(url):
    return "/movies/" in url.lower()


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

    match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)

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

    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text)

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

    month_pattern = "|".join(SWEDISH_MONTHS.keys())

    match = re.search(
        rf"\b(\d{{1,2}})\s+({month_pattern})\s+(20\d{{2}})\b",
        text,
    )

    if match:
        try:
            return datetime(
                int(match.group(3)),
                SWEDISH_MONTHS[match.group(2)],
                int(match.group(1)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    return None


def extract_release_date(text):
    patterns = [
        r"releasedatum\s+(.{1,40})",
        r"release\s+date\s+(.{1,40})",
        r"released\s+(.{1,40})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            date = parse_date(match.group(1))
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
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return match.group(1)

    return ""


def extract_price(text):
    patterns = [
        r"SEK\s*([0-9][0-9.,]*)",
        r"([0-9][0-9.,]*)\s*SEK",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return f"{match.group(1)} kr"

    return ""


def extract_title(soup):
    h1 = soup.find("h1")

    if h1:
        title = clean(h1.get_text(" ", strip=True))
        if title:
            return title

    if soup.title:
        return clean(soup.title.get_text())

    return "Okänd titel"


def collect_links_from_page(
    url,
    product_detector,
    include_link_detector=None,
):
    html = get_html(url)
    soup = BeautifulSoup(html, "html.parser")

    product_links = set()
    list_links = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")

        if not href:
            continue

        full_url = absolute_url(url, href)

        if urlparse(full_url).netloc != urlparse(url).netloc:
            continue

        if product_detector(full_url):
            product_links.add(full_url)
            continue

        if include_link_detector and include_link_detector(
            anchor,
            full_url,
        ):
            list_links.add(full_url)

    return product_links, list_links


def ginza_list_link(anchor, url):
    text = clean(anchor.get_text(" ", strip=True)).lower()

    if any(
        word in text
        for word in [
            "nästa",
            "föregående",
            "visa fler",
            "visa alla",
            "next",
        ]
    ):
        return True

    path = urlparse(url).path.lower()

    return "/film/" in path and "/product/" not in path


def parse_ginza_product(url):
    try:
        html = get_html(url)
        soup = BeautifulSoup(html, "html.parser")
        text = text_from_soup(soup)

        title = extract_title(soup)

        if not is_4k(text):
            return None

        if not is_limited(title + " " + text):
            return None

        release_date = extract_release_date(text)

        return {
            "source": "Ginza",
            "title": title,
            "url": url,
            "ean": extract_ean(text),
            "release_date": (
                release_date.strftime("%Y-%m-%d")
                if release_date
                else ""
            ),
            "price": extract_price(text),
            "is_4k": True,
            "is_limited": True,
            "is_preorder": True,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        print(f"Ginza product failed: {url}: {exc}")
        return None


def parse_ginza():
    print("Discovering Ginza products...")

    product_urls = set()
    pages_to_visit = [GINZA_START_URL]
    visited = set()

    while pages_to_visit and len(visited) < MAX_LIST_PAGES:
        url = pages_to_visit.pop(0)

        if url in visited:
            continue

        visited.add(url)

        try:
            products, list_links = collect_links_from_page(
                url,
                looks_like_ginza_product,
                ginza_list_link,
            )

            product_urls.update(products)

            for link in list_links:
                if link not in visited:
                    pages_to_visit.append(link)

        except Exception as exc:
            print(f"Ginza listing failed: {url}: {exc}")

    print(f"Ginza product URLs found: {len(product_urls)}")

    results = []

    for url in list(product_urls)[:MAX_PRODUCT_PAGES]:
        product = parse_ginza_product(url)

        if product:
            results.append(product)

    print(f"Ginza matching products: {len(results)}")

    return results


def imusic_list_link(anchor, url):
    text = clean(anchor.get_text(" ", strip=True)).lower()

    if "visa alla" in text:
        return True

    if any(
        word in text
        for word in [
            "nästa",
            "next",
            "föregående",
            "previous",
        ]
    ):
        return True

    path = urlparse(url).path.lower()

    return "/movies" in path and "/movies/" not in path


def parse_imusic_product(url):
    try:
        html = get_html(url)
        soup = BeautifulSoup(html, "html.parser")
        text = text_from_soup(soup)

        title = extract_title(soup)

        if not is_4k(text):
            return None

        if not is_limited(title + " " + text):
            return None

        if not has_preorder(text):
            return None

        release_date = extract_release_date(text)

        if release_date:
            today = datetime.now(timezone.utc).date()

            if release_date.date() < today:
                return None

        return {
            "source": "iMusic",
            "title": title,
            "url": url,
            "ean": extract_ean(text),
            "release_date": (
                release_date.strftime("%Y-%m-%d")
                if release_date
                else ""
            ),
            "price": extract_price(text),
            "is_4k": True,
            "is_limited": True,
            "is_preorder": True,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        print(f"iMusic product failed: {url}: {exc}")
        return None


def parse_imusic():
    print("Discovering iMusic products...")

    product_urls = set()
    pages_to_visit = [IMUSIC_START_URL]
    visited = set()

    while pages_to_visit and len(visited) < MAX_LIST_PAGES:
        url = pages_to_visit.pop(0)

        if url in visited:
            continue

        visited.add(url)

        try:
            products, list_links = collect_links_from_page(
                url,
                looks_like_imusic_product,
                imusic_list_link,
            )

            product_urls.update(products)

            for link in list_links:
                if link not in visited:
                    pages_to_visit.append(link)

        except Exception as exc:
            print(f"iMusic listing failed: {url}: {exc}")

    print(f"iMusic product URLs found: {len(product_urls)}")

    results = []

    for url in list(product_urls)[:MAX_PRODUCT_PAGES]:
        product = parse_imusic_product(url)

        if product:
            results.append(product)

    print(f"iMusic matching products: {len(results)}")

    return results


def product_id(product):
    if product.get("ean"):
        return f"{product['source']}:{product['ean']}"

    return f"{product['source']}:{product['url']}"


def merge_products(state, discovered):
    now = datetime.now(timezone.utc).isoformat()
    new_items = []

    for product in discovered:
        pid = product_id(product)

        if pid not in state:
            product["first_seen"] = now
            product["published"] = True
            product["preorder_first_seen"] = now

            state[pid] = product
            new_items.append(product)

            print(
                f"NEW: {product['source']} - "
                f"{product['title']}"
            )

            continue

        old = state[pid]

        was_preorder = old.get("is_preorder", False)
        is_preorder_now = product.get("is_preorder", False)

        first_seen = old.get("first_seen", now)
        preorder_first_seen = old.get(
            "preorder_first_seen",
            "",
        )

        old.update(product)
        old["first_seen"] = first_seen

        if not was_preorder and is_preorder_now:
            old["published"] = True

            if not preorder_first_seen:
                old["preorder_first_seen"] = now

            new_items.append(old)

            print(
                f"NEW PREORDER: {old['source']} - "
                f"{old['title']}"
            )

    return state, new_items


def xml_escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def make_description(product):
    source = xml_escape(product.get("source", ""))
    price = xml_escape(product.get("price", ""))
    release = xml_escape(product.get("release_date", ""))

    return (
        f"<strong>{source}</strong><br>"
        f"4K Ultra HD<br>"
        f"Limited / Special Edition<br>"
        f"Förbokning<br>"
        f"Release: {release or 'Ej angivet'}<br>"
        f"Pris: {price or 'Ej angivet'}"
    )


def make_feed(state):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cutoff = (
        datetime.now(timezone.utc).timestamp()
        - FEED_RETENTION_DAYS * 86400
    )

    items = []

    for product in state.values():
        if not product.get("published"):
            continue

        timestamp = (
            product.get("preorder_first_seen")
            or product.get("first_seen")
        )

        try:
            dt = datetime.fromisoformat(
                timestamp.replace("Z", "+00:00")
            )

            if dt.timestamp() >= cutoff:
                items.append((dt, product))

        except Exception:
            continue

    items.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    now = datetime.now(timezone.utc)

    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>4K Limited Editions – Ginza + iMusic</title>",
        "<link>https://www.ginza.se/</link>",
        (
            "<description>"
            "Kommande 4K Limited Editions och Steelbooks "
            "från Ginza och iMusic."
            "</description>"
        ),
        (
            f"<lastBuildDate>"
            f"{now.strftime('%a, %d %b %Y %H:%M:%S GMT')}"
            f"</lastBuildDate>"
        ),
    ]

    for dt, product in items:
        title = xml_escape(
            product.get("title", "Okänd titel")
        )

        url = xml_escape(product.get("url", ""))
        description = make_description(product)

        pub_date = dt.strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

        guid = xml_escape(product_id(product))

        chunks.extend(
            [
                "<item>",
                f"<title>{title}</title>",
                f"<link>{url}</link>",
                (
                    f"<guid isPermaLink=\"false\">"
                    f"{guid}</guid>"
                ),
                (
                    f"<description>"
                    f"{description}"
                    f"</description>"
                ),
                f"<pubDate>{pub_date}</pubDate>",
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

    print(
        f"RSS contains {len(items)} published items."
    )


def main():
    print("=" * 60)
    print("4K LIMITED EDITION WATCHER")
    print("=" * 60)

    state = load_state()

    print(
        f"Existing products in database: "
        f"{len(state)}"
    )

    ginza_products = []
    imusic_products = []

    try:
        ginza_products = parse_ginza()
    except Exception as exc:
        print(f"Ginza error: {exc}")

    try:
        imusic_products = parse_imusic()
    except Exception as exc:
        print(f"iMusic error: {exc}")

    discovered = ginza_products + imusic_products

    print(
        f"Total matching products discovered: "
        f"{len(discovered)}"
    )

    state, new_items = merge_products(
        state,
        discovered,
    )

    save_state(state)
    make_feed(state)

    print("=" * 60)
    print(
        f"New RSS items this run: "
        f"{len(new_items)}"
    )
    print(
        f"Database size: {len(state)}"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
```
