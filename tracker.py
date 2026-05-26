"""
Price Tracker Bot — rastrea precios automáticamente con Playwright
y envía alertas por email/consola cuando bajan.

Instalación:
    pip install playwright schedule plyer
    playwright install chromium

Uso:
    python tracker.py              # corre una sola vez
    python tracker.py --watch      # corre cada X minutos en loop
"""

import json
import os
import re
import time
import argparse
import html
import smtplib
import logging
import ssl
import shutil
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, UTC
from pathlib import Path
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("❌ Falta instalar playwright: pip install playwright && playwright install chromium")
    exit(1)

try:
    import schedule
except ImportError:
    schedule = None

try:
    import certifi
except ImportError:
    certifi = None

try:
    import storage
except ImportError:
    storage = None

# ─── Configuración ────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config.json"
DATA_FILE   = Path(__file__).parent / "data" / "precios.json"
LOG_FILE    = Path(__file__).parent / "logs" / "tracker.log"
ENV_FILE    = Path(__file__).parent / ".env"
STATUS_FILE = Path(__file__).parent / "data" / "status.json"
DASHBOARD_FILE = Path(__file__).parent / "dashboard.html"
LOCK_FILE = Path(__file__).parent / "data" / "tracker.lock"
DAILY_REPORT_FILE = Path(__file__).parent / "data" / "daily_report.json"
BACKUP_DIR = Path(__file__).parent / "data" / "backups"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
LOCAL_TZ = ZoneInfo("America/Bogota")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("tracker")


def load_env_file(path: Path = ENV_FILE):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

# ─── Selectores por tienda ────────────────────────────────────────────────────
# Cada tienda tiene una lista de selectores CSS para el precio,
# se prueban en orden hasta que uno funcione.

STORE_SELECTORS = {
    "falabella.com": [
        "[class*='jsx-'][class*='price'] .price",
        ".copy17.primary",
        "li.prices-0 span.copy17",
        "[data-id='mktPrice']",
        "span[class*='price']",
    ],
    "mercadolibre.com.co": [
        ".andes-money-amount__fraction",
        ".price-tag-fraction",
        "span.price-tag-amount span",
    ],
    "mercadolibre.com": [
        ".andes-money-amount__fraction",
        ".price-tag-fraction",
    ],
    "amazon.com": [
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "#apex_desktop .a-offscreen",
        "span.a-price.aok-align-center .a-offscreen",
        "span.a-price .a-offscreen",
        ".a-price .a-offscreen",
        "[data-a-color='price'] .a-offscreen",
        "meta[property='product:price:amount']",
        "#price_inside_buybox",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
    ],
    "amazon.com.co": [
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "span.a-price .a-offscreen",
        "#priceblock_ourprice",
    ],
    "steampowered.com": [
        ".game_purchase_price.price",
        ".discount_final_price",
        "[data-price-final]",
        ".game_area_purchase_game .game_purchase_price",
    ],
    "skyscanner.com": [
        "[class*='Price_mainPriceContainer'] span",
        "[data-testid='price']",
        "[class*='price-text']",
        "[class*='BpkText_bpk-text'] [aria-label*='$']",
        "span[class*='price']",
    ],
    "ktronix.com": [
        ".product-price .price",
        "span.price",
    ],
    "alkosto.com": [
        "span.price",
        ".pdp-precio",
        "[class*='precio']",
    ],
    "linio.com.co": [
        "span.price-main-new",
        ".price",
    ],
    # Fallback genérico
    "_default": [
        "[itemprop='price']",
        "[data-price]",
        ".product-price",
        "[class*='precio']",
    ],
}

# Sitios que necesitan manejo especial
SPECIAL_SITES = {
    "steampowered.com": "steam",
    "skyscanner.com": "skyscanner",
}

# ─── Utilidades ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"No se encontró config.json en {CONFIG_FILE}")
        log.info("Crea uno basado en config.example.json")
        exit(1)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_history() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_history(history: dict):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def acquire_run_lock():
    if fcntl is None:
        return None

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return None

    lock_handle.write(f"{os.getpid()} {datetime.now(LOCAL_TZ).isoformat(timespec='seconds')}\n")
    lock_handle.flush()
    return lock_handle


def release_run_lock(lock_handle):
    if not lock_handle or fcntl is None:
        return
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
    finally:
        lock_handle.close()


def ssl_context():
    return ssl.create_default_context(cafile=certifi.where()) if certifi else None


def get_json(url: str, timeout: int = 20) -> tuple[object | None, str | None]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PriceTrackerBot/1.0"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        return None, f"HTTP {e.code} {detail}"
    except Exception as e:
        return None, str(e)


def get_config_secret(section: dict, key: str, env_key: str) -> str:
    env_name = section.get(env_key, "").strip()
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value
    return section.get(key, "").strip()


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def format_match_time(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def raw_has_currency_mismatch(raw: str, expected_currency: str | None) -> bool:
    """Evita guardar COP como USD, o al revés, cuando el sitio muestra moneda explícita."""
    expected = (expected_currency or "").upper()
    upper = raw.upper()

    if expected == "USD" and re.search(r"\b(COP|COL\$)\b", upper):
        return True

    if expected == "COP" and re.search(r"\bUSD\b|US\$", upper):
        return True

    return False


def clean_price(raw: str, expected_currency: str | None = None) -> float | None:
    """Limpia un string de precio y retorna float."""
    if not raw:
        return None

    if raw_has_currency_mismatch(raw, expected_currency):
        return None

    cleaned = re.sub(r"[^\d.,]", "", raw.strip())
    if not cleaned:
        return None

    currency = (expected_currency or "").upper()
    dot_count   = cleaned.count(".")
    comma_count = cleaned.count(",")

    if currency == "COP":
        # En COP casi siempre los separadores son de miles: 59.999, 1,500,000.
        return float(re.sub(r"[^\d]", "", cleaned))

    if currency == "USD":
        if dot_count == 1 and comma_count == 0:
            return float(cleaned)
        if comma_count == 1 and dot_count == 0:
            parts = cleaned.split(",")
            if len(parts[-1]) <= 2:
                return float(cleaned.replace(",", "."))
            return float(cleaned.replace(",", ""))
        if dot_count == 1 and comma_count >= 1:
            return float(cleaned.replace(",", ""))
        if comma_count == 1 and dot_count >= 1:
            return float(cleaned.replace(".", "").replace(",", "."))

    # Múltiples separadores del mismo tipo → son miles: 3.500.000 o 3,500,000
    if dot_count > 1:
        cleaned = cleaned.replace(".", "")
    elif comma_count > 1:
        cleaned = cleaned.replace(",", "")
    # Un punto Y una coma → determinar cuál es decimal
    elif dot_count == 1 and comma_count == 1:
        if cleaned.index(",") < cleaned.index("."):
            cleaned = cleaned.replace(",", "")        # 3,500.99
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")  # 3.500,99
    # Solo una coma
    elif comma_count == 1:
        parts = cleaned.split(",")
        if len(parts[-1]) <= 2:
            cleaned = cleaned.replace(",", ".")       # 59,99 → decimal
        else:
            cleaned = cleaned.replace(",", "")        # 59,999 → miles
    # Solo un punto → si hay 3 dígitos después es separador de miles (59.999 = 59999)
    elif dot_count == 1:
        parts = cleaned.split(".")
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(".", "")        # 59.999 → 59999 COP
        # si hay 1-2 dígitos después es decimal: 59.99 → se deja igual

    try:
        return float(cleaned)
    except ValueError:
        return None


def get_selectors_for_url(url: str) -> list[str]:
    for domain, selectors in STORE_SELECTORS.items():
        if domain in url and domain != "_default":
            return selectors + STORE_SELECTORS["_default"]
    return STORE_SELECTORS["_default"]


# ─── Scraping ─────────────────────────────────────────────────────────────────

def get_special_site(url: str) -> str | None:
    for domain, name in SPECIAL_SITES.items():
        if domain in url:
            return name
    return None


def scrape_steam(page, url: str, expected_currency: str | None = None) -> tuple[float | None, str]:
    """Steam: bypasa verificación de edad con cookies y extrae precio."""
    # Cookies para saltarse age gate
    page.context.add_cookies([
        {"name": "birthtime", "value": "631152001", "domain": "store.steampowered.com", "path": "/"},
        {"name": "mature_content", "value": "1",    "domain": "store.steampowered.com", "path": "/"},
        {"name": "lastagecheckage", "value": "1-0-2000", "domain": "store.steampowered.com", "path": "/"},
    ])
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3000)

    # Si aparece el age gate, llenarlo
    try:
        if page.query_selector("#ageYear"):
            page.select_option("#ageYear", "2000")
            page.click("#view_product_page_btn")
            page.wait_for_timeout(2500)
    except Exception:
        pass

    selectors = STORE_SELECTORS["steampowered.com"]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                raw = el.inner_text().strip()
                # Steam a veces pone el precio como atributo
                if not raw:
                    raw = el.get_attribute("data-price-final") or ""
                    if raw:
                        raw = str(int(raw) / 100)  # Steam guarda en centavos
                price = clean_price(raw, expected_currency)
                if price and price > 0:
                    log.info(f"  ✅ Steam precio [{sel}]: {raw} → {price:,.2f}")
                    return price, sel
        except Exception:
            continue

    # Buscar en el HTML directamente el JSON de precio de Steam
    content = page.content()
    match = re.search(r'"final_formatted"\s*:\s*"([^"]+)"', content)
    if match:
        price = clean_price(match.group(1), expected_currency)
        if price and price > 0:
            log.info(f"  ✅ Steam precio por JSON: {match.group(1)} → {price}")
            return price, "_json"

    return None, "not_found"


