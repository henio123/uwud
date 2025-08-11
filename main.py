# main.py - ultra-optimized for VM.Standard.E2.1.Micro (no batch scraping)
import time
import requests
from bs4 import BeautifulSoup
import datetime
import platform
import os
import json as _json
try:
    import ujson as json  # faster if available
except Exception:
    json = _json
from twilio.rest import Client
import csv
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import threading
import traceback

load_dotenv()

# --- Config ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
TO_PHONE_NUMBER = os.getenv("TO_PHONE_NUMBER")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))     # seconds for requests-based checks
PLAYWRIGHT_MULTIPLIER = int(os.getenv("PLAYWRIGHT_MULTIPLIER", "3"))  # playwright runs every CHECK_INTERVAL * multiplier
NOTIFIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notified.json")

# --- Globals & thread-local storage ---
playwright_storage = threading.local()
PLAYWRIGHT_LOCK = threading.RLock()
PLAYWRIGHT_REINIT_LOCK = threading.Lock()
PLAYWRIGHT_FAILURE_COUNT = 0
PLAYWRIGHT_LAST_REINIT = 0
MAX_REINIT_ATTEMPTS = 5
REINIT_BACKOFF_BASE = 2

requests_session = requests.Session()
requests_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
})

# --- Utility functions ---
def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def delattr_safe(obj, name):
    try:
        delattr(obj, name)
    except Exception:
        pass

# --- Playwright init / cleanup with auto-reinit/backoff ---
def init_playwright(force_reinit=False):
    """Init or return single Playwright page. Handles limited auto-reinit/backoff."""
    global PLAYWRIGHT_FAILURE_COUNT, PLAYWRIGHT_LAST_REINIT

    with PLAYWRIGHT_REINIT_LOCK:
        if not force_reinit and hasattr(playwright_storage, 'page'):
            return playwright_storage.page

        now = time.time()
        if PLAYWRIGHT_FAILURE_COUNT >= MAX_REINIT_ATTEMPTS:
            backoff = REINIT_BACKOFF_BASE ** (PLAYWRIGHT_FAILURE_COUNT - MAX_REINIT_ATTEMPTS + 1)
            next_allowed = PLAYWRIGHT_LAST_REINIT + backoff
            if now < next_allowed:
                raise RuntimeError(f"Playwright reinit rate-limited. Try after {int(next_allowed - now)}s")

        # attempt cleanup of any previous
        try:
            cleanup_playwright()
        except Exception:
            pass

        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    '--headless=new',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-web-security',
                    '--disable-features=VizDisplayCompositor',
                    '--disable-extensions',
                    '--disable-software-rasterizer',
                    '--single-process',
                    '--disable-background-networking',
                    '--disable-background-timer-throttling',
                    '--disable-breakpad',
                    '--disable-client-side-phishing-detection',
                    '--disable-component-update',
                    '--disable-default-apps',
                    '--disable-domain-reliability',
                    '--disable-hang-monitor',
                    '--disable-ipc-flooding-protection',
                    '--disable-popup-blocking',
                    '--disable-prompt-on-repost',
                    '--disable-renderer-backgrounding',
                    '--metrics-recording-only',
                    '--no-first-run',
                    '--safebrowsing-disable-auto-update',
                    '--password-store=basic',
                    '--use-mock-keychain'
                ],
                # consider slowMo if debugging, but keep it off for prod
            )
            context = browser.new_context(
                user_agent=requests_session.headers["User-Agent"]
            )
            page = context.new_page()
            page.set_default_timeout(15000)  # 15s

            playwright_storage.playwright = pw
            playwright_storage.browser = browser
            playwright_storage.context = context
            playwright_storage.page = page

            PLAYWRIGHT_FAILURE_COUNT = 0
            PLAYWRIGHT_LAST_REINIT = time.time()
            print(f"[{timestamp()}] ✅ Playwright initialized.")
            return page

        except Exception as e:
            PLAYWRIGHT_FAILURE_COUNT += 1
            PLAYWRIGHT_LAST_REINIT = time.time()
            print(f"[{timestamp()}] ❌ Playwright init failed ({PLAYWRIGHT_FAILURE_COUNT}): {e}")
            traceback.print_exc()
            raise

def cleanup_playwright():
    """Safely cleanup Playwright resources."""
    try:
        if hasattr(playwright_storage, 'page'):
            try:
                playwright_storage.page.close()
            except:
                pass
            delattr_safe(playwright_storage, 'page')
        if hasattr(playwright_storage, 'context'):
            try:
                playwright_storage.context.close()
            except:
                pass
            delattr_safe(playwright_storage, 'context')
        if hasattr(playwright_storage, 'browser'):
            try:
                playwright_storage.browser.close()
            except:
                pass
            delattr_safe(playwright_storage, 'browser')
        if hasattr(playwright_storage, 'playwright'):
            try:
                playwright_storage.playwright.stop()
            except:
                pass
            delattr_safe(playwright_storage, 'playwright')
    except Exception:
        pass

