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
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)
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


DAILY_PREFIX = "Kun eslatmasi\n\n"
PAGE = 10

BTN_PANEL = "🛠 Admin panel"
BTN_LAST = "📖 Oxirgi xabarim"
# Eski versiyadagi tugmalar (ular hali ba'zi chatlarda turgan bo'lishi mumkin)
LEGACY_BUTTONS = {"📊 Statistika", "📢 Hammaga xabar", "👥 Foydalanuvchilar", "⏳ Mening hisoblagichlarim",
                  BTN_LAST}  # "Oxirgi xabarim" tugmasi olib tashlangan; eski klaviaturani tozalaydi

DEFAULT_WELCOME = (
    "Assalomu alaykum, {name}! Kundalik eslatmalar botiga xush kelibsiz!\n\n"
    "Kundalik eslatmalar har kuni soat {time} dan keyin yuboriladi!"
)


# =============== Yordamchilar ===============
def send_time() -> str:
    return db.get_setting("send_time", SEND_TIME_DEFAULT)


def short(text: str, n: int = 30) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def row_to_content(r: dict) -> dict:
    return {
        "text": r.get("text") or "",
        "voice_id": r.get("voice_id"),
        "btn_text": r.get("btn_text"),
        "btn_url": r.get("btn_url"),
    }


def data_to_content(data: dict) -> dict:
    return row_to_content(data)


def content_kb(c: dict):
    if c.get("btn_text") and c.get("btn_url"):
        kb = InlineKeyboardBuilder()
        kb.button(text=c["btn_text"], url=c["btn_url"])
        return kb.as_markup()
    return None


def describe(c: dict) -> str:
    parts = []
    if c.get("voice_id"):
        parts.append("🎤 Ovozli xabar")
    if c.get("text"):
        parts.append(f"📝 Matn: {c['text']}")
    if c.get("btn_text"):
        parts.append(f"🔗 Tugma: {c['btn_text']} → {c['btn_url']}")
    return "\n".join(parts) or "(bo'sh)"


async def send_content(chat_id: int, c: dict, prefix: str = ""):
    kb = content_kb(c)
    text = c.get("text") or ""
    if c.get("voice_id"):
        caption = (prefix + text) if (prefix and text) else (text or prefix.strip() or None)
        return await bot.send_voice(chat_id, c["voice_id"], caption=caption, reply_markup=kb)
    else:
        return await bot.send_message(chat_id, prefix + text, reply_markup=kb)


async def safe_send(user_id: int, c: dict, prefix: str = "") -> bool:
    try:
        await send_content(user_id, c, prefix)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        return await safe_send(user_id, c, prefix)
    except (TelegramForbiddenError, TelegramBadRequest):
        db.set_inactive(user_id)  # bot bloklangan yoki chat yo'q
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
        await target.answer()
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


class SetAutoclose(StatesGroup):
    minutes = State()

class HelperFlow(StatesGroup):
    add_id = State()
    remove_id = State()


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


def panel_sections():
    return {
        "msg": ("📨 Xabarlar", [
            ("➕ Kun xabari qo'shish", "add"),
            ("📋 Kunlik xabarlar", "list"),
            ("📢 Hammaga xabar", "bc"),
            ("✉️ Alohida xabar", "single"),
            ("📅 Rejalashtirilgan", "sched"),
        ]),
        "usr": ("👥 Foydalanuvchilar", [
            ("📊 Statistika", "stats"),
            ("👥 Ro'yxat / qidirish", "users:0"),
            ("📥 Excel yuklab olish", "export"),
        ]),
        "set": ("⚙️ Sozlamalar", [
            ("⏰ Yuborish vaqti", "time"),
            ("▶️ Yuborishni yoqish" if is_paused() else "⏸ Yuborishni to'xtatish", "pause"),
            ("👋 Salomlashuv matni", "welcome"),
            ("🔕 Yangi user xabari: o'chiq" if not notify_on() else "🔔 Yangi user xabari: yoqiq", "notify"),
            ("🧹 Avto-yopish", "autoclose"),
            ("📣 Majburiy obuna", "sub"),
            ("🧑‍💼 Yordamchi adminlar", "helpers"),
        ]),
    }