def scrape_skyscanner(page, url: str, expected_currency: str | None = None) -> tuple[float | None, str]:
    """Skyscanner: espera más tiempo para que cargue el JS de vuelos."""
    page.set_default_timeout(2500)
    page.set_default_navigation_timeout(25_000)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    except Exception as e:
        log.warning(f"  Skyscanner no cargo a tiempo: {e}")
        return None, "timeout"
    log.info("  Esperando que carguen los vuelos (8s)...")
    page.wait_for_timeout(8_000)

    # Intentar cerrar popups/cookies
    for btn_text in ["Aceptar", "Accept", "Continuar", "OK"]:
        try:
            btn = page.get_by_text(btn_text, exact=True).first
            if btn:
                btn.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

    selectors = STORE_SELECTORS["skyscanner.com"]
    for sel in selectors:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                raw = el.inner_text().strip()
                price = clean_price(raw, expected_currency)
                if price and price > 100_000:  # vuelos Colombia-Miami > 100k COP
                    log.info(f"  ✅ Skyscanner precio [{sel}]: {raw} → {price:,.0f}")
                    return price, sel
        except Exception:
            continue

    # Buscar rapido en el texto visible; evita quedarse pegado con HTML enorme.
    try:
        content = page.locator("body").inner_text(timeout=3000)
    except Exception:
        log.warning("  ❌ Skyscanner bloqueó el bot — no hubo texto usable")
        return None, "blocked"

    # Skyscanner usa COP: $1.234.567 o COP 1.234.567
    matches = re.findall(r'(?:COP|COL\$|\$)\s*(\d{1,3}(?:[.,]\d{3})+)', content)
    prices = []
    for m in matches:
        p = clean_price(m, expected_currency)
        if p and 300_000 < p < 30_000_000:
            prices.append(p)

    if prices:
        price = min(prices)  # precio más bajo = mejor oferta
        log.info(f"  ⚠️ Skyscanner precio por patrón: {price:,.0f}")
        return price, "_pattern"

    log.warning("  ❌ Skyscanner bloqueó el bot — prueba manualmente en el link")
    return None, "not_found"


def parse_skyscanner_route(url: str) -> dict | None:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[0] != "transport" or parts[1] != "flights":
        return None

    origin, destination, depart_token = parts[2], parts[3], parts[4]
    if not re.fullmatch(r"\d{4}", depart_token):
        return None

    year = 2000 + int(depart_token[:2])
    month = int(depart_token[2:])
    if not 1 <= month <= 12:
        return None

    # Amadeus necesita una fecha concreta. Usamos el primer dia del mes del link.
    departure_date = f"{year:04d}-{month:02d}-01"
    query = urllib.parse.parse_qs(parsed.query)
    adults = query.get("adultsv2", ["1"])[0]
    return {
        "origin": origin.upper(),
        "destination": destination.upper(),
        "departure_date": departure_date,
        "adults": adults if str(adults).isdigit() else "1",
    }


def get_amadeus_token(amadeus_cfg: dict) -> tuple[str | None, str | None]:
    client_id = get_config_secret(amadeus_cfg, "client_id", "client_id_env")
    client_secret = get_config_secret(amadeus_cfg, "client_secret", "client_secret_env")
    if not client_id or not client_secret:
        return None, "Faltan AMADEUS_CLIENT_ID o AMADEUS_CLIENT_SECRET"

    base_url = amadeus_cfg.get("base_url", "https://test.api.amadeus.com").rstrip("/")
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/security/oauth2/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "PriceTrackerBot/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20, context=ssl_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("access_token"), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        return None, f"Amadeus token HTTP {e.code}: {detail}"
    except Exception as e:
        return None, str(e)