# --- Load selectors/products and build caches ---
def load_selectors(filename="selectors.json"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_products(filename="products.json"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        print(f"❌ Brak pliku {filename}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

SELECTORS = load_selectors()
PRODUCTS = load_products()

# Pre-cache store selectors for speed
STORE_CACHE = {store: SELECTORS.get(store, {}) for store in {p.get("store","unknown") for p in PRODUCTS}}

def build_target_price_map(products):
    pid_to_price = {}
    grouped = {}
    for p in products:
        pid = p.get("product_id")
        if not pid:
            continue
        grouped.setdefault(pid, []).append(p)

    for pid, group in grouped.items():
        for p in group:
            if p.get("target_price") is not None:
                pid_to_price[pid] = p["target_price"]
                break

    return pid_to_price

print(f"✅ Załadowano {len(PRODUCTS)} produktów:")
for p in PRODUCTS:
    print(f"- {p.get('name')} ({p.get('url')})")

# --- Notified storage (minimal) ---
# Structure: { product_key: {"available": bool, "price": "xx zł", "ts": "YYYY-mm-dd hh:mm:ss"} }
def load_notified():
    if not os.path.exists(NOTIFIED_FILE):
        try:
            with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False)
        except:
            pass
        return {}
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # recover
        try:
            with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False)
        except:
            pass
        return {}

def save_notified_if_changed(old, new):
    if old == new:
        return
    try:
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False)
    except Exception as e:
        print(f"[{timestamp()}] ⚠️ Błąd zapisu notified.json: {e}")

# --- Price parsing ---
def parse_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", price_str)
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None

# --- Price extraction helpers ---
def get_price_from_soup(soup, store_selectors):
    """Extract price using selectors; returns string like '123.45 zł' or 'Brak ceny'"""
    discounted_selector = store_selectors.get("price_discounted")
    if discounted_selector:
        discounted = soup.select_one(discounted_selector)
        if discounted:
            price_text = discounted.get_text(strip=True)
            match = re.search(r"[\d\s]{1,7}[.,]\d{2}", price_text)
            if match:
                try:
                    price_str = match.group(0).replace(" ", "").replace(",", ".")
                    return f"{float(price_str):.2f} zł"
                except:
                    pass

    selector = store_selectors.get("price", "")
    if not selector or selector.startswith("xpath="):
        return "Brak ceny (tylko dla Playwright)"

    price_container = soup.select_one(selector)
    if not price_container:
        return "Brak ceny"
    price_text = price_container.get_text(strip=True)
    match = re.search(r"[\d\s]{1,7}[.,]\d{2}", price_text)
    if not match:
        return "Brak ceny"
    try:
        price_str = match.group(0).replace(" ", "").replace(",", ".")
        return f"{float(price_str):.2f} zł"
    except:
        return "Brak ceny"

# --- Notifications (kept minimal & robust) ---
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests_session.post(url, data=payload, timeout=8)
    except:
        pass

def send_to_discord(message):
    if not WEBHOOK_URL:
        return
    data = {"content": message}
    try:
        requests_session.post(WEBHOOK_URL, json=data, timeout=8)
    except:
        pass

def send_sms(message):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TO_PHONE_NUMBER]):
        return
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=message, from_=TWILIO_FROM_NUMBER, to=TO_PHONE_NUMBER)
    except:
        pass

def notify_available(product, price):
    print(f"[{timestamp()}] ✅ {product.get('name')} dostępny za {price}")
    msg = f"@everyone ✅ Produkt **{product.get('name')}** dostępny za **{price}**!\n🔗 {product.get('url')}"
    send_to_discord(msg)
    send_telegram(msg)

def notify_unavailable(product):
    print(f"[{timestamp()}] ❌ {product.get('name')} niedostępny.")

def notify_price_change(product, old_price, new_price):
    print(f"[{timestamp()}] 💸 Cena spadła: {product.get('name')} {old_price} → {new_price}")
    msg = (
        f"@everyone 💸 Cena SPADŁA dla **{product.get('name')}**!\n"
        f"Stara cena: {old_price}\nNowa cena: {new_price}\n{product.get('url')}"
    )
    send_to_discord(msg)
    send_telegram(msg)

