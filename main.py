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
import logging
import signal
import sys
if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('price_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

# Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
TO_PHONE_NUMBER = os.getenv("TO_PHONE_NUMBER")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
NOTIFIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notified.json")

# Global flag for graceful shutdown
shutdown_requested = False

def signal_handler(sig, frame):
    global shutdown_requested
    logger.info(f"Received signal {sig}. Initiating graceful shutdown...")
    shutdown_requested = True

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def health_check():
    """Simple health check endpoint"""
    try:
        # Test internet connectivity
        requests.get("https://google.com", timeout=5)

        selectors_path = os.path.join(BASE_DIR, "selectors.json")
        products_path = os.path.join(BASE_DIR, "products.json")

        # Test required files exist
        if not os.path.exists(selectors_path):
            raise FileNotFoundError(f"selectors.json not found at {selectors_path}")
        if not os.path.exists(products_path):
            raise FileNotFoundError(f"products.json not found at {products_path}")

        logger.info("Health check passed")
        return True
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return False


def load_selectors(filename="selectors.json"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading selectors: {e}")
        return {}

def load_products(filename="products.json"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        logger.error(f"❌ Missing file {filename}")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading products: {e}")
        return []

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
        logger.warning("Corrupted notified.json, recreating...")
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}

def save_notified(data):
    try:
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving notified data: {e}")

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
    import time
    import requests
    from bs4 import BeautifulSoup
    from playwright.sync_api import sync_playwright

    headers = {"User-Agent": "Mozilla/5.0"}
    use_playwright = SELECTORS.get(store, {}).get("use_selenium", False)

    attempt = 0
    while attempt < max_retries:
        try:
            price = "Brak ceny"

            if use_playwright:
                with sync_playwright() as p:
                    # Memory-optimized browser launch
                    browser = p.chromium.launch(
                        headless=True,
                        args=[
                            '--no-sandbox',
                            '--disable-dev-shm-usage',  # Reduces memory usage
                            '--disable-gpu',
                            '--disable-features=VizDisplayCompositor',
                            '--memory-pressure-off',
                            '--max_old_space_size=512',  # Limit V8 memory
                            '--disable-background-timer-throttling',
                            '--disable-backgrounding-occluded-windows',
                            '--disable-renderer-backgrounding'
                        ]
                    )
                    
                    # Create context with memory-conscious settings
                    context = browser.new_context(
                        viewport={'width': 1280, 'height': 720},  # Smaller viewport
                        java_script_enabled=True,
                        ignore_https_errors=True
                    )
                    
                    page = context.new_page()
                    page.set_extra_http_headers({
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36"
                    })
                    
                    try:
                        try:
                            page.goto(url, wait_until="networkidle", timeout=15000)
                        except Exception:
                            logger.warning(f"networkidle failed, trying domcontentloaded...")
                            page.goto(url, wait_until="domcontentloaded", timeout=15000)

                        page.wait_for_timeout(2000)
                        html = page.content()
                        soup = BeautifulSoup(html, "html.parser")
                        price = get_price(soup, store)

                        availability_selector = SELECTORS.get(store, {}).get("availability", "")
                        unavailability_selector = SELECTORS.get(store, {}).get("unavailability", "")

                        def try_selector(selector):
                            if selector.startswith("xpath="):
                                xpath = selector.replace("xpath=", "")
                                el = page.locator(f"xpath={xpath}")
                            else:
                                el = page.locator(selector)
                            return el.count() > 0 and el.first.is_visible()

                        available = False
                        if unavailability_selector and try_selector(unavailability_selector):
                            available = False
                        elif availability_selector and try_selector(availability_selector):
                            available = True
                        else:
                            available = False

                    finally:
                        # Ensure proper cleanup
                        try:
                            page.close()
                        except:
                            pass
                        try:
                            context.close()
                        except:
                            pass
                        try:
                            browser.close()
                        except:
                            pass

                    return available, price
                    
            else:
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 404:
                    logger.warning(f"Product not found (404): {url}")
                    return None, None
                resp.raise_for_status()

                soup = BeautifulSoup(resp.text, "html.parser")
                price = get_price(soup, store)

                availability_selector = SELECTORS.get(store, {}).get("availability", "")
                unavailability_selector = SELECTORS.get(store, {}).get("unavailability", "")

                if unavailability_selector:
                    el = soup.select_one(unavailability_selector)
                    if el and any(w in el.get_text(strip=True).lower() for w in ["brak", "wyprzedany", "niedostępny"]):
                        return False, price
                if availability_selector:
                    el = soup.select_one(availability_selector)
                    if el:
                        return True, price

                return False, price

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.warning(f"Product doesn't exist (404): {url}")
                return None, None
            raise
        except Exception as e:
            logger.error(f"Error checking {url} (attempt {attempt + 1}/{max_retries}): {e}")
            attempt += 1
            if attempt < max_retries:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Max retries exceeded for {url}")
                return False, "Brak ceny"

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code == 200:
            logger.info("Telegram sent successfully")
        else:
            logger.error(f"Telegram error: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Telegram exception: {e}")

def send_to_discord(message):
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL not set")
        return
    data = {"content": message}
    try:
        response = requests.post(WEBHOOK_URL, json=data, timeout=10)
        if response.status_code in [200, 204]:
            logger.info("Discord message sent successfully")
        else:
            logger.error(f"Discord error: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Discord error: {e}")

def send_sms(message):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TO_PHONE_NUMBER]):
        logger.warning("Twilio credentials not complete")
        return
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        sms = client.messages.create(body=message, from_=TWILIO_FROM_NUMBER, to=TO_PHONE_NUMBER)
        logger.info(f"SMS sent! SID: {sms.sid}")
    except Exception as e:
        logger.error(f"SMS error: {e}")

