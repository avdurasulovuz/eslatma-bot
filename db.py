import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_PATH = os.getenv("DB_PATH", "bot.db")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent")


def now():
    """Hozirgi vaqt (sozlangan vaqt zonasida), tzinfo'siz."""
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


def today():
    return now().date().isoformat()


def _ts():
    """Jadvallarga yoziladigan mahalliy vaqt matni."""
    return now().strftime("%Y-%m-%d %H:%M:%S")


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _dict(row):
    return dict(row) if row else None


def _dicts(rows):
    return [dict(r) for r in rows]


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
        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,              -- 'msg' (murojaat) yoki 'suggest' (taklif)
            text TEXT NOT NULL,
            status TEXT DEFAULT 'new',       -- 'new' yoki 'done'
            result TEXT,                     -- replied / accepted / rejected / archived
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status, kind, id);
        CREATE INDEX IF NOT EXISTS idx_inbox_user ON inbox(user_id, kind, created_at);
        CREATE TABLE IF NOT EXISTS reactions (
            day INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,              -- 'l' (like) yoki 'h' (heart)
            PRIMARY KEY (day, user_id)
        );
        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            admin_name TEXT,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT,
            preview TEXT,
            admin_id INTEGER,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS batch_items (
            batch_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_batch_items ON batch_items(batch_id);
        CREATE TABLE IF NOT EXISTS recurring (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            freq TEXT NOT NULL,              -- 'daily' yoki 'weekly'
            weekday INTEGER,                 -- 0 = dushanba (faqat weekly)
            send_at TEXT NOT NULL,           -- 'SS:DD'
            text TEXT,
            voice_id TEXT,
            btn_text TEXT,
            btn_url TEXT
        );
        """)
        # Eski bazalar uchun yangi ustunlar
        _add_column(c, "daily_messages", "voice_id", "TEXT")
        _add_column(c, "daily_messages", "btn_text", "TEXT")
        _add_column(c, "daily_messages", "btn_url", "TEXT")
        # Eski yuborilganlar ro'yxati (7 kundan eski) keraksiz: Telegram 48 soatdan keyin o'chirtirmaydi
        old = (now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("DELETE FROM batch_items WHERE batch_id IN (SELECT id FROM batches WHERE created_at<?)", (old,))


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


def get_user(user_id):
    with conn() as c:
        return _dict(c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())


def active_users():
    with conn() as c:
        return _dicts(c.execute("SELECT * FROM users WHERE active=1").fetchall())


def all_users():
    with conn() as c:
        return _dicts(c.execute("SELECT * FROM users ORDER BY start_date, user_id").fetchall())


def count_users():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users(limit, offset):
    with conn() as c:
        return _dicts(c.execute(
            "SELECT * FROM users ORDER BY start_date DESC, user_id DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall())


def search_users(q, limit=15):
    q = q.strip().lstrip("@")
    if not q:
        return []
    like = f"%{q}%"
    with conn() as c:
        if q.isdigit():
            return _dicts(c.execute(
                "SELECT * FROM users WHERE user_id=? OR username LIKE ? OR full_name LIKE ? LIMIT ?",
                (int(q), like, like, limit)).fetchall())
        return _dicts(c.execute(
            "SELECT * FROM users WHERE username LIKE ? OR full_name LIKE ? LIMIT ?",
            (like, like, limit)).fetchall())


def find_user(q):
    """ID yoki @username bo'yicha bitta foydalanuvchini topadi."""
    q = q.strip()
    with conn() as c:
        if q.lstrip("-").isdigit():
            return _dict(c.execute("SELECT * FROM users WHERE user_id=?", (int(q),)).fetchone())
        return _dict(c.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?)",
                               (q.lstrip("@"),)).fetchone())


def set_inactive(user_id):
    with conn() as c:
        c.execute("UPDATE users SET active=0 WHERE user_id=?", (user_id,))


def set_active(user_id, active=True):
    with conn() as c:
        c.execute("UPDATE users SET active=? WHERE user_id=?", (1 if active else 0, user_id))


def reset_user_progress(user_id):
    """Foydalanuvchi kunlarini 1-kundan qayta boshlaydi."""
    with conn() as c:
        c.execute("UPDATE users SET last_sent_day=0, last_sent_date=NULL WHERE user_id=?", (user_id,))


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


def growth(days=7):
    """Oxirgi `days` kunda har kuni nechta yangi foydalanuvchi qo'shilgani: [(sana, soni), ...]."""
    end = now().date()
    out = []
    with conn() as c:
        for i in range(days - 1, -1, -1):
            d = (end - timedelta(days=i)).isoformat()
            n = c.execute("SELECT COUNT(*) FROM users WHERE start_date=?", (d,)).fetchone()[0]
            out.append((d, n))
    return out