def notify_price_increase(product, old_price, new_price):
    print(f"[{timestamp()}] 🔺 Cena wzrosła: {product.get('name')} {old_price} → {new_price}")
    msg = (
        f"@everyone 🔺 Cena WZROSŁA dla **{product.get('name')}**!\n"
        f"Stara cena: {old_price}\nNowa cena: {new_price}\n{product.get('url')}"
    )
    send_to_discord(msg)
    send_telegram(msg)

def log_price_history(product, old_price, new_price):
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.csv")
    first_write = not os.path.exists(log_file)
    try:
        with open(log_file, "a", encoding="utf-8", newline='') as f:
            writer = csv.writer(f)
            if first_write:
                writer.writerow(["timestamp", "product_name", "old_price", "new_price", "url"])
            writer.writerow([timestamp(), product.get('name'), old_price, new_price, product.get('url')])
    except:
        pass

# --- Core availability check ---
def is_available(url, store, max_retries=2, retry_delay=3):
    """Return (available_bool_or_None, price_str_or_None)."""
    store_selectors = STORE_CACHE.get(store, {})
    use_playwright = store_selectors.get("use_selenium", False)

    for attempt in range(max_retries):
        try:
            if use_playwright:
                # Playwright checks — ensure exclusive access via lock
                try:
                    with PLAYWRIGHT_LOCK:
                        try:
                            page = init_playwright()
                        except Exception as e:
                            # unable to init playwright now => backoff and retry outer loop
                            print(f"[{timestamp()}] ⚠️ Playwright not ready: {e}")
                            time.sleep(min(5, REINIT_BACKOFF_BASE ** PLAYWRIGHT_FAILURE_COUNT))
                            continue

                        try:
                            try:
                                page.goto(url, wait_until="networkidle", timeout=12000)
                            except Exception:
                                page.goto(url, wait_until="domcontentloaded", timeout=9000)

                            page.wait_for_timeout(1200)
                            html = page.content()
                            soup = BeautifulSoup(html, "html.parser")
                            price = get_price_from_soup(soup, store_selectors)

                            availability_selector = store_selectors.get("availability", "")
                            unavailability_selector = store_selectors.get("unavailability", "")

                            available = True
                            try:
                                if unavailability_selector and page.locator(unavailability_selector).count() > 0:
                                    available = False
                                elif availability_selector and page.locator(availability_selector).count() == 0:
                                    available = False
                            except Exception:
                                # fallback to text-based
                                txt = html.lower()
                                if any(x in txt for x in ["brak w magazynie", "wyprzedany", "niedostępny", "brak towaru"]):
                                    available = False

                            return available, price

                        except Exception as e:
                            # runtime error in Playwright: cleanup & allow retry
                            print(f"[{timestamp()}] ⚠️ Playwright runtime error for {url}: {e}")
                            traceback.print_exc()
                            try:
                                cleanup_playwright()
                            except:
                                pass
                            time.sleep(1)
                            continue
                except Exception as e:
                    print(f"[{timestamp()}] ⚠️ Playwright outer error: {e}")
                    time.sleep(1)
                    continue

            else:
                # Requests-based scraping (fast path)
                resp = requests_session.get(url, timeout=10)
                if resp.status_code == 404:
                    return None, None
                resp.raise_for_status()
                # Use BeautifulSoup but avoid heavy operations: parse minimally
                soup = BeautifulSoup(resp.text, "html.parser")
                price = get_price_from_soup(soup, store_selectors)

                availability_selector = store_selectors.get("availability", "")
                unavailability_selector = store_selectors.get("unavailability", "")

                available = True
                if unavailability_selector:
                    el = soup.select_one(unavailability_selector)
                    if el and any(w in el.get_text(strip=True).lower() for w in ["brak", "wyprzedany", "niedostępny"]):
                        available = False
                elif availability_selector and not soup.select_one(availability_selector):
                    available = False

                return available, price

        except requests.exceptions.HTTPError as e:
            if getattr(e, 'response', None) is not None and e.response.status_code == 404:
                print(f"[{timestamp()}] ⚠️ Produkt nie istnieje (404): {url}")
                return None, None
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return False, "Brak ceny"
        except Exception as e:
            print(f"[{timestamp()}] ⚠️ Error checking {url}: {e}")
            traceback.print_exc()
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            return False, "Brak ceny"

    return False, "Brak ceny"

# --- Product checking logic using minimal notified storage ---
def product_key(product):
    # prefer product_id if present and unique, else fall back to url
    return str(product.get("product_id") or product.get("url") or product.get("name"))

