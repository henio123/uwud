from flask import Flask, render_template, request, redirect, url_for, flash
import os
import json

app = Flask(__name__)
app.secret_key = "tajny_klucz_do_flash"

PRODUCTS_FILE = "products.json"
NOTIFIED_FILE = "notified.json"

def load_products():
    path = os.path.join(os.path.dirname(__file__), PRODUCTS_FILE)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [{"name": k, **v} for k, v in data.items()]
    return data

def save_products(products):
    path = os.path.join(os.path.dirname(__file__), PRODUCTS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

def load_notified():
    path = os.path.join(os.path.dirname(__file__), NOTIFIED_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def build_target_prices(products):
    target_prices = {}
    for p in products:
        if p.get("target_price") is not None:
            target_prices[p["name"]] = f'{p["target_price"]} zł'
    return target_prices

def get_stores(products):
    stores = set()
    for p in products:
        if "store" in p and p["store"]:
            stores.add(p["store"])
    return sorted(stores)

@app.route("/")
def index():
    products = load_products()
    notified = load_notified()
    target_prices = build_target_prices(products)
    stores = get_stores(products)
    return render_template("index.html",
                           products=products,
                           notified=notified,
                           target_prices=target_prices,
                           stores=stores)

@app.route("/add_product", methods=["POST"])
def add_product():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    store = request.form.get("store", "").strip()
    target_price_raw = request.form.get("target_price", "").strip()

    if not name or not url or not store:
        flash("Wszystkie pola poza ceną docelową są wymagane!", "error")
        return redirect(url_for("index"))

    products = load_products()

    if any(p["name"] == name for p in products):
        flash("Produkt o takiej nazwie już istnieje!", "error")
        return redirect(url_for("index"))

    new_product = {"name": name, "url": url, "store": store}

    if target_price_raw:
        try:
            target_price = float(target_price_raw.replace(",", "."))
            new_product["target_price"] = target_price
        except ValueError:
            flash("Niepoprawny format ceny docelowej.", "error")
            return redirect(url_for("index"))

    products.append(new_product)

    try:
        save_products(products)
        flash(f"Produkt '{name}' dodany pomyślnie!", "success")
    except Exception as e:
        flash(f"Błąd podczas zapisywania: {e}", "error")

    return redirect(url_for("index"))

@app.route("/delete_product/<product_name>", methods=["POST"])
def delete_product(product_name):
    products = load_products()
    products = [p for p in products if p["name"] != product_name]

    try:
        save_products(products)
        flash(f"Produkt '{product_name}' usunięty.", "success")
    except Exception as e:
        flash(f"Błąd podczas usuwania produktu: {e}", "error")

    return redirect(url_for("index"))

@app.route("/update_target_price_inline", methods=["POST"])
def update_target_price_inline():
    product_name = request.form.get("product_name", "").strip()
    target_price_raw = request.form.get("target_price", "").strip()

    products = load_products()
    updated = False

    for p in products:
        if p["name"] == product_name:
            if target_price_raw:
                try:
                    target_price = float(target_price_raw.replace(",", "."))
                    p["target_price"] = target_price
                except ValueError:
                    flash("Niepoprawny format ceny docelowej.", "error")
                    return redirect(url_for("index"))
            else:
                p.pop("target_price", None)
            updated = True
            break

    if not updated:
        flash("Nie znaleziono produktu.", "error")
        return redirect(url_for("index"))

    try:
        save_products(products)
        flash(f"Cena docelowa dla '{product_name}' zaktualizowana.", "success")
    except Exception as e:
        flash(f"Błąd podczas zapisywania ceny docelowej: {e}", "error")

    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)
