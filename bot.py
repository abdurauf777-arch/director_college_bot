
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
from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    desc,
    extract,
    func,
    select,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv(override=True)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "").strip())
    if x.isdigit()
}
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///feedback.db",
)

TZ = timezone(timedelta(hours=5))

PAGE_SIZE = 8
COOLDOWN_SEC = 15
MAX_LEN = 3000
MIN_LEN = 5

_last_sent: dict[int, float] = {}

MONTHS = [
    "Yanvar", "Fevral", "Mart", "Aprel",
    "May", "Iyun", "Iyul", "Avgust",
    "Sentabr", "Oktabr", "Noyabr", "Dekabr",
]

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

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    tg_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True
    )
    full_name: Mapped[str] = mapped_column(String(200))
    username: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local
    )


class Feedback(Base):
    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    student_tg_id: Mapped[int] = mapped_column(
        BigInteger, index=True
    )
    student_name: Mapped[str] = mapped_column(String(200))
    username: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, index=True
    )


engine = create_async_engine(DATABASE_URL)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_student(tg_id: int) -> Optional[Student]:
    async with Session() as session:
        return await session.scalar(
            select(Student).where(Student.tg_id == tg_id)
        )


async def save_student(
    tg_id: int,
    full_name: str,
    username: Optional[str],
) -> Student:
    async with Session() as session:
        student = await session.scalar(
            select(Student).where(Student.tg_id == tg_id)
        )

        if student:
            student.full_name = full_name
            student.username = username
        else:
            student = Student(
                tg_id=tg_id,
                full_name=full_name,
                username=username,
            )
            session.add(student)

        await session.commit()
        return student


async def add_feedback(
    student: Student,
    username: Optional[str],
    text: str,
) -> Feedback:
    async with Session() as session:
        feedback = Feedback(
            student_tg_id=student.tg_id,
            student_name=student.full_name,
            username=username,
            text=text,
            created_at=now_local(),
        )
        session.add(feedback)
        await session.commit()
        return feedback


def _apply(query, year: int, month: int, day: int):
    if year:
        query = query.where(
            extract("year", Feedback.created_at) == year
        )
    if month:
        query = query.where(
            extract("month", Feedback.created_at) == month
        )
    if day:
        query = query.where(
            extract("day", Feedback.created_at) == day
        )
    return query


async def fetch_page(
    year: int,
    month: int,
    day: int,
    page: int,
    order: str,
):
    async with Session() as session:
        total = await session.scalar(
            _apply(select(func.count(Feedback.id)), year, month, day)
        ) or 0

        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(page, 0), pages - 1)

        query = _apply(
            select(Feedback), year, month, day
        )

        if order == "a":
            query = query.order_by(
                Feedback.created_at.asc(),
                Feedback.id.asc(),
            )
        else:
            query = query.order_by(
                Feedback.created_at.desc(),
                Feedback.id.desc(),
            )

        rows = (
            await session.scalars(
                query.limit(PAGE_SIZE).offset(page * PAGE_SIZE)
            )
        ).all()

    return total, pages, page, rows


async def fetch_all(year: int, month: int, day: int):
    async with Session() as session:
        query = _apply(
            select(Feedback), year, month, day
        ).order_by(
            Feedback.created_at.asc(),
            Feedback.id.asc(),
        )

        return (await session.scalars(query)).all()


async def years_summary():
    async with Session() as session:
        query = (
            select(
                extract("year", Feedback.created_at).label("y"),
                func.count(Feedback.id),
            )
            .group_by("y")
            .order_by(desc("y"))
        )
        rows = (await session.execute(query)).all()
        return [(int(year), count) for year, count in rows]


async def months_summary(year: int):
    async with Session() as session:
        query = (
            select(
                extract("month", Feedback.created_at).label("mo"),
                func.count(Feedback.id),
            )
            .where(extract("year", Feedback.created_at) == year)
            .group_by("mo")
            .order_by("mo")
        )
        rows = (await session.execute(query)).all()
        return [(int(month), count) for month, count in rows]


