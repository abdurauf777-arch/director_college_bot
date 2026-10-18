import asyncio
import csv
import html
import io
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import BigInteger, DateTime, Integer, String, Text, desc, extract, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv(override=True)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "").strip()) if x.isdigit()}
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///feedback.db")

TZ = timezone(timedelta(hours=5))

PAGE_SIZE = 8          
COOLDOWN_SEC = 15      
MAX_LEN = 3000         
MIN_LEN = 5

_last_sent: dict[int, float] = {}

MONTHS = ["Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
          "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"]

BTN_ALL = "📋 Barcha sharhlar"
BTN_YEARS = "📅 Yillar bo'yicha"
BTN_TODAY = "🗓 Bugun"
BTN_STATS = "📊 Statistika"


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)

class Base(DeclarativeBase):
    pass


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class Feedback(Base):
    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    student_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    student_name: Mapped[str] = mapped_column(String(200))
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local, index=True)


engine = create_async_engine(DATABASE_URL)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_student(tg_id: int) -> Optional[Student]:
    async with Session() as s:
        return await s.scalar(select(Student).where(Student.tg_id == tg_id))


async def save_student(tg_id: int, full_name: str, username: Optional[str]) -> Student:
    async with Session() as s:
        st = await s.scalar(select(Student).where(Student.tg_id == tg_id))
        if st:
            st.full_name = full_name
            st.username = username
        else:
            st = Student(tg_id=tg_id, full_name=full_name, username=username)
            s.add(st)
        await s.commit()
        return st


async def add_feedback(st: Student, username: Optional[str], text: str) -> Feedback:
    async with Session() as s:
        fb = Feedback(
            student_tg_id=st.tg_id,
            student_name=st.full_name,
            username=username,
            text=text,
            created_at=now_local(),
        )
        s.add(fb)
        await s.commit()
        return fb


def _apply(q, y: int, m: int, d: int):
    if y:
        q = q.where(extract("year", Feedback.created_at) == y)
    if m:
        q = q.where(extract("month", Feedback.created_at) == m)
    if d:
        q = q.where(extract("day", Feedback.created_at) == d)
    return q


async def fetch_page(y: int, m: int, d: int, page: int, order: str):
    async with Session() as s:
        total = await s.scalar(_apply(select(func.count(Feedback.id)), y, m, d)) or 0
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        q = _apply(select(Feedback), y, m, d)
        if order == "a":
            q = q.order_by(Feedback.created_at.asc(), Feedback.id.asc())
        else:
            q = q.order_by(Feedback.created_at.desc(), Feedback.id.desc())
        rows = (await s.scalars(q.limit(PAGE_SIZE).offset(page * PAGE_SIZE))).all()
    return total, pages, page, rows


async def fetch_all(y: int, m: int, d: int):
    async with Session() as s:
        q = _apply(select(Feedback), y, m, d).order_by(Feedback.created_at.asc(), Feedback.id.asc())
        return (await s.scalars(q)).all()


async def years_summary():
    async with Session() as s:
        q = (
            select(extract("year", Feedback.created_at).label("y"), func.count(Feedback.id))
            .group_by("y")
            .order_by(desc("y"))
        )
        return [(int(y), c) for y, c in (await s.execute(q)).all()]


async def months_summary(y: int):
    async with Session() as s:
        q = (
            select(extract("month", Feedback.created_at).label("mo"), func.count(Feedback.id))
            .where(extract("year", Feedback.created_at) == y)
            .group_by("mo")
            .order_by("mo")
        )
        return [(int(mo), c) for mo, c in (await s.execute(q)).all()]


async def get_feedback(fb_id: int) -> Optional[Feedback]:
    async with Session() as s:
        return await s.get(Feedback, fb_id)


async def stats():
    today = now_local()
    async with Session() as s:
        total = await s.scalar(select(func.count(Feedback.id))) or 0
        authors = await s.scalar(select(func.count(func.distinct(Feedback.student_tg_id)))) or 0
        students = await s.scalar(select(func.count(Student.id))) or 0
        n_today = await s.scalar(
            _apply(select(func.count(Feedback.id)), today.year, today.month, today.day)
        ) or 0
        n_month = await s.scalar(
            _apply(select(func.count(Feedback.id)), today.year, today.month, 0)
        ) or 0
    return total, authors, students, n_today, n_month


def esc(s: Optional[str]) -> str:
    return html.escape(s or "")


def fmt_dt(dt: datetime, seconds: bool = False) -> str:
    return dt.strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")


def title_for(y: int, m: int, d: int) -> str:
    if d:
        return f"{d:02d}.{m:02d}.{y}"
    if m:
        return f"{MONTHS[m - 1]} {y}"
    if y:
        return f"{y}-yil"
    return "barchasi"