def fetch_amadeus_flight_price(config: dict, product: dict) -> tuple[float | None, str]:
    amadeus_cfg = config.get("providers", {}).get("amadeus", {})
    if not amadeus_cfg.get("enabled"):
        return None, "api_missing"

    route = product.get("flight") or parse_skyscanner_route(product.get("url", ""))
    if not route:
        return None, "api_error"

    token, error = get_amadeus_token(amadeus_cfg)
    if error or not token:
        log.warning(f"  Amadeus no disponible: {error}")
        return None, "api_missing"

    base_url = amadeus_cfg.get("base_url", "https://test.api.amadeus.com").rstrip("/")
    params = urllib.parse.urlencode({
        "originLocationCode": route["origin"],
        "destinationLocationCode": route["destination"],
        "departureDate": route["departure_date"],
        "adults": route.get("adults", "1"),
        "currencyCode": product.get("currency", "COP"),
        "max": int(amadeus_cfg.get("max_offers", 10)),
    })
    request = urllib.request.Request(
        f"{base_url}/v2/shopping/flight-offers?{params}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "PriceTrackerBot/1.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=25, context=ssl_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        log.warning(f"  Amadeus respondio HTTP {e.code}: {detail}")
        return None, "api_error"
    except Exception as e:
        log.warning(f"  Error consultando Amadeus: {e}")
        return None, "api_error"

    offers = payload.get("data", [])
    prices = []
    for offer in offers:
        total = offer.get("price", {}).get("grandTotal") or offer.get("price", {}).get("total")
        try:
            prices.append(float(total))
        except (TypeError, ValueError):
            continue

    if not prices:
        return None, "not_found"

    price = min(prices)
    log.info(f"  ✅ Amadeus precio mas bajo: {price:,.2f}")
    return price, "amadeus"


def amazon_domain_id(url: str) -> int:
    host = urllib.parse.urlparse(url).netloc.lower()
    if "amazon.com.mx" in host:
        return 11
    if "amazon.co.uk" in host:
        return 2
    if "amazon.de" in host:
        return 3
    if "amazon.fr" in host:
        return 4
    if "amazon.co.jp" in host:
        return 5
    if "amazon.ca" in host:
        return 6
    if "amazon.it" in host:
        return 8
    if "amazon.es" in host:
        return 9
    if "amazon.in" in host:
        return 10
    return 1


def extract_amazon_asin(url: str) -> str | None:
    patterns = [
        r"/dp/([A-Z0-9]{10})(?:[/?]|$)",
        r"/gp/product/([A-Z0-9]{10})(?:[/?]|$)",
        r"/product/([A-Z0-9]{10})(?:[/?]|$)",
        r"[?&]asin=([A-Z0-9]{10})(?:&|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1).upper()
    return None


def keepa_price_from_stats(product: dict) -> float | None:
    stats = product.get("stats") or {}
    current = stats.get("current") or []
    # Keepa stores prices in cents. Prefer buy box, then marketplace new, then Amazon.
    for idx in (18, 1, 0):
        if idx >= len(current):
            continue
        raw = current[idx]
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw) / 100
    return None


def fetch_keepa_amazon_price(config: dict, product: dict) -> tuple[float | None, str]:
    keepa_cfg = config.get("providers", {}).get("keepa", {})
    if not keepa_cfg.get("enabled"):
        return None, "api_missing"

    api_key = get_config_secret(keepa_cfg, "api_key", "api_key_env")
    if not api_key:
        return None, "api_missing"

    asin = product.get("asin") or extract_amazon_asin(product.get("url", ""))
    if not asin:
        return None, "api_error"

    params = urllib.parse.urlencode({
        "key": api_key,
        "domain": int(product.get("keepa_domain") or amazon_domain_id(product.get("url", ""))),
        "asin": asin,
        "stats": int(keepa_cfg.get("stats_days", 30)),
        "history": 0,
    })
    request = urllib.request.Request(
        f"https://api.keepa.com/product?{params}",
        headers={"User-Agent": "PriceTrackerBot/1.0"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=25, context=ssl_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        log.warning(f"  Keepa respondio HTTP {e.code}: {detail}")
        return None, "api_error"
    except Exception as e:
        log.warning(f"  Error consultando Keepa: {e}")
        return None, "api_error"

    products = payload.get("products") or []
    if not products:
        return None, "not_found"

    price = keepa_price_from_stats(products[0])
    if price is None:
        return None, "not_found"

    log.info(f"  ✅ Keepa precio Amazon: {price:,.2f}")
    return price, "keepa"


def scrape_amazon(page, url: str, expected_currency: str | None = None) -> tuple[float | None, str]:
    """Amazon suele dejar conexiones abiertas; domcontentloaded evita timeouts por networkidle."""
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(5000)

    selectors = get_selectors_for_url(url)
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            raw = (
                el.inner_text().strip()
                or el.get_attribute("content")
                or el.get_attribute("value")
                or ""
            )
            price = clean_price(raw, expected_currency)
            if price and 0 < price < 10000:
                log.info(f"  ✅ Amazon precio [{sel}]: {raw} → {price:,.2f}")
                return price, sel
        except Exception:
            continue

    content = page.content()
    patterns = [
        r'"priceToPay"\s*:\s*\{[^}]*"amount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r'"displayPrice"\s*:\s*"([^"]+)"',
        r'"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if not match:
            continue
        raw = match.group(1)
        price = clean_price(raw, expected_currency)
        if price and 0 < price < 10000:
            log.info(f"  ✅ Amazon precio por HTML: {raw} → {price:,.2f}")
            return price, "_html"

    if re.search(r"captcha|robot check|enter the characters", content, re.I):
        log.warning("  ❌ Amazon pidió verificación anti-bot")
        return None, "blocked"

    body_text = page.locator("body").inner_text(timeout=5000)
    if "Buying options" in body_text and not re.search(r"\$\s*\d", body_text):
        log.warning("  ❌ Amazon cargó la página, pero no mostró precio directo")
        return None, "no_direct_price"

    log.warning("  ❌ No se pudo extraer el precio de Amazon")
    return None, "not_found"


def scrape_price(url: str, playwright_instance, expected_currency: str | None = None) -> tuple[float | None, str]:
    """Retorna (precio, selector_usado) o (None, 'error')."""
    is_amazon = "amazon.com" in url
    browser = playwright_instance.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
        locale="en-US" if is_amazon else "es-CO",
        timezone_id="America/New_York" if is_amazon else "America/Bogota",
    )
    page = context.new_page()

    try:
        special = get_special_site(url)

        if special == "steam":
            return scrape_steam(page, url, expected_currency)

        if special == "skyscanner":
            return scrape_skyscanner(page, url, expected_currency)

        if is_amazon:
            return scrape_amazon(page, url, expected_currency)

        log.info(f"  Abriendo: {url[:60]}...")
        page.goto(url, wait_until="domcontentloaded", timeout=40_000)
        page.wait_for_timeout(3500)

        selectors = get_selectors_for_url(url)
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    raw = el.inner_text().strip() or el.get_attribute("content") or ""
                    price = clean_price(raw, expected_currency)
                    if price and price > 0:
                        # Amazon: filtrar precios raros (no deben ser >10000 si es USD)
                        if is_amazon and (expected_currency or "").upper() == "USD" and price > 10000:
                            continue
                        log.info(f"  ✅ Precio encontrado [{sel}]: {raw} → {price:,.2f}")
                        return price, sel
            except Exception:
                continue

        log.warning("  ❌ No se pudo extraer el precio")
        return None, "not_found"

    except PWTimeout:
        log.error(f"  ❌ Timeout cargando {url}")
        return None, "timeout"
    except Exception as e:
        log.error(f"  ❌ Error: {e}")
        return None, "error"
    finally:
        browser.close()


# ─── Cuotas deportivas ───────────────────────────────────────────────────────

def get_odds_api_key(odds_cfg: dict) -> str:
    return get_config_secret(odds_cfg, "api_key", "api_key_env")


def event_matches_team(event: dict, aliases: list[str]) -> bool:
    haystack = normalize_text(" ".join([
        event.get("home_team", ""),
        event.get("away_team", ""),
    ]))
    return any(normalize_text(alias) in haystack for alias in aliases)


def best_h2h_prices(event: dict) -> list[dict]:
    best = {}
    for bookmaker in event.get("bookmakers", []):
        book_title = bookmaker.get("title", bookmaker.get("key", "book"))
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if name is None or price is None:
                    continue
                if name not in best or price > best[name]["price"]:
                    best[name] = {"name": name, "price": price, "bookmaker": book_title}
    return list(best.values())


def fetch_team_odds(odds_cfg: dict, team_cfg: dict, api_key: str) -> list[dict]:
    base_url = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
    regions = odds_cfg.get("regions", "us,eu")
    markets = odds_cfg.get("markets", "h2h")
    odds_format = odds_cfg.get("odds_format", "decimal")
    date_format = "iso"
    max_matches = int(team_cfg.get("max_matches", odds_cfg.get("max_matches_per_team", 2)))
    aliases = team_cfg.get("aliases", [team_cfg["name"]])
    matches = []

    for sport_key in team_cfg.get("sports", []):
        params = urllib.parse.urlencode({
            "apiKey": api_key,
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
            "dateFormat": date_format,
        })
        url = base_url.format(sport=sport_key) + "?" + params
        events, error = get_json(url)

        if error:
            matches.append({
                "team": team_cfg["name"],
                "sport": sport_key,
                "error": error,
            })
            continue

        for event in events or []:
            if not event_matches_team(event, aliases):
                continue

            outcomes = best_h2h_prices(event)
            matches.append({
                "team": team_cfg["name"],
                "sport": sport_key,
                "home_team": event.get("home_team", "Local"),
                "away_team": event.get("away_team", "Visitante"),
                "commence_time": event.get("commence_time", ""),
                "outcomes": outcomes,
            })

    matches.sort(key=lambda item: item.get("commence_time", ""))
    return matches[:max_matches]


def fetch_tracked_odds(config: dict) -> list[dict]:
    odds_cfg = config.get("odds", {})
    if not odds_cfg.get("enabled"):
        return []

    api_key = get_odds_api_key(odds_cfg)
    if not api_key:
        return [{"error": "Falta odds.api_key o la variable ODDS_API_KEY"}]

    tracked = []
    for team_cfg in odds_cfg.get("teams", []):
        tracked.extend(fetch_team_odds(odds_cfg, team_cfg, api_key))

    if not tracked:
        return [{"error": "No encontré partidos con cuotas para los equipos configurados"}]

    return tracked


# ─── Alertas ─────────────────────────────────────────────────────────────────

def send_email_alert(config: dict, alerts: list[dict]):
    """Envía resumen de alertas por email."""
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled"):
        return
    sender   = email_cfg["sender"]
    password = email_cfg["password"]
    receiver = email_cfg["receiver"]
    smtp     = email_cfg.get("smtp", "smtp.gmail.com")
    port     = email_cfg.get("port", 587)

    lines = []
    for a in alerts:
        lines.append(
            f"🎉 {a['name']}\n"
            f"   Precio actual: {a['currency']} {a['current']:,.0f}\n"
            f"   Tu objetivo:   {a['currency']} {a['target']:,.0f}\n"
            f"   Bajó:          {a['drop_pct']:.1f}%\n"
            f"   Link: {a['url']}\n"
        )
    body = "¡Tu bot detectó precios en objetivo!\n\n" + "\n".join(lines)

    msg = MIMEMultipart()
    msg["Subject"] = f"🔔 Price Bot: {len(alerts)} alerta(s) de precio"
    msg["From"]    = sender
    msg["To"]      = receiver
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp, port) as s:
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, receiver, msg.as_string())
        log.info(f"📧 Email enviado a {receiver}")
    except Exception as e:
        log.error(f"❌ Error enviando email: {e}")


