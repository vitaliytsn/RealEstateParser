import requests
from bs4 import BeautifulSoup
import json
import sqlite3
import time
from datetime import datetime
import os

# --- Database Setup ---
DB_NAME = os.getenv("DB_PATH", "otodom_ads.db")

OTODOM_LIST_URL = (
    "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/"
    "mazowieckie/warszawa/warszawa/warszawa"
    "?ownerTypeSingleSelect=ALL&by=LATEST&direction=DESC"
)


def init_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA busy_timeout=30000;")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ads (
            ad_id INTEGER PRIMARY KEY,
            title TEXT,
            price TEXT,
            area REAL,
            rooms TEXT,
            city TEXT,
            province TEXT,
            district TEXT,
            street TEXT,
            details_url TEXT,
            photos TEXT,
            first_seen DATETIME
        )
    ''')
    conn.commit()
    conn.close()


def is_ad_new(ad_id) -> bool:
    if not ad_id:
        return False
    conn = sqlite3.connect(DB_NAME, timeout=30)
    cursor = conn.cursor()
    cursor.execute("PRAGMA busy_timeout=30000;")
    cursor.execute("SELECT 1 FROM ads WHERE ad_id = ?", (ad_id,))
    exists = cursor.fetchone()
    conn.close()
    return exists is None


def save_ad(ad: dict):
    conn = sqlite3.connect(DB_NAME, timeout=30)
    cursor = conn.cursor()
    cursor.execute("PRAGMA busy_timeout=30000;")
    try:
        cursor.execute('''
            INSERT INTO ads (
                ad_id, title, price, area, rooms, city, province, district, street,
                details_url, photos, first_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            ad['ad_id'],
            ad.get('title'),
            ad.get('price'),
            ad.get('area') if isinstance(ad.get('area'), (int, float)) else None,
            ad.get('rooms'),
            ad.get('city'),
            ad.get('province'),
            ad.get('district'),
            ad.get('street'),
            ad.get('details_url'),
            json.dumps(ad.get('photos') or [], ensure_ascii=False),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # Already exists
    finally:
        conn.close()


def http_get(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Referer": "https://www.otodom.pl/",
    }
    return requests.get(url, headers=headers, timeout=20, allow_redirects=True)


def extract_items_from_next_data(next_data: dict) -> list:
    """
    Otodom sometimes changes nesting. Try a couple of known paths.
    """
    items = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("data", {})
        .get("searchAds", {})
        .get("items", [])
    )
    if items:
        return items

    items = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("initialProps", {})
        .get("data", {})
        .get("searchAds", {})
        .get("items", [])
    )
    return items or []


def parse_otodom(url: str):
    try:
        response = http_get(url)
        if response.status_code != 200:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: Status code {response.status_code}")
            return []
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Request error: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    listings = []

    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: Could not find __NEXT_DATA__")
        return []

    try:
        next_data = json.loads(script_tag.string)
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error parsing __NEXT_DATA__: {e}")
        return []

    items = extract_items_from_next_data(next_data)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Found {len(items)} items on listing page")

    for item in items:
        try:
            ad_id = item.get("id")
            if not ad_id:
                continue

            # Only new ads
            if not is_ad_new(ad_id):
                continue

            slug = item.get("slug")
            details_url = f"https://www.otodom.pl/pl/oferta/{slug}" if slug else None
            if not details_url:
                continue

            title = item.get("title") or "N/A"

            total_price_obj = item.get("totalPrice") or {}
            value = total_price_obj.get("value")
            currency = total_price_obj.get("currency")
            price = f"{value} {currency}" if value is not None and currency else "N/A"

            area = item.get("areaInSquareMeters")
            rooms = item.get("roomsNumber") or "N/A"

            location = item.get("location", {}) or {}
            address = location.get("address", {}) or {}

            city = (address.get("city") or {}).get("name") or "N/A"
            province = (address.get("province") or {}).get("name") or "N/A"

            district = "N/A"
            for loc in (location.get("reverseGeocoding", {}) or {}).get("locations", []) or []:
                if loc.get("locationLevel") == "district":
                    district = loc.get("name") or "N/A"
                    break

            street = (address.get("street") or {}).get("name")
            if not street:
                street = None

            photos = []
            for img in item.get("images", []) or []:
                photo_url = img.get("large") or img.get("medium")
                if photo_url:
                    photos.append(photo_url)

            listings.append({
                "ad_id": ad_id,
                "title": title,
                "price": price,
                "area": area if isinstance(area, (int, float)) else None,
                "rooms": rooms,
                "city": city,
                "province": province,
                "district": district,
                "street": street,
                "photos": photos,
                "details_url": details_url
            })
        except Exception:
            # Skip a broken item, keep scraper alive
            continue

    return listings


def run_task():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking Otodom for new ads...")
    new_ads = parse_otodom(OTODOM_LIST_URL)

    if new_ads:
        print(f"Found {len(new_ads)} NEW ads!")
        for ad in new_ads:
            save_ad(ad)
            print(f" - Saved: {ad.get('title')} ({ad.get('price')})")
    else:
        print("No new ads found.")


if __name__ == "__main__":
    init_db()
    print("Otodom scraper started. Press Ctrl+C to stop.")
    while True:
        try:
            run_task()
            print("Sleeping for 15 minutes...")
            time.sleep(15 * 60)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"Main loop error: {e}")
            time.sleep(60)