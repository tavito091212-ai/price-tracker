import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import storage
import tracker


BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATUS_FILE = BASE_DIR / "data" / "status.json"
ALLOWED_CURRENCIES = {"COP", "USD"}
CHECK_LOCK = threading.Lock()
CHECK_STATE = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
}


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")


def public_config(config: dict) -> dict:
    safe = json.loads(json.dumps(config))
    for section in ["discord", "odds", "whatsapp", "email"]:
        if section in safe:
            for key in ["webhook_url", "api_key", "access_token", "password"]:
                if key in safe[section]:
                    safe[section][key] = ""
    for provider in safe.get("providers", {}).values():
        for key in ["api_key", "client_id", "client_secret"]:
            if key in provider:
                provider[key] = ""
    return safe


def read_status() -> dict:
    if STATUS_FILE.exists():
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"generated_at": None, "products": [], "alerts": [], "odds": []}


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, body: str):
    data = body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def run_check_process():
    try:
        completed = subprocess.run(
            [sys.executable, "tracker.py"],
            cwd=BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        returncode = completed.returncode
    finally:
        with CHECK_LOCK:
            CHECK_STATE["running"] = False
            CHECK_STATE["finished_at"] = tracker.datetime.now(tracker.LOCAL_TZ).isoformat(timespec="seconds")
            CHECK_STATE["returncode"] = locals().get("returncode")


def start_check() -> bool:
    with CHECK_LOCK:
        if CHECK_STATE["running"]:
            return False
        CHECK_STATE["running"] = True
        CHECK_STATE["started_at"] = tracker.datetime.now(tracker.LOCAL_TZ).isoformat(timespec="seconds")
        CHECK_STATE["finished_at"] = None
        CHECK_STATE["returncode"] = None

    thread = threading.Thread(target=run_check_process, daemon=True)
    thread.start()
    return True


def check_state() -> dict:
    with CHECK_LOCK:
        return dict(CHECK_STATE)


def service_health(config: dict, status: dict) -> dict:
    summary = status.get("summary", {})
    providers = config.get("providers", {})
    return {
        "summary": summary,
        "bot": status.get("bot", {}),
        "channels": status.get("channels", {}),
        "providers": {
            "keepa": bool(providers.get("keepa", {}).get("enabled")),
            "amadeus": bool(providers.get("amadeus", {}).get("enabled")),
        },
    }


def parse_target_price(value):
    if value in ("", None):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise ValueError("target_price debe ser un numero")
    if price < 0:
        raise ValueError("target_price no puede ser negativo")
    return price


def normalize_currency(value: str) -> str:
    currency = str(value or "COP").strip().upper()
    if currency not in ALLOWED_CURRENCIES:
        raise ValueError(f"currency debe ser una de: {', '.join(sorted(ALLOWED_CURRENCIES))}")
    return currency


def validate_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url debe empezar por http:// o https://")
    return url


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "activar"}
    return bool(value)


def make_product_id(raw_id: str, existing: set[str]) -> str:
    product_id = "".join(ch.lower() if ch.isalnum() else "_" for ch in raw_id).strip("_")
    base_id = product_id or "producto"
    product_id = base_id
    suffix = 2
    while product_id in existing:
        product_id = f"{base_id}_{suffix}"
        suffix += 1
    return product_id


def update_product(product_id: str, fields: dict) -> bool:
    config = load_config()
    updated = False
    for product in config.get("products", []):
        if product.get("id") != product_id:
            continue
        if "active" in fields:
            product["active"] = coerce_bool(fields["active"])
        if "target_price" in fields:
            product["target_price"] = parse_target_price(fields["target_price"])
        if "name" in fields and fields["name"]:
            name = str(fields["name"]).strip()
            if not name:
                raise ValueError("name no puede estar vacio")
            product["name"] = name
        if "url" in fields and fields["url"]:
            product["url"] = validate_url(fields["url"])
        if "currency" in fields and fields["currency"]:
            product["currency"] = normalize_currency(fields["currency"])
        updated = True
        break
    if updated:
        save_config(config)
    return updated


def add_product(fields: dict) -> str:
    config = load_config()
    products = config.setdefault("products", [])
    name = str(fields.get("name") or "").strip()
    if not name:
        raise ValueError("name es obligatorio")
    url = validate_url(fields.get("url"))
    currency = normalize_currency(fields.get("currency", "COP"))
    target_price = parse_target_price(fields.get("target_price"))
    raw_id = str(fields.get("id") or name)
    existing = {product.get("id") for product in products}
    product_id = make_product_id(raw_id, existing)
    products.append({
        "id": product_id,
        "name": name,
        "url": url,
        "target_price": target_price,
        "currency": currency,
        "active": True,
    })
    save_config(config)
    return product_id