def check_product(product, notified):
    try:
        key = product_key(product)
        store = product.get("store", "unknown")
        name = product.get("name")
        target_price = product.get("target_price")

        available, price = is_available(product.get("url"), store)
        if available is None and price is None:
            # not found or skip
            return False  # no change

        prev = notified.get(key, {})
        last_available = prev.get("available")
        last_price = prev.get("price")

        current_price_value = parse_price(price)
        # Update logic
        changed = False

        # became available
        if available and last_available is not True:
            notified[key] = {"available": True, "price": price, "ts": timestamp()}
            changed = True
            if target_price is None or (current_price_value is not None and current_price_value <= target_price):
                notify_available(product, price)
            if last_price and last_price != price:
                log_price_history(product, last_price, price)

        # became unavailable
        elif not available and last_available is not False:
            notified[key] = {"available": False, "price": price, "ts": timestamp()}
            changed = True
            notify_unavailable(product)

        # still available but price changed
        elif available and price and last_price and price != last_price:
            old_val = parse_price(last_price)
            new_val = current_price_value
            if old_val is not None and new_val is not None:
                if new_val < old_val:
                    if target_price is None or new_val <= target_price:
                        notify_price_change(product, last_price, price)
                elif new_val > old_val:
                    notify_price_increase(product, last_price, price)
                log_price_history(product, last_price, price)
            notified[key] = {"available": True, "price": price, "ts": timestamp()}
            changed = True

        # first time seeing product
        elif key not in notified:
            notified[key] = {"available": bool(available), "price": price, "ts": timestamp()}
            changed = True

        return changed

    except Exception as e:
        print(f"[{timestamp()}] ⚠️ Błąd przy {product.get('name')}: {e}")
        traceback.print_exc()
        return False

# --- Main loop: split cadence for requests and playwright ---
def main():
    notified = load_notified()
    selenium_products = [p for p in PRODUCTS if STORE_CACHE.get(p.get("store",""), {}).get("use_selenium")]
    simple_products = [p for p in PRODUCTS if not STORE_CACHE.get(p.get("store",""), {}).get("use_selenium")]
    target_price_map = build_target_price_map(PRODUCTS)

    print(f"📊 Playwright products: {len(selenium_products)}")
    print(f"📊 Requests products: {len(simple_products)}")

    playwright_interval = CHECK_INTERVAL * PLAYWRIGHT_MULTIPLIER
    last_playwright_run = 0

    # pre-init Playwright if we have playwright products
    if selenium_products:
        try:
            init_playwright()
        except Exception:
            print(f"[{timestamp()}] ⚠️ Playwright initial init failed — will attempt on demand.")

    try:
        while True:
            cycle_start = time.time()
            print(f"\n[{timestamp()}] 🔍 Starting requests checks ({len(simple_products)} items)...")

            any_change = False

            # requests-based products: small pool
            if simple_products:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {executor.submit(check_product, p, notified): p for p in simple_products}
                    for fut in as_completed(futures):
                        try:
                            changed = fut.result()
                            any_change = any_change or bool(changed)
                        except Exception as e:
                            print(f"[{timestamp()}] ⚠️ Error in requests thread: {e}")

            # Playwright products: run less frequently
            now = time.time()
            if selenium_products and (now - last_playwright_run) >= playwright_interval:
                print(f"[{timestamp()}] 🎭 Starting Playwright checks ({len(selenium_products)} items)...")
                if selenium_products:
                    # single worker to avoid page concurrency; PLAYWRIGHT_LOCK ensures exclusive access
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        futures = {executor.submit(check_product, p, notified): p for p in selenium_products}
                        for fut in as_completed(futures):
                            try:
                                changed = fut.result()
                                any_change = any_change or bool(changed)
                            except Exception as e:
                                print(f"[{timestamp()}] ⚠️ Error in Playwright thread: {e}")
                last_playwright_run = time.time()

            # Save notified only if changed
            prev_state = load_notified()  # lightweight because file small
            if any_change or prev_state != notified:
                save_notified_if_changed(prev_state, notified)

            # sleep until next requests cycle (but preserve cadence)
            elapsed = time.time() - cycle_start
            to_sleep = CHECK_INTERVAL - elapsed
            if to_sleep > 0:
                print(f"[{timestamp()}] ⏳ Sleeping {int(to_sleep)}s until next requests cycle...")
                time.sleep(to_sleep)
            else:
                # immediate next cycle if we ran long
                print(f"[{timestamp()}] ⚠️ Cycle took longer ({int(elapsed)}s) than CHECK_INTERVAL ({CHECK_INTERVAL}s). Starting next cycle immediately.")

    except KeyboardInterrupt:
        print(f"\n[{timestamp()}] 🛑 Stopping monitor (keyboard interrupt).")
    finally:
        cleanup_playwright()
        print(f"[{timestamp()}] ✅ All resources closed.")

if __name__ == "__main__":
    main()
