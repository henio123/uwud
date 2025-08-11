import time
import requests
from bs4 import BeautifulSoup
import datetime
import platform
import os
import json
from twilio.rest import Client
import csv
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import threading

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
TO_PHONE_NUMBER = os.getenv("TO_PHONE_NUMBER")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 120
NOTIFIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notified.json")

# Thread-local storage for Playwright instances
playwright_storage = threading.local()

def get_playwright_instance():
    """Get a thread-local Playwright instance"""
    if not hasattr(playwright_storage, 'playwright'):
        playwright_storage.playwright = sync_playwright().start()
        playwright_storage.browser = playwright_storage.playwright.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-web-security',
                '--disable-features=VizDisplayCompositor'
            ]
        )
    return playwright_storage.playwright, playwright_storage.browser

def cleanup_playwright():
    """Clean up thread-local Playwright resources"""
    if hasattr(playwright_storage, 'browser'):
        try:
            playwright_storage.browser.close()
        except:
            pass
    if hasattr(playwright_storage, 'playwright'):
        try:
            playwright_storage.playwright.stop()
        except:
            pass

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

SELECTORS = load_selectors()
PRODUCTS = load_products()

print(f"✅ Załadowano {len(PRODUCTS)} produktów:")
for p in PRODUCTS:
    print(f"- {p['name']} ({p['url']})")

def parse_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", price_str)
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None

def load_notified():
    if not os.path.exists(NOTIFIED_FILE):
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}

def save_notified(data):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_price(soup, store):
    selectors = SELECTORS.get(store, {})

    # 1. Discounted price
    discounted_selector = selectors.get("price_discounted")
    if discounted_selector:
        discounted = soup.select_one(discounted_selector)
        if discounted:
            price_text = discounted.get_text(strip=True)
            match = re.search(r"[\d\s]{1,7}[.,]\d{2}", price_text)
            if match:
                try:
                    price_str = match.group(0).replace(" ", "").replace(",", ".")
                    value = float(price_str)
                    return f"{value:.2f} zł"
                except ValueError:
                    pass

    # 2. Standard price
    selector = selectors.get("price", "")
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
        value = float(price_str)
        return f"{value:.2f} zł"
    except ValueError:
        return "Brak ceny"

