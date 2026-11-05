import atexit
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg_pool import ConnectionPool

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent")

_pool = None


def now():
    """Hozirgi vaqt (sozlangan vaqt zonasida), tzinfo'siz."""
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


def today():
    return now().date().isoformat()


# ---------- Ulanish ----------
def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise SystemExit(
                "DATABASE_URL topilmadi. Railway'da botingiz xizmatiga Postgres'ning DATABASE_URL "
                "o'zgaruvchisini qo'shing."
            )
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            check=ConnectionPool.check_connection,  # uzilgan ulanishlarni avtomatik yangilaydi
            open=True,
        )
        _pool.wait(timeout=30)
        atexit.register(_pool.close)  # dastur tugaganda ulanishlar tartib bilan yopiladi
    return _pool


def _exec(sql, params=()):
    """So'rovni bajaradi (yozish). O'zgargan qatorlar sonini qaytaradi."""
    with _get_pool().connection() as c:
        return c.execute(sql, params).rowcount


def _all(sql, params=()):
    with _get_pool().connection() as c:
        return c.execute(sql, params).fetchall()


def _one(sql, params=()):
    with _get_pool().connection() as c:
        return c.execute(sql, params).fetchone()


def _scalar(sql, params=()):
    with _get_pool().connection() as c:
        cur = c.cursor(row_factory=tuple_row)
        cur.execute(sql, params)
        return cur.fetchone()[0]