def panel_kb(open_sec=None):
    """Bo'limlar ochilib-yopiladi: bosilgan bo'lim ochiladi, qayta bosilsa yig'iladi."""
    kb = InlineKeyboardBuilder()
    for key, (title, items) in panel_sections().items():
        is_open = key == open_sec
        kb.row(InlineKeyboardButton(
            text=("🔽 " if is_open else "▶️ ") + title,
            callback_data="sec:none" if is_open else f"sec:{key}"))
        if is_open:
            for i in range(0, len(items), 2):
                kb.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in items[i:i + 2]])
    kb.row(InlineKeyboardButton(text="❌ Yopish", callback_data="close"))
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


def panel_text():
    s = db.stats()
    channel = db.get_setting("required_channel")
    ac = autoclose_minutes()
    return (
        "🛠 Admin panel\n\n"
        f"👥 Foydalanuvchilar: {s['active']} faol / {s['total']} jami\n"
        f"📝 Kunlik xabarlar: {s['messages']} ta\n"
        f"📅 Rejalashtirilgan: {s['scheduled']} ta\n"
        f"⏰ Yuborish vaqti: {send_time()} ({TIMEZONE})\n"
        f"📨 Kunlik yuborish: {'⏸ to‘xtatilgan' if is_paused() else '✅ faol'}\n"
        f"📣 Majburiy obuna: {channel or 'o‘chiq'}\n"
        f"🧑‍💼 Yordamchi adminlar: {len(db.list_helper_admins())} ta\n"
        f"🧹 Avto-yopish: {f'{ac} daqiqa' if ac else 'o‘chiq'}"
    )


def stats_text():
    s = db.stats()
    return (
        "📊 Statistika\n\n"
        f"👥 Jami foydalanuvchilar: {s['total']}\n"
        f"✅ Faol: {s['active']}\n"
        f"🚫 Botni bloklagan: {s['blocked']}\n"
        f"🆕 Bugun qo'shilgan: {s['new_today']}\n"
        f"📨 Bugun xabar olgan: {s['sent_today']}\n"
        f"📝 Kunlik xabarlar soni: {s['messages']}\n"
        f"📅 Rejalashtirilgan xabarlar: {s['scheduled']}"
    )


# =============== Admin: panel (ochiladi / yopiladi) ===============
async def send_panel(msg: Message, section=None):
    """Yangi panel yuboradi; eski ochiq panel bo'lsa, uni o'chiradi."""
    chat_id = msg.chat.id
    old = _panels.pop(chat_id, None)
    if old:
        _cancel((chat_id, old))
        try:
            await bot.delete_message(chat_id, int(old))
        except TelegramBadRequest:
            pass
    m = await msg.answer(panel_text(), reply_markup=panel_kb(section))
    _panels[chat_id] = m.message_id
    schedule_autoclose(m)
    return m


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
async def toggle_section(call: CallbackQuery, state: FSMContext):
    await state.clear()
    sec = call.data.split(":", 1)[1]
    await show(call, panel_text(), panel_kb(sec if sec in panel_sections() else None))


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
    await show(call, panel_text(), panel_kb())


@admin.callback_query(F.data == "stats")
async def show_stats(call: CallbackQuery):
    await show(call, stats_text(), back_kb())


# ---------- Yangi sozlamalar ----------
@admin.callback_query(F.data == "pause")
async def toggle_pause(call: CallbackQuery):
    db.set_setting("paused", "0" if is_paused() else "1")
    await show(call, panel_text(), panel_kb("set"))


@admin.callback_query(F.data == "notify")
async def toggle_notify(call: CallbackQuery):
    db.set_setting("notify_new", "0" if notify_on() else "1")
    await show(call, panel_text(), panel_kb("set"))


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
    await send_panel(message, "set")


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
    await send_panel(message, "set")


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
        await send_panel(msg)

    elif mode == "daily_edit":
        day = data["day"]
        db.update_daily_message(day, c["text"], c["voice_id"], c["btn_text"], c["btn_url"])
        await state.clear()
        await note(msg, f"✅ {day}-kun xabari yangilandi.")
        await send_panel(msg)

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
        await msg.answer("Xatolik: amal topilmadi.", reply_markup=panel_kb())


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
    await message.answer(
        "✅ Yuborildi." if ok else "🚫 Yuborib bo'lmadi (foydalanuvchi botni bloklagan bo'lishi mumkin).")
    await send_panel(message)


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
        kb.button(text=f"🗑 {r['run_at']} · {icon}{short(r['text'], 20)}", callback_data=f"scdel:{r['id']}")
    kb.button(text="➕ Yangi rejalashtirish", callback_data="scnew")
    kb.button(text="⬅️ Orqaga", callback_data="panel")
    kb.adjust(1)
    text = ("📅 Rejalashtirilgan xabarlar\n(o'chirish uchun bosing)" if rows
            else "Rejalashtirilgan xabarlar yo'q.")
    await show(call, text, kb.as_markup())