async def get_feedback(feedback_id: int) -> Optional[Feedback]:
    async with Session() as session:
        return await session.get(Feedback, feedback_id)


async def stats():
    today = now_local()

    async with Session() as session:
        total = await session.scalar(
            select(func.count(Feedback.id))
        ) or 0

        authors = await session.scalar(
            select(func.count(func.distinct(Feedback.student_tg_id)))
        ) or 0

        students = await session.scalar(
            select(func.count(Student.id))
        ) or 0

        today_count = await session.scalar(
            _apply(
                select(func.count(Feedback.id)),
                today.year,
                today.month,
                today.day,
            )
        ) or 0

        month_count = await session.scalar(
            _apply(
                select(func.count(Feedback.id)),
                today.year,
                today.month,
                0,
            )
        ) or 0

    return total, authors, students, today_count, month_count


def esc(value: Optional[str]) -> str:
    return html.escape(value or "")


def fmt_dt(dt: datetime, seconds: bool = False) -> str:
    pattern = "%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M"
    return dt.strftime(pattern)


def title_for(year: int, month: int, day: int) -> str:
    if day:
        return f"{day:02d}.{month:02d}.{year}"
    if month:
        return f"{MONTHS[month - 1]} {year}"
    if year:
        return f"{year}-yil"
    return "barchasi"


def user_line(
    name: str,
    tg_id: int,
    username: Optional[str],
) -> str:
    username_text = f" (@{esc(username)})" if username else ""
    return (
        f'<a href="tg://user?id={tg_id}">{esc(name)}</a>'
        f"{username_text}"
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ALL), KeyboardButton(text=BTN_TODAY)],
            [KeyboardButton(text=BTN_YEARS), KeyboardButton(text=BTN_STATS)],
        ],
        resize_keyboard=True,
    )


async def build_list(
    year: int,
    month: int,
    day: int,
    page: int,
    order: str,
):
    total, pages, page, rows = await fetch_page(
        year, month, day, page, order
    )

    head = (
        f"📋 <b>Sharhlar — {esc(title_for(year, month, day))}</b>\n"
        f"Jami: <b>{total}</b> · Sahifa {page + 1}/{pages}\n"
        f"Tartib: {'yangi → eski' if order == 'd' else 'eski → yangi'}\n\n"
    )

    if not rows:
        body = "Bu davrda sharhlar yo'q."
    else:
        blocks = []

        for feedback in rows:
            preview = feedback.text.replace("\n", " ")
            if len(preview) > 180:
                preview = preview[:180] + "…"

            blocks.append(
                f"<b>#{feedback.id}</b> · 🕒 "
                f"{fmt_dt(feedback.created_at)}\n"
                f"👤 {user_line(feedback.student_name, feedback.student_tg_id, feedback.username)}\n"
                f"💬 {esc(preview)}\n"
                f"/s_{feedback.id}"
            )

        body = "\n\n".join(blocks)

    keyboard = InlineKeyboardBuilder()
    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=f"l:{year}:{month}:{day}:{page - 1}:{order}",
            )
        )

    navigation.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{pages}",
            callback_data="noop",
        )
    )

    if page + 1 < pages:
        navigation.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=f"l:{year}:{month}:{day}:{page + 1}:{order}",
            )
        )

    keyboard.row(*navigation)

    flip = "a" if order == "d" else "d"
    keyboard.row(
        InlineKeyboardButton(
            text="↕️ " + (
                "Eski → yangi" if order == "d" else "Yangi → eski"
            ),
            callback_data=f"l:{year}:{month}:{day}:0:{flip}",
        ),
        InlineKeyboardButton(
            text="📥 CSV (Excel)",
            callback_data=f"x:{year}:{month}:{day}",
        ),
    )

    back = f"m:{year}" if year else "y"
    keyboard.row(
        InlineKeyboardButton(
            text="🔙 Orqaga",
            callback_data=back,
        )
    )

    return head + body, keyboard.as_markup()


