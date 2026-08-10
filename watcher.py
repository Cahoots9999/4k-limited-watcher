import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (compatible; 4K-Limited-Watcher/1.0; "
    "+https://github.com/)"
)

DATA_FILE = Path("data/products.json")
OUTPUT_DIR = Path("public")
FEED_FILE = OUTPUT_DIR / "4k-limited.xml"

GINZA_URL = "https://www.ginza.se/Film/Kommande/4K/546"

# iMusic's catalogue/search endpoint can change over time.
# We start with the main movie catalogue and discover product links.
IMUSIC_URL = "https://imusic.se/movies"

MAX_PRODUCTS_PER_SOURCE = 200


def get(url):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    time.sleep(1)
    return response.text


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def load_state():
    if not DATA_FILE.exists():
        return {}

    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(products):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(products, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_limited(text):
    text = text.lower()

    keywords = [
        "limited",
        "steelbook",
        "collector",
        "collector's",
        "collectors",
        "special edition",
        "deluxe",
        "restored limited",
    ]

    return any(keyword in text for keyword in keywords)


def is_4k(text):
    text = text.lower()

    keywords = [
        "4k",
        "4k ultra hd",
        "4k uhd",
        "4k ultra hd/bd",
        "4k uhd + blu-ray",
    ]

    return any(keyword in text for keyword in keywords)


def is_future_date(date_string):
    if not date_string:
        return True

    formats = [
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
    ]

    date_string = clean(date_string)

    for fmt in formats:
        try:
            date = datetime.strptime(date_string, fmt)
            return date.date() >= datetime.now().date()
        except ValueError:
            pass

    return True


def parse_ginza():
    html = get(GINZA_URL)
    soup = BeautifulSoup(html, "html.parser")

    results = []

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        title = clean(link.get_text(" ", strip=True))

        if not href or not title:
            continue

        if "/product/" not in href.lower():
            continue

        full_url = urljoin(GINZA_URL, href)

        # We only use products that look like limited/special editions.
        if not is_limited(title):
            continue

        product = {
            "source": "Ginza",
            "title": title,
            "url": full_url,
            "ean": "",
            "release_date": "",
            "price": "",
            "description": "",
        }

        results.append(product)

        if len(results) >= MAX_PRODUCTS_PER_SOURCE:
            break

    return results


def parse_imusic():
    html = get(IMUSIC_URL)
    soup = BeautifulSoup(html, "html.parser")

    product_urls = []

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")

        if "/movies/" not in href:
            continue

        full_url = urljoin(IMUSIC_URL, href)

        if full_url not in product_urls:
            product_urls.append(full_url)

        if len(product_urls) >= MAX_PRODUCTS_PER_SOURCE:
            break

    results = []

    for url in product_urls:
        try:
            product_html = get(url)
            product_soup = BeautifulSoup(product_html, "html.parser")

            page_text = clean(product_soup.get_text(" ", strip=True))
            title = clean(product_soup.title.get_text()) if product_soup.title else ""

            if not is_4k(page_text):
                continue

            if not is_limited(page_text):
                continue

            # iMusic explicitly exposes "Förboka" on preorder pages.
            if "förboka" not in page_text.lower():
                continue

            # EAN/UPC
            ean = ""
            match = re.search(r"EAN/UPC\s+([0-9]{8,14})", page_text)
            if match:
                ean = match.group(1)

            # Release date
            release_date = ""
            match = re.search(
                r"Releasedatum\s+([0-9]{1,2}\s+\w+\s+[0-9]{4})",
                page_text,
                re.IGNORECASE,
            )
            if match:
                release_date = match.group(1)

            # Price
            price = ""
            match = re.search(r"SEK\s+([0-9][0-9.,]*)", page_text)
            if match:
                price = f"{match.group(1)} kr"

            # Better title: first H1
            h1 = product_soup.find("h1")
            if h1:
                title = clean(h1.get_text(" ", strip=True))

            results.append(
                {
                    "source": "iMusic",
                    "title": title,
                    "url": url,
                    "ean": ean,
                    "release_date": release_date,
                    "price": price,
                    "description": "",
                }
            )

        except Exception as exc:
            print(f"Could not parse {url}: {exc}")

    return results


def product_id(product):
    if product.get("ean"):
        return f"{product['source']}:{product['ean']}"

    return f"{product['source']}:{product['url']}"


def merge_products(old, new):
    now = datetime.now(timezone.utc).isoformat()

    for product in new:
        pid = product_id(product)

        if pid not in old:
            product["first_seen"] = now
            old[pid] = product
        else:
            # Update information without changing first_seen.
            first_seen = old[pid].get("first_seen", now)
            old[pid].update(product)
            old[pid]["first_seen"] = first_seen

    return old


def xml_escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def make_feed(products):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Newest discoveries first.
    items = sorted(
        products.values(),
        key=lambda x: x.get("first_seen", ""),
        reverse=True,
    )[:100]

    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        "<title>4K Limited Editions – Ginza + iMusic</title>",
        "<link>https://www.ginza.se/</link>",
        "<description>Kommande 4K Limited Editions från Ginza och iMusic.</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]

    for product in items:
        title = xml_escape(product.get("title", "Okänd titel"))
        url = xml_escape(product.get("url", ""))
        source = xml_escape(product.get("source", ""))
        price = xml_escape(product.get("price", ""))
        release = xml_escape(product.get("release_date", ""))

        description = (
            f"{source}<br>"
            f"4K Limited Edition<br>"
            f"Release: {release}<br>"
            f"Pris: {price}"
        )

        pub_date = product.get("first_seen", "")

        try:
            dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
            pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
        except Exception:
            pub_date = now

        chunks.extend(
            [
                "<item>",
                f"<title>{title}</title>",
                f"<link>{url}</link>",
                f"<guid isPermaLink=\"true\">{url}</guid>",
                f"<description>{description}</description>",
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

    FEED_FILE.write_text("\n".join(chunks), encoding="utf-8")


def main():
    print("Loading previous state...")
    state = load_state()

    print("Checking Ginza...")
    try:
        ginza = parse_ginza()
        print(f"Ginza candidates: {len(ginza)}")
    except Exception as exc:
        print(f"Ginza failed: {exc}")
        ginza = []

    print("Checking iMusic...")
    try:
        imusic = parse_imusic()
        print(f"iMusic candidates: {len(imusic)}")
    except Exception as exc:
        print(f"iMusic failed: {exc}")
        imusic = []

    all_products = ginza + imusic

    state = merge_products(state, all_products)
    save_state(state)
    make_feed(state)

    print(f"Total products in database: {len(state)}")
    print(f"RSS generated: {FEED_FILE}")


if __name__ == "__main__":
    main()
