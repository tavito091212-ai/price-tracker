# 🤖 Price Tracker Bot

Rastreador de precios automático que corre cada hora en GitHub Actions y te notifica por Discord, ntfy o WhatsApp cuando un precio baja a tu objetivo.

Soporta productos de **Falabella**, **MercadoLibre**, **Amazon**, vuelos de **Google Flights** (vía SerpAPI o Amadeus API), próximos lanzamientos en **Game Pass** con fechas, y cuotas deportivas de la **Odds API** (Champions League, Mundial, etc.).

---

## ✨ Funcionalidades

- 🛒 Scraping con Playwright (anti-bot mejorado por tienda)
- ✈️ Precios de vuelos via SerpAPI o Amadeus API con links propios
- 🎮 Próximos lanzamientos en Game Pass con fechas de salida
- 📦 Precios de Amazon con soporte Keepa API
- ⚽ Cuotas deportivas en tiempo real (Champions League, Eliminatorias 2026, Copa Libertadores)
- 🔔 Alertas por Discord, ntfy, WhatsApp y Email
- 📊 Dashboard HTML con historial de precios y cuotas
- 💰 Soporte para rastreo de cuotas/planes de pago
- 💾 Historial persistido en JSON
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

### 2. Configura las APIs (Local)

Copia `.env.example` a `.env` y completa con tus claves:

```bash
cp .env.example .env
# Edita .env con tus valores reales
```

**APIs disponibles:**
- 🎯 **ODDS_API_KEY** → https://api.odds-api.com (cuotas deportivas)
- ✈️ **AMADEUS_CLIENT_ID/SECRET** → https://developer.amadeus.com (precios de vuelos)
- 💬 **DISCORD_WEBHOOK_URL** → Webhook de tu servidor Discord
- 🔔 **Otros** → Ver `.env.example` para opcionales

### 3. Configura tus productos en `config.json`

Edita la sección `"products"` con:
- URLs de productos
- Precios objetivo
- Opciones de cuotas (si aplica)

### 4. Prueba localmente

```bash
python tracker.py              # Una sola ejecución
python tracker.py --watch      # Modo watch (cada X minutos)
python web_app.py              # Dashboard en http://localhost:8765
```

### 5. Sube a GitHub (Con los Secrets)

Ve a tu repo → **Settings → Secrets and variables → Actions → New repository secret**

Copia **TODOS** los valores de tu `.env` como secrets en GitHub:

| Secret | Desde .env | Descripción |
|--------|-----------|------------|
| `DISCORD_WEBHOOK_URL` | DISCORD_WEBHOOK_URL | Notificaciones Discord |
| `ODDS_API_KEY` | ODDS_API_KEY | Cuotas deportivas |
| `AMADEUS_CLIENT_ID` | AMADEUS_CLIENT_ID | Precios de vuelos |
| `AMADEUS_CLIENT_SECRET` | AMADEUS_CLIENT_SECRET | Precios de vuelos |
| `SERPAPI_KEY` | SERPAPI_KEY | *(Opcional)* Google Flights |
| `KEEPA_API_KEY` | KEEPA_API_KEY | *(Opcional)* Amazon histórico |
| `GAMEPASS_API_KEY` | GAMEPASS_API_KEY | *(Opcional)* Game Pass |
| `WHATSAPP_*` | WHATSAPP_* | *(Opcional)* WhatsApp |

> **⚠️ IMPORTANTE:** Nunca subase `.env` a GitHub. Ya está en `.gitignore`.

### 6. Activa el workflow

El workflow en `.github/workflows/tracker.yml` corre automáticamente cada hora. También puedes lanzarlo manualmente desde **Actions → Run workflow**.

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