async def build_years():
    data = await years_summary()
    keyboard = InlineKeyboardBuilder()

    for year, count in data:
        keyboard.button(
            text=f"{year} ({count})",
            callback_data=f"m:{year}",
        )

    keyboard.adjust(3)
    keyboard.row(
        InlineKeyboardButton(
            text="📋 Hammasi",
            callback_data="l:0:0:0:0:d",
        )
    )

    text = (
        "📅 <b>Yilni tanlang</b>\n\n"
        "Yoki sanani yozing: "
        "<code>18.10.2026</code>, "
        "<code>10.2026</code> yoki <code>2026</code>"
    )

    if not data:
        text = "Hozircha sharhlar yo'q."

    return text, keyboard.as_markup()


async def build_months(year: int):
    data = await months_summary(year)
    keyboard = InlineKeyboardBuilder()

    for month, count in data:
        keyboard.button(
            text=f"{MONTHS[month - 1]} ({count})",
            callback_data=f"l:{year}:{month}:0:0:d",
        )

    keyboard.adjust(2)

    keyboard.row(
        InlineKeyboardButton(
            text=f"📋 Butun {year}-yil",
            callback_data=f"l:{year}:0:0:0:d",
        )
    )

    keyboard.row(
        InlineKeyboardButton(
            text="🔙 Yillar",
            callback_data="y",
        )
    )

    return f"🗓 <b>{year}-yil — oyni tanlang</b>", keyboard.as_markup()


async def notify_admins(bot: Bot, feedback: Feedback):
    text = (
        f"📝 <b>Yangi sharh #{feedback.id}</b>\n"
        f"👤 {user_line(feedback.student_name, feedback.student_tg_id, feedback.username)}\n"
        f"🆔 <code>{feedback.student_tg_id}</code>\n"
        f"🕒 {fmt_dt(feedback.created_at, seconds=True)}\n\n"
        f"💬 {esc(feedback.text)}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                text,
                protect_content=True,
            )
        except Exception:
            logging.exception(
                "Adminga yuborib bo'lmadi: %s", admin_id
            )


async def safe_edit(
    message: types.Message,
    text: str,
    markup,
):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


def parse_date_query(text: str):
    text = text.strip()

    try:
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", text):
            day, month, year = map(int, text.split("."))
            datetime(year, month, day)
            return year, month, day

        if re.fullmatch(r"\d{1,2}\.\d{4}", text):
            month, year = map(int, text.split("."))
            datetime(year, month, 1)
            return year, month, 0

        if re.fullmatch(r"\d{4}", text):
            year = int(text)
            if 2000 <= year <= 2100:
                return year, 0, 0

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
    await message.answer(
        f"Sizning Telegram ID: <code>{message.from_user.id}</code>",
        protect_content=True,
    )

@student.message(CommandStart())
async def student_start(
    message: types.Message,
    state: FSMContext,
):
    student_data = await get_student(message.from_user.id)

    if student_data:
        await state.clear()
        await message.answer(
            f"Assalomu alaykum, <b>{esc(student_data.full_name)}</b>!\n\n"
            "Sharhingizni yozib yuboring — u to'g'ridan-to'g'ri "
            "direktorga yetkaziladi.\n"
            "Ismni o'zgartirish: /name",
            protect_content=True,
        )
    else:
        await state.set_state(Reg.name)
        await message.answer(
            "Assalomu alaykum! 👋\n\n"
            "Bu bot orqali o'quv markazi haqidagi sharh va "
            "takliflaringizni to'g'ridan-to'g'ri direktorga "
            "yuborishingiz mumkin.\n\n"
            "Avval <b>ism-familiyangiz va guruhingizni</b> yozing.\n"
            "Masalan: <i>Aliyev Vali, 12-guruh</i>",
            protect_content=True,
        )