def is_available(url, store, max_retries=3, retry_delay=5):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    use_playwright = SELECTORS.get(store, {}).get("use_selenium", False)

    for attempt in range(max_retries):
        try:
            price = "Brak ceny"

            if use_playwright:
                try:
                    playwright, browser = get_playwright_instance()
                    context = browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                    page = context.new_page()
                    
                    # Set longer timeout and better error handling
                    page.set_default_timeout(30000)  # 30 seconds
                    
                    try:
                        # Try to load the page with different wait strategies
                        try:
                            page.goto(url, wait_until="networkidle", timeout=20000)
                        except Exception as e:
                            print(f"[{timestamp()}] ⚠️ networkidle failed, trying domcontentloaded: {e}")
                            page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        
                        # Wait a bit for dynamic content
                        page.wait_for_timeout(3000)
                        
                        html = page.content()
                        soup = BeautifulSoup(html, "html.parser")
                        price = get_price(soup, store)

                        availability_selector = SELECTORS.get(store, {}).get("availability", "")
                        unavailability_selector = SELECTORS.get(store, {}).get("unavailability", "")

                        def try_selector(selector):
                            try:
                                if selector.startswith("xpath="):
                                    xpath = selector.replace("xpath=", "")
                                    el = page.locator(f"xpath={xpath}")
                                elif selector.startswith("text="):
                                    # Handle text-based selectors
                                    text = selector.replace("text=", "")
                                    el = page.locator(f"text={text}")
                                elif selector.startswith("contains="):
                                    # Handle contains-based selectors
                                    text = selector.replace("contains=", "")
                                    el = page.locator(f":has-text('{text}')")
                                elif ":" in selector and "has-text" in selector:
                                    # Handle :has-text() selectors directly
                                    el = page.locator(selector)
                                else:
                                    el = page.locator(selector)
                                return el.count() > 0 and el.first.is_visible()
                            except Exception as e:
                                print(f"[{timestamp()}] ⚠️ Błąd selektora '{selector}': {e}")
                                return False

                        # Check for text-based availability/unavailability
                        def check_text_availability():
                            # Common Polish unavailability phrases
                            unavailable_phrases = [
                                "brak w magazynie", "niedostępny", "wyprzedany", 
                                "brak towaru", "chwilowo niedostępny", "out of stock",
                                "sold out", "not available"
                            ]
                            
                            # Common Polish availability phrases
                            available_phrases = [
                                "dodaj do koszyka", "kup teraz", "do koszyka",
                                "add to cart", "buy now", "dostępny", "w magazynie"
                            ]
                            
                            page_text = page.content().lower()
                            
                            # Check for unavailability phrases
                            for phrase in unavailable_phrases:
                                if phrase in page_text:
                                    return False
                            
                            # Check for availability phrases
                            for phrase in available_phrases:
                                if phrase in page_text:
                                    return True
                            
                            return None  # Uncertain
                        
                        available = True  # Default assumption
                        
                        if unavailability_selector and try_selector(unavailability_selector):
                            available = False
                        elif availability_selector:
                            available = try_selector(availability_selector)
                        else:
                            # Fallback to text-based detection
                            text_result = check_text_availability()
                            if text_result is not None:
                                available = text_result

                        return available, price
                        
                    finally:
                        # Always close context
                        try:
                            context.close()
                        except:
                            pass
                            
                except Exception as e:
                    print(f"[{timestamp()}] ⚠️ Playwright error for {url}: {e}")
                    if attempt == max_retries - 1:
                        # On final attempt, try to cleanup and recreate
                        cleanup_playwright()
                    raise
                    
            else:
                # Regular requests handling
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code == 404:
                    print(f"[{timestamp()}] ⚠️ Produkt nie znaleziony (404): {url}")
                    return None, None
                resp.raise_for_status()

                soup = BeautifulSoup(resp.text, "html.parser")
                price = get_price(soup, store)

                availability_selector = SELECTORS.get(store, {}).get("availability", "")
                unavailability_selector = SELECTORS.get(store, {}).get("unavailability", "")

                available = True  # Default assumption
                
                if unavailability_selector:
                    el = soup.select_one(unavailability_selector)
                    if el and any(w in el.get_text(strip=True).lower() for w in ["brak", "wyprzedany", "niedostępny"]):
                        available = False
                elif availability_selector:
                    el = soup.select_one(availability_selector)
                    if not el:
                        available = False

                return available, price

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                print(f"[{timestamp()}] ⚠️ Produkt nie istnieje (404): {url}")
                return None, None
            raise
        except Exception as e:
            print(f"[{timestamp()}] ⚠️ Błąd przy sprawdzaniu {url} (próba {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                print(f"[{timestamp()}] ⏳ Próba ponownego sprawdzenia za {retry_delay} sekund...")
                time.sleep(retry_delay)
            else:
                print(f"[{timestamp()}] ❌ Maksymalna liczba prób wyczerpana dla {url}")
                return False, "Brak ceny"

    return False, "Brak ceny"

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN lub TELEGRAM_CHAT_ID nieustawione")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code == 200:
            print("✅ Telegram wysłany")
        else:
            print(f"❌ Błąd Telegram: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Wyjątek Telegram: {e}")

def send_to_discord(message):
    if not WEBHOOK_URL:
        print("⚠️ WEBHOOK_URL nieustawiony")
        return
    data = {"content": message}
    try:
        response = requests.post(WEBHOOK_URL, json=data, timeout=10)
        if response.status_code in [200, 204]:
            print("✅ Wiadomość wysłana na Discorda.")
        else:
            print(f"❌ Błąd Discord: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Błąd Discord: {e}")

def send_sms(message):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TO_PHONE_NUMBER]):
        print("⚠️ Twilio credentials nieustawione")
        return
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        sms = client.messages.create(body=message, from_=TWILIO_FROM_NUMBER, to=TO_PHONE_NUMBER)
        print(f"📱 SMS wysłany! SID: {sms.sid}")
    except Exception as e:
        print(f"❌ Błąd SMS: {e}")

def notify_available(product, price):
    print(f"[{timestamp()}] ✅ {product['name']} dostępny! Cena: {price}")
    discord_message = f"@everyone ✅ Produkt **{product['name']}** dostępny za **{price}**!\n🔗 {product['url']}"
    sms_message = f"{product['name']} za {price}. Link: {product['url']}"
    send_to_discord(discord_message)
    send_telegram(sms_message)
    play_sound()

def notify_unavailable(product):
    print(f"[{timestamp()}] ❌ {product['name']} niedostępny.")

def notify_price_change(product, old_price, new_price):
    print(f"[{timestamp()}] 💸 Cena spadła dla {product['name']}! {old_price} → {new_price}")
    msg = (
        f"@everyone 💸 Cena SPADŁA dla **{product['name']}**!\n"
        f"Stara cena: {old_price}\nNowa cena: {new_price}\n"
        f"{product['url']}"
    )
    send_to_discord(msg)
    send_telegram(msg)

def notify_price_increase(product, old_price, new_price):
    target_price = product.get("target_price")
    new_val = parse_price(new_price)
    if target_price is not None and new_val is not None and new_val > target_price:
        return  # Nie wysyłaj powiadomienia, jeśli cena przekracza target

    print(f"[{timestamp()}] 🔺 Cena wzrosła dla {product['name']}! {old_price} → {new_price}")
    msg = (
        f"@everyone 🔺 Cena WZROSŁA dla **{product['name']}**!\n"
        f"Stara cena: {old_price}\nNowa cena: {new_price}\n"
        f"{product['url']}"
    )
    send_to_discord(msg)
    send_telegram(msg)