def format_money(currency: str, amount: float) -> str:
    if currency.upper() == "USD":
        return f"{currency} {amount:,.2f}"
    return f"{currency} {amount:,.0f}"


def product_kind(product: dict) -> str:
    if product.get("kind"):
        return str(product["kind"]).strip().lower()

    url = product.get("url", "").lower()
    if "skyscanner." in url or "/flights/" in url:
        return "flight"
    if "amazon." in url:
        return "amazon"
    if "steampowered." in url:
        return "game"
    return "product"


def kind_label(kind: str) -> str:
    return {
        "amazon": "Amazon",
        "flight": "Vuelo",
        "game": "Juego",
        "product": "Producto",
    }.get(kind, kind.title())


def health_label(health: dict | None) -> str:
    if not health or not health.get("checks"):
        return "sin historial"
    rate = health.get("success_rate")
    if rate is None:
        return "sin historial"
    if rate >= 0.8:
        return "estable"
    if rate >= 0.35:
        return "intermitente"
    return "problematico"


def product_health(pid: str) -> dict:
    if storage is None or not pid:
        return {}
    try:
        return storage.product_health(pid)
    except Exception:
        return {}


def attach_product_context(item: dict, product: dict, history_item: dict | None = None) -> dict:
    pid = item.get("id") or product.get("id", "")
    kind = product_kind(product)
    item["kind"] = kind
    item["kind_label"] = kind_label(kind)
    item["health"] = product_health(pid)
    if history_item is not None:
        item["stats"] = latest_price_stats(history_item)
    return item


def result_state(item: dict) -> tuple[str, str, str]:
    if item.get("price") is not None:
        return "ok", "OK", "Precio detectado"

    source = item.get("source", "not_found")
    labels = {
        "blocked": ("blocked", "Bloqueado", "El sitio pidio verificacion anti-bot"),
        "timeout": ("timeout", "Timeout", "La pagina tardo demasiado"),
        "api_missing": ("missing", "Sin API", "Faltan credenciales del proveedor para revisar este producto"),
        "api_error": ("error", "API error", "El proveedor respondio con error"),
        "no_direct_price": ("missing", "Sin precio", "La pagina cargo, pero no mostro precio directo"),
        "not_found": ("missing", "Sin precio", "No se encontro precio usable"),
        "error": ("error", "Error", "Fallo inesperado al revisar"),
    }
    return labels.get(source, ("missing", "Sin precio", source))


def enrich_result_state(item: dict) -> dict:
    state, label, detail = result_state(item)
    item["state"] = state
    item["state_label"] = label
    item["state_detail"] = detail
    return item


def result_line(item: dict) -> str:
    state, label, _ = result_state(item)
    icon = {
        "ok": "✅",
        "blocked": "🛑",
        "timeout": "⏱️",
        "missing": "⚠️",
        "error": "❌",
    }.get(state, "⚠️")
    if item.get("price") is None:
        health = health_label(item.get("health"))
        return f"{icon} **{item['name']}** — {label} ({health}, `{item['source']}`)"

    stats = item.get("stats") or {}
    trend = {"down": "bajando", "up": "subiendo", "flat": "estable"}.get(stats.get("trend"), "estable")
    suffix = ""
    if stats.get("change_pct"):
        suffix = f" | {trend} {abs(stats['change_pct']):.1f}%"
    return f"{icon} **{item['name']}** — {format_money(item['currency'], item['price'])}{suffix}"


def plain_result_line(item: dict) -> str:
    if item.get("price") is None:
        _, label, _ = result_state(item)
        return f"- {item['name']}: {label} ({health_label(item.get('health'))}, {item['source']})"

    stats = item.get("stats") or {}
    trend = {"down": "bajando", "up": "subiendo", "flat": "estable"}.get(stats.get("trend"), "estable")
    extra = ""
    if stats.get("min") is not None:
        extra = f" | min {format_money(item['currency'], stats['min'])} | {trend}"
    return f"- {item['name']}: {format_money(item['currency'], item['price'])}{extra}"


def result_counts(results: list[dict]) -> dict:
    ok = sum(1 for item in results if item.get("price") is not None)
    blocked = sum(1 for item in results if item.get("state") == "blocked")
    timeout = sum(1 for item in results if item.get("state") == "timeout")
    missing = len(results) - ok
    return {"ok": ok, "missing": missing, "blocked": blocked, "timeout": timeout}


def problem_lines(results: list[dict], limit: int = 5) -> list[str]:
    problems = []
    for item in results:
        health = item.get("health") or {}
        rate = health.get("success_rate")
        if item.get("price") is None or (rate is not None and rate < 0.5):
            _, label, detail = result_state(item)
            problems.append(f"- {item['name']}: {label} | {detail}")
    return problems[:limit]


def alert_reason_label(reason: str) -> str:
    return {
        "target": "objetivo alcanzado",
        "drop": "caida fuerte vs maximo",
        "last_drop": "bajo desde la revision anterior",
        "historical_low": "nuevo minimo historico",
        "diagnostic": "producto con fallas repetidas",
        "channel_test": "prueba de canal",
    }.get(reason, reason or "alerta")


def diagnostic_alerts(config: dict, results: list[dict]) -> list[dict]:
    bot_cfg = config.get("bot", {})
    if not bot_cfg.get("diagnostics_enabled", True):
        return []

    threshold = int(bot_cfg.get("diagnostic_missed_threshold", 6))
    alerts = []
    for item in results:
        health = item.get("health") or {}
        missed = int(health.get("missed") or 0)
        detected = int(health.get("detected") or 0)
        if item.get("price") is not None or missed < threshold or detected > 0:
            continue
        alerts.append({
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "url": item.get("url", ""),
            "current": 0,
            "target": threshold,
            "currency": item.get("currency", ""),
            "drop_pct": 0,
            "reason": "diagnostic",
            "message": f"Lleva {missed} revisiones recientes sin precio detectable.",
        })
    return alerts


def append_diagnostic_alerts(config: dict, results: list[dict], alerts: list[dict]):
    cooldown = int(config.get("bot", {}).get("alert_cooldown_hours", 12))
    for alert in diagnostic_alerts(config, results):
        if storage is None or storage.should_send_alert(alert, cooldown):
            alerts.append(alert)


def latest_price_stats(history_item: dict) -> dict:
    prices = [row["price"] for row in history_item.get("prices", [])]
    if not prices:
        return {}
    current = prices[-1]
    previous = prices[-2] if len(prices) > 1 else current
    prev_min = min(prices[:-1]) if len(prices) > 1 else current
    change_pct = ((current - previous) / previous * 100) if previous else 0
    last_7 = prices[-7:]
    return {
        "current": current,
        "min": min(prices),
        "max": max(prices),
        "avg": sum(prices) / len(prices),
        "avg_7": sum(last_7) / len(last_7),
        "count": len(prices),
        "previous": previous,
        "change_pct": change_pct,
        "is_historical_low": len(prices) > 1 and current < prev_min,
        "last_seen": history_item.get("prices", [{}])[-1].get("date"),
        "trend": "down" if current < previous else ("up" if current > previous else "flat"),
    }


def post_discord_payload(config: dict, payload: dict) -> bool:
    discord_cfg = config.get("discord", {})
    if not discord_cfg.get("enabled"):
        log.warning("Discord está desactivado en config.json")
        return False

    webhook_url = get_config_secret(discord_cfg, "webhook_url", "webhook_url_env")
    if not webhook_url:
        log.warning("Discord está activado, pero falta discord.webhook_url en config.json")
        return False

    payload.setdefault("username", discord_cfg.get("username", "Price Tracker Bot"))

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "PriceTrackerBot/1.0",
        },
        method="POST",
    )

    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where()) if certifi else None
        with urllib.request.urlopen(request, timeout=15, context=ssl_context) as response:
            if response.status not in (200, 204):
                log.error(f"Discord respondió con estado {response.status}")
                return False
        log.info("Mensaje enviado a Discord")
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        log.error(f"Error enviando Discord: HTTP {e.code} {detail}")
    except Exception as e:
        log.error(f"Error enviando Discord: {e}")
    return False