@admin.callback_query(F.data == "scnew")
async def sched_new(call: CallbackQuery, state: FSMContext):
    await start_compose(call, state, "schedule", "📅 Rejalashtiriladigan xabar")


@admin.callback_query(F.data.startswith("scdel:"))
async def sched_delete(call: CallbackQuery, state: FSMContext):
    db.delete_scheduled(int(call.data.split(":")[1]))
    await call.answer("O'chirildi")
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
    await send_panel(message)


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
        kb.button(text=f"{r['day']}-kun: {icon}{short(r['text'])}", callback_data=f"msg:{r['day']}")
    kb.button(text="⬅️ Orqaga", callback_data="panel")
    kb.adjust(1)
    text = "📋 Kunlik xabarlar" if rows else "Hali kunlik xabarlar yo'q."
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
    kb.button(text="⬅️ Ro'yxat", callback_data="list")
    kb.adjust(2, 1, 1)
    await show(call, f"{day}-kun xabari:\n\n{describe(c)}", kb.as_markup())
    if c["voice_id"]:
        m = await send_content(call.message.chat.id, c)
        schedule_delete(m, 120)


@admin.callback_query(F.data.startswith("prev:"))
async def preview_message(call: CallbackQuery):
    row = db.get_daily_message(int(call.data.split(":")[1]))
    if row is None:
        await call.answer("Topilmadi", show_alert=True)
        return
    m = await send_content(call.message.chat.id, row_to_content(row), DAILY_PREFIX)
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


# =============== Admin: foydalanuvchilar ===============
async def users_page(target, page: int):
    total = db.count_users()
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = db.list_users(PAGE, page * PAGE)
    lines = [f"👥 Foydalanuvchilar ({total}) — {page + 1}/{pages}\n"]
    for i, u in enumerate(rows, start=page * PAGE + 1):
        uname = f"@{u['username']}" if u["username"] else "—"
        lines.append(f"{i}. {u['full_name'] or '—'} | {uname} | {u['user_id']} "
                     f"{'✅' if u['active'] else '🚫'}")
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
    kb.row(InlineKeyboardButton(text="⬅️ Panel", callback_data="panel"))
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
            uname = f"@{u['username']}" if u["username"] else "—"
            lines.append(f"{u['full_name'] or '—'} | {uname} | {u['user_id']} "
                         f"{'✅' if u['active'] else '🚫'}\n   Qo'shilgan: {u['start_date']}, "
                         f"oxirgi kun: {u['last_sent_day']}")
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    for u in rows:
        label = u["full_name"] or u["username"] or str(u["user_id"])
        kb.button(text=f"👤 {short(label, 28)}", callback_data=f"uview:{u['user_id']}")
    kb.button(text="⬅️ Ro'yxat", callback_data="users:0")
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
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Kunlarni boshidan boshlash", callback_data=f"ureset:{uid}")
    kb.button(text=("🚫 Faolsizlantirish" if u["active"] else "✅ Faollashtirish"),
              callback_data=f"utoggle:{uid}")
    kb.button(text="⬅️ Ro'yxat", callback_data="users:0")
    kb.adjust(1)
    await show(call, user_card(u), kb.as_markup())


@admin.callback_query(F.data.startswith("uview:"))
async def user_view(call: CallbackQuery):
    await show_user(call, int(call.data.split(":")[1]))


@admin.callback_query(F.data.startswith("ureset:"))
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
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Admin qo'shish", callback_data="helper:add")
    if rows:
        kb.button(text="➖ Admin olib tashlash", callback_data="helper:remove")
    kb.button(text="⬅️ Panel", callback_data="panel")
    kb.adjust(1)
    await show(call, "\n".join(lines), kb.as_markup())

@admin.callback_query(F.data == "helper:add")
async def helper_add_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Faqat asosiy admin.", show_alert=True); return
    await state.set_state(HelperFlow.add_id)
    await show(call, "Yordamchi adminning Telegram ID raqamini yuboring:", cancel_kb())

