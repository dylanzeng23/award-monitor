import sqlite3
from datetime import datetime
from pathlib import Path

from models import AwardResult, RunLog

DB_PATH = Path(__file__).parent / "award_monitor.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY,
            scraper TEXT,
            origin TEXT,
            destination TEXT,
            flight_date TEXT,
            flight_number TEXT,
            cabin TEXT,
            miles_cost INTEGER,
            stops INTEGER,
            departure_time TEXT,
            arrival_time TEXT,
            aircraft TEXT,
            operating_carrier TEXT,
            dedup_key TEXT UNIQUE,
            first_seen TEXT,
            last_seen TEXT,
            notified INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY,
            scraper TEXT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            routes_checked INTEGER,
            results_found INTEGER,
            error_message TEXT
        );
    """)
    conn.commit()
    conn.close()


def is_new_availability(result: AwardResult) -> bool:
    """Return True if this result hasn't been seen before."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM availability WHERE dedup_key = ?",
        (result.dedup_key,)
    ).fetchone()
    conn.close()
    return row is None


def save_result(result: AwardResult) -> bool:
    """Save result. Returns True if it's new (inserted), False if updated."""
    conn = get_conn()
    now = datetime.utcnow().isoformat()
    try:
        conn.execute(
            """INSERT INTO availability
               (scraper, origin, destination, flight_date, flight_number,
                cabin, miles_cost, stops, departure_time, arrival_time,
                aircraft, operating_carrier, dedup_key, first_seen, last_seen, notified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (result.scraper, result.origin, result.destination,
             result.flight_date.isoformat(), result.flight_number,
             result.cabin, result.miles_cost, result.stops,
             result.departure_time, result.arrival_time,
             result.aircraft, result.operating_carrier,
             result.dedup_key, now, now)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE availability SET last_seen = ?, miles_cost = ? WHERE dedup_key = ?",
            (now, result.miles_cost, result.dedup_key)
        )
        conn.commit()
        conn.close()
        return False


def mark_notified(result: AwardResult):
    conn = get_conn()
    conn.execute(
        "UPDATE availability SET notified = 1 WHERE dedup_key = ?",
        (result.dedup_key,)
    )
    conn.commit()
    conn.close()


def save_run_log(log: RunLog) -> int:
    conn = get_conn()
    cursor = conn.execute(
        """INSERT INTO run_log (scraper, started_at, finished_at, status, routes_checked, results_found, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (log.scraper, log.started_at.isoformat(),
         log.finished_at.isoformat() if log.finished_at else None,
         log.status, log.routes_checked, log.results_found, log.error_message)
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def update_run_log(row_id: int, log: RunLog):
    conn = get_conn()
    conn.execute(
        """UPDATE run_log SET finished_at = ?, status = ?, routes_checked = ?, results_found = ?, error_message = ?
           WHERE id = ?""",
        (log.finished_at.isoformat() if log.finished_at else None,
         log.status, log.routes_checked, log.results_found, log.error_message, row_id)
    )
    conn.commit()
    conn.close()


def get_last_run(scraper: str = None) -> dict | None:
    conn = get_conn()
    if scraper:
        row = conn.execute(
            "SELECT * FROM run_log WHERE scraper = ? ORDER BY id DESC LIMIT 1",
            (scraper,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM run_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "scraper": row[1], "started_at": row[2],
        "finished_at": row[3], "status": row[4], "routes_checked": row[5],
        "results_found": row[6], "error_message": row[7]
    }


def get_recent_availability(limit: int = 20) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT scraper, origin, destination, flight_date, flight_number,
                  cabin, miles_cost, stops, operating_carrier, first_seen
           FROM availability ORDER BY first_seen DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return [
        {"scraper": r[0], "origin": r[1], "destination": r[2],
         "flight_date": r[3], "flight_number": r[4], "cabin": r[5],
         "miles_cost": r[6], "stops": r[7], "operating_carrier": r[8],
         "first_seen": r[9]}
        for r in rows
    ]