def send_discord_alert(config: dict, alerts: list[dict]) -> bool:
    """Envía alertas a un canal de Discord usando un webhook."""
    embeds = []
    for alert in alerts[:10]:
        currency = alert["currency"]
        fields = [
            {"name": "Precio actual", "value": format_money(currency, alert["current"]) if alert.get("current") else "Sin precio", "inline": True},
            {"name": "Referencia", "value": format_money(currency, alert["target"]) if currency else str(alert.get("target", "")), "inline": True},
        ]
        if alert.get("drop_pct"):
            fields.append({"name": "Movimiento", "value": f"{alert['drop_pct']:.1f}%", "inline": True})
        if alert.get("message"):
            fields.append({"name": "Detalle", "value": alert["message"][:1024], "inline": False})
        embeds.append({
            "title": f"{alert_reason_label(alert.get('reason'))}: {alert['name']}",
            "url": alert["url"],
            "color": 0xe67e22 if alert.get("reason") == "diagnostic" else 0x2ecc71,
            "fields": fields,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        })

    payload = {
        "content": f"Detecté {len(alerts)} alerta(s) de precio.",
        "embeds": embeds,
    }
    return post_discord_payload(config, payload)


def format_odds_lines(odds_results: list[dict]) -> list[str]:
    lines = []
    for item in odds_results[:8]:
        if item.get("error"):
            team = item.get("team", "Cuotas")
            lines.append(f"- {team}: {item['error']}")
            continue

        match = f"{item['home_team']} vs {item['away_team']}"
        when = format_match_time(item.get("commence_time", ""))
        prices = []
        for outcome in item.get("outcomes", [])[:3]:
            prices.append(f"{outcome['name']} {outcome['price']} ({outcome['bookmaker']})")
        odds_text = " | ".join(prices) if prices else "sin cuotas h2h"
        lines.append(f"- {item['team']}: {match} | {when} | {odds_text}")

    if not lines:
        lines.append("- Cuotas: sin datos")

    return lines


def send_discord_summary(config: dict, results: list[dict], alerts: list[dict], odds_results: list[dict] | None = None) -> bool:
    discord_cfg = config.get("discord", {})
    if not discord_cfg.get("notify_every_check"):
        return False

    lines = [result_line(item) for item in results[:12]]

    if not lines:
        lines.append("- No se pudo revisar ningún producto.")

    counts = result_counts(results)
    ok_count = counts["ok"]
    missing_count = counts["missing"]
    fields = [
        {"name": "Productos revisados", "value": str(len(results)), "inline": True},
        {"name": "Precios detectados", "value": str(ok_count), "inline": True},
        {"name": "Sin precio", "value": str(missing_count), "inline": True},
        {"name": "Alertas", "value": str(len(alerts)), "inline": True},
        {"name": "Bloqueados / timeout", "value": f"{counts['blocked']} / {counts['timeout']}", "inline": True},
    ]

    problems = problem_lines(results)
    if problems:
        fields.append({
            "name": "Atencion",
            "value": "\n".join(problems)[:1024],
            "inline": False,
        })

    if odds_results is not None:
        fields.append({
            "name": "Cuotas Barca / Inter Miami",
            "value": "\n".join(format_odds_lines(odds_results))[:1024],
            "inline": False,
        })

    payload = {
        "content": f"Price Tracker {datetime.now(LOCAL_TZ).strftime('%H:%M')}: {ok_count}/{len(results)} precios detectados, {len(alerts)} alerta(s).",
        "embeds": [{
            "title": "Resumen del Price Tracker",
            "description": "\n".join(lines),
            "color": 0xf1c40f if alerts else 0x3498db,
            "fields": fields,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }],
    }
    return post_discord_payload(config, payload)


# ─── WhatsApp ────────────────────────────────────────────────────────────────

def build_plain_summary(results: list[dict], alerts: list[dict], odds_results: list[dict] | None = None) -> str:
    counts = result_counts(results)
    lines = [
        f"Price Tracker - {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}",
        f"{counts['ok']}/{len(results)} precios detectados | {len(alerts)} alerta(s)",
        "",
    ]

    if alerts:
        lines.append("Alertas:")
        for alert in alerts[:5]:
            lines.append(
                f"- {alert['name']}: {format_money(alert['currency'], alert['current'])} "
                f"({alert_reason_label(alert.get('reason'))}, ref {format_money(alert['currency'], alert['target'])})"
            )
        lines.append("")

    lines.append("Productos:")
    if results:
        for item in results[:10]:
            lines.append(plain_result_line(item))
    else:
        lines.append("- Sin productos revisados")

    problems = problem_lines(results, 4)
    if problems:
        lines.append("")
        lines.append("Atencion:")
        lines.extend(problems)

    if odds_results is not None:
        lines.append("")
        lines.append("Cuotas Barca / Inter Miami:")
        lines.extend(format_odds_lines(odds_results)[:6])

    lines.append("")
    lines.append(datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"))
    return "\n".join(lines)[:4096]


def send_whatsapp_message(config: dict, message: str) -> bool:
    whatsapp_cfg = config.get("whatsapp", {})
    if not whatsapp_cfg.get("enabled"):
        log.warning("WhatsApp está desactivado en config.json")
        return False

    phone_number_id = get_config_secret(whatsapp_cfg, "phone_number_id", "phone_number_id_env")
    access_token = get_config_secret(whatsapp_cfg, "access_token", "access_token_env")
    recipient_phone = get_config_secret(whatsapp_cfg, "recipient_phone", "recipient_phone_env")

    missing = []
    if not phone_number_id:
        missing.append("phone_number_id")
    if not access_token:
        missing.append("access_token")
    if not recipient_phone:
        missing.append("recipient_phone")
    if missing:
        log.warning(f"WhatsApp está activado, pero faltan: {', '.join(missing)}")
        return False

    graph_version = whatsapp_cfg.get("graph_version", "v25.0")
    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "PriceTrackerBot/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20, context=ssl_context()) as response:
            data = response.read().decode("utf-8", errors="replace")
            if response.status not in (200, 201):
                log.error(f"WhatsApp respondió con estado {response.status}: {data}")
                return False
        log.info("Mensaje enviado a WhatsApp")
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        log.error(f"Error enviando WhatsApp: HTTP {e.code} {detail}")
    except Exception as e:
        log.error(f"Error enviando WhatsApp: {e}")
    return False


def send_whatsapp_summary(config: dict, results: list[dict], alerts: list[dict], odds_results: list[dict] | None = None) -> bool:
    whatsapp_cfg = config.get("whatsapp", {})
    if not whatsapp_cfg.get("enabled"):
        return False
    if not whatsapp_cfg.get("notify_every_check") and not alerts:
        return False
    return send_whatsapp_message(config, build_plain_summary(results, alerts, odds_results))


def send_whatsapp_test(config: dict):
    print("Enviando prueba a WhatsApp...")
    message = "Price Tracker: prueba de WhatsApp OK."
    if send_whatsapp_message(config, message):
        print("Listo: revisa WhatsApp.")
    else:
        print("No se pudo enviar. Revisa config.json y logs/tracker.log.")


# ─── ntfy ────────────────────────────────────────────────────────────────────

def send_ntfy_message(config: dict, title: str, message: str, priority: str | None = None) -> bool:
    ntfy_cfg = config.get("ntfy", {})
    if not ntfy_cfg.get("enabled"):
        log.warning("ntfy está desactivado en config.json")
        return False

    server = ntfy_cfg.get("server", "https://ntfy.sh").rstrip("/")
    topic = ntfy_cfg.get("topic", "").strip()
    if not topic:
        log.warning("ntfy está activado, pero falta ntfy.topic en config.json")
        return False

    request = urllib.request.Request(
        f"{server}/{urllib.parse.quote(topic)}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority or ntfy_cfg.get("priority", "default"),
            "Tags": ntfy_cfg.get("tags", ""),
            "User-Agent": "PriceTrackerBot/1.0",
        },
        method="POST",
    )

    click = ntfy_cfg.get("click", "").strip()
    if click:
        request.add_header("Click", click)

    try:
        with urllib.request.urlopen(request, timeout=15, context=ssl_context()) as response:
            data = response.read().decode("utf-8", errors="replace")
            if response.status not in (200, 201):
                log.error(f"ntfy respondió con estado {response.status}: {data}")
                return False
        log.info("Mensaje enviado a ntfy")
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        log.error(f"Error enviando ntfy: HTTP {e.code} {detail}")
    except Exception as e:
        log.error(f"Error enviando ntfy: {e}")
    return False


