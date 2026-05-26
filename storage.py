import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).parent
DB_FILE = BASE_DIR / "data" / "tracker.db"
LOCAL_TZ = ZoneInfo("America/Bogota")


def connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT NOT NULL,
                product_id TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                currency TEXT NOT NULL,
                price REAL,
                target REAL,
                source TEXT NOT NULL,
                state TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_checks_product_time
            ON checks(product_id, checked_at);

            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_at TEXT NOT NULL,
                product_id TEXT NOT NULL,
                name TEXT NOT NULL,
                signature TEXT NOT NULL,
                payload TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_alert_signature_time
            ON alert_events(signature, sent_at);
            """
        )


def now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def record_results(results: list[dict]):
    init_db()
    with connect() as conn:
        for item in results:
            conn.execute(
                """
                INSERT INTO checks (
                    checked_at, product_id, name, url, currency, price, target, source, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("checked_at") or now_iso(),
                    item.get("id", ""),
                    item.get("name", ""),
                    item.get("url", ""),
                    item.get("currency", ""),
                    item.get("price"),
                    item.get("target"),
                    item.get("source", ""),
                    item.get("state", ""),
                ),
            )


def alert_signature(alert: dict) -> str:
    current = alert.get("current")
    if isinstance(current, float):
        current = round(current, 2)
    return f"{alert.get('id', '')}:{alert.get('reason', 'target')}:{current}"


def should_send_alert(alert: dict, cooldown_hours: int = 12) -> bool:
    init_db()
    signature = alert_signature(alert)
    cutoff = datetime.now(LOCAL_TZ) - timedelta(hours=cooldown_hours)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT sent_at FROM alert_events
            WHERE signature = ?
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            (signature,),
        ).fetchone()
    if not row:
        return True
    try:
        sent_at = datetime.fromisoformat(row["sent_at"])
    except ValueError:
        return True
    return sent_at < cutoff


def record_alerts(alerts: list[dict]):
    if not alerts:
        return
    init_db()
    sent_at = now_iso()
    with connect() as conn:
        for alert in alerts:
            conn.execute(
                """
                INSERT INTO alert_events (sent_at, product_id, name, signature, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    sent_at,
                    alert.get("id", ""),
                    alert.get("name", ""),
                    alert_signature(alert),
                    json.dumps(alert, ensure_ascii=False),
                ),
            )


def recent_checks(limit: int = 200) -> list[dict]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM checks
            ORDER BY checked_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def recent_alerts(limit: int = 50) -> list[dict]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM alert_events
            ORDER BY sent_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def product_series(product_id: str, limit: int = 120) -> list[dict]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT checked_at, price, source, state
            FROM checks
            WHERE product_id = ? AND price IS NOT NULL
            ORDER BY checked_at DESC
            LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def product_recent_checks(product_id: str, limit: int = 24) -> list[dict]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT checked_at, price, source, state
            FROM checks
            WHERE product_id = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def product_health(product_id: str, limit: int = 24) -> dict:
    checks = product_recent_checks(product_id, limit)
    if not checks:
        return {
            "checks": 0,
            "detected": 0,
            "missed": 0,
            "success_rate": None,
            "last_seen": None,
            "last_state": "pending",
            "last_source": "pending",
        }

    detected = sum(1 for row in checks if row.get("price") is not None)
    missed = len(checks) - detected
    last_seen = next((row["checked_at"] for row in checks if row.get("price") is not None), None)
    latest = checks[0]
    return {
        "checks": len(checks),
        "detected": detected,
        "missed": missed,
        "success_rate": detected / len(checks),
        "last_seen": last_seen,
        "last_state": latest.get("state", "pending"),
        "last_source": latest.get("source", "pending"),
    }
