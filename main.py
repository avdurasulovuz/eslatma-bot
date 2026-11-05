import asyncio
import io
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BotCommandScopeChat, BotCommandScopeDefault, BufferedInputFile,
                           CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from openpyxl import Workbook

load_dotenv(Path(__file__).parent / ".env")

import db  # .env yuklangandan keyin import qilinadi

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if ":" not in BOT_TOKEN:
    raise SystemExit("BOT_TOKEN topilmadi yoki noto'g'ri. .env faylni tekshiring.")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SEND_TIME_DEFAULT = os.getenv("SEND_TIME", "12:31")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tashkent")

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

admin = Router()
user_router = Router()


# Asosiy admin va bazaga kiritilgan yordamchi adminlar panelga kira oladi.
class AdminAccessFilter(BaseFilter):
    async def __call__(self, event):
        user = getattr(event, "from_user", None)
        return bool(user and (user.id == ADMIN_ID or db.is_helper_admin(user.id)))


admin.message.filter(AdminAccessFilter())
admin.callback_query.filter(AdminAccessFilter())


def is_admin_id(user_id: int) -> bool:
    return user_id == ADMIN_ID or db.is_helper_admin(user_id)


DEFAULT_TITLE = "Kun eslatmasi"
PAGE = 10
INBOX_PAGE = 8
MSG_LIMIT_PER_DAY = 10

BTN_PANEL = "🛠 Admin panel"
BTN_LAST = "📖 Oxirgi xabarim"
# Eski versiyadagi tugmalar (ular hali ba'zi chatlarda turgan bo'lishi mumkin)
LEGACY_BUTTONS = {"📊 Statistika", "📢 Hammaga xabar", "👥 Foydalanuvchilar", "⏳ Mening hisoblagichlarim",
                  BTN_LAST}

DEFAULT_WELCOME = (
    "👋 Assalomu alaykum, {name}!\n\n"
    "Kundalik eslatmalar botiga xush kelibsiz 🌿\n"
    "Har kuni soat {time} dan keyin sizga yangi eslatma yuboriladi.\n\n"
    "💬 Savol yoki fikringiz bo'lsa, shu yerga yozing — admin o'qib chiqadi."
)

CHANNEL_KEYS = {
    "required_channel": "📣 Majburiy obuna",
    "copy_channel": "📢 Kanalga nusxa",
}


# =============== Yordamchilar ===============
def send_time() -> str:
    return db.get_setting("send_time", SEND_TIME_DEFAULT)


def short(text: str, n: int = 30) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def who(u) -> str:
    """Foydalanuvchini qisqa ko'rinishda yozadi (ism | @username | id)."""
    if not u:
        return "—"
    uname = f"@{u['username']}" if u.get("username") else "—"
    return f"{u.get('full_name') or '—'} | {uname} | {u.get('user_id')}"


def row_to_content(r: dict) -> dict:
    return {
        "text": r.get("text") or "",
        "voice_id": r.get("voice_id"),
        "btn_text": r.get("btn_text"),
        "btn_url": r.get("btn_url"),
    }


def data_to_content(data: dict) -> dict:
    return row_to_content(data)


def rx_row(day: int, mine=None):
    return [
        InlineKeyboardButton(text="👍 ✓" if mine == "l" else "👍", callback_data=f"rx:{day}:l"),
        InlineKeyboardButton(text="❤️ ✓" if mine == "h" else "❤️", callback_data=f"rx:{day}:h"),
    ]


def content_kb(c: dict, rx_day=None, mine=None):
    rows = []
    if c.get("btn_text") and c.get("btn_url"):
        rows.append([InlineKeyboardButton(text=c["btn_text"], url=c["btn_url"])])
    if rx_day:
        rows.append(rx_row(rx_day, mine))
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def describe(c: dict) -> str:
    parts = []
    if c.get("voice_id"):
        parts.append("🎤 Ovozli xabar")
    if c.get("text"):
        parts.append(f"📝 Matn: {c['text']}")
    if c.get("btn_text"):
        parts.append(f"🔗 Tugma: {c['btn_text']} → {c['btn_url']}")
    return "\n".join(parts) or "(bo'sh)"


def daily_prefix(day: int) -> str:
    title = db.get_setting("daily_title") or DEFAULT_TITLE
    return title.replace("{day}", str(day)) + "\n\n"


async def send_content(chat_id, c: dict, prefix: str = "", rx_day=None):
    kb = content_kb(c, rx_day)
    text = c.get("text") or ""
    if c.get("voice_id"):
        caption = (prefix + text) if (prefix and text) else (text or prefix.strip() or None)
        return await bot.send_voice(chat_id, c["voice_id"], caption=caption, reply_markup=kb)
    return await bot.send_message(chat_id, prefix + text, reply_markup=kb)


GONE_MARKERS = ("chat not found", "deactivated", "user not found", "bot was blocked")


async def safe_send(user_id: int, c: dict, prefix: str = "", rx_day=None) -> bool:
    try:
        await send_content(user_id, c, prefix, rx_day)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await safe_send(user_id, c, prefix, rx_day)
    except TelegramForbiddenError:
        db.set_inactive(user_id)  # bot bloklangan
    except TelegramBadRequest as e:
        if any(m in str(e).lower() for m in GONE_MARKERS):
            db.set_inactive(user_id)  # chat yo'q yoki akkaunt o'chirilgan
        else:
            logging.warning("Yuborib bo'lmadi %s: %s", user_id, e)  # masalan, noto'g'ri file_id: foydalanuvchi aybsiz
    except Exception as e:
        logging.warning("Xatolik %s: %s", user_id, e)
    return False


async def send_to_all(c: dict, prefix: str = ""):
    sent = failed = 0
    for user in db.active_users():
        if await safe_send(user["user_id"], c, prefix):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)  # Telegram limiti: ~30 xabar/soniya
    return sent, failed


async def copy_to_channel(c: dict):
    """Ommaviy xabarning nusxasini sozlangan kanalga yuboradi (kunlik xabarlar kanalga ketmaydi)."""
    channel = db.get_setting("copy_channel")
    if not channel:
        return
    try:
        await send_content(channel, c)
    except Exception as e:
        logging.warning("Kanalga nusxa yuborib bo'lmadi: %s", e)


async def tell(user_id: int, text: str) -> bool:
    try:
        await bot.send_message(user_id, text)
        return True
    except Exception as e:
        logging.warning("Foydalanuvchiga xabar yetmadi %s: %s", user_id, e)
        return False


# ---------- Avto-tozalash ----------
_timers: dict[tuple[int, int], asyncio.Task] = {}  # (chat_id, message_id) -> Task
_panels: dict[int, int] = {}  # chat_id -> ochiq panel message_id


def autoclose_minutes() -> int:
    try:
        return max(0, int(db.get_setting("autoclose", "5")))
    except (TypeError, ValueError):
        return 5


def _cancel(key):
    t = _timers.pop(key, None)
    if t:
        t.cancel()


def schedule_delete(msg, seconds: int):
    """Xabarni `seconds` soniyadan keyin o'chiradi (qayta chaqirilsa, taymer yangilanadi)."""
    if not msg or seconds <= 0:
        return
    key = (msg.chat.id, msg.message_id)
    _cancel(key)

    async def job():
        me = asyncio.current_task()
        try:
            await asyncio.sleep(seconds)
            await bot.delete_message(*key)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if _timers.get(key) is me:
                _timers.pop(key, None)
            if _panels.get(key[0]) == key[1]:
                _panels.pop(key[0], None)

    _timers[key] = asyncio.create_task(job())


def schedule_autoclose(msg):
    schedule_delete(msg, autoclose_minutes() * 60)


async def note(msg, text: str, seconds: int = 20, **kw):
    """Qisqa xizmat xabari: bir necha soniyadan keyin o'zi o'chadi."""
    m = await msg.answer(text, **kw)
    schedule_delete(m, seconds)
    return m