def init_db():
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            start_date TEXT,
            active INTEGER DEFAULT 1,
            last_sent_day INTEGER DEFAULT 0,
            last_sent_date TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS daily_messages (
            day INTEGER PRIMARY KEY,
            text TEXT NOT NULL DEFAULT '',
            voice_id TEXT,
            btn_text TEXT,
            btn_url TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS sent_log (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            day INTEGER,
            sent_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS scheduled (
            id BIGSERIAL PRIMARY KEY,
            run_at TEXT NOT NULL,
            text TEXT,
            voice_id TEXT,
            btn_text TEXT,
            btn_url TEXT,
            done INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS helper_admins (
            user_id BIGINT PRIMARY KEY,
            role TEXT DEFAULT 'helper',
            added_by BIGINT,
            added_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS inbox (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            kind TEXT,
            text TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'new',
            result TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS reactions (
            day INTEGER,
            user_id BIGINT,
            value TEXT,
            PRIMARY KEY (day, user_id)
        )""",
    ]
    with _get_pool().connection() as c:
        for s in statements:
            c.execute(s)


# ---------- Sozlamalar ----------
def get_setting(key, default=None):
    row = _one("SELECT value FROM settings WHERE key=%s", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    if value is None:
        _exec("DELETE FROM settings WHERE key=%s", (key,))
    else:
        _exec("INSERT INTO settings (key, value) VALUES (%s,%s) "
              "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))


# ---------- Foydalanuvchilar ----------
def add_user(user_id, full_name, username):
    """Yangi foydalanuvchini saqlaydi. Yangi bo'lsa True qaytaradi."""
    updated = _exec("UPDATE users SET active=1, full_name=%s, username=%s WHERE user_id=%s",
                    (full_name, username, user_id))
    if updated:
        return False
    inserted = _exec(
        "INSERT INTO users (user_id, full_name, username, start_date) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (user_id) DO NOTHING",
        (user_id, full_name, username, today()))
    return inserted > 0


def active_users():
    return _all("SELECT * FROM users WHERE active=1")


def all_users():
    return _all("SELECT * FROM users ORDER BY start_date, user_id")


def count_users():
    return _scalar("SELECT COUNT(*) FROM users")


def list_users(limit, offset):
    return _all(
        "SELECT * FROM users ORDER BY start_date DESC, user_id DESC LIMIT %s OFFSET %s",
        (limit, offset))


def search_users(q, limit=15):
    q = q.strip().lstrip("@")
    if not q:
        return []
    like = f"%{q}%"
    if q.isdigit() and len(q) <= 18:
        return _all(
            "SELECT * FROM users WHERE user_id=%s OR username ILIKE %s OR full_name ILIKE %s LIMIT %s",
            (int(q), like, like, limit))
    return _all(
        "SELECT * FROM users WHERE username ILIKE %s OR full_name ILIKE %s LIMIT %s",
        (like, like, limit))


def find_user(q):
    """ID yoki @username bo'yicha bitta foydalanuvchini topadi."""
    q = q.strip()
    if q.lstrip("-").isdigit() and len(q) <= 19:
        return _one("SELECT * FROM users WHERE user_id=%s", (int(q),))
    return _one("SELECT * FROM users WHERE LOWER(username)=LOWER(%s)", (q.lstrip("@"),))


def get_user(user_id):
    return _one("SELECT * FROM users WHERE user_id=%s", (user_id,))


def reset_user_progress(user_id):
    """Foydalanuvchi kunlarini 1-kundan qayta boshlaydi."""
    _exec("UPDATE users SET last_sent_day=0, last_sent_date=NULL WHERE user_id=%s", (user_id,))


def set_active(user_id, active):
    _exec("UPDATE users SET active=%s WHERE user_id=%s", (int(bool(active)), user_id))


def set_inactive(user_id):
    _exec("UPDATE users SET active=0 WHERE user_id=%s", (user_id,))


def stats():
    t = today()
    total = _scalar("SELECT COUNT(*) FROM users")
    active = _scalar("SELECT COUNT(*) FROM users WHERE active=1")
    return {
        "total": total,
        "active": active,
        "blocked": total - active,
        "new_today": _scalar("SELECT COUNT(*) FROM users WHERE start_date=%s", (t,)),
        "sent_today": _scalar("SELECT COUNT(*) FROM users WHERE last_sent_date=%s", (t,)),
        "messages": _scalar("SELECT COUNT(*) FROM daily_messages"),
        "scheduled": _scalar("SELECT COUNT(*) FROM scheduled WHERE done=0"),
    }


# ---------- Kunlik xabarlar ----------
def add_daily_message(text, voice_id=None, btn_text=None, btn_url=None):
    with _get_pool().connection() as c:
        c.execute("LOCK TABLE daily_messages IN EXCLUSIVE MODE")  # ikkita admin bir vaqtda qo'shsa ham raqam takrorlanmaydi
        n = c.cursor(row_factory=tuple_row).execute(
            "SELECT COALESCE(MAX(day),0) FROM daily_messages").fetchone()[0] + 1
        c.execute(
            "INSERT INTO daily_messages (day, text, voice_id, btn_text, btn_url) VALUES (%s,%s,%s,%s,%s)",
            (n, text or "", voice_id, btn_text, btn_url))
        return n


def total_daily_messages():
    return _scalar("SELECT COUNT(*) FROM daily_messages")


def get_daily_message(day):
    row = _one("SELECT * FROM daily_messages WHERE day=%s", (day,))
    return dict(row) if row else None


def list_daily_messages():
    return [dict(r) for r in _all("SELECT * FROM daily_messages ORDER BY day")]


def update_daily_message(day, text, voice_id=None, btn_text=None, btn_url=None):
    _exec("UPDATE daily_messages SET text=%s, voice_id=%s, btn_text=%s, btn_url=%s WHERE day=%s",
          (text or "", voice_id, btn_text, btn_url, day))


def delete_daily_message(day):
    """Xabarni o'chiradi va keyingi kunlar raqamini bittaga kamaytiradi."""
    with _get_pool().connection() as c:
        c.execute("DELETE FROM daily_messages WHERE day=%s", (day,))
        # Postgres'da PRIMARY KEY har qator bo'yicha tekshiriladi, shuning uchun ikki bosqichda suriladi
        c.execute("UPDATE daily_messages SET day = -(day-1) WHERE day>%s", (day,))
        c.execute("UPDATE daily_messages SET day = -day WHERE day<0")
        # shu kunga bosilgan reaksiyalar o'chadi, keyingi kunlarniki ham birga suriladi
        c.execute("DELETE FROM reactions WHERE day=%s", (day,))
        c.execute("UPDATE reactions SET day = -(day-1) WHERE day>%s", (day,))
        c.execute("UPDATE reactions SET day = -day WHERE day<0")


def mark_sent(user_id, day, today_str):
    with _get_pool().connection() as c:
        c.execute("UPDATE users SET last_sent_day=%s, last_sent_date=%s WHERE user_id=%s",
                  (day, today_str, user_id))
        c.execute("INSERT INTO sent_log (user_id, day) VALUES (%s,%s)", (user_id, day))


# ---------- Rejalashtirilgan xabarlar ----------
def add_scheduled(run_at, text, voice_id=None, btn_text=None, btn_url=None):
    row = _one(
        "INSERT INTO scheduled (run_at, text, voice_id, btn_text, btn_url) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (run_at, text or "", voice_id, btn_text, btn_url))
    return row["id"]


def list_scheduled():
    return [dict(r) for r in _all("SELECT * FROM scheduled WHERE done=0 ORDER BY run_at")]


def due_scheduled(now_str):
    return [dict(r) for r in _all(
        "SELECT * FROM scheduled WHERE done=0 AND run_at<=%s ORDER BY run_at", (now_str,))]


def mark_scheduled_done(sid):
    _exec("UPDATE scheduled SET done=1 WHERE id=%s", (sid,))


def delete_scheduled(sid):
    _exec("DELETE FROM scheduled WHERE id=%s", (sid,))


# ---------- Yordamchi adminlar ----------
def is_helper_admin(user_id):
    return _one("SELECT 1 AS ok FROM helper_admins WHERE user_id=%s", (user_id,)) is not None


def list_helper_admins():
    return [dict(r) for r in _all("SELECT * FROM helper_admins ORDER BY added_at, user_id")]


def add_helper_admin(user_id, added_by=None, role="helper"):
    _exec(
        "INSERT INTO helper_admins (user_id, role, added_by) VALUES (%s,%s,%s) "
        "ON CONFLICT (user_id) DO UPDATE SET role=EXCLUDED.role",
        (user_id, role, added_by))


def remove_helper_admin(user_id):
    return _exec("DELETE FROM helper_admins WHERE user_id=%s", (user_id,)) > 0


# ---------- Murojaatlar va takliflar (inbox) ----------
def add_inbox(user_id, kind, text):
    """kind: 'msg' (adminga yozilgan xabar) yoki 'suggest' (kunlik xabar taklifi)."""
    row = _one(
        "INSERT INTO inbox (user_id, kind, text, created_at) VALUES (%s,%s,%s,%s) RETURNING id",
        (user_id, kind, text, now().strftime("%Y-%m-%d %H:%M")))
    return row["id"]


def count_inbox(kind=None, status="new"):
    if kind:
        return _scalar("SELECT COUNT(*) FROM inbox WHERE kind=%s AND status=%s", (kind, status))
    return _scalar("SELECT COUNT(*) FROM inbox WHERE status=%s", (status,))


def list_inbox(kind, status, limit, offset):
    where, params = "i.status=%s", [status]
    if kind:
        where += " AND i.kind=%s"
        params.append(kind)
    return _all(
        "SELECT i.*, u.full_name, u.username FROM inbox i "
        "LEFT JOIN users u ON u.user_id=i.user_id "
        f"WHERE {where} ORDER BY i.id DESC LIMIT %s OFFSET %s",
        (*params, limit, offset))


def get_inbox(item_id):
    return _one(
        "SELECT i.*, u.full_name, u.username FROM inbox i "
        "LEFT JOIN users u ON u.user_id=i.user_id WHERE i.id=%s", (item_id,))


def set_inbox_done(item_id, result):
    _exec("UPDATE inbox SET status='done', result=%s WHERE id=%s", (result, item_id))


def delete_inbox(item_id):
    _exec("DELETE FROM inbox WHERE id=%s", (item_id,))


def count_inbox_today(user_id, kind):
    return _scalar("SELECT COUNT(*) FROM inbox WHERE user_id=%s AND kind=%s AND created_at LIKE %s",
                   (user_id, kind, today() + "%"))


# ---------- Reaksiyalar ----------
def toggle_reaction(day, user_id, value):
    """Reaksiyani qo'yadi. Xuddi shu reaksiya qayta bosilsa, olib tashlaydi (None qaytaradi)."""
    row = _one("SELECT value FROM reactions WHERE day=%s AND user_id=%s", (day, user_id))
    if row and row["value"] == value:
        _exec("DELETE FROM reactions WHERE day=%s AND user_id=%s", (day, user_id))
        return None
    _exec("INSERT INTO reactions (day, user_id, value) VALUES (%s,%s,%s) "
          "ON CONFLICT (day, user_id) DO UPDATE SET value=EXCLUDED.value", (day, user_id, value))
    return value


def reaction_stats(limit=3):
    return _all(
        "SELECT day, COUNT(*) FILTER (WHERE value='l') AS likes, "
        "COUNT(*) FILTER (WHERE value='h') AS loves "
        "FROM reactions GROUP BY day ORDER BY COUNT(*) DESC, day LIMIT %s", (limit,))


# ---------- Statistika ----------
def growth(days=7):
    """Oxirgi `days` kun uchun har kuni nechta yangi foydalanuvchi qo'shilgani."""
    first = now().date() - timedelta(days=days - 1)
    rows = _all("SELECT start_date, COUNT(*) AS n FROM users WHERE start_date>=%s GROUP BY start_date",
                (first.isoformat(),))
    counts = {r["start_date"]: r["n"] for r in rows}
    out = []
    for i in range(days):
        d = (first + timedelta(days=i)).isoformat()
        out.append((d, counts.get(d, 0)))
    return out


def top_streaks(limit=5):
    return _all(
        "SELECT full_name, username, last_sent_day FROM users "
        "WHERE active=1 AND last_sent_day>0 ORDER BY last_sent_day DESC, user_id LIMIT %s", (limit,))