
import requests
from bs4 import BeautifulSoup
import json
import re
import sqlite3
import time
from datetime import datetime

# --- Database Setup ---
DB_NAME = "otodom_ads.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
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

def is_ad_new(ad_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM ads WHERE ad_id = ?", (ad_id,))
    exists = cursor.fetchone()
    conn.close()
    return exists is None

def save_ad(ad):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO ads (ad_id, title, price, area, rooms, city, province, district, street, details_url, photos, first_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            ad['ad_id'],
            ad['title'],
            ad['price'],
            ad['area'] if isinstance(ad['area'], (int, float)) else None,
            ad['rooms'],
            ad['city'],
            ad['province'],
            ad['district'],
            ad['street'],
            ad['details_url'],
            json.dumps(ad['photos']),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()
    except sqlite3.IntegrityError:
        pass # Already exists
    finally:
        conn.close()

# --- Scraper ---

def parse_otodom(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: Status code {response.status_code}")
            return []
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Request error: {e}")
        return []

    soup = BeautifulSoup(response.content, "html.parser")
    listings = []

    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: Could not find __NEXT_DATA__")
        return []

    try:
        json_data = json.loads(script_tag.string)
        props = json_data.get("props", {})
        pageProps = props.get("pageProps", {})
        data = pageProps.get("data", {})
        searchAds = data.get("searchAds", {})
        items = searchAds.get("items", [])

        if not items:
            items = json_data.get("props", {}).get("pageProps", {}).get("initialProps", {}).get("data", {}).get("searchAds", {}).get("items", [])

        for item in items:
            try:
                ad_id = item.get("id")
                
                # Check if new BEFORE full parsing to save resources
                if not is_ad_new(ad_id):
                    continue

                slug = item.get("slug")
                details_url = f"https://www.otodom.pl/pl/oferta/{slug}" if slug else "N/A"
                title = item.get("title", "N/A")
                
                total_price_obj = item.get("totalPrice") or {}
                price = f"{total_price_obj.get('value', 'N/A')} {total_price_obj.get('currency', 'N/A')}"
                
                area = item.get("areaInSquareMeters", "N/A")
                rooms = item.get("roomsNumber", "N/A")
                
                location = item.get("location", {})
                address = location.get("address", {})
                city = (address.get("city") or {}).get("name", "N/A")
                province = (address.get("province") or {}).get("name", "N/A")
                
                district = "N/A"
                for loc in location.get("reverseGeocoding", {}).get("locations", []):
                    if loc.get("locationLevel") == "district":
                        district = loc.get("name")
                        break

                street = (address.get("street") or {}).get("name", "N/A")
                
                photos = []
                for img in item.get("images", []):
                    photo_url = img.get("large") or img.get("medium")
                    if photo_url: photos.append(photo_url)

                listings.append({
                    "ad_id": ad_id,
                    "title": title,
                    "price": price,
                    "area": area,
                    "rooms": rooms,
                    "city": city,
                    "province": province,
                    "district": district,
                    "street": street,
                    "photos": photos,
                    "details_url": details_url
                })
            except Exception as e:
                continue

    except Exception as e:
        print(f"Parsing error: {e}")

    return listings

def run_task():
    url = "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/mazowieckie/warszawa/warszawa/warszawa?ownerTypeSingleSelect=ALL&by=LATEST&direction=DESC"
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking for new ads...")
    
    new_ads = parse_otodom(url)
    
    if new_ads:
        print(f"Found {len(new_ads)} NEW ads!")
        for ad in new_ads:
            save_ad(ad)
            print(f" - Saved: {ad['title']} ({ad['price']})")
    else:
        print("No new ads found.")

if __name__ == "__main__":
    init_db()
    print("Scraper started. Press Ctrl+C to stop.")
    while True:
        try:
            run_task()
            print(f"Sleeping for 15 minutes...")
            time.sleep(15 * 60)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"Main loop error: {e}")
            time.sleep(60) # Wait a bit before retry if error occurred