async def show(target, text, kb=None):
    """Tugma bosilsa xabarni tahrirlaydi, oddiy xabar bo'lsa yangi xabar yuboradi.
    Admin xabarlari harakatsiz qolsa, avto-yopish vaqtidan keyin o'chadi."""
    if isinstance(target, CallbackQuery):
        msg = target.message
        try:
            await msg.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                msg = await msg.answer(text, reply_markup=kb)
        try:
            await target.answer()
        except TelegramBadRequest:
            pass  # tugma allaqachon javoblangan bo'lsa, e'tibor bermaymiz
    else:
        msg = await target.answer(text, reply_markup=kb)
    schedule_autoclose(msg)
    return msg


# =============== Holatlar ===============
class Compose(StatesGroup):
    content = State()
    button = State()
    when = State()
    target = State()


class EditSearch(StatesGroup):
    query = State()


class SetTime(StatesGroup):
    time = State()


class SetChannel(StatesGroup):
    name = State()


class SetWelcome(StatesGroup):
    text = State()


class SetTitle(StatesGroup):
    text = State()


class SetAutoclose(StatesGroup):
    minutes = State()


class HelperFlow(StatesGroup):
    add_id = State()
    remove_id = State()


class InboxFlow(StatesGroup):
    reply = State()
    edit = State()


class Suggest(StatesGroup):
    text = State()


# =============== Klaviaturalar ===============
def reply_kb(is_admin: bool = False):
    """Pastdagi doimiy tugma: faqat adminda '🛠 Admin panel'. Oddiy foydalanuvchida tugma yo'q
    (ReplyKeyboardRemove eski tugmalarni ham o'chirib yuboradi)."""
    if not is_admin:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_PANEL)]],
                               resize_keyboard=True, is_persistent=True)


def is_paused() -> bool:
    return db.get_setting("paused", "0") == "1"


def notify_on() -> bool:
    return db.get_setting("notify_new", "1") == "1"


def notify_inbox_on() -> bool:
    return db.get_setting("notify_inbox", "1") == "1"


def build_kb(rows, back=None):
    """rows — [[(matn, callback), ...], ...]. Pastda doim ⬅️ Orqaga (bo'lsa) va ❌ Yopish."""
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in r])
    nav = []
    if back:
        nav.append(InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back))
    nav.append(InlineKeyboardButton(text="❌ Yopish", callback_data="close"))
    kb.row(*nav)
    return kb.as_markup()


def back_kb(target="panel", text="⬅️ Orqaga"):
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=target)
    kb.button(text="❌ Yopish", callback_data="close")
    kb.adjust(2)
    return kb.as_markup()


def cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Bekor qilish", callback_data="panel")
    return kb.as_markup()


def onoff(flag: bool) -> str:
    return "✅ yoqiq" if flag else "🚫 o‘chiq"


def screen(name: str, uid: int = 0):
    """Admin panelning ekranlari: (matn, klaviatura). Panel 2 darajali: bosh menyu → bo'lim."""
    s = db.stats()
    new_inb = db.count_inbox(None, "new")
    if name == "msg":
        text = "📨 Xabarlar\n\nFoydalanuvchilarga yuboriladigan xabarlar."
        rows = [
            [(f"📋 Kunlik xabarlar ({s['messages']})", "list")],
            [("📢 Hammaga xabar", "bc"), ("✉️ Alohida xabar", "single")],
            [(f"📅 Rejalashtirilgan ({s['scheduled']})", "sched")],
        ]
        return text, build_kb(rows, "panel")
    if name == "inb":
        n_msg, n_sug = db.count_inbox("msg", "new"), db.count_inbox("suggest", "new")
        text = ("💬 Murojaatlar\n\nFoydalanuvchilarning sizga yozgan xabarlari va "
                "kunlik xabar sifatida yuborgan takliflari.")
        rows = [
            [(f"💬 Xabarlar ({n_msg})", "inbox:msg:0")],
            [(f"💡 Takliflar ({n_sug})", "inbox:sug:0")],
            [(f"🗂 Arxiv ({db.count_inbox(None, 'done')})", "inbox:arch:0")],
        ]
        return text, build_kb(rows, "panel")
    if name == "usr":
        text = f"👥 Foydalanuvchilar\n\nFaol: {s['active']} · jami: {s['total']}"
        rows = [[("👥 Ro'yxat / qidirish", "users:0")], [("📥 Excel yuklab olish", "export")]]
        return text, build_kb(rows, "panel")
    if name == "set":
        text = "⚙️ Sozlamalar"
        rows = [
            [("⏰ Yuborish", "menu:send"), ("📝 Matnlar", "menu:text")],
            [("📣 Kanal", "menu:chan"), ("👁 Ko'rinish", "menu:look")],
        ]
        if uid == ADMIN_ID:
            rows.append([("🧑‍💼 Adminlar", "helpers")])
        return text, build_kb(rows, "panel")
    if name == "send":
        text = (f"⏰ Yuborish\n\nVaqt: {send_time()} ({TIMEZONE})\n"
                f"Kunlik yuborish: {'⏸ to‘xtatilgan' if is_paused() else '✅ faol'}")
        rows = [
            [("⏰ Vaqtni o'zgartirish", "time")],
            [("▶️ Yuborishni yoqish" if is_paused() else "⏸ Yuborishni to'xtatish", "pause")],
        ]
        return text, build_kb(rows, "menu:set")
    if name == "text":
        title = db.get_setting("daily_title") or DEFAULT_TITLE
        text = f"📝 Matnlar\n\nKunlik xabar sarlavhasi: {title}\nSalomlashuv: {short(welcome_text(), 60)}"
        rows = [[("👋 Salomlashuv matni", "welcome")], [("🏷 Kun sarlavhasi", "dtitle")]]
        return text, build_kb(rows, "menu:set")
    if name == "chan":
        text = ("📣 Kanal\n\n"
                f"Majburiy obuna: {db.get_setting('required_channel') or 'o‘chiq'}\n"
                f"Kanalga nusxa: {db.get_setting('copy_channel') or 'o‘chiq'}")
        rows = [[("📣 Majburiy obuna", "ch:required_channel")], [("📢 Kanalga nusxa", "ch:copy_channel")]]
        return text, build_kb(rows, "menu:set")
    if name == "look":
        ac = autoclose_minutes()
        text = (f"👁 Ko'rinish\n\nAvto-yopish: {f'{ac} daqiqa' if ac else 'o‘chiq'}\n"
                f"Yangi foydalanuvchi xabari: {onoff(notify_on())}\n"
                f"Yangi murojaat xabari: {onoff(notify_inbox_on())}")
        rows = [
            [("🧹 Avto-yopish", "autoclose")],
            [(f"{'🔔' if notify_on() else '🔕'} Yangi foydalanuvchi", "notify")],
            [(f"{'🔔' if notify_inbox_on() else '🔕'} Yangi murojaat", "notifyi")],
        ]
        return text, build_kb(rows, "menu:set")
    # bosh panel
    text = (
        "🛠 Admin panel\n\n"
        f"👥 {s['active']} faol / {s['total']} jami · 📝 {s['messages']} ta kunlik xabar\n"
        f"💬 Yangi murojaat: {new_inb}"
    )
    rows = [
        [("📨 Xabarlar", "menu:msg"), (f"💬 Murojaatlar ({new_inb})", "menu:inb")],
        [("👥 Foydalanuvchilar", "menu:usr"), ("📊 Statistika", "stats")],
        [("⚙️ Sozlamalar", "menu:set")],
    ]
    return text, build_kb(rows)


SCREENS = {"panel", "msg", "inb", "usr", "set", "send", "text", "chan", "look"}


def bar(n: int, mx: int) -> str:
    if not mx or not n:
        return ""
    return "█" * max(1, round(n * 10 / mx))


