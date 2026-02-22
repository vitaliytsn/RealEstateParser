import requests
from bs4 import BeautifulSoup
import json
import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import urljoin
import os

# --- Database Setup ---
DB_NAME = os.getenv("DB_PATH", "otodom_ads.db")

OLX_LIST_URL = "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/?search%5Border%5D=created_at:desc"
OLX_BASE = "https://www.olx.pl"


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


def is_ad_new(ad_id: int) -> bool:
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
            INSERT INTO ads (ad_id, title, price, area, rooms, city, province, district, street, details_url, photos, first_seen)
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


# --- Helpers ---
def to_unique_olx_ad_id(raw_olx_id: str) -> int:
    """
    "Криво" но надежно: делаем OLX id уникальным в общей таблице с Otodom
    через префикс 2: 1052796813 -> 21052796813
    """
    digits = re.sub(r"\D+", "", str(raw_olx_id))
    return int("2" + digits) if digits else 0


def http_get(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Referer": "https://www.olx.pl/",
    }
    return requests.get(url, headers=headers, timeout=20, allow_redirects=True)


def extract_area_from_card(card) -> float | None:
    """
    Robust parsing (no :has selector): find '36 m²' anywhere in card text.
    If no area — often a promoted service ad, we skip it.
    """
    txt = card.get_text(" ", strip=True).replace("\xa0", " ")
    mm = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", txt)
    if not mm:
        return None
    return float(mm.group(1).replace(",", "."))


NUM_TO_TEXT_ROOMS = {
    "1": "ONE",
    "2": "TWO",
    "3": "THREE",
    "4": "FOUR",
    "5": "FIVE",
    "6": "SIX",
    "7": "SEVEN",
    "8": "EIGHT",
    "9": "NINE",
    "10": "TEN",
}


def normalize_price_to_pln(price_text: str) -> str:
    """Convert OLX price like '3 000 zł' to '3000 PLN' (bot expects PLN + digits)."""
    if not price_text:
        return "N/A"
    txt = price_text.replace("\xa0", " ").strip()
    digits = re.sub(r"[^0-9 ,.]", "", txt)
    digits = digits.replace(" ", "")
    if not digits:
        return "N/A"
    digits = digits.split(",")[0].split(".")[0]
    try:
        value = int(digits)
        return f"{value} PLN"
    except Exception:
        return "N/A"


def normalize_rooms_to_text(rooms_raw: str | None) -> str:
    """Convert OLX rooms like '2 pokoje' or '2' -> 'TWO' to match Otodom format used by the bot."""
    if not rooms_raw:
        return "N/A"
    mm = re.search(r"(\d+)", str(rooms_raw))
    if not mm:
        return "N/A"
    return NUM_TO_TEXT_ROOMS.get(mm.group(1), "N/A")


# --- OLX List Parser ---
def parse_olx_list(list_url: str):
    try:
        r = http_get(list_url)
        if r.status_code != 200:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] OLX list status {r.status_code}")
            return []
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] OLX list error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    cards = soup.select('div[data-cy="l-card"][id]')
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Found {len(cards)} cards on OLX listing page")
    new_ads = []

    for card in cards:
        raw_id = card.get("id")
        if not raw_id:
            continue

        # ✅ FILTER 1: skip ads without area (often services/promotions)
        area = extract_area_from_card(card)
        if area is None:
            continue

        ad_id = to_unique_olx_ad_id(raw_id)
        if ad_id == 0:
            continue

        # ✅ FILTER 2: only new ads (don't open details for old)
        if not is_ad_new(ad_id):
            continue

        # title
        title_el = card.select_one('[data-testid="ad-card-title"] h4')
        title = title_el.get_text(" ", strip=True) if title_el else "N/A"

        # price
        price_el = card.select_one('[data-testid="ad-price"]')
        price = price_el.get_text(" ", strip=True).replace("\xa0", " ") if price_el else "N/A"
        price = normalize_price_to_pln(price)

        # details url
        link_el = card.select_one('a[href*="/d/oferta/"]')
        href = link_el.get("href") if link_el else None
        details_url = urljoin(OLX_BASE, href) if href else None
        if not details_url:
            continue

        # district from "Warszawa, Praga-Południe - дата"
        district = "N/A"
        loc_el = card.select_one('[data-testid="location-date"]')
        if loc_el:
            loc_txt = loc_el.get_text(" ", strip=True)
            mm = re.search(r"Warszawa,\s*([^-\n]+?)\s*-", loc_txt)
            if mm:
                district = mm.group(1).strip()

        # preview photo
        photos = []
        img_el = card.select_one("img[src*='apollo.olxcdn.com']")
        if img_el and img_el.get("src"):
            photos.append(img_el["src"])

        new_ads.append({
            "ad_id": ad_id,
            "title": title,
            "price": price,
            "area": area,
            "rooms": "N/A",
            "city": "Warszawa",
            "province": "mazowieckie",
            "district": district,
            "street": None,
            "photos": photos,
            "details_url": details_url
        })

    return new_ads


# --- OLX Details Parser ---
def parse_olx_details(details_url: str):
    try:
        r = http_get(details_url)
        if r.status_code != 200:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] OLX details status {r.status_code}: {details_url}")
            return {}
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] OLX details error: {e}")
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    result = {}

    # params from ad-parameters-container
    params = {}
    for p in soup.select('[data-testid="ad-parameters-container"] p'):
        txt = p.get_text(" ", strip=True).replace("\xa0", " ")
        if not txt:
            continue
        if ":" in txt:
            k, v = txt.split(":", 1)
            params[k.strip()] = v.strip()
        else:
            params[txt.strip()] = True

    # rooms
    rooms = None
    if isinstance(params.get("Liczba pokoi"), str):
        mm = re.search(r"(\d+)", params["Liczba pokoi"])
        if mm:
            rooms = mm.group(1)
    rooms = normalize_rooms_to_text(rooms)
    result["rooms"] = rooms

    # area
    area = None
    if isinstance(params.get("Powierzchnia"), str):
        mm = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", params["Powierzchnia"])
        if mm:
            area = float(mm.group(1).replace(",", "."))
    result["area"] = area

    # full gallery photos
    photos = []
    for img in soup.select('[data-testid="image-galery-container"] img'):
        src = img.get("src")
        if src and src.startswith("http") and src not in photos:
            photos.append(src)
    result["photos"] = photos

    return result


# --- Main Task ---
def run_task():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking OLX for new ads...")

    new_ads = parse_olx_list(OLX_LIST_URL)
    if not new_ads:
        print("No new OLX ads found.")
        return

    print(f"Found {len(new_ads)} NEW OLX ads!")
    for ad in new_ads:
        details = parse_olx_details(ad["details_url"])

        if details.get("rooms"):
            ad["rooms"] = details["rooms"]

        if details.get("area") is not None:
            ad["area"] = details["area"]

        if details.get("photos"):
            ad["photos"] = details["photos"]

        save_ad(ad)
        print(f" - Saved OLX: {ad['title']} ({ad['price']})")


if __name__ == "__main__":
    init_db()
    print("OLX scraper started. Press Ctrl+C to stop.")
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