def user_line(name: str, tg_id: int, username: Optional[str]) -> str:
    uname = f" (@{esc(username)})" if username else ""
    return f'<a href="tg://user?id={tg_id}">{esc(name)}</a>{uname}'


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ALL), KeyboardButton(text=BTN_TODAY)],
            [KeyboardButton(text=BTN_YEARS), KeyboardButton(text=BTN_STATS)],
        ],
        resize_keyboard=True,
    )


async def build_list(y: int, m: int, d: int, page: int, order: str):
    total, pages, page, rows = await fetch_page(y, m, d, page, order)

    head = (
        f"📋 <b>Sharhlar — {esc(title_for(y, m, d))}</b>\n"
        f"Jami: <b>{total}</b> · Sahifa {page + 1}/{pages}\n"
        f"Tartib: {'yangi → eski' if order == 'd' else 'eski → yangi'}\n\n"
    )
    if not rows:
        body = "Bu davrda sharhlar yo'q."
    else:
        blocks = []
        for f in rows:
            preview = f.text.replace("\n", " ")
            if len(preview) > 180:
                preview = preview[:180] + "…"
            blocks.append(
                f"<b>#{f.id}</b> · 🕒 {fmt_dt(f.created_at)}\n"
                f"👤 {user_line(f.student_name, f.student_tg_id, f.username)}\n"
                f"💬 {esc(preview)}\n"
                f"/s_{f.id}"
            )
        body = "\n\n".join(blocks)

    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"l:{y}:{m}:{d}:{page - 1}:{order}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"l:{y}:{m}:{d}:{page + 1}:{order}"))
    kb.row(*nav)

    flip = "a" if order == "d" else "d"
    kb.row(
        InlineKeyboardButton(
            text="↕️ " + ("Eski → yangi" if order == "d" else "Yangi → eski"),
            callback_data=f"l:{y}:{m}:{d}:0:{flip}",
        ),
        InlineKeyboardButton(text="📥 CSV (Excel)", callback_data=f"x:{y}:{m}:{d}"),
    )
    back = f"m:{y}" if y else "y"
    kb.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data=back))
    return head + body, kb.as_markup()


async def build_years():
    data = await years_summary()
    kb = InlineKeyboardBuilder()
    for y, c in data:
        kb.button(text=f"{y} ({c})", callback_data=f"m:{y}")
    kb.adjust(3)
    kb.row(InlineKeyboardButton(text="📋 Hammasi", callback_data="l:0:0:0:0:d"))
    text = "📅 <b>Yilni tanlang</b>\n\nYoki sanani yozing: <code>18.10.2026</code>, <code>10.2026</code> yoki <code>2026</code>"
    if not data:
        text = "Hozircha sharhlar yo'q."
    return text, kb.as_markup()


async def build_months(y: int):
    data = await months_summary(y)
    kb = InlineKeyboardBuilder()
    for mo, c in data:
        kb.button(text=f"{MONTHS[mo - 1]} ({c})", callback_data=f"l:{y}:{mo}:0:0:d")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text=f"📋 Butun {y}-yil", callback_data=f"l:{y}:0:0:0:d"))
    kb.row(InlineKeyboardButton(text="🔙 Yillar", callback_data="y"))
    return f"🗓 <b>{y}-yil — oyni tanlang</b>", kb.as_markup()