def send_ntfy_summary(config: dict, results: list[dict], alerts: list[dict], odds_results: list[dict] | None = None) -> bool:
    ntfy_cfg = config.get("ntfy", {})
    if not ntfy_cfg.get("notify_every_check") and not alerts:
        return False

    title = "Price Tracker"
    priority = ntfy_cfg.get("priority", "default")
    if alerts:
        title = f"Price Tracker: {len(alerts)} alerta(s)"
        priority = "high"

    return send_ntfy_message(config, title, build_plain_summary(results, alerts, odds_results), priority)


def send_ntfy_test(config: dict):
    print("Enviando prueba a ntfy...")
    if send_ntfy_message(config, "Price Tracker", "Prueba de ntfy OK.", "default"):
        print("Listo: revisa ntfy.")
    else:
        print("No se pudo enviar. Revisa config.json y logs/tracker.log.")


def read_daily_report_state() -> dict:
    if DAILY_REPORT_FILE.exists():
        try:
            with open(DAILY_REPORT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def write_daily_report_state(payload: dict):
    DAILY_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DAILY_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def should_send_daily_report(config: dict) -> bool:
    bot_cfg = config.get("bot", {})
    if not bot_cfg.get("daily_report_enabled", True):
        return False

    now = datetime.now(LOCAL_TZ)
    report_hour = int(bot_cfg.get("daily_report_hour", 20))
    if now.hour < report_hour:
        return False

    state = read_daily_report_state()
    return state.get("last_sent_date") != now.date().isoformat()


def daily_report_text(results: list[dict], alerts: list[dict], odds_results: list[dict] | None = None) -> str:
    counts = result_counts(results)
    lines = [
        f"Resumen diario Price Tracker - {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}",
        f"Detectados: {counts['ok']}/{len(results)} | alertas: {len(alerts)} | bloqueados: {counts['blocked']}",
        "",
        "Productos:",
    ]

    for item in results:
        lines.append(plain_result_line(item))

    problems = problem_lines(results, 8)
    if problems:
        lines.append("")
        lines.append("Para revisar:")
        lines.extend(problems)

    if odds_results is not None:
        lines.append("")
        lines.append("Cuotas destacadas:")
        lines.extend(format_odds_lines(odds_results)[:8])

    return "\n".join(lines)[:4096]


def send_daily_report(config: dict, results: list[dict], alerts: list[dict], odds_results: list[dict] | None = None):
    if not should_send_daily_report(config):
        return

    text = daily_report_text(results, alerts, odds_results)
    post_discord_payload(config, {
        "content": "Resumen diario del Price Tracker",
        "embeds": [{
            "title": "Resumen diario",
            "description": text[:4096],
            "color": 0x9b59b6,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }],
    })
    send_ntfy_message(config, "Price Tracker: resumen diario", text, "default")
    write_daily_report_state({
        "last_sent_date": datetime.now(LOCAL_TZ).date().isoformat(),
        "sent_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
    })


def should_send_channel_test(config: dict) -> bool:
    bot_cfg = config.get("bot", {})
    if not bot_cfg.get("channel_test_enabled", True):
        return False

    now = datetime.now(LOCAL_TZ)
    test_hour = int(bot_cfg.get("channel_test_hour", 9))
    if now.hour < test_hour:
        return False

    state = read_daily_report_state()
    return state.get("last_channel_test_date") != now.date().isoformat()


def send_channel_test(config: dict):
    if not should_send_channel_test(config):
        return

    text = f"Prueba automatica de canales OK - {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}"
    post_discord_payload(config, {"content": text})
    send_ntfy_message(config, "Price Tracker: canales OK", text, "default")

    state = read_daily_report_state()
    state["last_channel_test_date"] = datetime.now(LOCAL_TZ).date().isoformat()
    state["last_channel_test_at"] = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    write_daily_report_state(state)


def backup_files(config: dict):
    bot_cfg = config.get("bot", {})
    if not bot_cfg.get("backup_enabled", True):
        return

    today = datetime.now(LOCAL_TZ).date().isoformat()
    marker = BACKUP_DIR / f"{today}.marker"
    if marker.exists():
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for src in [CONFIG_FILE, DATA_FILE, STATUS_FILE, storage.DB_FILE if storage is not None else None]:
        if src and Path(src).exists():
            target = BACKUP_DIR / f"{today}-{Path(src).name}"
            shutil.copy2(src, target)
    marker.write_text(datetime.now(LOCAL_TZ).isoformat(timespec="seconds"), encoding="utf-8")

    keep_days = int(bot_cfg.get("backup_keep_days", 14))
    cutoff = datetime.now(LOCAL_TZ) - timedelta(days=keep_days)
    for item in BACKUP_DIR.iterdir():
        try:
            if datetime.fromtimestamp(item.stat().st_mtime, LOCAL_TZ) < cutoff:
                item.unlink()
        except Exception:
            continue


def send_discord_test(config: dict):
    alert = {
        "name": "Prueba del bot",
        "url": "https://discord.com",
        "current": 59999,
        "target": 70000,
        "currency": "COP",
        "drop_pct": 14.3,
    }
    print("Enviando prueba a Discord...")
    if send_discord_alert(config, [alert]):
        print("Listo: revisa el canal de Discord.")
    else:
        print("No se pudo enviar. Revisa config.json y los logs.")


def send_odds_test(config: dict):
    print("Consultando cuotas y enviando resumen a Discord...")
    odds_results = fetch_tracked_odds(config)
    if send_discord_summary(config, [], [], odds_results):
        print("Listo: revisa Discord.")
    else:
        print("No se pudo enviar el resumen de cuotas.")


def write_status(config: dict, history: dict, results: list[dict], alerts: list[dict], odds_results: list[dict] | None):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    counts = result_counts(results)
    payload = {
        "generated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "products": results,
        "alerts": alerts,
        "odds": odds_results or [],
        "history": history,
        "summary": {
            "checked": len(results),
            "detected": counts["ok"],
            "missing": counts["missing"],
            "blocked": counts["blocked"],
            "timeout": counts["timeout"],
            "problems": problem_lines(results, 8),
        },
        "bot": {
            "interval_minutes": config.get("bot", {}).get("interval_minutes", 60),
            "daily_report_enabled": bool(config.get("bot", {}).get("daily_report_enabled", True)),
            "diagnostics_enabled": bool(config.get("bot", {}).get("diagnostics_enabled", True)),
            "backup_enabled": bool(config.get("bot", {}).get("backup_enabled", True)),
            "last_backup_marker": str(max(BACKUP_DIR.glob("*.marker"), default="")) if BACKUP_DIR.exists() else "",
        },
        "channels": {
            "discord": bool(config.get("discord", {}).get("enabled")),
            "ntfy": bool(config.get("ntfy", {}).get("enabled")),
            "whatsapp": bool(config.get("whatsapp", {}).get("enabled")),
            "email": bool(config.get("email", {}).get("enabled")),
        },
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def sparkline(values: list[float]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return "Sin datos"
    low = min(values)
    high = max(values)
    if high == low:
        return blocks[3] * min(len(values), 24)
    scaled = []
    for value in values[-24:]:
        idx = round((value - low) / (high - low) * (len(blocks) - 1))
        scaled.append(blocks[idx])
    return "".join(scaled)


def render_dashboard(config: dict, history: dict, results: list[dict], alerts: list[dict], odds_results: list[dict] | None):
    generated_dt = datetime.now(LOCAL_TZ)
    generated_at = generated_dt.strftime("%Y-%m-%d %H:%M")
    ok_count = sum(1 for item in results if item.get("price") is not None)
    interval = config.get("bot", {}).get("interval_minutes", 60)
    next_check = (generated_dt + timedelta(minutes=int(interval))).strftime("%H:%M")
    cards = []

    by_id = {item.get("id"): item for item in results}
    by_name = {item["name"]: item for item in results}
    for product in config.get("products", []):
        name = product["name"]
        item = by_id.get(product.get("id")) or by_name.get(name, {
            "name": name,
            "url": product.get("url", ""),
            "currency": product.get("currency", ""),
            "price": None,
            "source": "pending",
            "target": product.get("target_price"),
        })
        item = attach_product_context(item, product, history.get(product.get("id", ""), {}))
        state, label, detail = result_state(item)
        hist = history.get(product.get("id", ""), {})
        prices = [row["price"] for row in hist.get("prices", [])]
        stats = latest_price_stats(hist)
        health = item.get("health") or {}
        health_text = health_label(health)
        success_text = f"{health.get('success_rate') * 100:.0f}%" if health.get("success_rate") is not None else "Sin datos"
        spark_text = sparkline(prices)
        spark_class = "spark" if prices else "spark no-data"
        price_text = format_money(item["currency"], item["price"]) if item.get("price") is not None else label
        target = product.get("target_price")
        target_text = format_money(product.get("currency", ""), target) if target else "Sin objetivo"
        trend = stats.get("trend", "flat")
        trend_text = {"down": "Bajando", "up": "Subiendo", "flat": "Estable"}.get(trend, "Estable")
        cards.append(f"""
        <article class="product-card {html.escape(state)}">
          <div class="card-top">
            <span class="status">{html.escape(label)}</span>
            <span class="source">{html.escape(item.get("kind_label", ""))} · {html.escape(item.get("source", ""))}</span>
          </div>
          <h2>{html.escape(name)}</h2>
          <p class="price">{html.escape(price_text)}</p>
          <div class="meta">
            <span>Objetivo<br><strong>{html.escape(target_text)}</strong></span>
            <span>Min<br><strong>{html.escape(format_money(hist.get("currency", product.get("currency", "")), stats["min"]) if stats else "Sin datos")}</strong></span>
            <span>Tendencia<br><strong>{html.escape(trend_text)}</strong></span>
            <span>Prom 7<br><strong>{html.escape(format_money(hist.get("currency", product.get("currency", "")), stats["avg_7"]) if stats else "Sin datos")}</strong></span>
            <span>Salud<br><strong>{html.escape(health_text)}</strong></span>
            <span>Exito 24<br><strong>{html.escape(success_text)}</strong></span>
          </div>
          <p class="{spark_class}">{html.escape(spark_text)}</p>
          <p class="detail">{html.escape(detail)}</p>
          <a href="{html.escape(item.get("url", product.get("url", "")))}">Abrir fuente</a>
        </article>
        """)

    odds_lines = format_odds_lines(odds_results or [])
    odds_html = "\n".join(f"<li>{html.escape(line.removeprefix('- '))}</li>" for line in odds_lines[:8])
    alert_html = "\n".join(
        f"<li>{html.escape(a['name'])}: {html.escape(format_money(a['currency'], a['current']))}<br><small>{html.escape(alert_reason_label(a.get('reason')))}</small></li>"
        for a in alerts
    ) or "<li>Sin alertas activas</li>"
    problems = problem_lines(results, 8)
    problem_html = "\n".join(f"<li>{html.escape(line.removeprefix('- '))}</li>" for line in problems) or "<li>Sin problemas criticos</li>"
    channels = config.get("discord", {}).get("enabled"), config.get("ntfy", {}).get("enabled"), config.get("whatsapp", {}).get("enabled")
    channel_text = " / ".join(["Discord" if channels[0] else "", "ntfy" if channels[1] else "", "WhatsApp" if channels[2] else ""]).strip(" /")

    html_doc = f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>Price Tracker Dashboard</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #101316;
      --panel: #181d22;
      --panel-2: #20262d;
      --text: #f3f6f8;
      --muted: #9ca8b3;
      --ok: #2fbf71;
      --warn: #f1b84b;
      --bad: #ff6b6b;
      --line: #2b333c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    main {{ width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 40px; }}
    header {{ display: flex; align-items: end; justify-content: space-between; gap: 20px; margin-bottom: 24px; }}
    h1 {{ font-size: 32px; margin: 0 0 8px; letter-spacing: 0; }}
    p {{ color: var(--muted); }}
    .summary {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .metric, .side-panel, .product-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
    .metric {{ padding: 16px; }}
    .metric span {{ display: block; color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    .metric strong {{ font-size: 26px; }}
    .layout {{ display: grid; grid-template-columns: 1fr 330px; gap: 16px; align-items: start; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .product-card {{ padding: 16px; min-height: 250px; }}
    .card-top {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 14px; }}
    .status, .source {{ border: 1px solid var(--line); border-radius: 999px; padding: 5px 9px; font-size: 12px; color: var(--muted); }}
    .ok .status {{ color: var(--ok); border-color: color-mix(in srgb, var(--ok) 50%, var(--line)); }}
    .missing .status, .timeout .status {{ color: var(--warn); border-color: color-mix(in srgb, var(--warn) 50%, var(--line)); }}
    .blocked .status, .error .status {{ color: var(--bad); border-color: color-mix(in srgb, var(--bad) 50%, var(--line)); }}
    h2 {{ font-size: 17px; line-height: 1.35; min-height: 46px; margin: 0 0 12px; }}
    .price {{ margin: 0 0 16px; font-size: 28px; color: var(--text); font-weight: 750; }}
    .meta {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-bottom: 14px; }}
    .meta span {{ background: var(--panel-2); border-radius: 8px; padding: 10px; color: var(--muted); font-size: 12px; min-height: 58px; }}
    .meta strong {{ color: var(--text); font-size: 13px; overflow-wrap: anywhere; }}
    .spark {{ font-size: 28px; line-height: 1; letter-spacing: 0; color: #8fd3ff; margin: 4px 0 12px; }}
    .spark.no-data {{ font-size: 14px; color: var(--muted); margin-top: 8px; }}
    .detail {{ min-height: 34px; margin: 0 0 12px; }}
    a {{ color: #8fd3ff; text-decoration: none; }}
    .side-panel {{ padding: 16px; margin-bottom: 14px; }}
    .side-panel h2 {{ min-height: auto; margin-bottom: 10px; }}
    small {{ color: var(--muted); }}
    ul {{ margin: 0; padding-left: 18px; color: var(--muted); }}
    li {{ margin: 8px 0; }}
    @media (max-width: 900px) {{
      header {{ display: block; }}
      .summary, .layout, .grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 27px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Price Tracker</h1>
        <p>Ultima revision: {html.escape(generated_at)} · proxima aprox: {html.escape(next_check)}</p>
      </div>
      <p>{html.escape(channel_text or "Canales sin activar")}</p>
    </header>
    <section class="summary">
      <div class="metric"><span>Productos</span><strong>{len(results)}</strong></div>
      <div class="metric"><span>Detectados</span><strong>{ok_count}</strong></div>
      <div class="metric"><span>Alertas</span><strong>{len(alerts)}</strong></div>
      <div class="metric"><span>Problemas</span><strong>{len(problems)}</strong></div>
      <div class="metric"><span>Intervalo</span><strong>{html.escape(str(interval))}m</strong></div>
    </section>
    <section class="layout">
      <div class="grid">
        {''.join(cards)}
      </div>
      <aside>
        <section class="side-panel">
          <h2>Alertas</h2>
          <ul>{alert_html}</ul>
        </section>
        <section class="side-panel">
          <h2>Atencion</h2>
          <ul>{problem_html}</ul>
        </section>
        <section class="side-panel">
          <h2>Cuotas</h2>
          <ul>{odds_html}</ul>
        </section>
      </aside>
    </section>
  </main>
</body>
</html>
"""
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html_doc)


def desktop_notify(title: str, message: str):
    """Notificación de escritorio (si plyer está instalado)."""
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=8)
    except Exception:
        pass


def print_alert(a: dict):
    print(f"\n{'='*55}")
    print(f"  🔔 ALERTA: {a['name']}")
    print(f"  Precio actual: {a['currency']} {a['current']:>12,.0f}")
    print(f"  Tu objetivo:   {a['currency']} {a['target']:>12,.0f}")
    print(f"  Bajó:          {a['drop_pct']:>10.1f}%")
    print(f"  Link: {a['url']}")
    print(f"{'='*55}\n")


# ─── Core ─────────────────────────────────────────────────────────────────────

def run_check():
    lock_handle = acquire_run_lock()
    if fcntl is not None and lock_handle is None:
        log.warning("Ya hay una revision corriendo; se omite esta ejecucion")
        return

    try:
        config  = load_config()
        history = load_history()
        products = [product for product in config.get("products", []) if product.get("active", True)]

        if not products:
            log.warning("No hay productos en config.json")
            return

        log.info(f"\n{'─'*50}")
        log.info(f"▶ Revisando {len(products)} producto(s) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        log.info(f"{'─'*50}")

        alerts = []
        results = []
        odds_results = fetch_tracked_odds(config)
        checked_at = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")

        with sync_playwright() as pw:
            for product in products:
                pid    = product.get("id") or product["name"].lower().replace(" ", "_")
                name   = product["name"]
                url    = product["url"]
                target = product.get("target_price")
                cur    = product.get("currency", "COP")
                kind   = product_kind(product)

                log.info(f"\n🔍 {name}")
                if kind == "amazon":
                    price, source = fetch_keepa_amazon_price(config, product)
                    if price is None and source == "api_missing":
                        log.info("  Keepa no configurado; usando scraping Amazon como respaldo")
                        price, source = scrape_price(url, pw, cur)
                elif kind == "flight":
                    price, source = fetch_amadeus_flight_price(config, product)
                    if price is None and source == "api_missing":
                        log.info("  Amadeus no configurado; usando Skyscanner como respaldo")
                        price, source = scrape_price(url, pw, cur)
                else:
                    price, source = scrape_price(url, pw, cur)

                if price is None:
                    for alt_url in product.get("alternate_urls", []):
                        log.info(f"  Probando fuente alterna: {alt_url[:60]}...")
                        price, source = scrape_price(alt_url, pw, cur)
                        if price is not None:
                            url = alt_url
                            source = f"alternate:{source}"
                            break

                if price is None:
                    log.warning(f"  Saltando — no se obtuvo precio")
                    item = enrich_result_state({
                        "id": pid,
                        "name": name,
                        "url": url,
                        "currency": cur,
                        "target": target,
                        "price": None,
                        "source": source,
                        "checked_at": checked_at,
                    })
                    results.append(attach_product_context(item, product, history.get(pid, {})))
                    continue

                item = enrich_result_state({
                    "id": pid,
                    "name": name,
                    "url": url,
                    "currency": cur,
                    "target": target,
                    "price": price,
                    "source": source,
                    "checked_at": checked_at,
                })

                # Guardar en historial
                if pid not in history:
                    history[pid] = {"name": name, "url": url, "currency": cur, "prices": []}
                previous_prices = [r["price"] for r in history[pid].get("prices", [])]

                history[pid]["prices"].append({
                    "price": price,
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "source": source,
                })
                history[pid]["prices"] = history[pid]["prices"][-90:]  # guardar 90 registros

                prices_list = [r["price"] for r in history[pid]["prices"]]
                max_price   = max(prices_list)
                min_price   = min(prices_list)
                avg_price   = sum(prices_list) / len(prices_list)
                stats = latest_price_stats(history[pid])
                item["stats"] = stats
                results.append(attach_product_context(item, product, history[pid]))

                log.info(f"  Actual: {cur} {price:,.0f}  |  Min: {min_price:,.0f}  |  Max: {max_price:,.0f}  |  Prom: {avg_price:,.0f}")

                # Evaluar alerta
                if target and price <= target:
                    drop_pct = (1 - price / max_price) * 100
                    alert = {"name": name, "url": url, "current": price, "target": target,
                             "currency": cur, "drop_pct": drop_pct, "id": pid, "reason": "target"}
                    if storage is None or storage.should_send_alert(alert, int(config.get("bot", {}).get("alert_cooldown_hours", 12))):
                        alerts.append(alert)
                        print_alert(alert)
                        desktop_notify(f"🔔 {name}", f"{cur} {price:,.0f} — objetivo alcanzado!")
                    else:
                        log.info("  Alerta repetida omitida por cooldown")

                elif previous_prices and price < previous_prices[-1]:
                    previous = previous_prices[-1]
                    drop_pct = (1 - price / previous) * 100 if previous else 0
                    threshold = float(config.get("alert_last_drop_pct", 5))
                    if drop_pct >= threshold:
                        alert = {"name": name, "url": url, "current": price,
                                 "target": previous, "currency": cur, "drop_pct": drop_pct,
                                 "id": pid, "reason": "last_drop"}
                        if storage is None or storage.should_send_alert(alert, int(config.get("bot", {}).get("alert_cooldown_hours", 12))):
                            alerts.append(alert)
                            log.info(f"  📉 Bajó {drop_pct:.1f}% desde la revisión anterior")
                            print_alert(alert)
                        else:
                            log.info("  Alerta repetida omitida por cooldown")

                elif stats.get("is_historical_low"):
                    drop_pct = (1 - price / max_price) * 100 if max_price else 0
                    alert = {"name": name, "url": url, "current": price,
                             "target": min(previous_prices), "currency": cur, "drop_pct": drop_pct,
                             "id": pid, "reason": "historical_low"}
                    if storage is None or storage.should_send_alert(alert, int(config.get("bot", {}).get("alert_cooldown_hours", 12))):
                        alerts.append(alert)
                        log.info("  🏆 Nuevo mínimo histórico")
                        print_alert(alert)
                    else:
                        log.info("  Alerta repetida omitida por cooldown")

                # Alerta por caída grande (≥15% vs precio más alto)
                elif len(prices_list) > 2:
                    drop_pct = (1 - price / max_price) * 100
                    if drop_pct >= config.get("alert_drop_pct", 15):
                        alert = {"name": name, "url": url, "current": price,
                                 "target": max_price, "currency": cur, "drop_pct": drop_pct,
                                 "id": pid, "reason": "drop"}
                        if storage is None or storage.should_send_alert(alert, int(config.get("bot", {}).get("alert_cooldown_hours", 12))):
                            alerts.append(alert)
                            log.info(f"  ⚡ Caída de {drop_pct:.1f}% vs precio más alto!")
                            print_alert(alert)
                        else:
                            log.info("  Alerta repetida omitida por cooldown")

        append_diagnostic_alerts(config, results, alerts)

        save_history(history)
        if storage is not None:
            storage.record_results(results)
        write_status(config, history, results, alerts, odds_results)
        render_dashboard(config, history, results, alerts, odds_results)
        backup_files(config)
        log.info(f"\n✅ Revisión completa — {len(alerts)} alerta(s)\n")

        if alerts:
            send_email_alert(config, alerts)
            send_discord_alert(config, alerts)
            if storage is not None:
                storage.record_alerts(alerts)

        send_discord_summary(config, results, alerts, odds_results)
        send_whatsapp_summary(config, results, alerts, odds_results)
        send_ntfy_summary(config, results, alerts, odds_results)
        send_daily_report(config, results, alerts, odds_results)
        send_channel_test(config)

        print_summary(history)
    finally:
        release_run_lock(lock_handle)


def print_summary(history: dict):
    if not history:
        return
    print(f"\n{'─'*55}")
    print(f"  {'Producto':<28} {'Actual':>12}  {'Min':>10}  {'Tendencia'}")
    print(f"{'─'*55}")
    for pid, data in history.items():
        prices = [r["price"] for r in data["prices"]]
        if not prices:
            continue
        cur_p = prices[-1]
        min_p = min(prices)
        trend = "↓" if len(prices) > 1 and prices[-1] < prices[-2] else ("↑" if len(prices) > 1 and prices[-1] > prices[-2] else "→")
        name  = data["name"][:27]
        cur   = data.get("currency", "")
        print(f"  {name:<28} {cur} {cur_p:>10,.0f}  {cur} {min_p:>8,.0f}  {trend}")
    print(f"{'─'*55}\n")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Price Tracker Bot")
    parser.add_argument("--watch", action="store_true", help="Correr en loop continuo")
    parser.add_argument("--interval", type=int, default=None, help="Minutos entre revisiones")
    parser.add_argument("--test-discord", action="store_true", help="Enviar una alerta de prueba a Discord")
    parser.add_argument("--test-whatsapp", action="store_true", help="Enviar una alerta de prueba a WhatsApp")
    parser.add_argument("--test-ntfy", action="store_true", help="Enviar una alerta de prueba a ntfy")
    parser.add_argument("--test-odds", action="store_true", help="Probar cuotas de Barcelona e Inter Miami")
    args = parser.parse_args()

    if args.test_discord:
        send_discord_test(load_config())
    elif args.test_whatsapp:
        send_whatsapp_test(load_config())
    elif args.test_ntfy:
        send_ntfy_test(load_config())
    elif args.test_odds:
        send_odds_test(load_config())
    elif args.watch:
        if schedule is None:
            print("❌ Instala schedule: pip install schedule")
            exit(1)
        config = load_config()
        interval = args.interval or int(config.get("bot", {}).get("interval_minutes", 60))
        log.info(f"🤖 Bot iniciado — revisando cada {interval} minutos")
        run_check()
        schedule.every(interval).minutes.do(run_check)
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        run_check()