@student.message(Command("name"))
async def student_rename(
    message: types.Message,
    state: FSMContext,
):
    await state.set_state(Reg.name)
    await message.answer(
        "Yangi ism-familiya va guruhingizni yozing:",
        protect_content=True,
    )


@student.message(Reg.name, F.text, ~F.text.startswith("/"))
async def student_name(
    message: types.Message,
    state: FSMContext,
):
    name = " ".join(message.text.split())

    if not 3 <= len(name) <= 150:
        await message.answer(
            "Iltimos, 3 dan 150 tagacha belgidan iborat "
            "ism-familiya (va guruh) yozing.",
            protect_content=True,
        )
        return

    await save_student(
        message.from_user.id,
        name,
        message.from_user.username,
    )

    await state.clear()

    await message.answer(
        f"✅ Rahmat, <b>{esc(name)}</b>!\n\n"
        "Endi sharhingizni yozib yuboring. "
        "Har bir xabaringiz alohida sharh sifatida direktorga boradi.",
        protect_content=True,
    )


@student.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def student_feedback(
    message: types.Message,
    state: FSMContext,
):
    student_data = await get_student(message.from_user.id)

    if not student_data:
        await state.set_state(Reg.name)
        await message.answer(
            "Avval ism-familiyangiz va guruhingizni yozing:",
            protect_content=True,
        )
        return

    feedback_text = message.text.strip()

    if len(feedback_text) < MIN_LEN:
        await message.answer(
            "Sharh juda qisqa. Iltimos, batafsilroq yozing.",
            protect_content=True,
        )
        return

    if len(feedback_text) > MAX_LEN:
        await message.answer(
            f"Sharh juda uzun (maksimum {MAX_LEN} belgi). "
            "Iltimos, qisqartiring.",
            protect_content=True,
        )
        return

    current_time = time.monotonic()
    wait = COOLDOWN_SEC - (
        current_time - _last_sent.get(message.from_user.id, 0)
    )

    if wait > 0:
        await message.answer(
            f"Iltimos, {int(wait) + 1} soniyadan keyin qayta yuboring.",
            protect_content=True,
        )
        return

    _last_sent[message.from_user.id] = current_time

    feedback = await add_feedback(
        student_data,
        message.from_user.username,
        feedback_text,
    )

    await message.answer(
        "✅ Sharhingiz direktorga yuborildi. Rahmat!",
        protect_content=True,
    )

    await notify_admins(message.bot, feedback)


@student.message(StateFilter(None))
async def student_other(message: types.Message):
    await message.answer(
        "Iltimos, sharhni matn ko'rinishida yozib yuboring.",
        protect_content=True,
    )


# ==========================================================
# ADMIN PANEL — /start VA /admin IKKALASI HAM ISHLAYDI
# ==========================================================

@admin.message(CommandStart())
@admin.message(Command("admin"))
async def admin_start(message: types.Message):
    await message.answer(
        "🔐 <b>Admin-panel</b>\n\nKerakli bo'limni tanlang:",
        reply_markup=admin_menu(),
        protect_content=True,
    )


@admin.message(F.text == BTN_ALL)
async def admin_all(message: types.Message):
    text, markup = await build_list(0, 0, 0, 0, "d")
    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


@admin.message(F.text == BTN_TODAY)
async def admin_today(message: types.Message):
    today = now_local()

    text, markup = await build_list(
        today.year,
        today.month,
        today.day,
        0,
        "d",
    )

    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


@admin.message(F.text == BTN_YEARS)
async def admin_years(message: types.Message):
    text, markup = await build_years()
    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


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

    feedback = await get_feedback(int(match.group(1)))

    if feedback is None:
        await message.answer(
            "❌ Sharh topilmadi.",
            protect_content=True,
        )
        return

    text = (
        f"📝 <b>Sharh #{feedback.id}</b>\n"
        f"👤 {user_line(feedback.student_name, feedback.student_tg_id, feedback.username)}\n"
        f"🆔 <code>{feedback.student_tg_id}</code>\n"
        f"🕒 {fmt_dt(feedback.created_at, seconds=True)}\n\n"
        f"💬 {esc(feedback.text)}"
    )

    await message.answer(text, protect_content=True)