INDEX_HTML = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Price Tracker Control</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1215;
      --panel: #171c21;
      --panel-2: #20262d;
      --line: #2d3540;
      --text: #f4f7f9;
      --muted: #a6b1bc;
      --accent: #6ec1ff;
      --ok: #2fbf71;
      --warn: #f0b84b;
      --bad: #ff6b6b;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    main { width: min(1220px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 42px; }
    header { display: flex; justify-content: space-between; align-items: end; gap: 20px; margin-bottom: 20px; }
    h1 { margin: 0 0 8px; font-size: 31px; letter-spacing: 0; }
    h2 { margin: 0 0 14px; font-size: 18px; }
    p { color: var(--muted); }
    button, input, select { font: inherit; }
    button { border: 1px solid var(--line); background: var(--panel-2); color: var(--text); border-radius: 8px; padding: 10px 12px; cursor: pointer; }
    button.primary { background: var(--accent); color: #071018; border-color: var(--accent); font-weight: 700; }
    button:hover { filter: brightness(1.08); }
    input, select { width: 100%; border: 1px solid var(--line); background: #11161b; color: var(--text); border-radius: 8px; padding: 10px; min-width: 0; }
    .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
    .metric, .panel, .product { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .metric { padding: 15px; }
    .metric span { display: block; color: var(--muted); font-size: 13px; margin-bottom: 8px; }
    .metric strong { font-size: 25px; }
    .layout { display: grid; grid-template-columns: 1fr 360px; gap: 16px; align-items: start; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .product { padding: 15px; min-height: 255px; }
    .top { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
    .badge { border: 1px solid var(--line); border-radius: 999px; padding: 5px 9px; color: var(--muted); font-size: 12px; }
    .badge.ok { color: var(--ok); }
    .badge.missing, .badge.timeout { color: var(--warn); }
    .badge.blocked, .badge.error { color: var(--bad); }
    .name { font-size: 17px; font-weight: 750; min-height: 44px; line-height: 1.3; }
    .price { font-size: 27px; font-weight: 800; margin: 12px 0; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
    .form-grid .wide { grid-column: 1 / -1; }
    .actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
    .panel { padding: 16px; margin-bottom: 14px; }
    ul { margin: 0; padding-left: 18px; color: var(--muted); }
    li { margin: 8px 0; }
    .tiny { font-size: 12px; color: var(--muted); }
    .series { height: 44px; color: var(--accent); font-size: 25px; line-height: 1; letter-spacing: 0; overflow: hidden; }
    /* Estilos para cuotas/odds */
    li.match { background: linear-gradient(135deg, var(--panel-2), var(--panel)); padding: 12px; border-radius: 6px; margin: 10px 0; border-left: 3px solid var(--accent); }
    .comp { font-size: 13px; color: var(--accent); margin-bottom: 6px; }
    .teams { font-weight: 700; font-size: 15px; margin-bottom: 8px; color: var(--text); }
    .details { display: grid; grid-template-columns: auto 1fr auto; gap: 12px; font-size: 12px; color: var(--muted); align-items: center; }
    .details span { display: flex; align-items: center; gap: 4px; }
    .details strong { color: var(--ok); }
    li.error { background: var(--panel-2); padding: 10px; border-radius: 6px; color: var(--bad); border-left: 3px solid var(--bad); }
    @media (max-width: 920px) {
      header, .layout { display: block; }
      .metrics, .grid { grid-template-columns: 1fr; }
      .toolbar { margin-top: 14px; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Price Tracker Control</h1>
        <p id="stamp">Cargando estado...</p>
      </div>
      <div class="toolbar">
        <button class="primary" onclick="runCheck()">Revisar ahora</button>
        <button onclick="refresh()">Actualizar</button>
        <button onclick="window.open('/dashboard.html', '_blank')">Dashboard clásico</button>
      </div>
    </header>

    <section class="metrics">
      <div class="metric"><span>Productos</span><strong id="mProducts">0</strong></div>
      <div class="metric"><span>Detectados</span><strong id="mDetected">0</strong></div>
      <div class="metric"><span>Alertas recientes</span><strong id="mAlerts">0</strong></div>
      <div class="metric"><span>Problemas</span><strong id="mProblems">0</strong></div>
    </section>

    <section class="layout">
      <div class="grid" id="products"></div>
      <aside>
        <section class="panel">
          <h2>Agregar producto</h2>
          <form id="addForm" onsubmit="addProduct(event)">
            <div class="form-grid">
              <input name="name" placeholder="Nombre" required>
              <select name="currency"><option>COP</option><option>USD</option></select>
              <input class="wide" name="url" placeholder="URL" required>
              <input name="target_price" type="number" step="0.01" placeholder="Objetivo">
              <button class="primary" type="submit">Agregar</button>
            </div>
          </form>
        </section>
        <section class="panel">
          <h2>Cuotas</h2>
          <ul id="odds"></ul>
        </section>
        <section class="panel">
          <h2>Ultimas alertas</h2>
          <ul id="alerts"></ul>
        </section>
        <section class="panel">
          <h2>Salud</h2>
          <ul id="health"></ul>
        </section>
      </aside>
    </section>
  </main>
  <script>
    const money = (currency, value) => {
      if (value === null || value === undefined) return 'Sin precio';
      return `${currency} ${Number(value).toLocaleString(undefined, { maximumFractionDigits: currency === 'USD' ? 2 : 0 })}`;
    };
    const spark = (values) => {
      if (!values.length) return 'Sin datos';
      const blocks = '▁▂▃▄▅▆▇█';
      const low = Math.min(...values), high = Math.max(...values);
      if (low === high) return '▄'.repeat(Math.min(values.length, 24));
      return values.slice(-24).map(v => blocks[Math.round((v - low) / (high - low) * (blocks.length - 1))]).join('');
    };
    async function api(path, options = {}) {
      options.headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
      const res = await fetch(path, options);
      if (!res.ok) {
        let message = await res.text();
        try { message = JSON.parse(message).error || message; } catch (_) {}
        throw new Error(message);
      }
      return res.json();
    }
    async function refresh() {
      const data = await api('/api/status');
      const configProducts = data.config.products || [];
      const latest = new Map((data.status.products || []).map(p => [p.id, p]));
      const running = data.check?.running ? ' | revision en curso' : '';
      document.getElementById('stamp').textContent = `Ultima revision: ${data.status.generated_at || 'sin datos'}${running}`;
      document.getElementById('mProducts').textContent = configProducts.length;
      document.getElementById('mDetected').textContent = (data.status.products || []).filter(p => p.price !== null && p.price !== undefined).length;
      document.getElementById('mAlerts').textContent = data.alerts.length;
      document.getElementById('mProblems').textContent = (data.health.summary?.problems || []).length;
      document.getElementById('products').innerHTML = configProducts.map(product => {
        const item = latest.get(product.id) || { id: product.id, name: product.name, price: null, currency: product.currency, source: 'pending', state: product.active === false ? 'blocked' : 'missing', state_label: product.active === false ? 'Pausado' : 'Pendiente' };
        const values = (data.series[product.id] || []).map(row => row.price);
        const activeText = product.active === false ? 'Activar' : 'Pausar';
        const label = item.state_label || ({ok: 'OK', missing: 'Sin precio', timeout: 'Timeout', blocked: 'Bloqueado', error: 'Error'}[item.state] || 'Pendiente');
        return `<article class="product">
          <div class="top"><span class="badge ${escapeAttr(item.state || 'missing')}">${escapeHtml(label)}</span><span class="badge">${escapeHtml(item.source || '')}</span></div>
          <div class="name">${escapeHtml(product.name)}</div>
          <div class="price">${money(product.currency, item.price)}</div>
          <div class="series">${spark(values)}</div>
          <form onsubmit="saveProduct(event, ${jsString(product.id)})">
            <div class="form-grid">
              <input name="target_price" type="number" step="0.01" value="${product.target_price ?? ''}" placeholder="Objetivo">
              <select name="currency">
                <option ${product.currency === 'COP' ? 'selected' : ''}>COP</option>
                <option ${product.currency === 'USD' ? 'selected' : ''}>USD</option>
              </select>
              <input class="wide" name="url" value="${escapeAttr(product.url || '')}" placeholder="URL">
            </div>
            <div class="actions">
              <button type="submit">Guardar</button>
              <button type="button" onclick="toggleProduct(${jsString(product.id)}, ${product.active === false ? 'true' : 'false'})">${activeText}</button>
              <button type="button" onclick="window.open(${jsString(product.url || '#')}, '_blank')">Abrir</button>
            </div>
          </form>
          <p class="tiny">${escapeHtml(item.state_detail || '')}</p>
        </article>`;
      }).join('');
      document.getElementById('odds').innerHTML = (data.status.odds || []).slice(0, 12).map(o => {
        if (o.error) return `<li class="error"><strong>⚠️ ${escapeHtml(o.error)}</strong></li>`;
        
        const time = o.commence_time ? new Date(o.commence_time).toLocaleString('es-CO') : 'TBD';
        const competition = o.competition || o.sport || 'Partido';
        const bestPrice = o.outcomes?.[0]?.best_price || 'N/A';
        const bookmaker = o.outcomes?.[0]?.bookmakers?.[0]?.title || 'Varios';
        
        return `<li class="match">
          <div class="comp"><strong>⚽ ${escapeHtml(competition)}</strong></div>
          <div class="teams">${escapeHtml(o.home_team)} vs ${escapeHtml(o.away_team)}</div>
          <div class="details">
            <span class="time">🕐 ${escapeHtml(time)}</span>
            <span class="odds">Cuota: <strong>${escapeHtml(String(bestPrice))}</strong></span>
            <span class="bookie">Via ${escapeHtml(bookmaker)}</span>
          </div>
        </li>`;
      }).join('') || '<li>Sin cuotas disponibles</li>';
      document.getElementById('alerts').innerHTML = data.alerts.slice(0, 8).map(a => `<li>${escapeHtml(a.name)}<br><span class="tiny">${escapeHtml(a.sent_at)}</span></li>`).join('') || '<li>Sin alertas recientes</li>';
      const health = data.health || {};
      const channels = health.channels || {};
      const providers = health.providers || {};
      const problems = health.summary?.problems || [];
      document.getElementById('health').innerHTML = [
        `Discord: ${channels.discord ? 'activo' : 'apagado'}`,
        `ntfy: ${channels.ntfy ? 'activo' : 'apagado'}`,
        `Keepa: ${providers.keepa ? 'activo' : 'pendiente'}`,
        `Amadeus: ${providers.amadeus ? 'activo' : 'pendiente'}`,
        `Backups: ${health.bot?.backup_enabled ? 'activo' : 'apagado'}`,
        ...(problems.length ? problems.map(p => p.replace(/^- /, '')) : ['Sin problemas criticos'])
      ].map(line => `<li>${escapeHtml(line)}</li>`).join('');
    }
    async function runCheck() {
      try {
        const result = await api('/api/check', { method: 'POST' });
        document.getElementById('stamp').textContent = result.started ? 'Revision iniciada...' : 'Ya hay una revision en curso...';
        setTimeout(refresh, 5000);
      } catch (error) {
        document.getElementById('stamp').textContent = `No se pudo iniciar: ${error.message}`;
      }
    }
    async function saveProduct(event, id) {
      event.preventDefault();
      const form = new FormData(event.target);
      await api('/api/product', { method: 'POST', body: JSON.stringify({ id, target_price: form.get('target_price'), currency: form.get('currency'), url: form.get('url') }) });
      await refresh();
    }
    async function toggleProduct(id, active) {
      await api('/api/product', { method: 'POST', body: JSON.stringify({ id, active }) });
      await refresh();
    }
    async function addProduct(event) {
      event.preventDefault();
      const form = new FormData(event.target);
      await api('/api/add-product', { method: 'POST', body: JSON.stringify(Object.fromEntries(form.entries())) });
      event.target.reset();
      await refresh();
    }
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[ch]));
    }
    function escapeAttr(value) { return escapeHtml(value).replace(/`/g, '&#096;'); }
    function jsString(value) { return JSON.stringify(String(value ?? '')); }
    refresh();
    setInterval(refresh, 30000);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return html_response(self, INDEX_HTML)
        if path == "/dashboard.html":
            dashboard = BASE_DIR / "dashboard.html"
            if dashboard.exists():
                return html_response(self, dashboard.read_text(encoding="utf-8"))
        if path == "/api/status":
            config = public_config(load_config())
            status = read_status()
            series = {
                product.get("id"): storage.product_series(product.get("id"))
                for product in config.get("products", [])
            }
            return json_response(self, {
                "config": config,
                "status": status,
                "alerts": storage.recent_alerts(),
                "checks": storage.recent_checks(60),
                "series": series,
                "check": check_state(),
                "health": service_health(config, status),
            })
        json_response(self, {"error": "not_found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {key: values[0] for key, values in parse_qs(raw).items()}

        try:
            if path == "/api/check":
                started = start_check()
                return json_response(self, {"ok": True, "started": started, "check": check_state()}, 202 if started else 200)
            if path == "/api/product":
                product_id = payload.pop("id", "")
                if update_product(product_id, payload):
                    return json_response(self, {"ok": True})
                return json_response(self, {"error": "product_not_found"}, 404)
            if path == "/api/add-product":
                product_id = add_product(payload)
                return json_response(self, {"ok": True, "id": product_id})
        except ValueError as e:
            return json_response(self, {"error": str(e)}, 400)
        json_response(self, {"error": "not_found"}, 404)

    def log_message(self, fmt, *args):
        return


def main():
    config = load_config()
    port = int(config.get("bot", {}).get("web_port", 8765))
    storage.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Price Tracker Control: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