def play_sound():
    try:
        system = platform.system()
        if system == "Windows":
            import winsound
            winsound.Beep(1000, 500)
        elif system == "Darwin":
            os.system("afplay /System/Library/Sounds/Ping.aiff")
        else:
            os.system("echo -e '\a'")
    except Exception as e:
        print(f"⚠️ Nie udało się odtworzyć dźwięku: {e}")

def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_price_history(product, old_price, new_price):
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.csv")
    first_write = not os.path.exists(log_file)
    try:
        with open(log_file, "a", encoding="utf-8", newline='') as f:
            writer = csv.writer(f)
            if first_write:
                writer.writerow(["timestamp", "product_name", "old_price", "new_price", "url"])
            writer.writerow([timestamp(), product['name'], old_price, new_price, product['url']])
    except Exception as e:
        print(f"⚠️ Błąd przy zapisie historii cen: {e}")

def check_product(product, notified, group_target_price=None):
    try:
        store = product.get("store", "unknown")
        name = product["name"]

        if store not in notified:
            notified[store] = {}

        available, price = is_available(product["url"], store)

        if available is None and price is None:
            print(f"[{timestamp()}] ⚠️ Pomijanie produktu '{name}' — brak strony.")
            return  # Pomijamy produkt

        previous_entry = notified[store].get(name, {})
        last_state = previous_entry.get("available")
        old_price = previous_entry.get("price")

        current_price_value = parse_price(price)
        target_price = group_target_price or product.get("target_price")

        if available and last_state != True:
            notified[store][name] = {"available": True, "price": price, "timestamp": timestamp()}
            if target_price is None or (current_price_value is not None and current_price_value <= target_price):
                notify_available(product, price)
            if old_price and price != old_price:
                old_val = parse_price(old_price)
                new_val = current_price_value
                if old_val is not None and new_val is not None:
                    log_price_history(product, old_price, price)

        elif not available and last_state != False:
            notify_unavailable(product)
            notified[store][name] = {"available": False, "price": price, "timestamp": timestamp()}

        elif available and price and old_price and price != old_price:
            old_val = parse_price(old_price)
            new_val = current_price_value
            if old_val is not None and new_val is not None:
                if new_val < old_val:
                    if target_price is None or new_val <= target_price:
                        notify_price_change(product, old_price, price)
                elif new_val > old_val:
                    notify_price_increase(product, old_price, price)
                log_price_history(product, old_price, price)
                notified[store][name]["price"] = price
                notified[store][name]["timestamp"] = timestamp()

    except Exception as e:
        print(f"[{timestamp()}] ⚠️ Błąd przy {product['name']}: {e}")

def main():
    notified = load_notified()
    selenium_products = [p for p in PRODUCTS if SELECTORS.get(p["store"], {}).get("use_selenium")]
    simple_products = [p for p in PRODUCTS if not SELECTORS.get(p["store"], {}).get("use_selenium")]
    target_price_map = build_target_price_map(PRODUCTS)

    print(f"📊 Produkty wymagające Playwright: {len(selenium_products)}")
    print(f"📊 Produkty używające requests: {len(simple_products)}")

    try:
        # Use fewer threads for Playwright products to avoid resource conflicts
        playwright_max_workers = min(2, len(selenium_products)) if selenium_products else 1
        requests_max_workers = min(5, len(simple_products)) if simple_products else 1
        
        while True:
            print(f"\n[{timestamp()}] 🔍 Sprawdzanie produktów...\n")
            
            # Process requests-based products first (they're faster and more reliable)
            if simple_products:
                print(f"[{timestamp()}] 📡 Sprawdzanie {len(simple_products)} produktów (requests)...")
                with ThreadPoolExecutor(max_workers=requests_max_workers) as executor:
                    futures = [
                        executor.submit(check_product, p, notified, target_price_map.get(p.get("product_id")))
                        for p in simple_products
                    ]
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            print(f"[{timestamp()}] ⚠️ Błąd w wątku requests: {e}")

            # Process Playwright products with more limited concurrency
            if selenium_products:
                print(f"[{timestamp()}] 🎭 Sprawdzanie {len(selenium_products)} produktów (Playwright)...")
                with ThreadPoolExecutor(max_workers=playwright_max_workers) as executor:
                    futures = [
                        executor.submit(check_product, p, notified, target_price_map.get(p.get("product_id")))
                        for p in selenium_products
                    ]
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            print(f"[{timestamp()}] ⚠️ Błąd w wątku Playwright: {e}")

            save_notified(notified)

            print(f"\n[{timestamp()}] ⏳ Następne sprawdzenie za {CHECK_INTERVAL} sekund...\n")
            for remaining in range(CHECK_INTERVAL, 0, -1):
                print(f"\r[{timestamp()}] ⏳ Odliczanie: {remaining} sekund ", end="", flush=True)
                time.sleep(1)
            print()
            
    except KeyboardInterrupt:
        print(f"\n[{timestamp()}] 🛑 Zatrzymywanie monitorowania...")
    finally:
        # Clean up any remaining Playwright resources
        cleanup_playwright()
        print(f"[{timestamp()}] ✅ Zamknięto wszystkie zasoby.")

if __name__ == "__main__":
    main()