def notify_available(product, price):
    logger.info(f"✅ {product['name']} available! Price: {price}")
    discord_message = f"@everyone ✅ Product **{product['name']}** available for **{price}**!\n🔗 {product['url']}"
    sms_message = f"{product['name']} for {price}. Link: {product['url']}"
    send_to_discord(discord_message)
    send_telegram(sms_message)
    play_sound()

def notify_unavailable(product):
    logger.info(f"❌ {product['name']} unavailable")

def notify_price_change(product, old_price, new_price):
    logger.info(f"💸 Price dropped for {product['name']}! {old_price} → {new_price}")
    msg = (
        f"@everyone 💸 Price DROPPED for **{product['name']}**!\n"
        f"Old price: {old_price}\nNew price: {new_price}\n"
        f"{product['url']}"
    )
    send_to_discord(msg)
    send_telegram(msg)

def notify_price_increase(product, old_price, new_price):
    target_price = product.get("target_price")
    new_val = parse_price(new_price)
    if target_price is not None and new_val is not None and new_val > target_price:
        return  # Don't notify if price exceeds target

    logger.info(f"🔺 Price increased for {product['name']}! {old_price} → {new_price}")
    msg = (
        f"@everyone 🔺 Price INCREASED for **{product['name']}**!\n"
        f"Old price: {old_price}\nNew price: {new_price}\n"
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
        logger.error(f"Error playing sound: {e}")

def log_price_history(product, old_price, new_price):
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.csv")
    first_write = not os.path.exists(log_file)
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            if first_write:
                f.write("timestamp,product_name,old_price,new_price,url\n")
            f.write(f"{datetime.datetime.now().isoformat()},{product['name']},{old_price},{new_price},{product['url']}\n")
    except Exception as e:
        logger.error(f"Error logging price history: {e}")

def check_product(product, notified, group_target_price=None):
    try:
        store = product.get("store", "unknown")
        name = product["name"]

        if store not in notified:
            notified[store] = {}

        available, price = is_available(product["url"], store)

        if available is None and price is None:
            logger.warning(f"Skipping product '{name}' — page not found")
            return

        previous_entry = notified[store].get(name, {})
        last_state = previous_entry.get("available")
        old_price = previous_entry.get("price")

        current_price_value = parse_price(price)
        target_price = group_target_price or product.get("target_price")

        if available and last_state != True:
            notified[store][name] = {
                "available": True, 
                "price": price, 
                "timestamp": datetime.datetime.now().isoformat()
            }
            if target_price is None or (current_price_value is not None and current_price_value <= target_price):
                notify_available(product, price)
            if old_price and price != old_price:
                old_val = parse_price(old_price)
                new_val = current_price_value
                if old_val is not None and new_val is not None:
                    log_price_history(product, old_price, price)

        elif not available and last_state != False:
            notify_unavailable(product)
            notified[store][name] = {
                "available": False, 
                "price": price, 
                "timestamp": datetime.datetime.now().isoformat()
            }

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
                notified[store][name]["timestamp"] = datetime.datetime.now().isoformat()

    except Exception as e:
        logger.error(f"Error checking {product['name']}: {e}")

def main():
    logger.info("Starting price monitor...")
    
    # Initial health check
    if not health_check():
        logger.error("Initial health check failed. Exiting.")
        sys.exit(1)
    
    # Load configuration
    global SELECTORS, PRODUCTS
    SELECTORS = load_selectors()
    PRODUCTS = load_products()
    
    if not PRODUCTS:
        logger.error("No products loaded. Exiting.")
        sys.exit(1)
    
    logger.info(f"Loaded {len(PRODUCTS)} products")
    
    notified = load_notified()
    selenium_products = [p for p in PRODUCTS if SELECTORS.get(p["store"], {}).get("use_selenium")]
    simple_products = [p for p in PRODUCTS if not SELECTORS.get(p["store"], {}).get("use_selenium")]
    target_price_map = build_target_price_map(PRODUCTS)

    with ThreadPoolExecutor(max_workers=2) as executor:
        while not shutdown_requested:
            try:
                logger.info("🔍 Checking products...")
                
                # Process products in groups
                for group in [simple_products, selenium_products]:
                    if shutdown_requested:
                        break
                    futures = [
                        executor.submit(check_product, p, notified, target_price_map.get(p.get("product_id")))
                        for p in group
                    ]
                    for future in as_completed(futures):
                        if shutdown_requested:
                            break
                        try:
                            future.result()  # This will raise any exceptions
                        except Exception as e:
                            logger.error(f"Product check failed: {e}")
                
                save_notified(notified)
                
                # Countdown with interruption check
                logger.info(f"Next check in {CHECK_INTERVAL} seconds...")
                for remaining in range(CHECK_INTERVAL, 0, -1):
                    if shutdown_requested:
                        break
                    time.sleep(1)
                    if remaining % 30 == 0:  # Log every 30 seconds
                        logger.info(f"Next check in {remaining} seconds...")
                        
            except KeyboardInterrupt:
                logger.info("Received keyboard interrupt")
                break
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")
                time.sleep(60)  # Wait before retrying
    

if __name__ == "__main__":
    main()