async def notify_admins(bot: Bot, fb: Feedback):
    text = (
        f"📝 <b>Yangi sharh #{fb.id}</b>\n"
        f"👤 {user_line(fb.student_name, fb.student_tg_id, fb.username)}\n"
        f"🆔 <code>{fb.student_tg_id}</code>\n"
        f"🕒 {fmt_dt(fb.created_at, seconds=True)}\n\n"
        f"💬 {esc(fb.text)}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, protect_content=True)
        except Exception as e:
            logging.warning("Adminga yuborib bo'lmadi (%s): %r", admin_id, e)


async def safe_edit(message: types.Message, text: str, markup):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def parse_date_query(text: str):
    text = text.strip()
    try:
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", text):
            d, m, y = (int(p) for p in text.split("."))
            datetime(y, m, d)
            return y, m, d
        if re.fullmatch(r"\d{1,2}\.\d{4}", text):
            m, y = (int(p) for p in text.split("."))
            datetime(y, m, 1)
            return y, m, 0
        if re.fullmatch(r"\d{4}", text):
            y = int(text)
            if 2000 <= y <= 2100:
                return y, 0, 0
    except ValueError:
        return None
    return None

common = Router()
admin = Router()
student = Router()

admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
student.message.filter(~F.from_user.id.in_(ADMIN_IDS))


class Reg(StatesGroup):
    name = State()


@common.message(Command("id"))
async def id_handler(message: types.Message):
    await message.answer(f"Sizning Telegram ID: <code>{message.from_user.id}</code>", protect_content=True)


@student.message(CommandStart())
async def student_start(message: types.Message, state: FSMContext):
    st = await get_student(message.from_user.id)
    if st:
        await state.clear()
        await message.answer(
            f"Assalomu alaykum, <b>{esc(st.full_name)}</b>!\n\n"
            "Sharhingizni yozib yuboring — u to'g'ridan-to'g'ri direktorga yetkaziladi.\n"
            "Ismni o'zgartirish: /name",
            protect_content=True
        )
    else:
        await state.set_state(Reg.name)
        await message.answer(
            "Assalomu alaykum! 👋\n\n"
            "Bu bot orqali o'quv markazi haqidagi sharh va takliflaringizni "
            "to'g'ridan-to'g'ri direktorga yuborishingiz mumkin.\n\n"
            "Avval <b>ism-familiyangiz va guruhingizni</b> yozing.\n"
            "Masalan: <i>Aliyev Vali, 12-guruh</i>",
            protect_content=True
        )


@student.message(Command("name"))
async def student_rename(message: types.Message, state: FSMContext):
    await state.set_state(Reg.name)
    await message.answer("Yangi ism-familiya va guruhingizni yozing:", protect_content=True)


@student.message(Reg.name, F.text, ~F.text.startswith("/"))
async def student_name(message: types.Message, state: FSMContext):
    name = " ".join(message.text.split())
    if not 3 <= len(name) <= 150:
        await message.answer("Iltimos, 3 dan 150 tagacha belgidan iborat ism-familiya (va guruh) yozing.", protect_content=True)
        return
    await save_student(message.from_user.id, name, message.from_user.username)
    await state.clear()
    await message.answer(
        f"✅ Rahmat, <b>{esc(name)}</b>!\n\nEndi sharhingizni yozib yuboring. "
        "Har bir xabaringiz alohida sharh sifatida direktorga boradi.",
        protect_content=True
    )


@student.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def student_feedback(message: types.Message, state: FSMContext):
    st = await get_student(message.from_user.id)
    if not st:
        await state.set_state(Reg.name)
        await message.answer("Avval ism-familiyangiz va guruhingizni yozing:", protect_content=True)
        return

    text = message.text.strip()
    if len(text) < MIN_LEN:
        await message.answer("Sharh juda qisqa. Iltimos, batafsilroq yozing.", protect_content=True)
        return
    if len(text) > MAX_LEN:
        await message.answer(f"Sharh juda uzun (maksimum {MAX_LEN} belgi). Iltimos, qisqartiring.", protect_content=True)
        return

    now = time.monotonic()
    wait = COOLDOWN_SEC - (now - _last_sent.get(message.from_user.id, 0))
    if wait > 0:
        await message.answer(f"Iltimos, {int(wait) + 1} soniyadan keyin qayta yuboring.", protect_content=True)
        return
    _last_sent[message.from_user.id] = now

    fb = await add_feedback(st, message.from_user.username, text)
    await message.answer("✅ Sharhingiz direktorga yuborildi. Rahmat!", protect_content=True)
    await notify_admins(message.bot, fb)


@student.message(StateFilter(None))
async def student_other(message: types.Message):
    await message.answer("Iltimos, sharhni matn ko'rinishida yozib yuboring.", protect_content=True)



# Обработчики административной панели
@admin.message(Command("admin"))
async def admin_start(message: types.Message):
    await message.answer(
        "🔐 <b>Админ-панель</b>\nВыберите нужный раздел:",
        reply_markup=admin_menu(),
        protect_content=True,
    )


@admin.message(F.text == BTN_ALL)
async def admin_all(message: types.Message):
    text, markup = await build_list(0, 0, 0, 0, "d")
    await message.answer(text, reply_markup=markup, protect_content=True)


@admin.message(F.text == BTN_TODAY)
async def admin_today(message: types.Message):
    today = now_local()
    text, markup = await build_list(today.year, today.month, today.day, 0, "d")
    await message.answer(text, reply_markup=markup, protect_content=True)


@admin.message(F.text == BTN_YEARS)
async def admin_years(message: types.Message):
    text, markup = await build_years()
    await message.answer(text, reply_markup=markup, protect_content=True)


@admin.message(F.text == BTN_STATS)
async def admin_stats(message: types.Message):
    total, authors, students, today_count, month_count = await stats()
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"📝 Jami sharhlar: <b>{total}</b>\n"
        f"👥 Sharh yozgan foydalanuvchilar: <b>{authors}</b>\n"
        f"🎓 Ro'yxatdan o'tgan foydalanuvchilar: <b>{students}</b>\n"
        f"🗓 Bugun: <b>{today_count}</b>\n"
        f"📅 Shu oy: <b>{month_count}</b>"
    )
    await message.answer(text, protect_content=True)


