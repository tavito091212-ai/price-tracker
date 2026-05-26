# 🤖 Price Tracker Bot

Rastreador de precios automático que corre cada hora en GitHub Actions y te notifica por Discord, ntfy o WhatsApp cuando un precio baja a tu objetivo.

Soporta productos de **Falabella**, **MercadoLibre**, **Amazon**, **Steam**, vuelos de **Skyscanner** (vía SerpAPI), y cuotas deportivas de la **Odds API**.

---

## ✨ Funcionalidades

- 🛒 Scraping con Playwright (anti-bot mejorado por tienda)
- ✈️ Precios de vuelos via SerpAPI o Amadeus API
- 🎮 Precios de Steam con bypass de verificación de edad
- 📦 Precios de Amazon con soporte Keepa API
- ⚽ Cuotas deportivas en tiempo real (The Odds API)
- 🔔 Alertas por Discord, ntfy, WhatsApp y Email
- 📊 Dashboard HTML con historial de precios
- 💾 Historial persistido en SQLite (commiteado al repo automáticamente)
- 🔁 Corre solo en GitHub Actions, gratis

---

## 🚀 Setup

### 1. Clona el repo

```bash
git clone https://github.com/tu-usuario/price-tracker.git
cd price-tracker
pip install -r requirements.txt
playwright install chromium
```

### 2. Configura tus productos en `config.json`

Edita la sección `"products"` con las URLs y precios objetivo que quieres rastrear.

### 3. Agrega tus Secrets en GitHub

Ve a tu repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Descripción |
|--------|------------|
| `DISCORD_WEBHOOK_URL` | URL del webhook de tu canal Discord |
| `NTFY_TOPIC` | Tema de ntfy.sh (ej. `mi-price-tracker-abc123`) |
| `ODDS_API_KEY` | API key de [the-odds-api.com](https://the-odds-api.com) |
| `SERPAPI_KEY` | API key de [serpapi.com](https://serpapi.com) (vuelos) |
| `EMAIL_SENDER` | Gmail que envía las alertas |
| `EMAIL_PASSWORD` | App password de Gmail (no la contraseña normal) |
| `EMAIL_RECEIVER` | Email donde recibes las alertas |
| `KEEPA_API_KEY` | *(Opcional)* Para precios confiables de Amazon |
| `AMADEUS_CLIENT_ID` | *(Opcional)* Para vuelos via Amadeus |
| `AMADEUS_CLIENT_SECRET` | *(Opcional)* Para vuelos via Amadeus |

> **Nota sobre Gmail:** Necesitas activar verificación en 2 pasos y generar un *App Password* en [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).

### 4. Activa el workflow

El workflow en `.github/workflows/tracker.yml` corre automáticamente cada hora. También puedes lanzarlo manualmente desde la pestaña **Actions → Run workflow**.

---

## 📁 Estructura

```
price-tracker/
├── tracker.py          # Bot principal (scraping, alertas, dashboard)
├── storage.py          # Base de datos SQLite (historial y alertas)
├── web_app.py          # Dashboard web local
├── config.json         # Configuración de productos y canales
├── requirements.txt    # Dependencias Python
├── data/
│   ├── tracker.db      # Historial de precios (auto-commiteado)
│   └── precios.json    # Caché de precios (auto-commiteado)
├── logs/
│   └── tracker.log     # Logs de ejecución
└── .github/
    └── workflows/
        └── tracker.yml # GitHub Actions workflow
```

---

## ⚙️ Personalizar alertas

En `config.json`:

```json
"alert_drop_pct": 15,      // alerta si baja 15% del precio histórico
"alert_last_drop_pct": 5,  // alerta si baja 5% respecto al último check
"alert_rise_pct": 10       // alerta si sube 10% (para vuelos: avisa que subió)
```

---

## 🧪 Correr localmente

```bash
# Una sola vez
python tracker.py

# Loop cada X minutos (usa schedule)
python tracker.py --watch
```

---

## 🛑 Problemas comunes

**El workflow se pausó solo**
GitHub Actions pausa workflows con cron si el repo no tiene actividad en 60 días. Solución: lanzar manualmente desde la pestaña Actions de vez en cuando, o hacer cualquier commit.

**Skyscanner no detecta precio**
Skyscanner bloquea bots agresivamente. Activa SerpAPI en `config.json` para obtener precios de vuelos de forma confiable.

**Amazon pide captcha**
Normal en IPs de CI. Si falla frecuentemente, activa Keepa API que usa la API oficial de Amazon.

---

## 📄 Licencia

MIT