def top_streaks(limit=5):
    """Eng ko'p kun olgan foydalanuvchilar."""
    with conn() as c:
        return _dicts(c.execute(
            "SELECT user_id, full_name, username, last_sent_day FROM users "
            "WHERE last_sent_day>0 ORDER BY last_sent_day DESC, user_id LIMIT ?", (limit,)).fetchall())


# ---------- Yordamchi adminlar ----------
def is_helper_admin(user_id):
    with conn() as c:
        return c.execute("SELECT 1 FROM helper_admins WHERE user_id=?", (user_id,)).fetchone() is not None


def list_helper_admins():
    with conn() as c:
        return _dicts(c.execute("SELECT * FROM helper_admins ORDER BY added_at, user_id").fetchall())


def add_helper_admin(user_id, added_by=None, role="helper"):
    with conn() as c:
        c.execute(
            "INSERT INTO helper_admins (user_id, role, added_by) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",
            (user_id, role, added_by))


def remove_helper_admin(user_id):
    with conn() as c:
        return c.execute("DELETE FROM helper_admins WHERE user_id=?", (user_id,)).rowcount > 0


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
        return _dict(c.execute("SELECT * FROM daily_messages WHERE day=?", (day,)).fetchone())


def list_daily_messages():
    with conn() as c:
        return _dicts(c.execute("SELECT * FROM daily_messages ORDER BY day").fetchall())


def update_daily_message(day, text, voice_id=None, btn_text=None, btn_url=None):
    with conn() as c:
        c.execute(
            "UPDATE daily_messages SET text=?, voice_id=?, btn_text=?, btn_url=? WHERE day=?",
            (text or "", voice_id, btn_text, btn_url, day))


def delete_daily_message(day):
    """Xabarni o'chiradi, keyingi kunlar raqamini bittaga kamaytiradi (reaksiyalar ham ko'chadi)."""
    with conn() as c:
        c.execute("DELETE FROM daily_messages WHERE day=?", (day,))
        c.execute("UPDATE daily_messages SET day=day-1 WHERE day>?", (day,))
        c.execute("DELETE FROM reactions WHERE day=?", (day,))
        # Ikki bosqichda: bir xil (day, user_id) to'qnashuvi bo'lmasligi uchun
        c.execute("UPDATE reactions SET day=-(day-1) WHERE day>?", (day,))
        c.execute("UPDATE reactions SET day=-day WHERE day<0")


def mark_sent(user_id, day, today_str):
    with conn() as c:
        c.execute("UPDATE users SET last_sent_day=?, last_sent_date=? WHERE user_id=?",
                  (day, today_str, user_id))
        c.execute("INSERT INTO sent_log (user_id, day) VALUES (?,?)", (user_id, day))


# ---------- Reaksiyalar ----------
def toggle_reaction(day, user_id, kind):
    """Reaksiyani qo'yadi / almashtiradi / olib tashlaydi. Hozirgi reaksiyani ('l'/'h') yoki None qaytaradi."""
    with conn() as c:
        row = c.execute("SELECT kind FROM reactions WHERE day=? AND user_id=?", (day, user_id)).fetchone()
        if row and row["kind"] == kind:
            c.execute("DELETE FROM reactions WHERE day=? AND user_id=?", (day, user_id))
            return None
        c.execute("INSERT INTO reactions (day, user_id, kind) VALUES (?,?,?) "
                  "ON CONFLICT(day, user_id) DO UPDATE SET kind=excluded.kind", (day, user_id, kind))
        return kind


def reaction_stats(limit=3):
    with conn() as c:
        return _dicts(c.execute(
            "SELECT day, SUM(kind='l') AS likes, SUM(kind='h') AS loves FROM reactions "
            "GROUP BY day ORDER BY (likes+loves) DESC, day LIMIT ?", (limit,)).fetchall())


# ---------- Murojaatlar va takliflar ----------
def add_inbox(user_id, kind, text):
    with conn() as c:
        cur = c.execute("INSERT INTO inbox (user_id, kind, text, created_at) VALUES (?,?,?,?)",
                        (user_id, kind, text, _ts()))
        return cur.lastrowid


def _inbox_where(kind, status):
    where, args = ["1=1"], []
    if kind:
        where.append("i.kind=?")
        args.append(kind)
    if status:
        where.append("i.status=?")
        args.append(status)
    return " AND ".join(where), args


def count_inbox(kind=None, status=None):
    where, args = _inbox_where(kind, status)
    with conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM inbox i WHERE {where}", args).fetchone()[0]