@admin.message(HelperFlow.add_id, F.text)
async def helper_add_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear(); return
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) == ADMIN_ID:
        await message.answer("ID raqam bo'lishi kerak (asosiy admin ID'sini kiritmang).")
        return
    db.add_helper_admin(int(raw), ADMIN_ID)
    await state.clear()
    await message.answer(f"✅ {raw} yordamchi admin qilindi.", reply_markup=panel_kb())

@admin.callback_query(F.data == "helper:remove")
async def helper_remove_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Faqat asosiy admin.", show_alert=True); return
    await state.set_state(HelperFlow.remove_id)
    await show(call, "Adminlikdan olinadigan foydalanuvchi ID'sini yuboring:", cancel_kb())

@admin.message(HelperFlow.remove_id, F.text)
async def helper_remove_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear(); return
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("ID raqamini yuboring."); return
    removed = db.remove_helper_admin(int(raw))
    await state.clear()
    await message.answer("✅ Yordamchi admin olib tashlandi." if removed else "Bunday admin topilmadi.",
                         reply_markup=panel_kb())


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
        await message.answer("Noto'g'ri format. Masalan: 09:30")
        return
    hour, minute = int(m.group(1)), int(m.group(2))
    value = f"{hour:02d}:{minute:02d}"
    db.set_setting("send_time", value)
    scheduler.reschedule_job("daily", trigger="cron", hour=hour, minute=minute)
    await state.clear()
    await note(message, f"✅ Endi xabarlar har kuni {value} da yuboriladi.")
    await send_panel(message)


# =============== Admin: majburiy obuna ===============
@admin.callback_query(F.data == "sub")
async def sub_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    channel = db.get_setting("required_channel")
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Kanalni kiritish / o'zgartirish", callback_data="subset")
    if channel:
        kb.button(text="🚫 O'chirish", callback_data="suboff")
    kb.button(text="⬅️ Orqaga", callback_data="panel")
    kb.adjust(1)
    await show(call,
               f"📣 Majburiy obuna: {channel or 'o‘chiq'}\n\n"
               "Yoqilsa, foydalanuvchi avval kanalga a'zo bo'lishi kerak, shundan keyin botdan foydalana oladi.\n"
               "Muhim: bot shu kanalda administrator bo'lishi kerak.",
               kb.as_markup())


@admin.callback_query(F.data == "subset")
async def sub_set(call: CallbackQuery, state: FSMContext):
    await state.set_state(SetChannel.name)
    await show(call, "Kanal username'ini yozing, masalan: @kanal_nomi", cancel_kb())


@admin.callback_query(F.data == "suboff")
async def sub_off(call: CallbackQuery, state: FSMContext):
    db.set_setting("required_channel", None)
    await call.answer("O'chirildi")
    await sub_menu(call, state)


@admin.message(SetChannel.name, F.text)
async def sub_save(message: Message, state: FSMContext):
    channel = message.text.strip()
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
    db.set_setting("required_channel", channel)
    await state.clear()
    await note(message, f"✅ Majburiy obuna yoqildi: {channel}")
    await send_panel(message)


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


async def welcome(message: Message, user):
    is_new = db.add_user(user.id, user.full_name, user.username)
    text = (welcome_text()
            .replace("{name}", user.first_name or "")
            .replace("{time}", send_time()))
    await message.answer(text, reply_markup=reply_kb(is_admin_id(user.id)))
    if is_new and ADMIN_ID and notify_on():
        uname = f"@{user.username}" if user.username else "—"
        try:
            await bot.send_message(
                ADMIN_ID, f"🆕 Yangi foydalanuvchi: {user.full_name} | {uname} | {user.id}\n"
                          f"Jami: {db.count_users()} ta")
        except Exception as e:
            logging.warning("Adminga xabar yuborib bo'lmadi: %s", e)


@user_router.message(CommandStart())
async def start(message: Message):
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
        if row and await safe_send(user["user_id"], row_to_content(row), DAILY_PREFIX):
            db.mark_sent(user["user_id"], next_day, today)
            sent += 1
        await asyncio.sleep(0.05)
    logging.info("Kunlik xabar yuborildi: %s ta", sent)


async def check_scheduled():
    """Har 30 soniyada rejalashtirilgan xabarlarni tekshiradi."""
    now_str = db.now().strftime("%Y-%m-%d %H:%M")
    for r in db.due_scheduled(now_str):
        db.mark_scheduled_done(r["id"])  # ikki marta yuborilmasligi uchun avval belgilanadi
        sent, failed = await send_to_all(row_to_content(r))
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())