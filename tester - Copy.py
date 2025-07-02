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


TWILIO_ACCOUNT_SID = ""
TWILIO_AUTH_TOKEN = ""
TWILIO_FROM_NUMBER = ""  
TO_PHONE_NUMBER = ""   
WEBHOOK_URL = "" 



CHECK_INTERVAL = 120  # co ile sekund sprawdzać
NOTIFIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notified.json")

from .empik import get_selectors as empik
from .juniorkurczak import get_selectors as juniorkurczak
from .swiatkart import get_selectors as swiatkart
from .loficards import get_selectors as loficards
from .strefa_tcg import get_selectors as strefa_tcg

SELECTORS = {
    "empik": empik(),
    "juniorkurczak": juniorkurczak(),
    "swiatkart": swiatkart(),
    "loficards": loficards(),
    "strefa_tcg": strefa_tcg(),
}



def parse_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", price_str)
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None 

def load_products(filename="products.json"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        print(f"❌ Brak pliku {filename}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

PRODUCTS = load_products()

print(f"✅ Załadowano {len(PRODUCTS)} produktów:")
for p in PRODUCTS:
    print(f"- {p['name']} ({p['url']})")

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
    selector = SELECTORS.get(store, {}).get("price", "")
    if not selector:
        return "Brak ceny"
    price_tag = soup.select_one(selector)
    if price_tag:
        return price_tag.get_text(strip=True)
    return "Brak ceny"

def is_available(url, store, max_retries=3, retry_delay=5):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    attempt = 0
    while attempt < max_retries:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            availability_selector = SELECTORS.get(store, {}).get("availability", "")
            unavailability_selector = SELECTORS.get(store, {}).get("unavailability", "")
            price = get_price(soup, store)

            if unavailability_selector:
                unavailable_element = soup.select_one(unavailability_selector)
                if unavailable_element:
                    text = unavailable_element.get_text(strip=True).lower()
                    if any(phrase in text for phrase in ["wyprzedany", "brak", "brak w magazynie"]):
                        return False, price

            if availability_selector:
                availability_element = soup.select_one(availability_selector)
                if availability_element:
                    return True, price

            return False, price

        except Exception as e:
            print(f"[{timestamp()}] ⚠️ Błąd przy sprawdzaniu {url} (próba {attempt + 1}/{max_retries}): {e}")
            attempt += 1
            if attempt < max_retries:
                print(f"[{timestamp()}] ⏳ Próba ponownego sprawdzenia za {retry_delay} sekund...")
                time.sleep(retry_delay)
            else:
                print(f"[{timestamp()}] ❌ Maksymalna liczba prób wyczerpana dla {url}")
                return False, "Brak ceny"
            
def send_to_discord(message):
    data = {"content": message}
    try:
        response = requests.post(WEBHOOK_URL, json=data)
        if response.status_code in [200, 204]:
            print("✅ Wiadomość wysłana na Discorda.")
        else:
            print(f"❌ Błąd przy wysyłaniu Discord: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Błąd przy wysyłaniu Discord: {e}")

def send_sms(message):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        sms = client.messages.create(
            body=message,
            from_=TWILIO_FROM_NUMBER,
            to=TO_PHONE_NUMBER
        )
        print(f"📱 SMS wysłany! SID: {sms.sid}")
    except Exception as e:
        print(f"❌ Błąd podczas wysyłania SMS: {e}")

def notify_available(product, price):
    print(f"[{timestamp()}] ✅ {product['name']} dostępny! Cena: {price}")
    discord_message = f"@everyone ✅ Produkt **{product['name']}** dostępny za **{price}**!\n🔗 {product['url']}"
    sms_message = f"{product['name']} dostępny za {price}. Link: {product['url']}"
    send_to_discord(discord_message)
    #send_sms(sms_message)
    play_sound()

def notify_unavailable(product):
    print(f"[{timestamp()}] ❌ {product['name']} niedostępny.")

def notify_price_change(product, old_price, new_price):
    print(f"[{timestamp()}] 💸 Cena spadła dla {product['name']}! {old_price} → {new_price}")
    discord_message = (
        f"@everyone💸 Cena SPADŁA dla **{product['name']}**!\n"
        f"Stara cena: {old_price}\nNowa cena: {new_price}\n"
        f"{product['url']}"
    )
    sms_message = (
        f"Cena SPADŁA: {product['name']}\n"
        f"Stara: {old_price}\nNowa: {new_price}\n"
        f"Link: {product['url']}"
    )
    send_to_discord(discord_message)
    send_sms(sms_message)

def notify_price_increase(product, old_price, new_price):
    print(f"[{timestamp()}] 🔺 Cena wzrosła dla {product['name']}! {old_price} → {new_price}")
    discord_message = (
        f"@everyone 🔺 Cena WZROSŁA dla **{product['name']}**!\n"
        f"Stara cena: {old_price}\nNowa cena: {new_price}\n"
        f"{product['url']}"
    )
    send_to_discord(discord_message)

def play_sound():
    system = platform.system()
    if system == "Windows":
        import winsound
        winsound.Beep(1000, 500)
    elif system == "Darwin":
        os.system("afplay /System/Library/Sounds/Ping.aiff")
    else:
        os.system("echo -e '\\a'")

def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log_price_history(product, old_price, new_price):
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.csv")
    first_write = not os.path.exists(log_file)
    with open(log_file, "a", encoding="utf-8") as f:
        if first_write:
            f.write("timestamp,product_name,old_price,new_price,url\n")
        f.write(f"{timestamp()},{product['name']},{old_price},{new_price},{product['url']}\n")

def check_product(product, notified):
    try:
        store = product.get("store", "juniorkurczak")
        available, price = is_available(product['url'], store)
        name = product['name']
        previous_entry = notified.get(name, {})
        last_state = previous_entry.get("available")
        old_price = previous_entry.get("price")

        current_price_value = parse_price(price)
        target_price = product.get("target_price")

        messages = []

        if available and last_state != True:
            if target_price is None or (current_price_value is not None and current_price_value <= target_price):
                notify_available(product, price)
                notified[name] = {
                    "available": True,
                    "price": price,
                    "timestamp": timestamp()
                }
            else:
                messages.append(f"Cena {current_price_value} powyżej celu {target_price}")

        elif not available and last_state != False:
            notify_unavailable(product)
            notified[name] = {
                "available": False,
                "price": price,
                "timestamp": timestamp()
            }

        elif available and price and old_price and price != old_price:
            old_val = parse_price(old_price)
            new_val = current_price_value
            if old_val is not None and new_val is not None:
                if new_val < old_val:
                    notify_price_change(product, old_price, price)
                elif new_val > old_val:
                    notify_price_increase(product, old_price, price)
                log_price_history(product, old_price, price)

                notified[name]["price"] = price
                notified[name]["timestamp"] = timestamp()

        else:
            messages.append(f"Brak zmian dla {name}")

        for m in messages:
            print(f"[{timestamp()}] ⏳ {m}")

    except Exception as e:
        print(f"[{timestamp()}] ⚠️ Błąd przy {product['name']}: {e}")

def main():
    notified = load_notified()
    with ThreadPoolExecutor(max_workers=5) as executor:
        while True:
            futures = [executor.submit(check_product, p, notified) for p in PRODUCTS]

            for future in as_completed(futures):
                pass  # Wszystkie taski wykonane, logi są w check_product

            save_notified(notified)

            print(f"\n[{timestamp()}] ⏳ Następne sprawdzenie za {CHECK_INTERVAL} sekund...\n")
            for remaining in range(CHECK_INTERVAL, 0, -1):
                print(f"\r[{timestamp()}] ⏳ Odliczanie: {remaining} sekund ", end="", flush=True)
                time.sleep(1)
            print()

if __name__ == "__main__":
    main()