def list_inbox(kind, status, limit, offset):
    where, args = _inbox_where(kind, status)
    with conn() as c:
        return _dicts(c.execute(
            "SELECT i.*, u.full_name, u.username FROM inbox i "
            f"LEFT JOIN users u ON u.user_id=i.user_id WHERE {where} "
            "ORDER BY i.id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall())


def get_inbox(inbox_id):
    with conn() as c:
        return _dict(c.execute(
            "SELECT i.*, u.full_name, u.username FROM inbox i "
            "LEFT JOIN users u ON u.user_id=i.user_id WHERE i.id=?", (inbox_id,)).fetchone())


def set_inbox_done(inbox_id, result):
    with conn() as c:
        c.execute("UPDATE inbox SET status='done', result=? WHERE id=?", (result, inbox_id))


def delete_inbox(inbox_id):
    with conn() as c:
        c.execute("DELETE FROM inbox WHERE id=?", (inbox_id,))


def count_inbox_today(user_id, kind):
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM inbox WHERE user_id=? AND kind=? AND created_at LIKE ?",
                         (user_id, kind, today() + "%")).fetchone()[0]


# ---------- Admin harakatlari jurnali ----------
def log_action(admin_id, admin_name, action, details=""):
    with conn() as c:
        c.execute("INSERT INTO admin_log (admin_id, admin_name, action, details, created_at) "
                  "VALUES (?,?,?,?,?)", (admin_id, admin_name, action, details or "", _ts()))
        # Jurnal cheksiz o'smasin: oxirgi 2000 ta qoladi
        c.execute("DELETE FROM admin_log WHERE id <= (SELECT MAX(id) FROM admin_log) - 2000")


def get_log(limit=30):
    with conn() as c:
        return _dicts(c.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall())


# ---------- Yuborilgan xabarlar (hammadan o'chirish uchun) ----------
def save_batch(kind, preview, admin_id, sent, failed, items):
    """items: [(chat_id, message_id), ...]. Yangi batch ID sini qaytaradi."""
    with conn() as c:
        cur = c.execute(
            "INSERT INTO batches (kind, preview, admin_id, sent, failed, created_at) VALUES (?,?,?,?,?,?)",
            (kind, preview, admin_id, sent, failed, _ts()))
        bid = cur.lastrowid
        c.executemany("INSERT INTO batch_items (batch_id, chat_id, message_id) VALUES (?,?,?)",
                      [(bid, int(a), int(b)) for a, b in items])
        return bid


def list_batches(hours=48):
    since = (now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with conn() as c:
        return _dicts(c.execute(
            "SELECT * FROM batches WHERE created_at>=? ORDER BY id DESC", (since,)).fetchall())


def get_batch(batch_id):
    with conn() as c:
        return _dict(c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone())


def batch_items(batch_id):
    with conn() as c:
        return [(r["chat_id"], r["message_id"]) for r in c.execute(
            "SELECT chat_id, message_id FROM batch_items WHERE batch_id=?", (batch_id,))]


def mark_batch_deleted(batch_id):
    with conn() as c:
        c.execute("UPDATE batches SET deleted=1 WHERE id=?", (batch_id,))
        c.execute("DELETE FROM batch_items WHERE batch_id=?", (batch_id,))


# ---------- Rejalashtirilgan xabarlar (bir martalik) ----------
def add_scheduled(run_at, text, voice_id=None, btn_text=None, btn_url=None):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO scheduled (run_at, text, voice_id, btn_text, btn_url) VALUES (?,?,?,?,?)",
            (run_at, text or "", voice_id, btn_text, btn_url))
        return cur.lastrowid


def list_scheduled():
    with conn() as c:
        return _dicts(c.execute("SELECT * FROM scheduled WHERE done=0 ORDER BY run_at").fetchall())


def due_scheduled(now_str):
    with conn() as c:
        return _dicts(c.execute(
            "SELECT * FROM scheduled WHERE done=0 AND run_at<=? ORDER BY run_at", (now_str,)).fetchall())


def mark_scheduled_done(sid):
    with conn() as c:
        c.execute("UPDATE scheduled SET done=1 WHERE id=?", (sid,))


def delete_scheduled(sid):
    with conn() as c:
        c.execute("DELETE FROM scheduled WHERE id=?", (sid,))


# ---------- Takrorlanuvchi xabarlar ----------
def add_recurring(freq, weekday, send_at, text, voice_id=None, btn_text=None, btn_url=None):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO recurring (freq, weekday, send_at, text, voice_id, btn_text, btn_url) "
            "VALUES (?,?,?,?,?,?,?)",
            (freq, weekday, send_at, text or "", voice_id, btn_text, btn_url))
        return cur.lastrowid


def list_recurring():
    with conn() as c:
        return _dicts(c.execute("SELECT * FROM recurring ORDER BY id").fetchall())


def get_recurring(rid):
    with conn() as c:
        return _dict(c.execute("SELECT * FROM recurring WHERE id=?", (rid,)).fetchone())


def delete_recurring(rid):
    with conn() as c:
        c.execute("DELETE FROM recurring WHERE id=?", (rid,))