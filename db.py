import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.getenv("DB_PATH", "bot.db")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent")


def now():
    """Hozirgi vaqt (sozlangan vaqt zonasida), tzinfo'siz."""
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


def today():
    return now().date().isoformat()


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _add_column(c, table, column, ddl):
    cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            start_date TEXT,
            active INTEGER DEFAULT 1,
            last_sent_day INTEGER DEFAULT 0,
            last_sent_date TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_messages (
            day INTEGER PRIMARY KEY,
            text TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sent_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day INTEGER,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS scheduled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            text TEXT,
            voice_id TEXT,
            btn_text TEXT,
            btn_url TEXT,
            done INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS helper_admins (
            user_id INTEGER PRIMARY KEY,
            role TEXT DEFAULT 'helper',
            added_by INTEGER,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Eski bazalar uchun yangi ustunlar
        _add_column(c, "daily_messages", "voice_id", "TEXT")
        _add_column(c, "daily_messages", "btn_text", "TEXT")
        _add_column(c, "daily_messages", "btn_url", "TEXT")


# ---------- Sozlamalar ----------
def get_setting(key, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with conn() as c:
        if value is None:
            c.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            c.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------- Foydalanuvchilar ----------
def add_user(user_id, full_name, username):
    """Yangi foydalanuvchini saqlaydi. Yangi bo'lsa True qaytaradi."""
    with conn() as c:
        row = c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            c.execute("UPDATE users SET active=1, full_name=?, username=? WHERE user_id=?",
                      (full_name, username, user_id))
            return False
        c.execute(
            "INSERT INTO users (user_id, full_name, username, start_date) VALUES (?,?,?,?)",
            (user_id, full_name, username, today()),
        )
        return True


def active_users():
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE active=1").fetchall()


def all_users():
    with conn() as c:
        return c.execute("SELECT * FROM users ORDER BY start_date, user_id").fetchall()


def count_users():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users(limit, offset):
    with conn() as c:
        return c.execute(
            "SELECT * FROM users ORDER BY start_date DESC, user_id DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()


def search_users(q, limit=15):
    q = q.strip().lstrip("@")
    if not q:
        return []
    like = f"%{q}%"
    with conn() as c:
        if q.isdigit():
            return c.execute(
                "SELECT * FROM users WHERE user_id=? OR username LIKE ? OR full_name LIKE ? LIMIT ?",
                (int(q), like, like, limit)).fetchall()
        return c.execute(
            "SELECT * FROM users WHERE username LIKE ? OR full_name LIKE ? LIMIT ?",
            (like, like, limit)).fetchall()


def find_user(q):
    """ID yoki @username bo'yicha bitta foydalanuvchini topadi."""
    q = q.strip()
    with conn() as c:
        if q.lstrip("-").isdigit():
            return c.execute("SELECT * FROM users WHERE user_id=?", (int(q),)).fetchone()
        return c.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)",
                         (q.lstrip("@"),)).fetchone()


def get_user(user_id):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def reset_user_progress(user_id):
    """Foydalanuvchi kunlarini 1-kundan qayta boshlaydi."""
    with conn() as c:
        c.execute("UPDATE users SET last_sent_day=0, last_sent_date=NULL WHERE user_id=?", (user_id,))


def set_active(user_id, active):
    with conn() as c:
        c.execute("UPDATE users SET active=? WHERE user_id=?", (int(bool(active)), user_id))


def set_inactive(user_id):
    with conn() as c:
        c.execute("UPDATE users SET active=0 WHERE user_id=?", (user_id,))


def stats():
    t = today()
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
        new_today = c.execute("SELECT COUNT(*) FROM users WHERE start_date=?", (t,)).fetchone()[0]
        sent_today = c.execute("SELECT COUNT(*) FROM users WHERE last_sent_date=?", (t,)).fetchone()[0]
        msgs = c.execute("SELECT COUNT(*) FROM daily_messages").fetchone()[0]
        sched = c.execute("SELECT COUNT(*) FROM scheduled WHERE done=0").fetchone()[0]
        return {
            "total": total,
            "active": active,
            "blocked": total - active,
            "new_today": new_today,
            "sent_today": sent_today,
            "messages": msgs,
            "scheduled": sched,
        }


# ---------- Kunlik xabarlar ----------
def add_daily_message(text, voice_id=None, btn_text=None, btn_url=None):
    with conn() as c:
        n = c.execute("SELECT COALESCE(MAX(day),0) FROM daily_messages").fetchone()[0] + 1
        c.execute(
            "INSERT INTO daily_messages (day, text, voice_id, btn_text, btn_url) VALUES (?,?,?,?,?)",
            (n, text or "", voice_id, btn_text, btn_url))
        return n


def total_daily_messages():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM daily_messages").fetchone()[0]


def get_daily_message(day):
    with conn() as c:
        row = c.execute("SELECT * FROM daily_messages WHERE day=?", (day,)).fetchone()
        return dict(row) if row else None


def list_daily_messages():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM daily_messages ORDER BY day")]


def update_daily_message(day, text, voice_id=None, btn_text=None, btn_url=None):
    with conn() as c:
        c.execute(
            "UPDATE daily_messages SET text=?, voice_id=?, btn_text=?, btn_url=? WHERE day=?",
            (text or "", voice_id, btn_text, btn_url, day))


def delete_daily_message(day):
    """Xabarni o'chiradi va keyingi kunlar raqamini bittaga kamaytiradi."""
    with conn() as c:
        c.execute("DELETE FROM daily_messages WHERE day=?", (day,))
        c.execute("UPDATE daily_messages SET day=day-1 WHERE day>?", (day,))


def mark_sent(user_id, day, today_str):
    with conn() as c:
        c.execute("UPDATE users SET last_sent_day=?, last_sent_date=? WHERE user_id=?",
                  (day, today_str, user_id))
        c.execute("INSERT INTO sent_log (user_id, day) VALUES (?,?)", (user_id, day))


# ---------- Rejalashtirilgan xabarlar ----------
def add_scheduled(run_at, text, voice_id=None, btn_text=None, btn_url=None):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO scheduled (run_at, text, voice_id, btn_text, btn_url) VALUES (?,?,?,?,?)",
            (run_at, text or "", voice_id, btn_text, btn_url))
        return cur.lastrowid


def list_scheduled():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM scheduled WHERE done=0 ORDER BY run_at")]


def due_scheduled(now_str):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM scheduled WHERE done=0 AND run_at<=? ORDER BY run_at", (now_str,))]


def mark_scheduled_done(sid):
    with conn() as c:
        c.execute("UPDATE scheduled SET done=1 WHERE id=?", (sid,))


def delete_scheduled(sid):
    with conn() as c:
        c.execute("DELETE FROM scheduled WHERE id=?", (sid,))


# ---------- Yordamchi adminlar ----------
def is_helper_admin(user_id):
    with conn() as c:
        return c.execute("SELECT 1 FROM helper_admins WHERE user_id=?", (user_id,)).fetchone() is not None


def list_helper_admins():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM helper_admins ORDER BY added_at, user_id")]


def add_helper_admin(user_id, added_by=None, role="helper"):
    with conn() as c:
        c.execute(
            "INSERT INTO helper_admins (user_id, role, added_by) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",
            (user_id, role, added_by))


def remove_helper_admin(user_id):
    with conn() as c:
        return c.execute("DELETE FROM helper_admins WHERE user_id=?", (user_id,)).rowcount > 0