def stats_text() -> str:
    s = db.stats()
    g = db.growth(7)
    mx = max((n for _, n in g), default=0)
    lines = [
        "📊 Statistika\n",
        f"👥 Jami: {s['total']}   ✅ Faol: {s['active']}   🚫 Bloklagan: {s['blocked']}",
        f"🆕 Bugun qo'shilgan: {s['new_today']}   📨 Bugun xabar olgan: {s['sent_today']}",
        f"📝 Kunlik xabarlar: {s['messages']}   📅 Rejalashtirilgan: {s['scheduled']}",
        "",
        "📈 Oxirgi 7 kunda qo'shilganlar:",
    ]
    for d, n in g:
        lines.append(f"{d[5:]}  {bar(n, mx) or '·'} {n}")
    top = db.top_streaks(5)
    if top:
        lines.append("\n🔥 Eng ko'p kun olganlar:")
        for i, u in enumerate(top, 1):
            name = u["full_name"] or (f"@{u['username']}" if u["username"] else "—")
            lines.append(f"{i}. {short(name, 24)} — {u['last_sent_day']} kun")
    rx = db.reaction_stats(3)
    if rx:
        lines.append("\n💛 Eng ko'p reaksiya olgan kunlar:")
        for r in rx:
            lines.append(f"{r['day']}-xabar: 👍 {r['likes']}  ❤️ {r['loves']}")
    return "\n".join(lines)


# =============== Admin: panel (ochiladi / yopiladi) ===============
async def send_panel(msg: Message, name=None):
    """Yangi panel yuboradi; eski ochiq panel bo'lsa, uni o'chiradi."""
    chat_id = msg.chat.id
    old = _panels.pop(chat_id, None)
    if old:
        _cancel((chat_id, old))
        try:
            await bot.delete_message(chat_id, int(old))
        except TelegramBadRequest:
            pass
    name = name if name in SCREENS else "panel"
    uid = msg.from_user.id if msg.from_user and not msg.from_user.is_bot else ADMIN_ID
    text, kb = screen(name, uid)
    m = await msg.answer(text, reply_markup=kb)
    _panels[chat_id] = m.message_id
    schedule_autoclose(m)
    return m


async def show_screen(call: CallbackQuery, name: str):
    text, kb = screen(name if name in SCREENS else "panel", call.from_user.id)
    await show(call, text, kb)


@admin.message(Command("admin"))
async def open_panel(message: Message, state: FSMContext):
    await state.clear()
    await note(message, "Pastdagi 🛠 Admin panel tugmasi panelni ochadi / yopadi 👇", 8,
               reply_markup=reply_kb(True))
    await send_panel(message)


@admin.message(F.text == BTN_PANEL)
async def reply_panel(message: Message, state: FSMContext):
    """Tugma bosilsa: panel ochiq bo'lsa yopiladi, yopiq bo'lsa ochiladi."""
    await state.clear()
    chat_id = message.chat.id
    try:
        await message.delete()  # admin yozgan tugma matni chatda qolmasin
    except TelegramBadRequest:
        pass
    old = _panels.pop(chat_id, None)
    if old:
        _cancel((chat_id, old))
        try:
            await bot.delete_message(chat_id, int(old))
            return  # panel yopildi
        except TelegramBadRequest:
            pass  # allaqachon o'chib ketgan bo'lsa, yangisini ochamiz
    await send_panel(message)


@admin.message(F.text.in_(LEGACY_BUTTONS))
async def legacy_buttons(message: Message, state: FSMContext):
    await state.clear()
    await note(message, "Tugmalar yangilandi 👇", 5, reply_markup=reply_kb(True))
    await send_panel(message)


@admin.callback_query(F.data.startswith("sec:"))
async def legacy_section(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_screen(call, "panel")


@admin.callback_query(F.data.startswith("menu:"))
async def open_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_screen(call, call.data.split(":", 1)[1])


@admin.callback_query(F.data == "close")
async def close_panel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    msg = call.message
    _cancel((msg.chat.id, msg.message_id))
    if _panels.get(msg.chat.id) == msg.message_id:
        _panels.pop(msg.chat.id, None)
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    await call.answer()


@admin.callback_query(F.data == "panel")
async def back_to_panel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_screen(call, "panel")


@admin.callback_query(F.data == "stats")
async def show_stats(call: CallbackQuery):
    await show(call, stats_text(), back_kb())


# ---------- Sozlamalar ----------
@admin.callback_query(F.data == "pause")
async def toggle_pause(call: CallbackQuery):
    db.set_setting("paused", "0" if is_paused() else "1")
    await show_screen(call, "send")


@admin.callback_query(F.data == "notify")
async def toggle_notify(call: CallbackQuery):
    db.set_setting("notify_new", "0" if notify_on() else "1")
    await show_screen(call, "look")


@admin.callback_query(F.data == "notifyi")
async def toggle_notify_inbox(call: CallbackQuery):
    db.set_setting("notify_inbox", "0" if notify_inbox_on() else "1")
    await show_screen(call, "look")


@admin.callback_query(F.data == "welcome")
async def welcome_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SetWelcome.text)
    await show(call,
               "👋 Yangi salomlashuv matnini yuboring.\n\n"
               "{name} — foydalanuvchi ismi, {time} — yuborish vaqti.\n"
               "Asl holiga qaytarish uchun: - (tire) yuboring.\n\n"
               f"Hozirgi matn:\n\n{welcome_text()}",
               cancel_kb())


@admin.message(SetWelcome.text, F.text)
async def welcome_save(message: Message, state: FSMContext):
    val = message.text.strip()
    db.set_setting("welcome_text", None if val == "-" else val)
    await state.clear()
    await note(message, "✅ Salomlashuv matni saqlandi.")
    await send_panel(message, "text")


@admin.callback_query(F.data == "dtitle")
async def title_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SetTitle.text)
    cur = db.get_setting("daily_title") or DEFAULT_TITLE
    await show(call,
               "🏷 Kunlik xabar tepasida chiqadigan sarlavhani yozing.\n\n"
               "{day} — foydalanuvchining kun raqami. Masalan: {day}-kun\n"
               "Asl holiga qaytarish uchun: - (tire) yuboring.\n\n"
               f"Hozirgi sarlavha: {cur}",
               cancel_kb())


@admin.message(SetTitle.text, F.text)
async def title_save(message: Message, state: FSMContext):
    val = message.text.strip()
    if len(val) > 100:
        await message.answer("Sarlavha 100 belgidan oshmasin.", reply_markup=cancel_kb())
        return
    db.set_setting("daily_title", None if val == "-" else val)
    await state.clear()
    await note(message, "✅ Sarlavha saqlandi.")
    await send_panel(message, "text")


@admin.callback_query(F.data == "autoclose")
async def autoclose_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SetAutoclose.minutes)
    await show(call,
               f"🧹 Avto-yopish: hozir {autoclose_minutes()} daqiqa.\n\n"
               "Panel va xizmat xabarlari shuncha vaqt harakatsiz tursa, o'zi o'chib ketadi.\n"
               "Yangi qiymatni daqiqada yozing (0 = o'chirish, eng ko'pi 1440):",
               cancel_kb())


@admin.message(SetAutoclose.minutes, F.text)
async def autoclose_save(message: Message, state: FSMContext):
    raw = message.text.strip()
    if not raw.isdigit() or not 0 <= int(raw) <= 1440:
        await message.answer("0 dan 1440 gacha butun son yozing.", reply_markup=cancel_kb())
        return
    db.set_setting("autoclose", raw)
    await state.clear()
    await note(message, f"✅ Avto-yopish: {raw} daqiqa." if int(raw) else "✅ Avto-yopish o'chirildi.")
    await send_panel(message, "look")


# =============== Admin: xabar yaratish (umumiy oqim) ===============
async def start_compose(target, state: FSMContext, mode: str, title: str, day: int = None):
    await state.clear()
    await state.set_state(Compose.content)
    await state.update_data(mode=mode, day=day, text="", voice_id=None, btn_text=None, btn_url=None)
    await show(
        target,
        f"{title}\n\nMatn yoki 🎤 ovozli xabar yuboring.",
        cancel_kb(),
    )


