from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import threading
import os, sys
import json
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse
from PIL import Image
from io import BytesIO
import datetime
from playwright.sync_api import sync_playwright
from collections import defaultdict
import plotly.graph_objs as go
from datetime import datetime
import product_detector
import threading
from flask import request, render_template, redirect, url_for, flash
from store_admin import load_stores, save_stores
from store_selector_tool import select_store_selectors
import threading
import urllib.parse


app = Flask(__name__)
app.secret_key = 'supersecretkey'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
PRODUCTS_FILE = os.path.join(BASE_DIR, "products.json")
NOTIFIED_FILE = os.path.join(BASE_DIR, "notified.json")
SELECTORS_FILE = os.path.join(BASE_DIR, "selectors.json")

running_stores = set()


# ====== UTILS ======
def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return []
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [{"name": k, **v} for k, v in data.items()]
    return data

def save_products(products):
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

def load_notified():
    if not os.path.exists(NOTIFIED_FILE):
        return {}
    with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_selectors():
    if not os.path.exists(SELECTORS_FILE):
        return {}
    with open(SELECTORS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_selectors_data(data):
    with open(SELECTORS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def detect_store(url):
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower()

    selectors = load_selectors()
    for store_name in selectors:
        # Obsługa dopasowania np. "poketrader" w "poketrader.eu"
        if store_name in domain or domain.startswith(store_name):
            return store_name
    return ""

def detect_product_name(url, store=None):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        if store:
            selectors = load_selectors()
            title_selector = selectors.get(store, {}).get("title")
            if title_selector:
                el = soup.select_one(title_selector)
                if el:
                    return el.get_text(strip=True)


        title_tag = soup.find("title")
        if title_tag:
            return f"{title_tag.get_text(strip=True)} ({store})"
    except Exception as e:
        print(f"Błąd przy wykrywaniu nazwy: {e}")
    return None

def load_price_history_by_product_id(products):
    import os

    history_file = os.path.join(BASE_DIR, "price_history.csv")
    if not os.path.exists(history_file):
        return {}

    history = {}

    # Mapa: nazwa produktu → jego product_id lub sama nazwa
    name_to_pid = {
        p["name"]: p.get("product_id", p["name"])
        for p in products
    }

    with open(history_file, encoding="utf-8") as f:
        header = next(f)  # pomiń nagłówek

        for line_num, line in enumerate(f, start=2):
            try:
                parts = line.strip().split(",", 4)
                if len(parts) < 5:
                    print(f"⚠️ Pominięto linię {line_num}: zbyt mało pól")
                    continue

                timestamp, name, old_price_raw, new_price_raw, url = parts
                pid = name_to_pid.get(name)
                if not pid:
                    continue

                # Oczyszczanie ceny: usuń zł, spacje, zamień przecinki
                def clean(price):
                    return float(
                        price.replace("zł", "")
                             .replace("PLN", "")
                             .replace(" ", "")  # niełamania spacji
                             .replace(" ", "")
                             .replace(",", ".")
                             .strip()
                    )

                price_val = clean(new_price_raw)

                history.setdefault(pid, []).append({
                    "date": timestamp[:10],
                    "price": price_val
                })

            except Exception as e:
                print(f"⚠️ Błąd w linii {line_num}: {e}")

    return history

# ====== ROUTES ======
@app.route("/")
def index():
    all_products = load_products()
    store_filter = request.args.get("store_filter", "").strip()

    if store_filter:
        products = [p for p in all_products if p.get("store") == store_filter]
    else:
        products = all_products

    notified_data = load_notified()
    selectors = load_selectors()

    # Spłaszczona mapa notified
    notified = {}
    for store, entries in notified_data.items():
        for name, data in entries.items():
            notified[name] = data

    # Ceny docelowe
    target_prices = {
        p["name"]: f'{p["target_price"]} zł'
        for p in products if p.get("target_price")
    }

    # Funkcja sortująca po dostępności i cenie
    def sort_key(p):
        info = notified.get(p["name"], {})
        available = info.get("available", False)
        price_raw = info.get("price")
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            price = float("inf")
        return (0 if available else 1, price)

    # Grupowanie po sklepie
    grouped_products = {}
    for p in products:
        store = p["store"]
        grouped_products.setdefault(store, []).append(p)
    for store, group in grouped_products.items():
        grouped_products[store] = sorted(group, key=sort_key)

    # Grupowanie po product_id ("" oznacza brak ID)
    grouped_by_product = {}
    for p in products:
        pid = p.get("product_id", "")
        grouped_by_product.setdefault(pid, []).append(p)
    for pid, group in grouped_by_product.items():
        grouped_by_product[pid] = sorted(group, key=sort_key)

    # Nazwa reprezentująca każdą grupę logiczną
    product_groups_display = {
        pid: grouped_by_product[pid][0]["name"] for pid in grouped_by_product
    }

    # Lista sklepów do filtra
    stores = sorted({p["store"] for p in all_products if "store" in p})

    return render_template("index.html",
                           price_history=load_price_history_by_product_id(all_products),
                           grouped_products=grouped_products,
                           grouped_by_product=grouped_by_product,
                           product_groups_display=product_groups_display,
                           notified=notified,
                           target_prices=target_prices,
                           stores=stores,
                           selectors=selectors)



@app.route("/history", methods=["GET"])
def history():
    from datetime import datetime

    products = load_products()
    price_history = load_price_history_by_product_id(products)

    # Mapowanie PID → nazwa
    product_names = {}
    for p in products:
        pid = p.get("product_id") or p["name"]
        if pid not in product_names:
            product_names[pid] = p["name"]

    # Parametry filtrowania
    selected_pids = request.args.getlist("pid")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    fig = go.Figure()

    def parse_date(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").date()
        except:
            return None

    df = parse_date(date_from)
    dt = parse_date(date_to)

    # Jeśli nie wybrano żadnych, pokaż wszystkie
    if not selected_pids:
        selected_pids = list(price_history.keys())

    # Kolory produktów (dopasowane do wykresu)
    color_palette = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"
    ]
    pid_colors = {}
    color_index = 0

    for pid in selected_pids:
        entries = price_history.get(pid)
        if not entries:
            continue

        filtered = []
        for entry in entries:
            entry_date = parse_date(entry["date"])
            if df and entry_date and entry_date < df:
                continue
            if dt and entry_date and entry_date > dt:
                continue
            filtered.append(entry)

        if not filtered:
            continue

        dates = [e["date"] for e in filtered]
        prices = [e["price"] for e in filtered]
        color = color_palette[color_index % len(color_palette)]
        pid_colors[pid] = color
        color_index += 1

        fig.add_trace(go.Scatter(
            x=dates,
            y=prices,
            mode="lines+markers",
            name=product_names.get(pid, pid),
            line=dict(color=color)
        ))

    fig.update_layout(
        title="📈 Historia cen produktów",
        xaxis_title="Data",
        yaxis_title="Cena (zł)",
        template="plotly_dark",
        height=600
    )

    graph_html = fig.to_html(full_html=False)

    return render_template("history.html",
                           product_names=product_names,
                           selected_pids=selected_pids,
                           graph_html=graph_html,
                           date_from=date_from,
                           date_to=date_to,
                           pid_colors=pid_colors)



@app.route('/add_product', methods=['POST'])
def add_product():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    store = request.form.get("store", "").strip()
    target_price = request.form.get("target_price", "").strip()
    product_id = request.form.get("product_id", "").strip()  # <=== NOWE

    if not store:
        store = detect_store(url)

    if not name:
        name = detect_product_name(url, store) or ""

    if not name or not url or not store:
        flash("Wszystkie pola poza ceną są wymagane!", "error")
        return redirect(url_for("index"))

    try:
        price = float(target_price) if target_price else None
    except:
        flash("Niepoprawna cena docelowa.", "error")
        return redirect(url_for("index"))

    product = {"name": name, "url": url, "store": store}
    if price is not None:
        product["target_price"] = price
    if product_id:
        product["product_id"] = product_id  # <=== NOWE

    products = load_products()
    products.append(product)
    save_products(products)

    flash("✅ Produkt dodany!")
    return redirect(url_for("index"))

@app.route("/detector_log")
def detector_log():
    path = os.path.join(BASE_DIR, "detector_log.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""

@app.route("/delete_unmatched", methods=["POST"])
def delete_unmatched_entry():
    url = request.form.get("url")
    path = os.path.join(BASE_DIR, "unmatched.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data = [p for p in data if p["url"] != url]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        flash("🗑️ Usunięto niedopasowany produkt", "success")
    except:
        flash("❌ Błąd przy usuwaniu", "error")
    return redirect(url_for("detector"))

@app.route('/delete_product/<product_name>', methods=['POST'])
def delete_product(product_name):
    products = load_products()
    products = [p for p in products if p["name"] != product_name]
    try:
        save_products(products)
        flash(f"Produkt '{product_name}' usunięty.", "success")
    except Exception as e:
        flash(f"Błąd podczas usuwania: {e}", "error")
    return redirect(url_for("index"))

@app.route("/update_target_price_inline", methods=["POST"])
def update_target_price_inline():
    product_name = request.form.get("product_name", "").strip()
    target_price_raw = request.form.get("target_price", "").strip()

    products = load_products()

    # Znajdź product_id dla danego produktu
    product_id = None
    for p in products:
        if p["name"] == product_name:
            product_id = p.get("product_id")
            break

    if not product_id:
        flash("Nie znaleziono ID produktu — nie można zaktualizować grupowo.", "error")
        return redirect(url_for("index"))

    # Przetwórz cenę docelową
    if target_price_raw:
        try:
            new_price = float(target_price_raw.replace(",", "."))
        except:
            flash("Niepoprawna cena.", "error")
            return redirect(url_for("index"))
    else:
        new_price = None

    # Zaktualizuj wszystkie produkty z tym samym product_id
    for p in products:
        if p.get("product_id") == product_id:
            if new_price is not None:
                p["target_price"] = new_price
            else:
                p.pop("target_price", None)

    save_products(products)
    flash(f"Cena docelowa dla ID '{product_id}' została zaktualizowana.", "success")
    return redirect(url_for("index"))

@app.route("/add-store", methods=['POST'])
def add_store():
    store_name = request.form.get('store_name')
    example_url = request.form.get('example_url')

    if not store_name or not example_url:
        flash("Musisz podać nazwę sklepu i przykładowy URL.", "error")
        return redirect(url_for("index"))

    if store_name in running_stores:
        flash(f"Selektory dla sklepu '{store_name}' już są wybierane.", "warning")
        return redirect(url_for("index"))

    selectors = load_selectors()
    if store_name not in selectors:
        selectors[store_name] = {
            "price": "",
            "availability": "",
            "use_selenium": False,
            "title": ""
        }
        save_selectors_data(selectors)
        download_favicon(store_name, example_url)

    running_stores.add(store_name)

    def run_and_clear():
        try:
            run_selector_picker(store_name, example_url)
        finally:
            running_stores.discard(store_name)

    threading.Thread(target=run_and_clear).start()
    flash(f"Uruchomiono wybieranie selektorów dla sklepu '{store_name}'.", "success")
    return redirect(url_for("index"))


@app.route("/save_selectors", methods=["POST"])
def save_selectors():
    store = request.form['store']
    price_selector = request.form['price_selector']
    availability_selector = request.form['availability_selector']
    use_selenium = 'use_selenium' in request.form

    data = load_selectors()
    data[store] = {
        "price": price_selector,
        "availability": availability_selector,
        "use_selenium": use_selenium
    }
    save_selectors_data(data)

    flash(f"Selektory dla sklepu '{store}' zapisane!", "success")
    return redirect(url_for("index"))

@app.route("/detect_name", methods=["POST"])
def detect_name():
    try:
        data = request.get_json()
        url = data.get("url", "")
        if not url:
            return jsonify({"error": "Brak URL"}), 400
        store = detect_store(url)
        name = detect_product_name(url, store)
        if name:
            return jsonify({"name": f"{name} ({store})"})
        else:
            return jsonify({"error": "Nie udało się pobrać nazwy"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ====== SELECTOR PICKER FUNCTION ======
SELECTOR_PICKER_JS = """
if (!window.__selectorPickerInjected__) {
  window.__selectorPickerInjected__ = true;
  window.__selectedSelectors__ = {};

  const pickerStyles = `
  * { cursor: default !important; }
  .__hover-highlight__ {
    outline: 2px dashed red !important;
    animation: pulse 1s infinite;
  }
  .__clicked__highlight__ {
    outline: 3px solid limegreen !important;
    background-color: rgba(144, 238, 144, 0.3) !important;
  }
  @keyframes pulse {
    0% { outline-color: red; }
    50% { outline-color: orange; }
    100% { outline-color: red; }
  }
  #__selector-prompt__ {
    position: fixed !important;
    bottom: 10px !important;
    left: 10px !important;
    background: yellow !important;
    color: black !important;
    padding: 6px 10px !important;
    font-size: 14px !important;
    font-weight: bold !important;
    z-index: 2147483647 !important;
    font-family: sans-serif !important;
    box-shadow: 0 0 5px rgba(0,0,0,0.3) !important;
  }
`;


  const style = document.createElement('style');
  style.textContent = pickerStyles;
  document.head.appendChild(style);

  const prompt = document.createElement('div');
  prompt.id = '__selector-prompt__';
  document.body.appendChild(prompt);

  let currentStep = 0;
  const steps = ['CENA', 'TYTUŁ', 'LOGO', 'DOSTĘPNOŚĆ'];

  function updatePrompt(message = '') {
    if (currentStep < steps.length) {
      prompt.innerText = '🖱️ Kliknij element: ' + steps[currentStep] + '\\n' + message;
    } else {
      prompt.innerText = '✅ Gotowe! Możesz zamknąć stronę.';
    }
  }

  updatePrompt();

  document.body.addEventListener('mouseover', e => {
    e.target.classList.add('__hover-highlight__');
  });

  document.body.addEventListener('mouseout', e => {
    e.target.classList.remove('__hover-highlight__');
  });

  document.body.addEventListener('click', e => {
    if (currentStep >= steps.length) return;

    e.preventDefault();
    e.stopPropagation();

    const el = e.composedPath ? e.composedPath()[0] : e.target;
    const selector = generateUniqueSelector(el);
    const text = el.innerText.trim().slice(0, 80);  // przycinamy

    window.__selectedSelectors__[steps[currentStep]] = selector;
    console.log(`📌 Wybrano [${steps[currentStep]}]:`, selector, '| Tekst:', text);

    // Feedback wizualny
    el.classList.add('__clicked__highlight__');
    setTimeout(() => {
      el.classList.remove('__clicked__highlight__');
    }, 1000);

    currentStep++;
    updatePrompt(`Ostatnio: ${text || '[brak tekstu]'}`);
  });

  function generateUniqueSelector(el) {
    if (!el) return '';

    const parts = [];
    while (el && el.nodeType === 1 && el.tagName.toLowerCase() !== 'html') {
      let selector = el.tagName.toLowerCase();

      if (el.id) {
        selector += `#${el.id}`;
        parts.unshift(selector);
        break;
      } else {
        const siblings = Array.from(el.parentNode.children)
          .filter(child => child.tagName === el.tagName);
        if (siblings.length > 1) {
          const index = siblings.indexOf(el) + 1;
          selector += `:nth-of-type(${index})`;
        }

        parts.unshift(selector);
        el = el.parentNode;
      }
    }

    return parts.join(' > ');
  }
}
"""










def run_selector_picker(store_name, url):
    from playwright.sync_api import sync_playwright
    import os
    import requests
    from bs4 import BeautifulSoup

    steps = ["CENA", "TYTUŁ", "LOGO", "DOSTĘPNOŚĆ"]
    selected = {}

    JS_INJECT = """
    if (!window.__selectorPicker__) {
      window.__selectorPicker__ = {
        step: "",
        result: {},
      };

      const pickerStyles = `
        * { cursor: default !important; }
        .__hover-highlight__ {
          outline: 2px dashed red !important;
          animation: pulse 1s infinite;
        }
        .__clicked__highlight__ {
          outline: 3px solid limegreen !important;
          background-color: rgba(144, 238, 144, 0.3) !important;
        }
        @keyframes pulse {
          0% { outline-color: red; }
          50% { outline-color: orange; }
          100% { outline-color: red; }
        }
        #__selector-prompt__ {
          position: fixed; bottom: 10px; left: 10px;
          background: yellow; color: black; padding: 6px 10px;
          font-size: 14px; font-weight: bold; z-index: 99999;
          font-family: sans-serif;
          box-shadow: 0 0 5px rgba(0,0,0,0.3);
        }
      `;

      const style = document.createElement('style');
      style.textContent = pickerStyles;
      document.head.appendChild(style);

      const prompt = document.createElement('div');
      prompt.id = '__selector-prompt__';
      document.body.appendChild(prompt);

      const steps = ['CENA', 'TYTUŁ', 'LOGO', 'DOSTĘPNOŚĆ'];
      let currentStep = 0;

      function updatePrompt(message = '') {
        if (currentStep < steps.length) {
          prompt.innerText = '🖱️ Kliknij element: ' + steps[currentStep] + '\\n' + message;
        } else {
          prompt.innerText = '✅ Gotowe! Możesz zamknąć stronę.';
        }
      }

      updatePrompt();

      function generateSimpleSelector(el) {
        if (!el || !el.tagName) return "";

        const ignored = ['__hover-highlight__', '__clicked__highlight__'];
        const classList = Array.from(el.classList).filter(c =>
          !ignored.includes(c) && !/^css-[a-z0-9]+-/.test(c)
        );

        let baseSelector = el.tagName.toLowerCase();
        if (classList.length > 0) {
          baseSelector += "." + classList.join(".");
        }

        let path = baseSelector;
        let parent = el.parentElement;
        let depth = 0;

        while (parent && depth < 3) {
          const parentTag = parent.tagName.toLowerCase();
          const parentClass = Array.from(parent.classList)
            .filter(c => !/^css-[a-z0-9]+-/.test(c))
            .join(".");
          const parentSelector = parentClass ? `${parentTag}.${parentClass}` : parentTag;

          path = `${parentSelector} > ${path}`;
          parent = parent.parentElement;
          depth++;
        }

        return path;
      }

      document.body.addEventListener('mouseover', e => {
        e.target.classList.add('__hover-highlight__');
      });

      document.body.addEventListener('mouseout', e => {
        e.target.classList.remove('__hover-highlight__');
      });

      document.body.addEventListener('click', e => {
        if (currentStep >= steps.length) return;

        const el = e.composedPath ? e.composedPath()[0] : e.target;
        const ignoredTexts = ['akceptuj', 'zgadzam', 'zaakceptuj', 'przejdź dalej'];

        const elText = (el.innerText || "").toLowerCase();
        if (ignoredTexts.some(txt => elText.includes(txt))) {
          console.log('⛔️ Kliknięcie zignorowane:', elText);
          return;
        }

        e.preventDefault();
        e.stopPropagation();

        const selector = generateSimpleSelector(el);
        const step = steps[currentStep];

        window.__selectorPicker__.result[step] = selector;

        el.classList.add('__clicked__highlight__');
        setTimeout(() => {
          el.classList.remove('__clicked__highlight__');
        }, 1000);

        const text = el.innerText.trim().slice(0, 80);
        console.log(`📌 Wybrano [${step}]:`, selector, '| Tekst:', text);

        currentStep++;
        updatePrompt(`Ostatnio: ${text || '[brak tekstu]'}`);
      });
    }
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process"
            ]
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/114.0.0.0 Safari/537.36",
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
            viewport={"width": 1280, "height": 720},
            is_mobile=False
        )


        page = context.new_page()
        page.add_init_script("window.close = () => console.log('🔒 Zablokowano window.close()')")
        page.goto(url, timeout=60000)
        page.wait_for_timeout(1500)

        page.mouse.move(200, 200)
        page.keyboard.press("PageDown")
        page.wait_for_timeout(1000)

        page.add_script_tag(content=JS_INJECT)
        page.wait_for_timeout(1000)

        try:
            for sel in ['button:has-text("Akceptuj")', 'button:has-text("Zgadzam się")']:
                if page.locator(sel).first.is_visible():
                    page.locator(sel).first.click()
                    break
        except Exception:
            pass

        for step in steps:
            print(f"\n👉 Kliknij element dla: {step}")
            page.evaluate(f"window.__selectorPicker__.step = '{step}';")
            last_selector = None

            for _ in range(300):
                result = page.evaluate("window.__selectorPicker__.result || {}")
                if step in result:
                    last_selector = result[step]
                    break
                page.wait_for_timeout(100)

            if last_selector:
                print(f"✅ {step}: {last_selector}")
                selected[step] = last_selector
            else:
                print(f"⚠️ Nie wykryto kliknięcia dla: {step}")

        try:
            html = page.content()
            with open("debug_strefakart.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print(f"⚠️ Nie udało się zapisać debug HTML: {e}")

        # Logo
        logo_selector = selected.get("LOGO")
        if logo_selector:
            try:
                logo_el = page.locator(logo_selector).first
                if logo_el.is_visible():
                    save_logo_image(logo_el, store_name)
            except Exception as e:
                print(f"⚠️ Błąd przy zapisie logo: {e}")

        page.close()
        browser.close()

    # Test bez JS
    use_selenium = False
    try:
        print("🧪 Zebrane selektory:", selected)
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        test_selector = selected.get("DOSTĘPNOŚĆ") or selected.get("CENA")
        print("🔎 Testowany selektor:", test_selector)

        if test_selector:
            el = soup.select_one(test_selector)
            if not el or not el.get_text(strip=True):
                use_selenium = True
                print("⚠️ Brak elementu — wymagana obsługa JS (Playwright).")
            else:
                print("✅ Selektor działa bez JS.")
        else:
            print("⚠️ Brak selektora do testu.")
            use_selenium = True
    except Exception as e:
        print(f"⚠️ Błąd przy testowaniu bez JS: {e}")
        use_selenium = True

    # Zapis
    selectors = load_selectors()
    selectors[store_name] = {
        "price": selected.get("CENA", ""),
        "title": selected.get("TYTUŁ", ""),
        "availability": selected.get("DOSTĘPNOŚĆ", ""),
        "use_selenium": use_selenium
    }
    save_selectors_data(selectors)
    print(f"\n💾 Zapisano selektory dla sklepu '{store_name}' (use_selenium={use_selenium})")





import os

def save_logo_image(element, store_name):
    """
    Zapisuje zrzut ekranu elementu (logo) do folderu static/logos obok pliku skryptu.
    
    :param element: Playwright locator element (np. page.locator(...).first)
    :param store_name: Nazwa sklepu używana jako nazwa pliku
    """
    try:
        if not element.is_visible():
            print("⚠️ Element nie jest widoczny — nie zapisano logo.")
            return
        
        # Ścieżka względna względem pliku .py
        base_dir = os.path.dirname(os.path.abspath(__file__))
        logo_dir = os.path.join(base_dir, "static", "logos")
        os.makedirs(logo_dir, exist_ok=True)
        
        logo_path = os.path.join(logo_dir, f"{store_name}.png")
        element.screenshot(path=logo_path)
        print(f"🖼️ Logo zapisane: {logo_path}")
    except Exception as e:
        print(f"⚠️ Błąd przy zapisie logo: {e}")



@app.route("/edit_store/<store>", methods=["GET", "POST"])
def edit_store(store):
    selectors = load_selectors()
    current = selectors.get(store)

    if not current:
        flash(f"Nie znaleziono selektorów dla sklepu '{store}'", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        selectors[store] = {
            "price": request.form.get("price", "").strip(),
            "title": request.form.get("title", "").strip(),
            "availability": request.form.get("availability", "").strip(),
            "use_selenium": 'use_selenium' in request.form
        }
        save_selectors_data(selectors)
        flash(f"Zaktualizowano selektory dla sklepu '{store}'", "success")
        return redirect(url_for("index"))

    return render_template("edit_store.html", store=store, data=current)

@app.route("/stores")
def stores():
    selectors = load_selectors()
    return render_template("stores.html", selectors=selectors)

@app.route("/delete_store/<store>", methods=["POST"])
def delete_store(store):
    selectors = load_selectors()
    if store in selectors:
        del selectors[store]
        save_selectors_data(selectors)
        flash(f"Sklep '{store}' został usunięty.", "success")
    else:
        flash(f"Sklep '{store}' nie istnieje.", "error")
    return redirect(url_for("stores"))

def run_detector_in_background():
    progress_path = os.path.join(BASE_DIR, "detector_progress.txt")
    log_path = os.path.join(BASE_DIR, "detector_log.txt")
    with open(progress_path, "w", encoding="utf-8") as f:
        f.write("0")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("[INFO] Start detektora\n")
    import product_detector
    product_detector.main()
    with open(progress_path, "w", encoding="utf-8") as f:
        f.write("100")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("[DONE] Detektor zakończony\n")

@app.route("/stores/select/<store_name>", methods=["GET", "POST"])
def select_store_selector(store_name):
    from store_admin import load_stores, save_stores
    import urllib.parse

    stores = load_stores()
    store = next((s for s in stores if s["name"] == store_name), None)

    if not store:
        flash("Nie znaleziono sklepu", "error")
        return redirect(url_for("stores"))

    if request.method == "POST":
        # Aktualizacja danych sklepu
        store["name"] = request.form.get("name")
        store["url"] = request.form.get("url")
        store["product_selector"] = request.form.get("product_selector")
        store["title_selector"] = request.form.get("title_selector")
        store["link_selector"] = request.form.get("link_selector")
        store["base_url"] = request.form.get("base_url")
        store["requests"] = request.form.get("requests") == "true"
        save_stores(stores)

        flash(f"✅ Zapisano zmiany dla sklepu {store['name']}", "success")
        return redirect(url_for("stores"))

    test_output = request.args.get("test_output")
    return render_template("select_store_selector.html", store=store, test_output=test_output)

from bs4 import BeautifulSoup

@app.route("/stores/test/<store_name>")
def test_selectors(store_name):
    stores = load_stores()
    store = next((s for s in stores if s["name"] == store_name), None)
    if not store:
        return "Sklep nie znaleziony"

    try:
        if store["requests"]:
            import requests
            res = requests.get(store["url"], timeout=10)
            html = res.text
        else:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(store["url"])
                html = page.content()
                browser.close()

        soup = BeautifulSoup(html, "html.parser")
        products = soup.select(store["product_selector"])[:5]
        lines = []
        for p in products:
            title = p.select_one(store["title_selector"])
            link = p.select_one(store["link_selector"])
            lines.append(f"- {title.text.strip() if title else '[brak tytułu]'} → {link['href'] if link else '[brak linku]'}")

        preview = "\n".join(lines) if lines else "❌ Nic nie znaleziono"
    except Exception as e:
        preview = f"❌ Błąd testu: {e}"

    import urllib.parse
    encoded = urllib.parse.quote_plus(store_name)
    return redirect(url_for("select_store_selector", store_name=encoded, test_output=preview))



@app.route("/stores/add-selectors", methods=["GET", "POST"])
def add_store_selectors():
    if request.method == "POST":
        from store_admin import add_store

        data = {
            "name": request.form.get("name"),
            "url": request.form.get("url"),
            "product_selector": request.form.get("product_selector"),
            "title_selector": request.form.get("title_selector"),
            "link_selector": request.form.get("link_selector"),
            "base_url": request.form.get("base_url"),
            "requests": request.form.get("requests") == "true"
        }

        add_store(data)
        flash(f"✅ Dodano sklep: {data['name']}", "success")
        return redirect(url_for("stores"))

    return render_template("add_selectors_form.html")



@app.route("/detector", methods=["GET", "POST"])
def detector():
    suspicious_path = os.path.join(BASE_DIR, "suspicious.json")
    unmatched_path = os.path.join(BASE_DIR, "unmatched.json")

    if request.method == "POST":
        if "store_url" in request.form:
            name = request.form.get("store_name")
            url = request.form.get("store_url")
            threading.Thread(target=select_store_selectors, args=(name, url)).start()
            flash("🧭 Otworzono Playwright do wyboru selektorów", "info")
            return redirect(url_for("detector"))
        else:
            threading.Thread(target=run_detector_in_background).start()

    suspicious = []
    unmatched = []

    if os.path.exists(suspicious_path):
        with open(suspicious_path, encoding="utf-8") as f:
            suspicious = json.load(f)

    if os.path.exists(unmatched_path):
        with open(unmatched_path, encoding="utf-8") as f:
            unmatched = json.load(f)

    return render_template("detector_trigger.html", suspicious=suspicious, unmatched=unmatched)

@app.route("/assign_product_id", methods=["POST"])
def assign_product_id():
    url = request.form.get("url")
    product_id = request.form.get("product_id")
    products_path = os.path.join(BASE_DIR, "products.json")

    try:
        if not os.path.exists(products_path):
            flash("❌ Plik products.json nie istnieje", "error")
            return redirect(url_for("index"))

        with open(products_path, encoding="utf-8") as f:
            products = json.load(f)

        # Znajdź i zaktualizuj produkt po URL
        updated = False
        for p in products:
            if p["url"] == url:
                p["product_id"] = product_id
                updated = True
                break

        if not updated:
            flash("❌ Nie znaleziono produktu o podanym URL", "error")
            return redirect(url_for("index"))

        with open(products_path, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

        flash("✅ Przypisano produktowi ID logiczne", "success")
    except Exception as e:
        flash(f"❌ Błąd przy przypisywaniu ID: {e}", "error")

    return redirect(url_for("index"))


@app.route("/detector_progress")
def detector_progress():
    path = os.path.join(BASE_DIR, "detector_progress.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "0"

@app.route("/delete_suspicious_entry", methods=["POST"])
def delete_suspicious_entry():
    url = request.form.get("url")
    path = os.path.join(BASE_DIR, "suspicious.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data = [entry for entry in data if entry.get("url") != url]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return redirect(url_for("detector"))


def download_favicon(store_name, example_url):
    try:
        domain = urlparse(example_url).netloc
        favicon_url = f"https://{domain}/favicon.ico"
        response = requests.get(favicon_url, timeout=5)

        if response.status_code == 200 and response.content:
            image = Image.open(BytesIO(response.content)).convert("RGBA")

            # Ścieżka do zapisu
            os.makedirs("static/logos", exist_ok=True)
            save_path = os.path.join("static/logos", f"{store_name}.png")

            # Zapis jako PNG
            image.save(save_path)
            print(f"✅ Logo zapisane: {save_path}")
        else:
            print(f"⚠️ Nie udało się pobrać favicony: {favicon_url}")
    except Exception as e:
        print(f"❌ Błąd przy pobieraniu favicony: {e}")


if __name__ == "__main__":
    print(f"🚀 Aplikacja uruchomiona {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    # Make it accessible from external IPs
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)