@admin.message(F.text.startswith("/s_"))
async def admin_feedback_detail(message: types.Message):
    match = re.fullmatch(r"/s_(\d+)", (message.text or "").strip())
    if not match:
        await message.answer("Sharh raqami noto'g'ri.")
        return

    fb = await get_feedback(int(match.group(1)))
    if fb is None:
        await message.answer("❌ Sharh topilmadi.", protect_content=True)
        return

    text = (
        f"📝 <b>Sharh #{fb.id}</b>\n"
        f"👤 {user_line(fb.student_name, fb.student_tg_id, fb.username)}\n"
        f"🆔 <code>{fb.student_tg_id}</code>\n"
        f"🕒 {fmt_dt(fb.created_at, seconds=True)}\n\n"
        f"💬 {esc(fb.text)}"
    )
    await message.answer(text, protect_content=True)


@admin.message(F.text, ~F.text.startswith("/"))
async def admin_date_search(message: types.Message):
    query = parse_date_query(message.text or "")
    if query is None:
        return
    y, m, d = query
    text, markup = await build_list(y, m, d, 0, "d")
    await message.answer(text, reply_markup=markup, protect_content=True)


@admin.callback_query(F.data.startswith("l:"))
async def admin_list_callback(callback: types.CallbackQuery):
    try:
        _, y, m, d, page, order = (callback.data or "").split(":")
        y, m, d, page = map(int, (y, m, d, page))
        if order not in {"a", "d"}:
            raise ValueError("Noto'g'ri tartib")
        text, markup = await build_list(y, m, d, page, order)
        if callback.message:
            await safe_edit(callback.message, text, markup)
        await callback.answer()
    except (ValueError, TypeError):
        await callback.answer("Parametrlarda xato.", show_alert=True)


@admin.callback_query(F.data == "y")
async def admin_years_callback(callback: types.CallbackQuery):
    text, markup = await build_years()
    if callback.message:
        await safe_edit(callback.message, text, markup)
    await callback.answer()


@admin.callback_query(F.data.startswith("m:"))
async def admin_months_callback(callback: types.CallbackQuery):
    try:
        year = int((callback.data or "").split(":")[1])
        text, markup = await build_months(year)
        if callback.message:
            await safe_edit(callback.message, text, markup)
        await callback.answer()
    except (ValueError, TypeError, IndexError):
        await callback.answer("Yil noto'g'ri.", show_alert=True)


@admin.callback_query(F.data == "noop")
async def admin_noop(callback: types.CallbackQuery):
    await callback.answer()


@admin.callback_query(F.data.startswith("x:"))
async def admin_export_csv(callback: types.CallbackQuery):
    try:
        _, y, m, d = (callback.data or "").split(":")
        y, m, d = map(int, (y, m, d))
        rows = await fetch_all(y, m, d)

        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow([
            "ID", "Sana", "Ism-familiya",
            "Telegram ID", "Username", "Sharh",
        ])
        for fb in rows:
            writer.writerow([
                fb.id,
                fmt_dt(fb.created_at, seconds=True),
                fb.student_name,
                fb.student_tg_id,
                fb.username or "",
                fb.text,
            ])

        filename = f"sharhlar_{y or 'all'}_{m}_{d}.csv"
        document = BufferedInputFile(
            buffer.getvalue().encode("utf-8-sig"),
            filename=filename,
        )
        if callback.message:
            await callback.message.answer_document(
                document,
                caption=f"{title_for(y, m, d)} — {len(rows)} ta sharh",
                protect_content=True,
            )
        await callback.answer()
    except (ValueError, TypeError):
        await callback.answer("Eksport parametrlarida xato.", show_alert=True)



async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi (.env)")
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS bo'sh! Direktor ID sini .env ga yozing (ID ni /id buyrug'i beradi).")

    await init_db()

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(common)
    dp.include_router(admin)
    dp.include_router(student)

    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="name", description="Ismni o'zgartirish"),
        BotCommand(command="id", description="Mening Telegram ID raqamim"),
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="admin", description="Admin panel"),
                    BotCommand(command="id", description="Mening Telegram ID raqamim"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            logging.warning("Admin buyruqlarini o'rnatib bo'lmadi (%s): %r", admin_id, e)

    me = await bot.get_me()
    print(f"Bot ishga tushdi: @{me.username} (adminlar: {sorted(ADMIN_IDS) or 'yo`q'})")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())