@admin.message(Compose.content, F.text | F.voice)
async def compose_content(message: Message, state: FSMContext):
    text = message.text or message.caption or ""
    voice_id = message.voice.file_id if message.voice else None
    await state.update_data(text=text, voice_id=voice_id)
    await state.set_state(Compose.button)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Tugma qo'shish", callback_data="btn_yes")
    kb.button(text="⏭ Tugmasiz davom etish", callback_data="btn_no")
    kb.button(text="❌ Bekor qilish", callback_data="panel")
    kb.adjust(1)
    await message.answer("Xabarga havola tugmasi qo'shamizmi?", reply_markup=kb.as_markup())


@admin.message(Compose.content)
async def compose_wrong_type(message: Message):
    await message.answer("Faqat matn yoki ovozli xabar (🎤) yuboring.", reply_markup=cancel_kb())


@admin.callback_query(Compose.button, F.data == "btn_yes")
async def button_ask(call: CallbackQuery):
    await show(
        call,
        "Tugma matni va havolani shunday yozing:\n\nKanalga o'tish | https://t.me/kanal_nomi",
        cancel_kb(),
    )


@admin.callback_query(Compose.button, F.data == "btn_no")
async def button_skip(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await finalize(call.message, state)


@admin.message(Compose.button, F.text)
async def button_parse(message: Message, state: FSMContext):
    if "|" not in message.text:
        await message.answer("Format: Tugma matni | https://havola", reply_markup=cancel_kb())
        return
    label, _, url = message.text.partition("|")
    label, url = label.strip(), url.strip()
    if not label or not re.match(r"^https?://\S+$", url):
        await message.answer("Havola http:// yoki https:// bilan boshlanishi kerak.", reply_markup=cancel_kb())
        return
    await state.update_data(btn_text=label, btn_url=url)
    await finalize(message, state)


async def finalize(msg: Message, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode")
    c = data_to_content(data)

    if mode == "daily_add":
        n = db.add_daily_message(c["text"], c["voice_id"], c["btn_text"], c["btn_url"])
        await state.clear()
        await note(msg, f"✅ {n}-kun xabari saqlandi.")
        await send_panel(msg, "msg")

    elif mode == "daily_edit":
        day = data["day"]
        db.update_daily_message(day, c["text"], c["voice_id"], c["btn_text"], c["btn_url"])
        await state.clear()
        await note(msg, f"✅ {day}-kun xabari yangilandi.")
        await send_panel(msg, "msg")

    elif mode == "broadcast":
        await state.set_state(None)  # ma'lumot saqlanib qoladi
        await msg.answer("👇 Yuboriladigan xabar ko'rinishi:")
        await send_content(msg.chat.id, c)
        kb = InlineKeyboardBuilder()
        kb.button(text=f"✅ Yuborish ({len(db.active_users())} ta)", callback_data="bcok")
        kb.button(text="❌ Bekor qilish", callback_data="panel")
        kb.adjust(1)
        await msg.answer("Hammaga yuborilsinmi?", reply_markup=kb.as_markup())

    elif mode == "schedule":
        await state.set_state(Compose.when)
        await msg.answer(
            f"📅 Qachon yuborilsin? Sana va vaqtni yozing ({TIMEZONE} vaqti bilan):\n\n"
            "2026-11-05 09:30",
            reply_markup=cancel_kb(),
        )

    elif mode == "single":
        await state.set_state(Compose.target)
        await msg.answer("Kimga yuborilsin? Foydalanuvchi ID raqami yoki @username ni yozing:",
                         reply_markup=cancel_kb())

    else:
        await state.clear()
        text, kb = screen("panel", ADMIN_ID)
        await msg.answer("Xatolik: amal topilmadi.\n\n" + text, reply_markup=kb)


# ---------- Hammaga yuborish ----------
@admin.callback_query(F.data == "bc")
async def bc_start(call: CallbackQuery, state: FSMContext):
    await start_compose(call, state, "broadcast", "📢 Hammaga yuboriladigan xabar")


@admin.callback_query(F.data == "bcok")
async def bc_send(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    c = data_to_content(data)
    await state.clear()
    if not (c["text"] or c["voice_id"]):
        await call.answer("Xabar topilmadi", show_alert=True)
        return
    await call.answer()
    await call.message.edit_text("⏳ Yuborilmoqda...")
    sent, failed = await send_to_all(c)
    await copy_to_channel(c)
    await call.message.edit_text(
        f"✅ Yuborildi: {sent}\n🚫 Yetmadi: {failed}", reply_markup=back_kb("panel", "⬅️ Panel"))


# ---------- Alohida foydalanuvchiga ----------
@admin.callback_query(F.data == "single")
async def single_start(call: CallbackQuery, state: FSMContext):
    await start_compose(call, state, "single", "✉️ Bitta foydalanuvchiga xabar")


@admin.message(Compose.target, F.text)
async def single_send(message: Message, state: FSMContext):
    u = db.find_user(message.text)
    if not u:
        await message.answer("Foydalanuvchi topilmadi. ID yoki @username ni tekshiring.",
                             reply_markup=cancel_kb())
        return
    data = await state.get_data()
    ok = await safe_send(u["user_id"], data_to_content(data))
    await state.clear()
    await note(message,
               "✅ Yuborildi." if ok else "🚫 Yuborib bo'lmadi (foydalanuvchi botni bloklagan bo'lishi mumkin).")
    await send_panel(message, "msg")


# ---------- Rejalashtirish ----------
def parse_when(s: str):
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None


@admin.callback_query(F.data == "sched")
async def sched_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = db.list_scheduled()
    kb = InlineKeyboardBuilder()
    for r in rows:
        icon = "🎤 " if r["voice_id"] else ""
        kb.row(InlineKeyboardButton(text=f"🗑 {r['run_at']} · {icon}{short(r['text'], 20)}",
                                    callback_data=f"scdel:{r['id']}"))
    kb.row(InlineKeyboardButton(text="➕ Yangi rejalashtirish", callback_data="scnew"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:msg"),
           InlineKeyboardButton(text="❌ Yopish", callback_data="close"))
    text = ("📅 Rejalashtirilgan xabarlar\n(o'chirish uchun bosing)" if rows
            else "Rejalashtirilgan xabarlar yo'q.")
    await show(call, text, kb.as_markup())


@admin.callback_query(F.data == "scnew")
async def sched_new(call: CallbackQuery, state: FSMContext):
    await start_compose(call, state, "schedule", "📅 Rejalashtiriladigan xabar")


@admin.callback_query(F.data.startswith("scdel:"))
async def sched_delete(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split(":")[1])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data=f"scdelok:{sid}")
    kb.button(text="❌ Yo'q", callback_data="sched")
    kb.adjust(2)
    await show(call, "Bu rejalashtirilgan xabarni o'chirasizmi?", kb.as_markup())


@admin.callback_query(F.data.startswith("scdelok:"))
async def sched_delete_do(call: CallbackQuery, state: FSMContext):
    db.delete_scheduled(int(call.data.split(":")[1]))
    await sched_list(call, state)


@admin.message(Compose.when, F.text)
async def sched_save(message: Message, state: FSMContext):
    dt = parse_when(message.text)
    if not dt:
        await message.answer("Format noto'g'ri. Masalan: 2026-11-05 09:30", reply_markup=cancel_kb())
        return
    if dt <= db.now():
        await message.answer("Bu vaqt o'tib ketgan. Kelajakdagi vaqtni yozing.", reply_markup=cancel_kb())
        return
    c = data_to_content(await state.get_data())
    run_at = dt.strftime("%Y-%m-%d %H:%M")
    db.add_scheduled(run_at, c["text"], c["voice_id"], c["btn_text"], c["btn_url"])
    await state.clear()
    await note(message, f"✅ Xabar {run_at} da hammaga yuboriladi.")
    await send_panel(message, "msg")


# =============== Admin: kunlik xabarlar ===============
@admin.callback_query(F.data == "add")
async def add_start(call: CallbackQuery, state: FSMContext):
    n = db.total_daily_messages() + 1
    await start_compose(call, state, "daily_add", f"➕ {n}-kun uchun xabar")


@admin.callback_query(F.data == "list")
async def list_messages(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = db.list_daily_messages()
    kb = InlineKeyboardBuilder()
    for r in rows:
        icon = "🎤 " if r["voice_id"] else ""
        kb.row(InlineKeyboardButton(text=f"{r['day']}-kun: {icon}{short(r['text'])}",
                                    callback_data=f"msg:{r['day']}"))
    kb.row(InlineKeyboardButton(text="➕ Yangi xabar", callback_data="add"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:msg"),
           InlineKeyboardButton(text="❌ Yopish", callback_data="close"))
    text = "📋 Kunlik xabarlar" if rows else "Hali kunlik xabarlar yo'q. ➕ bilan birinchisini qo'shing."
    await show(call, text, kb.as_markup())


@admin.callback_query(F.data.startswith("msg:"))
async def view_message(call: CallbackQuery):
    day = int(call.data.split(":")[1])
    row = db.get_daily_message(day)
    if row is None:
        await call.answer("Topilmadi", show_alert=True)
        return
    c = row_to_content(row)
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Tahrirlash", callback_data=f"edit:{day}")
    kb.button(text="🗑 O'chirish", callback_data=f"del:{day}")
    kb.button(text="👁 Menga yuborib ko'rish", callback_data=f"prev:{day}")
    kb.button(text="⬅️ Orqaga", callback_data="list")
    kb.button(text="❌ Yopish", callback_data="close")
    kb.adjust(2, 1, 2)
    await show(call, f"{day}-kun xabari:\n\n{describe(c)}", kb.as_markup())
    if c["voice_id"]:
        m = await send_content(call.message.chat.id, c)
        schedule_delete(m, 120)


@admin.callback_query(F.data.startswith("prev:"))
async def preview_message(call: CallbackQuery):
    day = int(call.data.split(":")[1])
    row = db.get_daily_message(day)
    if row is None:
        await call.answer("Topilmadi", show_alert=True)
        return
    m = await send_content(call.message.chat.id, row_to_content(row), daily_prefix(day), rx_day=day)
    schedule_delete(m, 120)  # sinov xabari 2 daqiqadan keyin o'chadi
    await call.answer("Yuborildi (2 daqiqadan keyin o'chadi)")


@admin.callback_query(F.data.startswith("edit:"))
async def edit_start(call: CallbackQuery, state: FSMContext):
    day = int(call.data.split(":")[1])
    await start_compose(call, state, "daily_edit", f"✏️ {day}-kun uchun yangi xabar", day=day)


@admin.callback_query(F.data.startswith("del:"))
async def delete_ask(call: CallbackQuery):
    day = int(call.data.split(":")[1])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data=f"delok:{day}")
    kb.button(text="❌ Yo'q", callback_data=f"msg:{day}")
    kb.adjust(2)
    await show(call, f"{day}-kun xabarini o'chirasizmi?\n(Keyingi kunlar raqami bittaga kamayadi.)",
               kb.as_markup())


@admin.callback_query(F.data.startswith("delok:"))
async def delete_do(call: CallbackQuery, state: FSMContext):
    db.delete_daily_message(int(call.data.split(":")[1]))
    await call.answer("O'chirildi")
    await list_messages(call, state)


# =============== Admin: murojaatlar va takliflar ===============
INBOX_MODES = {
    "msg": ("💬 Xabarlar", "msg", "new"),
    "sug": ("💡 Takliflar", "suggest", "new"),
    "arch": ("🗂 Arxiv", None, "done"),
}
RESULT_LABEL = {"replied": "✍️ javob berilgan", "accepted": "✅ qabul qilingan",
                "rejected": "❌ rad etilgan", "archived": "🗂 arxivga olingan"}


async def inbox_page(call: CallbackQuery, mode: str, page: int):
    title, kind, status = INBOX_MODES[mode]
    total = db.count_inbox(kind, status)
    pages = max(1, (total + INBOX_PAGE - 1) // INBOX_PAGE)
    page = max(0, min(page, pages - 1))
    rows = db.list_inbox(kind, status, INBOX_PAGE, page * INBOX_PAGE)
    kb = InlineKeyboardBuilder()
    for r in rows:
        name = r["full_name"] or (f"@{r['username']}" if r["username"] else str(r["user_id"]))
        icon = "💡" if r["kind"] == "suggest" else "💬"
        kb.row(InlineKeyboardButton(text=f"{icon} {short(name, 14)}: {short(r['text'], 22)}",
                                    callback_data=f"iv:{r['id']}:{mode}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"inbox:{mode}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"inbox:{mode}:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:inb"),
           InlineKeyboardButton(text="❌ Yopish", callback_data="close"))
    text = f"{title} ({total}) — {page + 1}/{pages}" if rows else f"{title}\n\nHozircha bo'sh."
    await show(call, text, kb.as_markup())


@admin.callback_query(F.data.startswith("inbox:"))
async def inbox_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    _, mode, page = call.data.split(":")
    if mode not in INBOX_MODES:
        await call.answer()
        return
    await inbox_page(call, mode, int(page))


def inbox_text(r) -> str:
    head = "💡 Taklif" if r["kind"] == "suggest" else "💬 Murojaat"
    u = {"full_name": r["full_name"], "username": r["username"], "user_id": r["user_id"]}
    text = f"{head} #{r['id']}\n👤 {who(u)}\n🕒 {r['created_at']}\n\n{r['text']}"
    if r["status"] == "done":
        text += f"\n\nHolat: {RESULT_LABEL.get(r['result'], r['result'] or '—')}"
    return text


@admin.callback_query(F.data.startswith("iv:"))
async def inbox_view(call: CallbackQuery):
    _, iid, mode = call.data.split(":")
    r = db.get_inbox(int(iid))
    if r is None:
        await call.answer("Topilmadi", show_alert=True)
        return
    rows = []
    if r["status"] == "new":
        if r["kind"] == "suggest":
            rows.append([("✅ Qabul qilish", f"iacc:{iid}"), ("✏️ Tahrirlab qabul", f"iedit:{iid}")])
            rows.append([("❌ Rad etish", f"irej:{iid}")])
        else:
            rows.append([("✍️ Javob yozish", f"irep:{iid}")])
            rows.append([("🗂 Arxivga", f"iarc:{iid}")])
    rows.append([("🗑 O'chirish", f"idel:{iid}")])
    await show(call, inbox_text(r), build_kb(rows, f"inbox:{mode}:0"))


async def _open_item(call: CallbackQuery, iid: int, want_kind=None):
    r = db.get_inbox(iid)
    if r is None or r["status"] != "new" or (want_kind and r["kind"] != want_kind):
        await call.answer("Bu element allaqachon ko'rib chiqilgan yoki topilmadi.", show_alert=True)
        return None
    return r


@admin.callback_query(F.data.startswith("irep:"))
async def inbox_reply_start(call: CallbackQuery, state: FSMContext):
    r = await _open_item(call, int(call.data.split(":")[1]), "msg")
    if not r:
        return
    await state.set_state(InboxFlow.reply)
    await state.update_data(iid=r["id"])
    await show(call, f"✍️ Javobingizni yozing. U foydalanuvchiga yuboriladi.\n\n"
                     f"Murojaat: {short(r['text'], 200)}", cancel_kb())


@admin.message(InboxFlow.reply, F.text)
async def inbox_reply_send(message: Message, state: FSMContext):
    iid = (await state.get_data()).get("iid")
    r = db.get_inbox(iid) if iid else None
    await state.clear()
    if not r:
        await note(message, "Murojaat topilmadi.")
        await send_panel(message, "inb")
        return
    ok = await tell(r["user_id"], f"💌 Admin javobi:\n\n{message.text}")
    if ok:
        db.set_inbox_done(r["id"], "replied")
    await note(message, "✅ Javob yuborildi va arxivga olindi." if ok
               else "🚫 Javob yetmadi (foydalanuvchi botni bloklagan bo'lishi mumkin). Murojaat joyida qoldi.")
    await send_panel(message, "inb")


@admin.callback_query(F.data.startswith("iarc:"))
async def inbox_archive(call: CallbackQuery):
    r = await _open_item(call, int(call.data.split(":")[1]))
    if not r:
        return
    db.set_inbox_done(r["id"], "archived")
    await call.answer("Arxivga olindi")
    await show_screen(call, "inb")


@admin.callback_query(F.data.startswith("iacc:"))
async def inbox_accept(call: CallbackQuery):
    r = await _open_item(call, int(call.data.split(":")[1]), "suggest")
    if not r:
        return
    n = db.add_daily_message(r["text"])
    db.set_inbox_done(r["id"], "accepted")
    await tell(r["user_id"], "✅ Taklifingiz qabul qilindi va kunlik xabarlarga qo'shildi. Rahmat! 💛")
    await call.answer(f"✅ {n}-kun xabari bo'ldi")
    await show_screen(call, "inb")


@admin.callback_query(F.data.startswith("iedit:"))
async def inbox_edit_start(call: CallbackQuery, state: FSMContext):
    r = await _open_item(call, int(call.data.split(":")[1]), "suggest")
    if not r:
        return
    await state.set_state(InboxFlow.edit)
    await state.update_data(iid=r["id"])
    await show(call, "✏️ Taklifni tahrirlab, yangi matnni yuboring. U kunlik xabar sifatida qo'shiladi.\n\n"
                     f"Asl matn:\n{r['text']}", cancel_kb())


@admin.message(InboxFlow.edit, F.text)
async def inbox_edit_save(message: Message, state: FSMContext):
    iid = (await state.get_data()).get("iid")
    r = db.get_inbox(iid) if iid else None
    await state.clear()
    if not r or r["status"] != "new":
        await note(message, "Taklif topilmadi.")
        await send_panel(message, "inb")
        return
    n = db.add_daily_message(message.text)
    db.set_inbox_done(r["id"], "accepted")
    await tell(r["user_id"], "✅ Taklifingiz (kichik tahrir bilan) kunlik xabarlarga qo'shildi. Rahmat! 💛")
    await note(message, f"✅ {n}-kun xabari sifatida saqlandi.")
    await send_panel(message, "inb")


@admin.callback_query(F.data.startswith("irej:"))
async def inbox_reject(call: CallbackQuery):
    r = await _open_item(call, int(call.data.split(":")[1]), "suggest")
    if not r:
        return
    db.set_inbox_done(r["id"], "rejected")
    await tell(r["user_id"], "Taklifingiz uchun rahmat! Bu safar uni qo'sha olmadik, lekin yangi g'oyalaringizni kutamiz 🌿")
    await call.answer("Rad etildi")
    await show_screen(call, "inb")


@admin.callback_query(F.data.startswith("idel:"))
async def inbox_delete_ask(call: CallbackQuery):
    iid = call.data.split(":")[1]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, o'chirish", callback_data=f"idelok:{iid}")
    kb.button(text="❌ Yo'q", callback_data=f"iv:{iid}:msg")
    kb.adjust(2)
    await show(call, "Bu yozuvni butunlay o'chirasizmi?", kb.as_markup())


@admin.callback_query(F.data.startswith("idelok:"))
async def inbox_delete_do(call: CallbackQuery):
    db.delete_inbox(int(call.data.split(":")[1]))
    await call.answer("O'chirildi")
    await show_screen(call, "inb")


# =============== Admin: foydalanuvchilar ===============
async def users_page(target, page: int):
    total = db.count_users()
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = db.list_users(PAGE, page * PAGE)
    lines = [f"👥 Foydalanuvchilar ({total}) — {page + 1}/{pages}\n"]
    for i, u in enumerate(rows, start=page * PAGE + 1):
        lines.append(f"{i}. {who(u)} {'✅' if u['active'] else '🚫'}")
    if not rows:
        lines.append("Hali foydalanuvchilar yo'q.")
    kb = InlineKeyboardBuilder()
    for u in rows:
        label = u["full_name"] or u["username"] or str(u["user_id"])
        kb.row(InlineKeyboardButton(text=f"{'✅' if u['active'] else '🚫'} {short(label, 28)}",
                                    callback_data=f"uview:{u['user_id']}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"users:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"users:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🔎 Qidirish", callback_data="usearch"))
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:usr"),
           InlineKeyboardButton(text="❌ Yopish", callback_data="close"))
    await show(target, "\n".join(lines), kb.as_markup())


@admin.callback_query(F.data.startswith("users:"))
async def users_cb(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await users_page(call, int(call.data.split(":")[1]))


@admin.callback_query(F.data == "usearch")
async def search_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(EditSearch.query)
    await show(call, "🔎 Ism, @username yoki ID yozing:", cancel_kb())


@admin.message(EditSearch.query, F.text)
async def search_do(message: Message, state: FSMContext):
    rows = db.search_users(message.text)
    await state.clear()
    if not rows:
        text = "Hech narsa topilmadi."
    else:
        lines = ["🔎 Natijalar:\n"]
        for u in rows:
            lines.append(f"{who(u)} {'✅' if u['active'] else '🚫'}\n   Qo'shilgan: {u['start_date']}, "
                         f"oxirgi kun: {u['last_sent_day']}")
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    for u in rows:
        label = u["full_name"] or u["username"] or str(u["user_id"])
        kb.button(text=f"👤 {short(label, 28)}", callback_data=f"uview:{u['user_id']}")
    kb.button(text="⬅️ Ro'yxat", callback_data="users:0")
    kb.button(text="❌ Yopish", callback_data="close")
    kb.adjust(1)
    await show(message, text, kb.as_markup())


def user_card(u) -> str:
    uname = f"@{u['username']}" if u["username"] else "—"
    return (f"👤 {u['full_name'] or '—'}\n"
            f"🔗 {uname}\n🆔 {u['user_id']}\n"
            f"📅 Qo'shilgan: {u['start_date']}\n"
            f"📨 Oxirgi yuborilgan kun: {u['last_sent_day']}\n"
            f"Holat: {'✅ faol' if u['active'] else '🚫 nofaol'}")


async def show_user(call: CallbackQuery, uid: int):
    u = db.get_user(uid)
    if not u:
        await call.answer("Topilmadi", show_alert=True)
        return
    rows = [
        [("🔄 Kunlarni boshidan boshlash", f"ureset:{uid}")],
        [("🚫 Faolsizlantirish" if u["active"] else "✅ Faollashtirish", f"utoggle:{uid}")],
    ]
    await show(call, user_card(u), build_kb(rows, "users:0"))


@admin.callback_query(F.data.startswith("uview:"))
async def user_view(call: CallbackQuery):
    await show_user(call, int(call.data.split(":")[1]))


@admin.callback_query(F.data.startswith("ureset:"))
async def user_reset_ask(call: CallbackQuery):
    uid = int(call.data.split(":")[1])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ha, boshidan", callback_data=f"uresetok:{uid}")
    kb.button(text="❌ Yo'q", callback_data=f"uview:{uid}")
    kb.adjust(2)
    await show(call, "Bu foydalanuvchi kunlari 1-kundan qayta boshlansinmi?", kb.as_markup())


@admin.callback_query(F.data.startswith("uresetok:"))
async def user_reset(call: CallbackQuery):
    uid = int(call.data.split(":")[1])
    db.reset_user_progress(uid)
    await call.answer("1-kundan boshlanadi")
    await show_user(call, uid)


@admin.callback_query(F.data.startswith("utoggle:"))
async def user_toggle(call: CallbackQuery):
    uid = int(call.data.split(":")[1])
    u = db.get_user(uid)
    if u:
        db.set_active(uid, not u["active"])
    await call.answer("Yangilandi")
    await show_user(call, uid)


# ---------- Excel ----------
def build_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Foydalanuvchilar"
    ws.append(["ID", "Ism", "Username", "Qo'shilgan sana", "Faol", "Oxirgi yuborilgan kun"])
    for u in db.all_users():
        ws.append([u["user_id"], u["full_name"] or "", u["username"] or "",
                   u["start_date"], "Ha" if u["active"] else "Yo'q", u["last_sent_day"]])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


@admin.callback_query(F.data == "export")
async def export_users(call: CallbackQuery):
    await call.answer("Tayyorlanmoqda...")
    name = f"users_{db.today()}.xlsx"
    await bot.send_document(call.message.chat.id,
                            BufferedInputFile(build_xlsx(), filename=name),
                            caption=f"👥 Foydalanuvchilar: {db.count_users()} ta")


# =============== Yordamchi adminlar ===============
async def set_commands_for(user_id: int, is_admin: bool):
    cmds = [BotCommand(command="start", description="Botni ishga tushirish")]
    try:
        if is_admin:
            cmds.append(BotCommand(command="admin", description="Admin panel"))
            await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=user_id))
        else:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except Exception as e:
        logging.warning("Buyruqlarni sozlab bo'lmadi (%s): %s", user_id, e)


async def set_commands():
    """Oddiy foydalanuvchi faqat /start ni ko'radi; adminlar /start va /admin ni."""
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Botni ishga tushirish")],
                                  scope=BotCommandScopeDefault())
    except Exception as e:
        logging.warning("Buyruqlarni sozlab bo'lmadi: %s", e)
    ids = {ADMIN_ID} | {h["user_id"] for h in db.list_helper_admins()}
    for i in ids:
        if i:
            await set_commands_for(i, True)


@admin.callback_query(F.data == "helpers")
async def helpers_menu(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Bu bo'lim faqat asosiy admin uchun.", show_alert=True)
        return
    await state.clear()
    rows = db.list_helper_admins()
    lines = ["🧑‍💼 Yordamchi adminlar\n"]
    for item in rows:
        lines.append(f"• {item['user_id']} — {item['role']}")
    if not rows:
        lines.append("Hozircha yordamchi admin yo'q.")
    btns = [[("➕ Admin qo'shish", "helper:add")]]
    if rows:
        btns.append([("➖ Admin olib tashlash", "helper:remove")])
    await show(call, "\n".join(lines), build_kb(btns, "menu:set"))


@admin.callback_query(F.data == "helper:add")
async def helper_add_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Faqat asosiy admin.", show_alert=True)
        return
    await state.set_state(HelperFlow.add_id)
    await show(call, "Yordamchi adminning Telegram ID raqamini yuboring:", cancel_kb())


@admin.message(HelperFlow.add_id, F.text)
async def helper_add_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) == ADMIN_ID:
        await message.answer("ID raqam bo'lishi kerak (asosiy admin ID'sini kiritmang).",
                             reply_markup=cancel_kb())
        return
    db.add_helper_admin(int(raw), ADMIN_ID)
    await set_commands_for(int(raw), True)
    await state.clear()
    await note(message, f"✅ {raw} yordamchi admin qilindi.")
    await send_panel(message, "set")


@admin.callback_query(F.data == "helper:remove")
async def helper_remove_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Faqat asosiy admin.", show_alert=True)
        return
    await state.set_state(HelperFlow.remove_id)
    await show(call, "Adminlikdan olinadigan foydalanuvchi ID'sini yuboring:", cancel_kb())


@admin.message(HelperFlow.remove_id, F.text)
async def helper_remove_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("ID raqamini yuboring.", reply_markup=cancel_kb())
        return
    removed = db.remove_helper_admin(int(raw))
    if removed:
        await set_commands_for(int(raw), False)
    await state.clear()
    await note(message, "✅ Yordamchi admin olib tashlandi." if removed else "Bunday admin topilmadi.")
    await send_panel(message, "set")


# =============== Admin: yuborish vaqti ===============
@admin.callback_query(F.data == "time")
async def time_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SetTime.time)
    await show(call, f"⏰ Hozirgi vaqt: {send_time()}\n\n"
                     "Yangi vaqtni SS:DD ko'rinishida yozing (masalan 09:30):", cancel_kb())


@admin.message(SetTime.time, F.text)
async def time_save(message: Message, state: FSMContext):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", message.text.strip())
    if not m:
        await message.answer("Noto'g'ri format. Masalan: 09:30", reply_markup=cancel_kb())
        return
    hour, minute = int(m.group(1)), int(m.group(2))
    value = f"{hour:02d}:{minute:02d}"
    db.set_setting("send_time", value)
    scheduler.reschedule_job("daily", trigger="cron", hour=hour, minute=minute)
    await state.clear()
    await note(message, f"✅ Endi xabarlar har kuni {value} da yuboriladi.")
    await send_panel(message, "send")


# =============== Admin: kanallar (majburiy obuna / nusxa) ===============
@admin.callback_query(F.data.startswith("ch:"))
async def channel_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    key = call.data.split(":", 1)[1]
    if key not in CHANNEL_KEYS:
        await call.answer()
        return
    channel = db.get_setting(key)
    if key == "required_channel":
        info = ("Yoqilsa, foydalanuvchi avval kanalga a'zo bo'lishi kerak, shundan keyin botdan foydalana oladi.\n"
                "Muhim: bot shu kanalda administrator bo'lishi kerak.")
    else:
        info = ("Hammaga yuboriladigan va rejalashtirilgan xabarlarning nusxasi shu kanalga ham tushadi "
                "(kunlik xabarlar kanalga ketmaydi).\n"
                "Muhim: bot kanalda administrator bo'lib, xabar yuborish huquqiga ega bo'lishi kerak.")
    rows = [[("✏️ Kanalni kiritish / o'zgartirish", f"chset:{key}")]]
    if channel:
        rows.append([("🚫 O'chirish", f"choff:{key}")])
    await show(call, f"{CHANNEL_KEYS[key]}: {channel or 'o‘chiq'}\n\n{info}", build_kb(rows, "menu:chan"))


@admin.callback_query(F.data.startswith("chset:"))
async def channel_set(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    if key not in CHANNEL_KEYS:
        await call.answer()
        return
    await state.set_state(SetChannel.name)
    await state.update_data(key=key)
    await show(call, "Kanal username'ini yozing, masalan: @kanal_nomi", cancel_kb())


@admin.callback_query(F.data.startswith("choff:"))
async def channel_off(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":", 1)[1]
    if key in CHANNEL_KEYS:
        db.set_setting(key, None)
    await call.answer("O'chirildi")
    call.data = f"ch:{key}"
    await channel_menu(call, state)


@admin.message(SetChannel.name, F.text)
async def channel_save(message: Message, state: FSMContext):
    channel = message.text.strip()
    key = (await state.get_data()).get("key")
    if key not in CHANNEL_KEYS:
        await state.clear()
        return
    if not re.fullmatch(r"@[A-Za-z0-9_]{4,}", channel):
        await message.answer("Format: @kanal_nomi", reply_markup=cancel_kb())
        return
    try:
        me = await bot.me()
        member = await bot.get_chat_member(channel, me.id)
        if member.status not in ("administrator", "creator"):
            raise ValueError("bot admin emas")
    except Exception:
        await message.answer(
            "Kanal topilmadi yoki bot unda administrator emas. Botni kanalga admin qilib qo'shing "
            "va qayta yozing.", reply_markup=cancel_kb())
        return
    db.set_setting(key, channel)
    await state.clear()
    await note(message, f"✅ {CHANNEL_KEYS[key]}: {channel}")
    await send_panel(message, "chan")


# =============== Foydalanuvchi ===============
async def is_subscribed(user_id: int) -> bool:
    channel = db.get_setting("required_channel")
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(channel, user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logging.warning("Obunani tekshirib bo'lmadi: %s", e)
        return True  # tekshirib bo'lmasa, foydalanuvchini to'smaymiz


def welcome_text() -> str:
    return db.get_setting("welcome_text") or DEFAULT_WELCOME


def user_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💡 Xabar taklif qilish", callback_data="sug")
    return kb.as_markup()


async def welcome(message: Message, user):
    is_new = db.add_user(user.id, user.full_name, user.username)
    text = (welcome_text()
            .replace("{name}", user.first_name or "")
            .replace("{time}", send_time()))
    if is_admin_id(user.id):
        await message.answer(text, reply_markup=reply_kb(True))
    else:
        # eski pastdagi tugmalarni olib tashlash uchun qisqa xabar; u darrov o'chadi
        await note(message, "✨", 2, reply_markup=ReplyKeyboardRemove())
        await message.answer(text, reply_markup=user_kb())
    if is_new and ADMIN_ID and notify_on():
        uname = f"@{user.username}" if user.username else "—"
        try:
            await bot.send_message(
                ADMIN_ID, f"🆕 Yangi foydalanuvchi: {user.full_name} | {uname} | {user.id}\n"
                          f"Jami: {db.count_users()} ta")
        except Exception as e:
            logging.warning("Adminga xabar yuborib bo'lmadi: %s", e)


@user_router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    if not is_admin_id(u.id) and not await is_subscribed(u.id):
        channel = db.get_setting("required_channel")
        kb = InlineKeyboardBuilder()
        kb.button(text="📣 Kanalga o'tish", url=f"https://t.me/{channel.lstrip('@')}")
        kb.button(text="✅ Tekshirish", callback_data="chk")
        kb.adjust(1)
        await message.answer("Botdan foydalanish uchun avval kanalga obuna bo'ling:",
                             reply_markup=kb.as_markup())
        return
    await welcome(message, u)


@user_router.callback_query(F.data == "chk")
async def check_subscription(call: CallbackQuery):
    if not await is_subscribed(call.from_user.id):
        await call.answer("Siz hali obuna bo'lmagansiz.", show_alert=True)
        return
    await call.answer("Rahmat!")
    await welcome(call.message, call.from_user)


@user_router.message(F.text.in_(LEGACY_BUTTONS))
async def legacy_buttons_user(message: Message):
    await message.answer("Bu bo'lim olib tashlangan. Tugmalar yangilandi 👇",
                         reply_markup=reply_kb(is_admin_id(message.from_user.id)))


# ---------- Taklif (kunlik xabar g'oyasi) ----------
@user_router.callback_query(F.data == "sug")
async def suggest_start(call: CallbackQuery, state: FSMContext):
    if db.count_inbox_today(call.from_user.id, "suggest") >= 1:
        await call.answer("Bugun taklif yuborgansiz. Ertaga yana urinib ko'ring 🌿", show_alert=True)
        return
    await state.set_state(Suggest.text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Bekor qilish", callback_data="sugcancel")
    await call.answer()
    await call.message.answer(
        "💡 Kunlik xabar uchun g'oyangizni yozing.\n\n"
        "Yoqsa, admin uni kunlik xabarlarga qo'shadi. Kuniga bitta taklif yuborish mumkin.",
        reply_markup=kb.as_markup())


@user_router.callback_query(F.data == "sugcancel")
async def suggest_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await call.answer("Bekor qilindi")


@user_router.message(Suggest.text, F.text)
async def suggest_save(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.startswith("/"):
        await state.clear()
        return
    if len(text) < 5 or len(text) > 2000:
        await message.answer("Taklif 5 dan 2000 gacha belgi bo'lsin.")
        return
    await state.clear()
    if db.count_inbox_today(message.from_user.id, "suggest") >= 1:
        await message.answer("Bugun taklif yuborgansiz. Ertaga yana urinib ko'ring 🌿")
        return
    db.add_inbox(message.from_user.id, "suggest", text)
    await message.answer("✅ Taklifingiz qabul qilindi. Rahmat! Admin ko'rib chiqadi 💌")
    await notify_admin_inbox(message.from_user, "💡 Yangi taklif")


@user_router.message(Suggest.text)
async def suggest_wrong(message: Message):
    await message.answer("Taklifni matn ko'rinishida yozing.")


async def notify_admin_inbox(user, title: str):
    if ADMIN_ID and notify_inbox_on():
        uname = f"@{user.username}" if user.username else "—"
        await tell(ADMIN_ID, f"{title}: {user.full_name} | {uname}\n"
                             f"Ko'rish: /admin → 💬 Murojaatlar ({db.count_inbox(None, 'new')})")


# ---------- Reaksiyalar ----------
@user_router.callback_query(F.data.startswith("rx:"))
async def reaction(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 3 or parts[2] not in ("l", "h") or not parts[1].isdigit():
        await call.answer()
        return
    day = int(parts[1])
    mine = db.toggle_reaction(day, call.from_user.id, parts[2])
    row = db.get_daily_message(day)
    kb = content_kb(row_to_content(row) if row else {}, day, mine)
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass
    await call.answer("Rahmat 💛" if mine else "Reaksiya olib tashlandi")


# ---------- Foydalanuvchining erkin matni → adminga murojaat ----------
@user_router.message(F.text, ~F.text.startswith("/"))
async def user_free_text(message: Message):
    u = message.from_user
    if is_admin_id(u.id):
        return
    if not db.get_user(u.id):
        await message.answer("Avval /start ni bosing 🌿")
        return
    if db.count_inbox_today(u.id, "msg") >= MSG_LIMIT_PER_DAY:
        await message.answer("Bugungi xabarlar limiti tugadi. Ertaga yana yozing 🌿")
        return
    db.add_inbox(u.id, "msg", message.text.strip()[:3000])
    await message.answer("✅ Xabaringiz qabul qilindi. Iltimos, admin javobini kuting 💌")
    await notify_admin_inbox(u, "💬 Yangi murojaat")


# =============== Rejali yuborishlar ===============
async def send_daily():
    """Har kuni belgilangan vaqtda ishlaydi. Har kim o'zining keyingi kunini oladi."""
    if is_paused():
        logging.info("Kunlik yuborish to'xtatilgan.")
        return
    total = db.total_daily_messages()
    if total == 0:
        logging.info("Kunlik xabarlar yo'q.")
        return
    today = db.today()
    sent = 0
    for user in db.active_users():
        if user["last_sent_date"] == today:
            continue
        next_day = user["last_sent_day"] + 1
        msg_day = (next_day - 1) % total + 1  # tugasa, boshidan boshlanadi
        row = db.get_daily_message(msg_day)
        if row and await safe_send(user["user_id"], row_to_content(row), daily_prefix(next_day), rx_day=msg_day):
            db.mark_sent(user["user_id"], next_day, today)
            sent += 1
        await asyncio.sleep(0.05)
    logging.info("Kunlik xabar yuborildi: %s ta", sent)


async def check_scheduled():
    """Har 30 soniyada rejalashtirilgan xabarlarni tekshiradi."""
    now_str = db.now().strftime("%Y-%m-%d %H:%M")
    for r in db.due_scheduled(now_str):
        db.mark_scheduled_done(r["id"])  # ikki marta yuborilmasligi uchun avval belgilanadi
        c = row_to_content(r)
        sent, failed = await send_to_all(c)
        await copy_to_channel(c)
        try:
            await bot.send_message(
                ADMIN_ID, f"📅 Rejalashtirilgan xabar yuborildi ({r['run_at']}): {sent} ta, yetmadi: {failed}")
        except Exception:
            pass


async def main():
    db.init_db()
    hour, minute = map(int, send_time().split(":"))
    scheduler.add_job(send_daily, "cron", hour=hour, minute=minute,
                      id="daily", replace_existing=True)
    scheduler.add_job(check_scheduled, "interval", seconds=30,
                      id="sched", replace_existing=True, max_instances=1)
    scheduler.start()
    dp.include_router(admin)
    dp.include_router(user_router)
    await set_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())