@admin.message(F.text, ~F.text.startswith("/"))
async def admin_date_search(message: types.Message):
    query = parse_date_query(message.text or "")

    if query is None:
        return

    year, month, day = query

    text, markup = await build_list(
        year,
        month,
        day,
        0,
        "d",
    )

    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


@admin.callback_query(F.data.startswith("l:"))
async def admin_list_callback(callback: types.CallbackQuery):
    try:
        _, year, month, day, page, order = (
            callback.data or ""
        ).split(":")

        year, month, day, page = map(
            int, (year, month, day, page)
        )

        if order not in {"a", "d"}:
            raise ValueError("Noto'g'ri tartib")

        text, markup = await build_list(
            year,
            month,
            day,
            page,
            order,
        )

        if callback.message:
            await safe_edit(callback.message, text, markup)

        await callback.answer()

    except (ValueError, TypeError):
        await callback.answer(
            "Parametrlarda xato.",
            show_alert=True,
        )


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
        await callback.answer(
            "Yil noto'g'ri.",
            show_alert=True,
        )


@admin.callback_query(F.data == "noop")
async def admin_noop(callback: types.CallbackQuery):
    await callback.answer()


@admin.callback_query(F.data.startswith("x:"))
async def admin_export_csv(callback: types.CallbackQuery):
    try:
        _, year, month, day = (
            callback.data or ""
        ).split(":")

        year, month, day = map(int, (year, month, day))
        rows = await fetch_all(year, month, day)

        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, delimiter=";")

        writer.writerow([
            "ID",
            "Sana",
            "Ism-familiya",
            "Telegram ID",
            "Username",
            "Sharh",
        ])

        for feedback in rows:
            writer.writerow([
                feedback.id,
                fmt_dt(feedback.created_at, seconds=True),
                feedback.student_name,
                feedback.student_tg_id,
                feedback.username or "",
                feedback.text,
            ])

        filename = f"sharhlar_{year or 'all'}_{month}_{day}.csv"

        document = BufferedInputFile(
            buffer.getvalue().encode("utf-8-sig"),
            filename=filename,
        )

        if callback.message:
            await callback.message.answer_document(
                document,
                caption=f"{title_for(year, month, day)} — {len(rows)} ta sharh",
                protect_content=True,
            )

        await callback.answer()

    except (ValueError, TypeError):
        await callback.answer(
            "Eksport parametrlarida xato.",
            show_alert=True,
        )


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not TOKEN:
        raise SystemExit(
            "BOT_TOKEN topilmadi. .env faylini tekshiring."
        )

    if not ADMIN_IDS:
        raise SystemExit(
            "ADMIN_IDS bo'sh. .env fayliga admin Telegram ID raqamini yozing."
        )

    await init_db()

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(common)
    dp.include_router(admin)
    dp.include_router(student)

    # Oddiy foydalanuvchilar uchun buyruqlar
    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash"),
        BotCommand(command="name", description="Ismni o'zgartirish"),
        BotCommand(command="id", description="Telegram ID raqamim"),
    ])

    # Admin uchun buyruqlar
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Admin panelni ochish"),
                    BotCommand(command="admin", description="Admin panel"),
                    BotCommand(command="id", description="Telegram ID raqamim"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )

        except Exception:
            logging.exception(
                "Admin buyruqlarini o'rnatib bo'lmadi: %s",
                admin_id,
            )

    me = await bot.get_me()

    print(
        f"Bot ishga tushdi: @{me.username} "
        f"(adminlar: {sorted(ADMIN_IDS)})"
    )

    try:
        await dp.start_polling(bot)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot to'xtatildi.")
