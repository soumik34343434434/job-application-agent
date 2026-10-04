import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "applications.db"
IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            description TEXT,
            score INTEGER,
            recommendation TEXT,
            status TEXT NOT NULL DEFAULT 'FOUND',
            created_at TEXT NOT NULL,
            fingerprint TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS application_prep (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE,
            prep_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'READY_FOR_REVIEW',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )
        """)
        # Safe migration from v0.3.
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "fingerprint" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN fingerprint TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")


def add_job(company, title, url, description, score, recommendation, fingerprint=None):
    with connect() as c:
        cur = c.execute(
            "INSERT INTO jobs(company,title,url,description,score,recommendation,status,created_at,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
            (company, title, url, description, score, recommendation, "FOUND", datetime.now(timezone.utc).isoformat(), fingerprint)
        )
        return cur.lastrowid


def find_duplicate(company, title, url, fingerprint):
    with connect() as c:
        if fingerprint:
            row = c.execute("SELECT * FROM jobs WHERE fingerprint=? ORDER BY id DESC LIMIT 1", (fingerprint,)).fetchone()
            if row:
                return row
        row = c.execute(
            "SELECT * FROM jobs WHERE lower(company)=lower(?) AND lower(title)=lower(?) ORDER BY id DESC LIMIT 1",
            (company.strip(), title.strip())
        ).fetchone()
        return row


def list_jobs():
    with connect() as c:
        return c.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()


def get_job(job_id):
    with connect() as c:
        return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def save_prep(job_id, prep_json):
    now = datetime.now(timezone.utc).isoformat()
    with connect() as c:
        c.execute(
            "INSERT INTO application_prep(job_id,prep_json,status,created_at,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET prep_json=excluded.prep_json, status='READY_FOR_REVIEW', updated_at=excluded.updated_at",
            (job_id, prep_json, "READY_FOR_REVIEW", now, now),
        )
        c.execute("UPDATE jobs SET status='READY_FOR_REVIEW' WHERE id=?", (job_id,))


def get_prep(job_id):
    with connect() as c:
        return c.execute("SELECT * FROM application_prep WHERE job_id=?", (job_id,)).fetchone()


def log_event(job_id, event, note=""):
    with connect() as c:
        c.execute(
            "INSERT INTO application_events(job_id,event,note,created_at) VALUES(?,?,?,?)",
            (job_id, event, note, datetime.now(timezone.utc).isoformat()),
        )


def submitted_count_today():
    current = now_ist()
    start_ist = current.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    end_ist = start_ist + timedelta(days=1)
    start_utc = start_ist.astimezone(timezone.utc).isoformat()
    end_utc = end_ist.astimezone(timezone.utc).isoformat()
    with connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM application_events WHERE event='SUBMITTED' AND created_at>=? AND created_at<?",
            (start_utc, end_utc)
        ).fetchone()
        return int(row["n"])


def set_status(job_id, status, note="", daily_cap=10):
    status = status.upper()
    current = get_job(job_id)
    if not current:
        raise ValueError(f"Job #{job_id} not found.")
    if status == "SUBMITTED":
        if current["status"] == "SUBMITTED":
            return
        if submitted_count_today() >= daily_cap:
            raise ValueError(f"Daily application cap of {daily_cap} reached.")
    with connect() as c:
        c.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    log_event(job_id, status, note)


def review_queue():
    with connect() as c:
        return c.execute("""
            SELECT j.*, p.prep_json, p.status AS prep_status
            FROM jobs j
            JOIN application_prep p ON p.job_id=j.id
            WHERE j.status IN ('READY_FOR_REVIEW','APPROVED')
            ORDER BY j.score DESC, j.id DESC
        """).fetchall()
