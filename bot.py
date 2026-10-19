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
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    desc,
    extract,
    func,
    inspect,
    select,
)
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
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
NEWS_MAX_LEN = 3500

_last_sent: dict[int, float] = {}

LANGS = {
    "uz": "🇺🇿 O'zbekcha",
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
}
DEFAULT_LANG = "uz"

MONTHS = {
    "uz": [
        "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
        "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr",
    ],
    "ru": [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ],
    "en": [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ],
}

WEEKDAYS = {
    "uz": [
        "Dushanba", "Seshanba", "Chorshanba", "Payshanba",
        "Juma", "Shanba", "Yakshanba",
    ],
    "ru": [
        "Понедельник", "Вторник", "Среда", "Четверг",
        "Пятница", "Суббота", "Воскресенье",
    ],
    "en": [
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    ],
}

CSV_HEAD = {
    "uz": ["ID", "Turi", "Sana", "Ism-familiya", "Guruh", "Telegram ID", "Username", "Matn"],
    "ru": ["ID", "Тип", "Дата", "Имя и фамилия", "Группа", "Telegram ID", "Username", "Текст"],
    "en": ["ID", "Type", "Date", "Full name", "Group", "Telegram ID", "Username", "Text"],
}

CSV_HEAD_PARTICIPANTS = {
    "uz": ["ID", "Ism-familiya", "Guruh", "Telegram ID", "Username", "Ro'yxatdan o'tgan vaqt"],
    "ru": ["ID", "Имя и фамилия", "Группа", "Telegram ID", "Username", "Время записи"],
    "en": ["ID", "Full name", "Group", "Telegram ID", "Username", "Joined at"],
}


def _t(uz: str, ru: str, en: str) -> dict:
    return {"uz": uz, "ru": ru, "en": en}


_CHOOSE_LANG = "🌐 Tilni tanlang / Выберите язык / Choose language"

T = {
    "choose_lang": _t(_CHOOSE_LANG, _CHOOSE_LANG, _CHOOSE_LANG),
    "lang_set": _t(
        "✅ Til o'zgartirildi: 🇺🇿 O'zbekcha",
        "✅ Язык изменён: 🇷🇺 Русский",
        "✅ Language changed: 🇬🇧 English",
    ),
    "cancelled": _t("❌ Bekor qilindi.", "❌ Отменено.", "❌ Cancelled."),
    "back": _t("🔙 Orqaga", "🔙 Назад", "🔙 Back"),
    "only_text": _t(
        "Iltimos, matn ko'rinishida yozib yuboring.",
        "Пожалуйста, отправьте текстовое сообщение.",
        "Please send a text message.",
    ),
    "tg_id": _t(
        "Sizning Telegram ID: <code>{id}</code>",
        "Ваш Telegram ID: <code>{id}</code>",
        "Your Telegram ID: <code>{id}</code>",
    ),

    "cmd_start": _t("Boshlash", "Начать", "Start"),
    "cmd_name": _t("Ismni o'zgartirish", "Изменить имя", "Change name"),
    "cmd_lang": _t("Tilni o'zgartirish", "Сменить язык", "Change language"),
    "cmd_id": _t("Telegram ID raqamim", "Мой Telegram ID", "My Telegram ID"),
    "cmd_admin_start": _t("Admin panelni ochish", "Открыть админ-панель", "Open admin panel"),
    "cmd_admin": _t("Admin panel", "Админ-панель", "Admin panel"),

    "b_feedback": _t("✍️ Sharh yozish", "✍️ Написать отзыв", "✍️ Write feedback"),
    "b_complaint": _t("⚠️ Shikoyat", "⚠️ Жалоба", "⚠️ Complaint"),
    "b_news": _t("📰 Yangiliklar", "📰 Новости", "📰 News"),
    "b_contest": _t("🏆 Musobaqa", "🏆 Соревнование", "🏆 Competition"),
    "b_name": _t("👤 Ismni o'zgartirish", "👤 Изменить имя", "👤 Change name"),
    "b_lang": _t("🌐 Til", "🌐 Язык", "🌐 Language"),
    "b_cancel": _t("❌ Bekor qilish", "❌ Отмена", "❌ Cancel"),

    "a_all": _t("📋 Barchasi", "📋 Все", "📋 All"),
    "a_feedbacks": _t("📝 Sharhlar", "📝 Отзывы", "📝 Feedback"),
    "a_complaints": _t("⚠️ Shikoyatlar", "⚠️ Жалобы", "⚠️ Complaints"),
    "a_today": _t("🗓 Bugun", "🗓 Сегодня", "🗓 Today"),
    "a_calendar": _t("📅 Yil / Oy / Kun", "📅 Год / Месяц / День", "📅 Year / Month / Day"),
    "a_weekdays": _t("📆 Hafta kunlari", "📆 Дни недели", "📆 Weekdays"),
    "a_news": _t("📰 Yangilik yuborish", "📰 Отправить новость", "📰 Send news"),
    "a_contests": _t("🏆 Musobaqalar", "🏆 Соревнования", "🏆 Competitions"),
    "a_stats": _t("📊 Statistika", "📊 Статистика", "📊 Statistics"),

    "welcome_new": _t(
        "Assalomu alaykum! 👋\n\n"
        "Bu bot orqali o'quv markazi haqidagi sharh, shikoyat va takliflaringizni "
        "to'g'ridan-to'g'ri direktorga yuborishingiz, shuningdek yangiliklar va "
        "musobaqalardan xabardor bo'lishingiz mumkin.\n\n"
        "Avval <b>ism-familiyangiz va guruhingizni</b> yozing.\n"
        "Masalan: <i>Aliyev Vali, 12-guruh</i>",
        "Здравствуйте! 👋\n\n"
        "Через этого бота вы можете отправлять отзывы, жалобы и предложения об "
        "учебном центре напрямую директору, а также следить за новостями и "
        "соревнованиями.\n\n"
        "Сначала напишите <b>имя, фамилию и группу</b>.\n"
        "Например: <i>Алиев Вали, группа 12</i>",
        "Hello! 👋\n\n"
        "With this bot you can send feedback, complaints and suggestions about the "
        "learning center straight to the director, and stay up to date with news "
        "and competitions.\n\n"
        "First, write your <b>full name and group</b>.\n"
        "For example: <i>Valiyev Ali, group 12</i>",
    ),
    "hello": _t(
        "Assalomu alaykum, <b>{name}</b>!\n\n"
        "Quyidagi menyudan bo'limni tanlang yoki sharhingizni yozib yuboring.\n"
        "Ismni o'zgartirish: /name",
        "Здравствуйте, <b>{name}</b>!\n\n"
        "Выберите раздел в меню ниже или просто напишите свой отзыв.\n"
        "Изменить имя: /name",
        "Hello, <b>{name}</b>!\n\n"
        "Choose a section from the menu below or just write your feedback.\n"
        "Change name: /name",
    ),
    "ask_name_change": _t(
        "Yangi ism-familiya va guruhingizni yozing:",
        "Напишите новое имя, фамилию и группу:",
        "Write your new full name and group:",
    ),
    "need_name": _t(
        "Avval ism-familiyangiz va guruhingizni yozing:",
        "Сначала напишите имя, фамилию и группу:",
        "First, write your full name and group:",
    ),
    "name_bad": _t(
        "Iltimos, 3 dan 150 tagacha belgidan iborat ism-familiya (va guruh) yozing.",
        "Пожалуйста, напишите имя и фамилию (и группу) длиной от 3 до 150 символов.",
        "Please write a full name (and group) between 3 and 150 characters.",
    ),
    "name_ok": _t(
        "✅ Rahmat, <b>{name}</b>!\n\n"
        "Endi menyudan bo'limni tanlang yoki sharhingizni yozib yuboring.",
        "✅ Спасибо, <b>{name}</b>!\n\n"
        "Теперь выберите раздел в меню или напишите свой отзыв.",
        "✅ Thank you, <b>{name}</b>!\n\n"
        "Now choose a section from the menu or write your feedback.",
    ),

    "write_feedback": _t(
        "✍️ Sharhingizni yozib yuboring.\n\nBekor qilish: {cancel}",
        "✍️ Напишите свой отзыв.\n\nОтмена: {cancel}",
        "✍️ Write your feedback.\n\nCancel: {cancel}",
    ),
    "write_complaint": _t(
        "⚠️ Shikoyatingizni batafsil yozib yuboring.\n\nBekor qilish: {cancel}",
        "⚠️ Подробно опишите вашу жалобу.\n\nОтмена: {cancel}",
        "⚠️ Describe your complaint in detail.\n\nCancel: {cancel}",
    ),
    "too_short": _t(
        "Xabar juda qisqa. Iltimos, batafsilroq yozing.",
        "Сообщение слишком короткое. Пожалуйста, напишите подробнее.",
        "The message is too short. Please write in more detail.",
    ),
    "too_long": _t(
        "Xabar juda uzun (maksimum {max} belgi). Iltimos, qisqartiring.",
        "Сообщение слишком длинное (максимум {max} символов). Пожалуйста, сократите.",
        "The message is too long (maximum {max} characters). Please shorten it.",
    ),
    "cooldown": _t(
        "Iltimos, {sec} soniyadan keyin qayta yuboring.",
        "Пожалуйста, отправьте снова через {sec} сек.",
        "Please try again in {sec} seconds.",
    ),
    "sent_feedback": _t(
        "✅ Sharhingiz direktorga yuborildi. Rahmat!",
        "✅ Ваш отзыв отправлен директору. Спасибо!",
        "✅ Your feedback has been sent to the director. Thank you!",
    ),
    "sent_complaint": _t(
        "✅ Shikoyatingiz direktorga yuborildi. U ko'rib chiqiladi.",
        "✅ Ваша жалоба отправлена директору. Она будет рассмотрена.",
        "✅ Your complaint has been sent to the director. It will be reviewed.",
    ),

    "news_empty": _t(
        "Hozircha yangiliklar yo'q.",
        "Пока новостей нет.",
        "No news yet.",
    ),
    "news_head": _t(
        "📰 <b>So'nggi yangiliklar</b>",
        "📰 <b>Последние новости</b>",
        "📰 <b>Latest news</b>",
    ),
    "news_item": _t(
        "🕒 <i>{date}</i>\n\n{text}",
        "🕒 <i>{date}</i>\n\n{text}",
        "🕒 <i>{date}</i>\n\n{text}",
    ),
    "news_broadcast": _t(
        "📰 <b>Yangilik</b>\n\n{text}",
        "📰 <b>Новость</b>\n\n{text}",
        "📰 <b>News</b>\n\n{text}",
    ),

    "contest_none": _t(
        "Hozircha faol musobaqalar yo'q.",
        "Сейчас нет активных соревнований.",
        "There are no active competitions right now.",
    ),
    "contest_head": _t(
        "🏆 <b>Faol musobaqalar</b>",
        "🏆 <b>Активные соревнования</b>",
        "🏆 <b>Active competitions</b>",
    ),
    "contest_card": _t(
        "🏆 <b>{title}</b>\n📅 {date}\n\n{desc}",
        "🏆 <b>{title}</b>\n📅 {date}\n\n{desc}",
        "🏆 <b>{title}</b>\n📅 {date}\n\n{desc}",
    ),
    "contest_announce": _t(
        "🏆 <b>Yangi musobaqa!</b>\n\n<b>{title}</b>\n📅 {date}\n\n{desc}",
        "🏆 <b>Новое соревнование!</b>\n\n<b>{title}</b>\n📅 {date}\n\n{desc}",
        "🏆 <b>New competition!</b>\n\n<b>{title}</b>\n📅 {date}\n\n{desc}",
    ),
    "join_btn": _t("✅ Qatnashaman", "✅ Участвую", "✅ Join"),
    "joined_mark": _t(
        "✅ Siz ro'yxatdasiz",
        "✅ Вы записаны",
        "✅ You are registered",
    ),
    "joined_ok": _t(
        "✅ Siz musobaqaga yozildingiz!",
        "✅ Вы записались на соревнование!",
        "✅ You have joined the competition!",
    ),
    "joined_already": _t(
        "Siz allaqachon ro'yxatdasiz.",
        "Вы уже записаны.",
        "You are already registered.",
    ),
    "contest_closed": _t(
        "Bu musobaqa yopilgan.",
        "Это соревнование закрыто.",
        "This competition is closed.",
    ),

    # ---------- admin: umumiy ----------
    "admin_panel": _t(
        "🔐 <b>Admin-panel</b>\n\nKerakli bo'limni tanlang:",
        "🔐 <b>Админ-панель</b>\n\nВыберите нужный раздел:",
        "🔐 <b>Admin panel</b>\n\nChoose a section:",
    ),
    "choose_section": _t("Bo'limni tanlang:", "Выберите раздел:", "Choose a section:"),
    "kind_a": _t("Barchasi", "Все обращения", "Everything"),
    "kind_f": _t("Sharhlar", "Отзывы", "Feedback"),
    "kind_c": _t("Shikoyatlar", "Жалобы", "Complaints"),
    "sec_all": _t("📋 Barchasi", "📋 Все", "📋 All"),
    "sec_today": _t("🗓 Bugun", "🗓 Сегодня", "🗓 Today"),
    "sec_cal": _t("📅 Yil / Oy / Kun", "📅 Год / Месяц / День", "📅 Year / Month / Day"),
    "sec_wd": _t("📆 Hafta kunlari", "📆 Дни недели", "📆 Weekdays"),
    "no_data": _t("Hozircha ma'lumot yo'q.", "Пока данных нет.", "No data yet."),
    "year_fmt": _t("{year}-yil", "{year} год", "Year {year}"),
    "all_time": _t("barchasi", "всё время", "all time"),
    "choose_year": _t(
        "📅 <b>Yilni tanlang</b>\n\n"
        "Yoki sanani yozing: <code>18.10.2026</code>, <code>10.2026</code> yoki <code>2026</code>",
        "📅 <b>Выберите год</b>\n\n"
        "Или напишите дату: <code>18.10.2026</code>, <code>10.2026</code> или <code>2026</code>",
        "📅 <b>Choose a year</b>\n\n"
        "Or type a date: <code>18.10.2026</code>, <code>10.2026</code> or <code>2026</code>",
    ),
    "choose_month": _t(
        "🗓 <b>{year}-yil — oyni tanlang</b>",
        "🗓 <b>{year} год — выберите месяц</b>",
        "🗓 <b>Year {year} — choose a month</b>",
    ),
    "choose_day": _t(
        "🗓 <b>{month} {year} — kunni tanlang</b>",
        "🗓 <b>{month} {year} — выберите день</b>",
        "🗓 <b>{month} {year} — choose a day</b>",
    ),
    "choose_wd": _t(
        "📆 <b>Hafta kunini tanlang</b>\n{scope}",
        "📆 <b>Выберите день недели</b>\n{scope}",
        "📆 <b>Choose a weekday</b>\n{scope}",
    ),
    "all_btn": _t("📋 Hammasi", "📋 Всё", "📋 Everything"),
    "whole_year": _t("📋 Butun {year}-yil", "📋 Весь {year} год", "📋 Whole {year}"),
    "whole_month": _t("📋 Butun {month}", "📋 Весь месяц: {month}", "📋 Whole {month}"),
    "wd_all": _t("📆 Hafta kunlari", "📆 Дни недели", "📆 Weekdays"),
    "wd_year": _t("📆 Hafta kunlari ({year})", "📆 Дни недели ({year})", "📆 Weekdays ({year})"),
    "wd_month": _t("📆 Hafta kunlari ({month})", "📆 Дни недели ({month})", "📆 Weekdays ({month})"),
    "list_info": _t(
        "Jami: <b>{total}</b> · Sahifa {page}/{pages}\nTartib: {order}",
        "Всего: <b>{total}</b> · Страница {page}/{pages}\nПорядок: {order}",
        "Total: <b>{total}</b> · Page {page}/{pages}\nOrder: {order}",
    ),
    "order_d": _t("yangi → eski", "новые → старые", "newest → oldest"),
    "order_a": _t("eski → yangi", "старые → новые", "oldest → newest"),
    "flip_a": _t("↕️ Eski → yangi", "↕️ Старые → новые", "↕️ Oldest → newest"),
    "flip_d": _t("↕️ Yangi → eski", "↕️ Новые → старые", "↕️ Newest → oldest"),
    "empty_period": _t(
        "Bu davrda yozuvlar yo'q.",
        "За этот период записей нет.",
        "No records for this period.",
    ),
    "bad_params": _t("Parametrlarda xato.", "Ошибка в параметрах.", "Invalid parameters."),
    "bad_number": _t("Raqam noto'g'ri.", "Неверный номер.", "Invalid number."),
    "not_found": _t("❌ Topilmadi.", "❌ Не найдено.", "❌ Not found."),
    "detail": _t(
        "{icon} <b>{kind} #{id}</b>\n👤 {user}\n🆔 <code>{tg}</code>\n🕒 {dt}\n\n💬 {text}",
        "{icon} <b>{kind} #{id}</b>\n👤 {user}\n🆔 <code>{tg}</code>\n🕒 {dt}\n\n💬 {text}",
        "{icon} <b>{kind} #{id}</b>\n👤 {user}\n🆔 <code>{tg}</code>\n🕒 {dt}\n\n💬 {text}",
    ),
    "new_feedback": _t(
        "📝 <b>Yangi sharh #{id}</b>",
        "📝 <b>Новый отзыв #{id}</b>",
        "📝 <b>New feedback #{id}</b>",
    ),
    "new_complaint": _t(
        "⚠️ <b>Yangi shikoyat #{id}</b>",
        "⚠️ <b>Новая жалоба #{id}</b>",
        "⚠️ <b>New complaint #{id}</b>",
    ),
    "stats": _t(
        "📊 <b>Statistika</b>\n\n"
        "📝 Sharhlar: <b>{f}</b>\n"
        "⚠️ Shikoyatlar: <b>{c}</b>\n"
        "👥 Yozgan foydalanuvchilar: <b>{authors}</b>\n"
        "🎓 Ro'yxatdan o'tganlar: <b>{students}</b>\n"
        "🗓 Bugun: <b>{today}</b>\n"
        "📅 Shu oy: <b>{month}</b>\n"
        "📰 Yangiliklar: <b>{news}</b>\n"
        "🏆 Musobaqalar: <b>{contests}</b>",
        "📊 <b>Статистика</b>\n\n"
        "📝 Отзывы: <b>{f}</b>\n"
        "⚠️ Жалобы: <b>{c}</b>\n"
        "👥 Писавшие пользователи: <b>{authors}</b>\n"
        "🎓 Зарегистрированные: <b>{students}</b>\n"
        "🗓 Сегодня: <b>{today}</b>\n"
        "📅 В этом месяце: <b>{month}</b>\n"
        "📰 Новости: <b>{news}</b>\n"
        "🏆 Соревнования: <b>{contests}</b>",
        "📊 <b>Statistics</b>\n\n"
        "📝 Feedback: <b>{f}</b>\n"
        "⚠️ Complaints: <b>{c}</b>\n"
        "👥 Users who wrote: <b>{authors}</b>\n"
        "🎓 Registered users: <b>{students}</b>\n"
        "🗓 Today: <b>{today}</b>\n"
        "📅 This month: <b>{month}</b>\n"
        "📰 News: <b>{news}</b>\n"
        "🏆 Competitions: <b>{contests}</b>",
    ),

    "news_prompt": _t(
        "📰 Yangilik matnini yuboring. U barcha o'quvchilarga jo'natiladi.",
        "📰 Отправьте текст новости. Он будет разослан всем ученикам.",
        "📰 Send the news text. It will be delivered to all students.",
    ),
    "preview_head": _t(
        "👀 <b>Ko'rib chiqing:</b>",
        "👀 <b>Предпросмотр:</b>",
        "👀 <b>Preview:</b>",
    ),
    "will_send": _t(
        "<i>{n} ta o'quvchiga yuboriladi.</i>",
        "<i>Будет отправлено ученикам: {n}.</i>",
        "<i>Will be sent to {n} students.</i>",
    ),
    "send_btn": _t("✅ Yuborish", "✅ Отправить", "✅ Send"),
    "cancel_btn": _t("❌ Bekor qilish", "❌ Отмена", "❌ Cancel"),
    "sending": _t("⏳ Yuborilmoqda...", "⏳ Отправка...", "⏳ Sending..."),
    "news_done": _t(
        "✅ Yangilik yuborildi.\nYetkazildi: <b>{ok}</b> · Yetkazilmadi: <b>{fail}</b>",
        "✅ Новость отправлена.\nДоставлено: <b>{ok}</b> · Не доставлено: <b>{fail}</b>",
        "✅ News sent.\nDelivered: <b>{ok}</b> · Failed: <b>{fail}</b>",
    ),

    # ---------- admin: musobaqa ----------
    "contests_head": _t(
        "🏆 <b>Musobaqalar</b>\n\nBirini tanlang yoki yangisini yarating:",
        "🏆 <b>Соревнования</b>\n\nВыберите одно или создайте новое:",
        "🏆 <b>Competitions</b>\n\nPick one or create a new one:",
    ),
    "new_contest_btn": _t("➕ Yangi musobaqa", "➕ Новое соревнование", "➕ New competition"),
    "contest_title_prompt": _t(
        "🏆 Musobaqa nomini yozing:",
        "🏆 Напишите название соревнования:",
        "🏆 Write the competition title:",
    ),
    "contest_desc_prompt": _t(
        "📝 Musobaqa tavsifini yozing (shartlar, mukofotlar):",
        "📝 Напишите описание соревнования (условия, призы):",
        "📝 Write the competition description (rules, prizes):",
    ),
    "contest_date_prompt": _t(
        "📅 Sana va vaqtini yozing (masalan: 25.10.2026, 15:00):",
        "📅 Напишите дату и время (например: 25.10.2026, 15:00):",
        "📅 Write the date and time (for example: 25.10.2026, 15:00):",
    ),
    "contest_created": _t(
        "✅ Musobaqa yaratildi.\nYetkazildi: <b>{ok}</b> · Yetkazilmadi: <b>{fail}</b>",
        "✅ Соревнование создано.\nДоставлено: <b>{ok}</b> · Не доставлено: <b>{fail}</b>",
        "✅ Competition created.\nDelivered: <b>{ok}</b> · Failed: <b>{fail}</b>",
    ),
    "contest_admin_card": _t(
        "🏆 <b>#{id} {title}</b>\n📅 {date}\n{status}\n👥 Ishtirokchilar: <b>{n}</b>\n\n{desc}",
        "🏆 <b>#{id} {title}</b>\n📅 {date}\n{status}\n👥 Участников: <b>{n}</b>\n\n{desc}",
        "🏆 <b>#{id} {title}</b>\n📅 {date}\n{status}\n👥 Participants: <b>{n}</b>\n\n{desc}",
    ),
    "status_active": _t("🟢 Faol", "🟢 Активно", "🟢 Active"),
    "status_closed": _t("🔴 Yopilgan", "🔴 Закрыто", "🔴 Closed"),
    "btn_participants": _t(
        "👥 Ishtirokchilar (CSV)",
        "👥 Участники (CSV)",
        "👥 Participants (CSV)",
    ),
    "btn_close": _t("🔒 Yopish", "🔒 Закрыть", "🔒 Close"),
    "btn_open": _t("🔓 Ochish", "🔓 Открыть", "🔓 Reopen"),
    "participants_caption": _t(
        "{title} — {n} ta ishtirokchi",
        "{title} — участников: {n}",
        "{title} — {n} participants",
    ),
}


def tr(lang: str, key: str, **kw) -> str:
    entry = T[key]
    value = entry.get(lang) or entry[DEFAULT_LANG]
    return value.format(**kw) if kw else value


def btn_set(key: str) -> set:
    return set(T[key].values())


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


class UserLang(Base):
    __tablename__ = "user_langs"

    tg_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    lang: Mapped[str] = mapped_column(String(2), default=DEFAULT_LANG)


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
    # "feedback" (sharh) yoki "complaint" (shikoyat)
    kind: Mapped[str] = mapped_column(String(16), default="feedback")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, index=True
    )


class News(Base):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    text: Mapped[str] = mapped_column(Text)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local, index=True
    )


class Contest(Base):
    __tablename__ = "contests"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local
    )


class ContestEntry(Base):
    __tablename__ = "contest_entries"
    __table_args__ = (
        UniqueConstraint("contest_id", "student_tg_id", name="uq_contest_student"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    contest_id: Mapped[int] = mapped_column(Integer, index=True)
    student_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    student_name: Mapped[str] = mapped_column(String(200))
    username: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_local
    )


engine = create_async_engine(DATABASE_URL)
Session = async_sessionmaker(engine, expire_on_commit=False)


def _migrate(sync_conn):
    """Eski bazaga yangi ustunlarni qo'shadi (ma'lumotlar saqlanadi)."""
    insp = inspect(sync_conn)
    columns = {c["name"] for c in insp.get_columns("feedbacks")}
    if "kind" not in columns:
        sync_conn.execute(
            sa_text(
                "ALTER TABLE feedbacks "
                "ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'feedback'"
            )
        )


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)


async def get_lang(tg_id: int) -> Optional[str]:
    async with Session() as session:
        return await session.scalar(
            select(UserLang.lang).where(UserLang.tg_id == tg_id)
        )


async def lang_of(tg_id: int) -> str:
    return await get_lang(tg_id) or DEFAULT_LANG


async def set_lang(tg_id: int, lang: str):
    async with Session() as session:
        row = await session.get(UserLang, tg_id)
        if row:
            row.lang = lang
        else:
            session.add(UserLang(tg_id=tg_id, lang=lang))
        await session.commit()

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


async def recipients() -> list[tuple[int, str]]:
    async with Session() as session:
        ids = (await session.scalars(select(Student.tg_id))).all()
        rows = (
            await session.execute(select(UserLang.tg_id, UserLang.lang))
        ).all()

    langs = {row[0]: row[1] for row in rows}
    return [
        (tg_id, langs.get(tg_id, DEFAULT_LANG))
        for tg_id in ids
        if tg_id not in ADMIN_IDS
    ]

KIND_ICON = {"a": "📋", "f": "📝", "c": "⚠️"}
KIND_DB = {"f": "feedback", "c": "complaint"}
DB_KIND_KEY = {"feedback": "kind_f", "complaint": "kind_c"}
DB_KIND_ICON = {"feedback": "📝", "complaint": "⚠️"}


async def add_feedback(
    student: Student,
    username: Optional[str],
    text: str,
    kind: str,
) -> Feedback:
    async with Session() as session:
        feedback = Feedback(
            student_tg_id=student.tg_id,
            student_name=student.full_name,
            username=username,
            text=text,
            kind=kind,
            created_at=now_local(),
        )
        session.add(feedback)
        await session.commit()
        return feedback


def _apply(query, kind: str, year: int, month: int, day: int, wd: int):
    if kind in KIND_DB:
        query = query.where(Feedback.kind == KIND_DB[kind])
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
    if wd:
        # dow: 0 = yakshanba, 1 = dushanba ... 6 = shanba
        query = query.where(
            extract("dow", Feedback.created_at) == wd % 7
        )
    return query


async def fetch_page(
    kind: str,
    year: int,
    month: int,
    day: int,
    wd: int,
    page: int,
    order: str,
):
    async with Session() as session:
        total = await session.scalar(
            _apply(
                select(func.count(Feedback.id)),
                kind, year, month, day, wd,
            )
        ) or 0

        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(max(page, 0), pages - 1)

        query = _apply(select(Feedback), kind, year, month, day, wd)

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


async def fetch_all(
    kind: str, year: int, month: int, day: int, wd: int
):
    async with Session() as session:
        query = _apply(
            select(Feedback), kind, year, month, day, wd
        ).order_by(
            Feedback.created_at.asc(),
            Feedback.id.asc(),
        )

        return (await session.scalars(query)).all()


async def years_summary(kind: str):
    async with Session() as session:
        query = _apply(
            select(
                extract("year", Feedback.created_at).label("y"),
                func.count(Feedback.id),
            ),
            kind, 0, 0, 0, 0,
        ).group_by("y").order_by(desc("y"))
        rows = (await session.execute(query)).all()
        return [(int(year), count) for year, count in rows]


async def months_summary(kind: str, year: int):
    async with Session() as session:
        query = _apply(
            select(
                extract("month", Feedback.created_at).label("mo"),
                func.count(Feedback.id),
            ),
            kind, year, 0, 0, 0,
        ).group_by("mo").order_by("mo")
        rows = (await session.execute(query)).all()
        return [(int(month), count) for month, count in rows]


async def days_summary(kind: str, year: int, month: int):
    async with Session() as session:
        query = _apply(
            select(
                extract("day", Feedback.created_at).label("dd"),
                func.count(Feedback.id),
            ),
            kind, year, month, 0, 0,
        ).group_by("dd").order_by("dd")
        rows = (await session.execute(query)).all()
        return [(int(day), count) for day, count in rows]


async def weekday_summary(kind: str, year: int, month: int) -> dict:
    async with Session() as session:
        query = _apply(
            select(
                extract("dow", Feedback.created_at).label("wd"),
                func.count(Feedback.id),
            ),
            kind, year, month, 0, 0,
        ).group_by("wd")
        rows = (await session.execute(query)).all()

    result = {}
    for dow, count in rows:
        dow = int(dow)
        result[7 if dow == 0 else dow] = count
    return result


async def get_feedback(feedback_id: int) -> Optional[Feedback]:
    async with Session() as session:
        return await session.get(Feedback, feedback_id)

async def add_news(text: str, sent_count: int) -> News:
    async with Session() as session:
        item = News(text=text, sent_count=sent_count, created_at=now_local())
        session.add(item)
        await session.commit()
        return item


async def latest_news(limit: int = 5) -> list[News]:
    async with Session() as session:
        rows = (
            await session.scalars(
                select(News).order_by(desc(News.id)).limit(limit)
            )
        ).all()
    return list(reversed(rows))

async def create_contest(title: str, description: str, starts_at: str) -> Contest:
    async with Session() as session:
        contest = Contest(
            title=title,
            description=description,
            starts_at=starts_at,
            is_active=True,
            created_at=now_local(),
        )
        session.add(contest)
        await session.commit()
        return contest


async def get_contest(contest_id: int) -> Optional[Contest]:
    async with Session() as session:
        return await session.get(Contest, contest_id)


async def active_contests() -> list[Contest]:
    async with Session() as session:
        return list(
            (
                await session.scalars(
                    select(Contest)
                    .where(Contest.is_active.is_(True))
                    .order_by(desc(Contest.id))
                    .limit(10)
                )
            ).all()
        )


async def contests_with_counts() -> list[tuple[Contest, int]]:
    async with Session() as session:
        contests = (
            await session.scalars(
                select(Contest).order_by(desc(Contest.id)).limit(20)
            )
        ).all()
        rows = (
            await session.execute(
                select(ContestEntry.contest_id, func.count(ContestEntry.id))
                .group_by(ContestEntry.contest_id)
            )
        ).all()

    counts = {row[0]: row[1] for row in rows}
    return [(c, counts.get(c.id, 0)) for c in contests]


async def contest_entries(contest_id: int) -> list[ContestEntry]:
    async with Session() as session:
        return list(
            (
                await session.scalars(
                    select(ContestEntry)
                    .where(ContestEntry.contest_id == contest_id)
                    .order_by(ContestEntry.id.asc())
                )
            ).all()
        )


async def toggle_contest(contest_id: int) -> Optional[Contest]:
    async with Session() as session:
        contest = await session.get(Contest, contest_id)
        if contest is None:
            return None
        contest.is_active = not contest.is_active
        await session.commit()
        return contest


async def has_joined(contest_id: int, tg_id: int) -> bool:
    async with Session() as session:
        found = await session.scalar(
            select(ContestEntry.id)
            .where(ContestEntry.contest_id == contest_id)
            .where(ContestEntry.student_tg_id == tg_id)
        )
        return found is not None


async def join_contest(
    contest_id: int, student: Student, username: Optional[str]
) -> str:
    async with Session() as session:
        contest = await session.get(Contest, contest_id)
        if contest is None or not contest.is_active:
            return "closed"

        exists = await session.scalar(
            select(ContestEntry.id)
            .where(ContestEntry.contest_id == contest_id)
            .where(ContestEntry.student_tg_id == student.tg_id)
        )
        if exists is not None:
            return "already"

        session.add(
            ContestEntry(
                contest_id=contest_id,
                student_tg_id=student.tg_id,
                student_name=student.full_name,
                username=username,
                created_at=now_local(),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return "already"
    return "ok"


# ---------- statistika ----------

async def stats() -> dict:
    today = now_local()

    async with Session() as session:
        f_total = await session.scalar(
            _apply(select(func.count(Feedback.id)), "f", 0, 0, 0, 0)
        ) or 0
        c_total = await session.scalar(
            _apply(select(func.count(Feedback.id)), "c", 0, 0, 0, 0)
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
                "a", today.year, today.month, today.day, 0,
            )
        ) or 0
        month_count = await session.scalar(
            _apply(
                select(func.count(Feedback.id)),
                "a", today.year, today.month, 0, 0,
            )
        ) or 0
        news = await session.scalar(select(func.count(News.id))) or 0
        contests = await session.scalar(select(func.count(Contest.id))) or 0

    return {
        "f": f_total,
        "c": c_total,
        "authors": authors,
        "students": students,
        "today": today_count,
        "month": month_count,
        "news": news,
        "contests": contests,
    }


def esc(value: Optional[str]) -> str:
    return html.escape(value or "")


def cell(value) -> str:
    """CSV/Excel formula in'ektsiyasidan himoya."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def fmt_dt(dt: datetime, seconds: bool = False) -> str:
    pattern = "%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M"
    return dt.strftime(pattern)


def title_for(lang: str, year: int, month: int, day: int, wd: int) -> str:
    parts = []

    if wd:
        parts.append(WEEKDAYS[lang][wd - 1])

    if day:
        parts.append(f"{day:02d}.{month:02d}.{year}")
    elif month:
        parts.append(f"{MONTHS[lang][month - 1]} {year}")
    elif year:
        parts.append(tr(lang, "year_fmt", year=year))

    if not parts:
        return tr(lang, "all_time")

    return " · ".join(parts)


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


def split_name_group(full_name: str) -> tuple[str, str]:
    """"Aliyev Vali, 12-guruh" -> ("Aliyev Vali", "12-guruh")."""
    value = " ".join((full_name or "").split())

    if "," in value:
        name, group = value.rsplit(",", 1)
        return name.strip(), group.strip()

    match = re.search(
        r"(\d+\s*-?\s*(guruh|группа|group)|(guruh|группа|group)\s*-?\s*\d+)",
        value,
        re.IGNORECASE,
    )
    if match:
        name = (value[:match.start()] + value[match.end():]).strip(" ,-")
        return name or value, match.group(0).strip()

    return value, ""


def make_csv(
    filename: str, header: list[str], rows: list[list]
) -> BufferedInputFile:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return BufferedInputFile(
        buffer.getvalue().encode("utf-8-sig"),
        filename=filename,
    )


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


async def safe_edit(
    message: types.Message,
    text: str,
    markup=None,
):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def send_safe(bot: Bot, chat_id: int, text: str, markup=None) -> bool:
    for _ in range(2):
        try:
            await bot.send_message(
                chat_id,
                text,
                reply_markup=markup,
                protect_content=True,
            )
            return True
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after + 1)
        except TelegramForbiddenError:
            return False
        except Exception:
            logging.exception("Xabar yuborib bo'lmadi: %s", chat_id)
            return False
    return False


async def broadcast(bot: Bot, build) -> tuple[int, int]:
    """build(lang) -> (text, markup). Barcha o'quvchilarga yuboradi."""
    ok = 0
    fail = 0

    for tg_id, lang in await recipients():
        text, markup = build(lang)
        if await send_safe(bot, tg_id, text, markup):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)

    return ok, fail


def lang_kb() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    for code, label in LANGS.items():
        keyboard.button(text=label, callback_data=f"lang:{code}")
    keyboard.adjust(1)
    return keyboard.as_markup()


def student_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=tr(lang, "b_feedback")),
                KeyboardButton(text=tr(lang, "b_complaint")),
            ],
            [
                KeyboardButton(text=tr(lang, "b_news")),
                KeyboardButton(text=tr(lang, "b_contest")),
            ],
            [
                KeyboardButton(text=tr(lang, "b_name")),
                KeyboardButton(text=tr(lang, "b_lang")),
            ],
        ],
        resize_keyboard=True,
    )


def cancel_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=tr(lang, "b_cancel"))]],
        resize_keyboard=True,
    )


def admin_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=tr(lang, "a_all")),
                KeyboardButton(text=tr(lang, "a_feedbacks")),
                KeyboardButton(text=tr(lang, "a_complaints")),
            ],
            [
                KeyboardButton(text=tr(lang, "a_today")),
                KeyboardButton(text=tr(lang, "a_calendar")),
                KeyboardButton(text=tr(lang, "a_weekdays")),
            ],
            [
                KeyboardButton(text=tr(lang, "a_news")),
                KeyboardButton(text=tr(lang, "a_contests")),
            ],
            [
                KeyboardButton(text=tr(lang, "a_stats")),
                KeyboardButton(text=tr(lang, "b_lang")),
            ],
        ],
        resize_keyboard=True,
    )


def cancel_inline(lang: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=tr(lang, "cancel_btn"), callback_data="adm:no")
    return keyboard.as_markup()


def confirm_inline(lang: str, ok_data: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text=tr(lang, "send_btn"), callback_data=ok_data)
    keyboard.button(text=tr(lang, "cancel_btn"), callback_data="adm:no")
    keyboard.adjust(2)
    return keyboard.as_markup()


def cb_list(
    kind: str, year: int, month: int, day: int, wd: int, page: int, order: str
) -> str:
    return f"l:{kind}:{year}:{month}:{day}:{wd}:{page}:{order}"


def section_text(lang: str, kind: str) -> str:
    return (
        f"{KIND_ICON[kind]} <b>{esc(tr(lang, 'kind_' + kind))}</b>\n\n"
        f"{tr(lang, 'choose_section')}"
    )


def section_kb(lang: str, kind: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=tr(lang, "sec_all"),
        callback_data=cb_list(kind, 0, 0, 0, 0, 0, "d"),
    )
    keyboard.button(text=tr(lang, "sec_today"), callback_data=f"td:{kind}")
    keyboard.button(text=tr(lang, "sec_cal"), callback_data=f"y:{kind}")
    keyboard.button(text=tr(lang, "sec_wd"), callback_data=f"w:{kind}:0:0")
    keyboard.adjust(2)
    return keyboard.as_markup()


async def build_list(
    lang: str,
    kind: str,
    year: int,
    month: int,
    day: int,
    wd: int,
    page: int,
    order: str,
):
    total, pages, page, rows = await fetch_page(
        kind, year, month, day, wd, page, order
    )

    kind_name = esc(tr(lang, "kind_" + kind))
    period = esc(title_for(lang, year, month, day, wd))
    info = tr(
        lang,
        "list_info",
        total=total,
        page=page + 1,
        pages=pages,
        order=tr(lang, "order_" + order),
    )

    head = f"{KIND_ICON[kind]} <b>{kind_name} — {period}</b>\n{info}\n\n"

    if not rows:
        body = tr(lang, "empty_period")
    else:
        blocks = []

        for feedback in rows:
            preview = feedback.text.replace("\n", " ")
            if len(preview) > 180:
                preview = preview[:180] + "…"

            icon = DB_KIND_ICON.get(feedback.kind, "📝")
            who = user_line(
                feedback.student_name,
                feedback.student_tg_id,
                feedback.username,
            )

            blocks.append(
                f"<b>{icon} #{feedback.id}</b> · 🕒 "
                f"{fmt_dt(feedback.created_at)}\n"
                f"👤 {who}\n"
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
                callback_data=cb_list(
                    kind, year, month, day, wd, page - 1, order
                ),
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
                callback_data=cb_list(
                    kind, year, month, day, wd, page + 1, order
                ),
            )
        )

    keyboard.row(*navigation)

    flip = "a" if order == "d" else "d"
    flip_text = tr(lang, "flip_a" if order == "d" else "flip_d")

    keyboard.row(
        InlineKeyboardButton(
            text=flip_text,
            callback_data=cb_list(kind, year, month, day, wd, 0, flip),
        ),
        InlineKeyboardButton(
            text="📥 CSV (Excel)",
            callback_data=f"x:{kind}:{year}:{month}:{day}:{wd}",
        ),
    )

    if wd:
        back = f"w:{kind}:{year}:{month}"
    elif day:
        back = f"d:{kind}:{year}:{month}"
    elif month:
        back = f"m:{kind}:{year}"
    elif year:
        back = f"y:{kind}"
    else:
        back = f"sec:{kind}"

    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "back"),
            callback_data=back,
        )
    )

    return head + body, keyboard.as_markup()


async def build_years(lang: str, kind: str):
    data = await years_summary(kind)
    keyboard = InlineKeyboardBuilder()

    for year, count in data:
        keyboard.button(
            text=f"{year} ({count})",
            callback_data=f"m:{kind}:{year}",
        )

    keyboard.adjust(3)

    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "all_btn"),
            callback_data=cb_list(kind, 0, 0, 0, 0, 0, "d"),
        )
    )
    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "wd_all"),
            callback_data=f"w:{kind}:0:0",
        )
    )
    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "back"),
            callback_data=f"sec:{kind}",
        )
    )

    text = tr(lang, "choose_year") if data else tr(lang, "no_data")
    return text, keyboard.as_markup()


async def build_months(lang: str, kind: str, year: int):
    data = await months_summary(kind, year)
    keyboard = InlineKeyboardBuilder()

    for month, count in data:
        keyboard.button(
            text=f"{MONTHS[lang][month - 1]} ({count})",
            callback_data=f"d:{kind}:{year}:{month}",
        )

    keyboard.adjust(2)

    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "whole_year", year=year),
            callback_data=cb_list(kind, year, 0, 0, 0, 0, "d"),
        )
    )
    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "wd_year", year=year),
            callback_data=f"w:{kind}:{year}:0",
        )
    )
    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "back"),
            callback_data=f"y:{kind}",
        )
    )

    return tr(lang, "choose_month", year=year), keyboard.as_markup()


async def build_days(lang: str, kind: str, year: int, month: int):
    data = await days_summary(kind, year, month)
    keyboard = InlineKeyboardBuilder()

    for day, count in data:
        keyboard.button(
            text=f"{day} ({count})",
            callback_data=cb_list(kind, year, month, day, 0, 0, "d"),
        )

    keyboard.adjust(4)

    month_name = MONTHS[lang][month - 1]

    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "whole_month", month=month_name),
            callback_data=cb_list(kind, year, month, 0, 0, 0, "d"),
        )
    )
    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "wd_month", month=month_name),
            callback_data=f"w:{kind}:{year}:{month}",
        )
    )
    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "back"),
            callback_data=f"m:{kind}:{year}",
        )
    )

    text = tr(lang, "choose_day", month=month_name, year=year)
    return text, keyboard.as_markup()


async def build_weekdays(lang: str, kind: str, year: int, month: int):
    data = await weekday_summary(kind, year, month)
    keyboard = InlineKeyboardBuilder()

    for number in range(1, 8):
        keyboard.button(
            text=f"{WEEKDAYS[lang][number - 1]} ({data.get(number, 0)})",
            callback_data=cb_list(kind, year, month, 0, number, 0, "d"),
        )

    keyboard.adjust(2)

    if month:
        back = f"d:{kind}:{year}:{month}"
    elif year:
        back = f"m:{kind}:{year}"
    else:
        back = f"sec:{kind}"

    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "back"),
            callback_data=back,
        )
    )

    scope = esc(title_for(lang, year, month, 0, 0))
    return tr(lang, "choose_wd", scope=scope), keyboard.as_markup()


def detail_text(lang: str, feedback: Feedback) -> str:
    kind_name = tr(lang, DB_KIND_KEY.get(feedback.kind, "kind_f"))
    return tr(
        lang,
        "detail",
        icon=DB_KIND_ICON.get(feedback.kind, "📝"),
        kind=esc(kind_name),
        id=feedback.id,
        user=user_line(
            feedback.student_name,
            feedback.student_tg_id,
            feedback.username,
        ),
        tg=feedback.student_tg_id,
        dt=fmt_dt(feedback.created_at, seconds=True),
        text=esc(feedback.text),
    )


async def notify_admins(bot: Bot, feedback: Feedback):
    key = "new_complaint" if feedback.kind == "complaint" else "new_feedback"

    for admin_id in ADMIN_IDS:
        lang = await lang_of(admin_id)
        head = tr(lang, key, id=feedback.id)
        who = user_line(
            feedback.student_name,
            feedback.student_tg_id,
            feedback.username,
        )
        text = (
            f"{head}\n"
            f"👤 {who}\n"
            f"🆔 <code>{feedback.student_tg_id}</code>\n"
            f"🕒 {fmt_dt(feedback.created_at, seconds=True)}\n\n"
            f"💬 {esc(feedback.text)}"
        )

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

def join_kb(lang: str, contest_id: int) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=tr(lang, "join_btn"),
        callback_data=f"cj:{contest_id}",
    )
    return keyboard.as_markup()


def contest_announce(lang: str, contest: Contest):
    text = tr(
        lang,
        "contest_announce",
        title=esc(contest.title),
        date=esc(contest.starts_at),
        desc=esc(contest.description),
    )
    return text, join_kb(lang, contest.id)


async def build_contest_list(lang: str):
    items = await contests_with_counts()
    keyboard = InlineKeyboardBuilder()

    for contest, count in items:
        mark = "🟢" if contest.is_active else "🔴"
        title = contest.title if len(contest.title) <= 35 else contest.title[:35] + "…"
        keyboard.button(
            text=f"{mark} {title} ({count})",
            callback_data=f"cv:{contest.id}",
        )

    keyboard.adjust(1)
    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "new_contest_btn"),
            callback_data="cn",
        )
    )

    return tr(lang, "contests_head"), keyboard.as_markup()


async def build_contest_card(lang: str, contest: Contest):
    entries = await contest_entries(contest.id)
    status = tr(lang, "status_active" if contest.is_active else "status_closed")

    text = tr(
        lang,
        "contest_admin_card",
        id=contest.id,
        title=esc(contest.title),
        date=esc(contest.starts_at),
        status=status,
        n=len(entries),
        desc=esc(contest.description),
    )

    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text=tr(lang, "btn_participants"),
        callback_data=f"cp:{contest.id}",
    )
    keyboard.button(
        text=tr(lang, "btn_close" if contest.is_active else "btn_open"),
        callback_data=f"ct:{contest.id}",
    )
    keyboard.adjust(1)
    keyboard.row(
        InlineKeyboardButton(
            text=tr(lang, "back"),
            callback_data="cl",
        )
    )

    return text, keyboard.as_markup()


common = Router()
admin = Router()
student = Router()

admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

student.message.filter(~F.from_user.id.in_(ADMIN_IDS))
student.callback_query.filter(~F.from_user.id.in_(ADMIN_IDS))


class Reg(StatesGroup):
    name = State()


class Sub(StatesGroup):
    feedback = State()
    complaint = State()


class AdminSt(StatesGroup):
    news_text = State()
    news_confirm = State()
    contest_title = State()
    contest_desc = State()
    contest_date = State()
    contest_confirm = State()


async def set_admin_commands(bot: Bot, admin_id: int, lang: str):
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description=tr(lang, "cmd_admin_start")),
                BotCommand(command="admin", description=tr(lang, "cmd_admin")),
                BotCommand(command="lang", description=tr(lang, "cmd_lang")),
                BotCommand(command="id", description=tr(lang, "cmd_id")),
            ],
            scope=BotCommandScopeChat(chat_id=admin_id),
        )
    except Exception:
        logging.exception(
            "Admin buyruqlarini o'rnatib bo'lmadi: %s", admin_id
        )

@common.message(Command("id"))
async def id_handler(message: types.Message):
    lang = await lang_of(message.from_user.id)
    await message.answer(
        tr(lang, "tg_id", id=message.from_user.id),
        protect_content=True,
    )


@common.message(Command("lang"))
@common.message(F.text.in_(btn_set("b_lang")))
async def choose_language(message: types.Message):
    await message.answer(
        tr(DEFAULT_LANG, "choose_lang"),
        reply_markup=lang_kb(),
        protect_content=True,
    )


@common.callback_query(F.data.startswith("lang:"))
async def language_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    code = (callback.data or "").split(":")[-1]

    if code not in LANGS:
        await callback.answer()
        return

    user_id = callback.from_user.id
    await set_lang(user_id, code)
    await callback.answer()

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    bot = callback.bot
    done = tr(code, "lang_set")

    if user_id in ADMIN_IDS:
        await state.clear()
        await set_admin_commands(bot, user_id, code)
        await bot.send_message(
            user_id,
            f"{done}\n\n{tr(code, 'admin_panel')}",
            reply_markup=admin_menu(code),
            protect_content=True,
        )
        return

    student_data = await get_student(user_id)

    if student_data:
        await state.clear()
        await bot.send_message(
            user_id,
            f"{done}\n\n{tr(code, 'hello', name=esc(student_data.full_name))}",
            reply_markup=student_menu(code),
            protect_content=True,
        )
    else:
        await state.set_state(Reg.name)
        await bot.send_message(
            user_id,
            f"{done}\n\n{tr(code, 'welcome_new')}",
            reply_markup=ReplyKeyboardRemove(),
            protect_content=True,
        )


@student.message(CommandStart())
async def student_start(
    message: types.Message,
    state: FSMContext,
):
    await state.clear()
    user_id = message.from_user.id
    lang = await get_lang(user_id)

    if lang is None:
        await message.answer(
            tr(DEFAULT_LANG, "choose_lang"),
            reply_markup=lang_kb(),
            protect_content=True,
        )
        return

    student_data = await get_student(user_id)

    if student_data:
        await message.answer(
            tr(lang, "hello", name=esc(student_data.full_name)),
            reply_markup=student_menu(lang),
            protect_content=True,
        )
    else:
        await state.set_state(Reg.name)
        await message.answer(
            tr(lang, "welcome_new"),
            reply_markup=ReplyKeyboardRemove(),
            protect_content=True,
        )


@student.message(F.text.in_(btn_set("b_cancel")))
async def student_cancel(
    message: types.Message,
    state: FSMContext,
):
    await state.clear()
    lang = await lang_of(message.from_user.id)
    await message.answer(
        tr(lang, "cancelled"),
        reply_markup=student_menu(lang),
        protect_content=True,
    )


@student.message(Command("name"))
@student.message(F.text.in_(btn_set("b_name")))
async def student_rename(
    message: types.Message,
    state: FSMContext,
):
    lang = await lang_of(message.from_user.id)
    await state.set_state(Reg.name)
    await message.answer(
        tr(lang, "ask_name_change"),
        reply_markup=cancel_kb(lang),
        protect_content=True,
    )


async def start_compose(
    message: types.Message,
    state: FSMContext,
    target_state: State,
    key: str,
):
    user_id = message.from_user.id
    lang = await lang_of(user_id)

    if not await get_student(user_id):
        await state.set_state(Reg.name)
        await message.answer(
            tr(lang, "need_name"),
            reply_markup=ReplyKeyboardRemove(),
            protect_content=True,
        )
        return

    await state.set_state(target_state)
    await message.answer(
        tr(lang, key, cancel=tr(lang, "b_cancel")),
        reply_markup=cancel_kb(lang),
        protect_content=True,
    )


@student.message(F.text.in_(btn_set("b_feedback")))
async def student_feedback_button(
    message: types.Message,
    state: FSMContext,
):
    await start_compose(message, state, Sub.feedback, "write_feedback")


@student.message(F.text.in_(btn_set("b_complaint")))
async def student_complaint_button(
    message: types.Message,
    state: FSMContext,
):
    await start_compose(message, state, Sub.complaint, "write_complaint")


@student.message(F.text.in_(btn_set("b_news")))
async def student_news(
    message: types.Message,
    state: FSMContext,
):
    await state.clear()
    lang = await lang_of(message.from_user.id)
    items = await latest_news(5)

    if not items:
        await message.answer(
            tr(lang, "news_empty"),
            reply_markup=student_menu(lang),
            protect_content=True,
        )
        return

    await message.answer(
        tr(lang, "news_head"),
        reply_markup=student_menu(lang),
        protect_content=True,
    )

    for item in items:
        await message.answer(
            tr(lang, "news_item", date=fmt_dt(item.created_at), text=item.text),
            protect_content=True,
        )


@student.message(F.text.in_(btn_set("b_contest")))
async def student_contests(
    message: types.Message,
    state: FSMContext,
):
    await state.clear()
    user_id = message.from_user.id
    lang = await lang_of(user_id)
    items = await active_contests()

    if not items:
        await message.answer(
            tr(lang, "contest_none"),
            reply_markup=student_menu(lang),
            protect_content=True,
        )
        return

    await message.answer(
        tr(lang, "contest_head"),
        reply_markup=student_menu(lang),
        protect_content=True,
    )

    for contest in items:
        card = tr(
            lang,
            "contest_card",
            title=esc(contest.title),
            date=esc(contest.starts_at),
            desc=esc(contest.description),
        )

        if await has_joined(contest.id, user_id):
            await message.answer(
                f"{card}\n\n{tr(lang, 'joined_mark')}",
                protect_content=True,
            )
        else:
            await message.answer(
                card,
                reply_markup=join_kb(lang, contest.id),
                protect_content=True,
            )


@student.callback_query(F.data.startswith("cj:"))
async def student_join_contest(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    lang = await lang_of(user_id)

    student_data = await get_student(user_id)
    if not student_data:
        await callback.answer(tr(lang, "need_name"), show_alert=True)
        return

    try:
        contest_id = int((callback.data or "").split(":")[1])
    except (ValueError, IndexError):
        await callback.answer(tr(lang, "bad_params"), show_alert=True)
        return

    result = await join_contest(
        contest_id, student_data, callback.from_user.username
    )

    if result == "ok":
        await callback.answer(tr(lang, "joined_ok"), show_alert=True)
    elif result == "already":
        await callback.answer(tr(lang, "joined_already"), show_alert=True)
    else:
        await callback.answer(tr(lang, "contest_closed"), show_alert=True)

    if result in {"ok", "already"} and callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@student.message(Reg.name, F.text, ~F.text.startswith("/"))
async def student_name(
    message: types.Message,
    state: FSMContext,
):
    lang = await lang_of(message.from_user.id)
    name = " ".join(message.text.split())

    if not 3 <= len(name) <= 150:
        await message.answer(
            tr(lang, "name_bad"),
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
        tr(lang, "name_ok", name=esc(name)),
        reply_markup=student_menu(lang),
        protect_content=True,
    )


async def submit_message(
    message: types.Message,
    state: FSMContext,
    kind: str,
):
    user_id = message.from_user.id
    lang = await lang_of(user_id)
    student_data = await get_student(user_id)

    if not student_data:
        await state.set_state(Reg.name)
        await message.answer(
            tr(lang, "need_name"),
            reply_markup=ReplyKeyboardRemove(),
            protect_content=True,
        )
        return

    body = message.text.strip()

    if len(body) < MIN_LEN:
        await message.answer(
            tr(lang, "too_short"),
            protect_content=True,
        )
        return

    if len(body) > MAX_LEN:
        await message.answer(
            tr(lang, "too_long", max=MAX_LEN),
            protect_content=True,
        )
        return

    current_time = time.monotonic()
    wait = COOLDOWN_SEC - (
        current_time - _last_sent.get(user_id, 0)
    )

    if wait > 0:
        await message.answer(
            tr(lang, "cooldown", sec=int(wait) + 1),
            protect_content=True,
        )
        return

    _last_sent[user_id] = current_time

    feedback = await add_feedback(
        student_data,
        message.from_user.username,
        body,
        kind,
    )

    await state.clear()

    await message.answer(
        tr(lang, "sent_" + kind),
        reply_markup=student_menu(lang),
        protect_content=True,
    )

    await notify_admins(message.bot, feedback)


@student.message(Sub.feedback, F.text, ~F.text.startswith("/"))
async def student_send_feedback(
    message: types.Message,
    state: FSMContext,
):
    await submit_message(message, state, "feedback")


@student.message(Sub.complaint, F.text, ~F.text.startswith("/"))
async def student_send_complaint(
    message: types.Message,
    state: FSMContext,
):
    await submit_message(message, state, "complaint")


@student.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def student_plain_text(
    message: types.Message,
    state: FSMContext,
):
    await submit_message(message, state, "feedback")


@student.message()
async def student_other(message: types.Message):
    lang = await lang_of(message.from_user.id)
    await message.answer(
        tr(lang, "only_text"),
        protect_content=True,
    )

@admin.message(CommandStart())
@admin.message(Command("admin"))
async def admin_start(
    message: types.Message,
    state: FSMContext,
):
    await state.clear()
    user_id = message.from_user.id
    lang = await get_lang(user_id)

    if lang is None:
        await message.answer(
            tr(DEFAULT_LANG, "choose_lang"),
            reply_markup=lang_kb(),
            protect_content=True,
        )
        return

    await message.answer(
        tr(lang, "admin_panel"),
        reply_markup=admin_menu(lang),
        protect_content=True,
    )


async def send_section(message: types.Message, state: FSMContext, kind: str):
    await state.clear()
    lang = await lang_of(message.from_user.id)
    await message.answer(
        section_text(lang, kind),
        reply_markup=section_kb(lang, kind),
        protect_content=True,
    )


@admin.message(F.text.in_(btn_set("a_all")))
async def admin_all(message: types.Message, state: FSMContext):
    await send_section(message, state, "a")


@admin.message(F.text.in_(btn_set("a_feedbacks")))
async def admin_feedbacks(message: types.Message, state: FSMContext):
    await send_section(message, state, "f")


@admin.message(F.text.in_(btn_set("a_complaints")))
async def admin_complaints(message: types.Message, state: FSMContext):
    await send_section(message, state, "c")


@admin.message(F.text.in_(btn_set("a_today")))
async def admin_today(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await lang_of(message.from_user.id)
    today = now_local()

    text, markup = await build_list(
        lang, "a", today.year, today.month, today.day, 0, 0, "d"
    )

    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


@admin.message(F.text.in_(btn_set("a_calendar")))
async def admin_calendar(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await lang_of(message.from_user.id)
    text, markup = await build_years(lang, "a")
    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


@admin.message(F.text.in_(btn_set("a_weekdays")))
async def admin_weekdays(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await lang_of(message.from_user.id)
    text, markup = await build_weekdays(lang, "a", 0, 0)
    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


@admin.message(F.text.in_(btn_set("a_stats")))
async def admin_stats(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await lang_of(message.from_user.id)
    data = await stats()
    await message.answer(
        tr(lang, "stats", **data),
        protect_content=True,
    )


# ---------- admin: yangilik yuborish ----------

@admin.message(F.text.in_(btn_set("a_news")))
async def admin_news(message: types.Message, state: FSMContext):
    lang = await lang_of(message.from_user.id)
    await state.set_state(AdminSt.news_text)
    await message.answer(
        tr(lang, "news_prompt"),
        reply_markup=cancel_inline(lang),
        protect_content=True,
    )


@admin.message(AdminSt.news_text, F.text)
async def admin_news_text(message: types.Message, state: FSMContext):
    lang = await lang_of(message.from_user.id)
    body = message.html_text

    if len(message.text) > NEWS_MAX_LEN:
        await message.answer(
            tr(lang, "too_long", max=NEWS_MAX_LEN),
            protect_content=True,
        )
        return

    await state.update_data(news=body)
    await state.set_state(AdminSt.news_confirm)

    count = len(await recipients())
    preview = (
        f"{tr(lang, 'preview_head')}\n\n"
        f"{tr(lang, 'news_broadcast', text=body)}\n\n"
        f"{tr(lang, 'will_send', n=count)}"
    )

    await message.answer(
        preview,
        reply_markup=confirm_inline(lang, "nw:ok"),
        protect_content=True,
    )


@admin.callback_query(F.data == "nw:ok", AdminSt.news_confirm)
async def admin_news_send(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    lang = await lang_of(callback.from_user.id)
    data = await state.get_data()
    body = data.get("news")
    await state.clear()
    await callback.answer()

    if not body or not callback.message:
        return

    await safe_edit(callback.message, tr(lang, "sending"), None)

    ok, fail = await broadcast(
        callback.bot,
        lambda student_lang: (
            tr(student_lang, "news_broadcast", text=body),
            None,
        ),
    )

    await add_news(body, ok)

    await safe_edit(
        callback.message,
        tr(lang, "news_done", ok=ok, fail=fail),
        None,
    )


@admin.callback_query(F.data == "adm:no")
async def admin_cancel_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    lang = await lang_of(callback.from_user.id)
    await state.clear()
    await callback.answer()

    if callback.message:
        await safe_edit(callback.message, tr(lang, "cancelled"), None)


@admin.message(F.text.in_(btn_set("a_contests")))
async def admin_contests(message: types.Message, state: FSMContext):
    await state.clear()
    lang = await lang_of(message.from_user.id)
    text, markup = await build_contest_list(lang)
    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


@admin.callback_query(F.data == "cl")
async def admin_contest_list_callback(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)
    text, markup = await build_contest_list(lang)

    if callback.message:
        await safe_edit(callback.message, text, markup)

    await callback.answer()


@admin.callback_query(F.data == "cn")
async def admin_contest_new(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    lang = await lang_of(callback.from_user.id)
    await state.set_state(AdminSt.contest_title)
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            tr(lang, "contest_title_prompt"),
            reply_markup=cancel_inline(lang),
            protect_content=True,
        )


@admin.message(AdminSt.contest_title, F.text)
async def admin_contest_title(message: types.Message, state: FSMContext):
    lang = await lang_of(message.from_user.id)
    title = " ".join(message.text.split())

    if not 3 <= len(title) <= 200:
        await message.answer(
            tr(lang, "name_bad"),
            protect_content=True,
        )
        return

    await state.update_data(title=title)
    await state.set_state(AdminSt.contest_desc)
    await message.answer(
        tr(lang, "contest_desc_prompt"),
        reply_markup=cancel_inline(lang),
        protect_content=True,
    )


@admin.message(AdminSt.contest_desc, F.text)
async def admin_contest_desc(message: types.Message, state: FSMContext):
    lang = await lang_of(message.from_user.id)
    description = message.text.strip()

    if len(description) > 2500:
        await message.answer(
            tr(lang, "too_long", max=2500),
            protect_content=True,
        )
        return

    await state.update_data(desc=description)
    await state.set_state(AdminSt.contest_date)
    await message.answer(
        tr(lang, "contest_date_prompt"),
        reply_markup=cancel_inline(lang),
        protect_content=True,
    )


@admin.message(AdminSt.contest_date, F.text)
async def admin_contest_date(message: types.Message, state: FSMContext):
    lang = await lang_of(message.from_user.id)
    starts_at = " ".join(message.text.split())[:100]

    await state.update_data(date=starts_at)
    await state.set_state(AdminSt.contest_confirm)

    data = await state.get_data()
    count = len(await recipients())

    card = tr(
        lang,
        "contest_card",
        title=esc(data["title"]),
        date=esc(starts_at),
        desc=esc(data["desc"]),
    )

    await message.answer(
        f"{tr(lang, 'preview_head')}\n\n{card}\n\n"
        f"{tr(lang, 'will_send', n=count)}",
        reply_markup=confirm_inline(lang, "cc:ok"),
        protect_content=True,
    )


@admin.callback_query(F.data == "cc:ok", AdminSt.contest_confirm)
async def admin_contest_create(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    lang = await lang_of(callback.from_user.id)
    data = await state.get_data()
    await state.clear()
    await callback.answer()

    if not data or not callback.message:
        return

    contest = await create_contest(
        data["title"], data["desc"], data["date"]
    )

    await safe_edit(callback.message, tr(lang, "sending"), None)

    ok, fail = await broadcast(
        callback.bot,
        lambda student_lang: contest_announce(student_lang, contest),
    )

    await safe_edit(
        callback.message,
        tr(lang, "contest_created", ok=ok, fail=fail),
        None,
    )


@admin.callback_query(F.data.startswith("cv:"))
async def admin_contest_view(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)

    try:
        contest_id = int((callback.data or "").split(":")[1])
    except (ValueError, IndexError):
        await callback.answer(tr(lang, "bad_params"), show_alert=True)
        return

    contest = await get_contest(contest_id)

    if contest is None:
        await callback.answer(tr(lang, "not_found"), show_alert=True)
        return

    text, markup = await build_contest_card(lang, contest)

    if callback.message:
        await safe_edit(callback.message, text, markup)

    await callback.answer()


@admin.callback_query(F.data.startswith("ct:"))
async def admin_contest_toggle(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)

    try:
        contest_id = int((callback.data or "").split(":")[1])
    except (ValueError, IndexError):
        await callback.answer(tr(lang, "bad_params"), show_alert=True)
        return

    contest = await toggle_contest(contest_id)

    if contest is None:
        await callback.answer(tr(lang, "not_found"), show_alert=True)
        return

    text, markup = await build_contest_card(lang, contest)

    if callback.message:
        await safe_edit(callback.message, text, markup)

    await callback.answer()


@admin.callback_query(F.data.startswith("cp:"))
async def admin_contest_participants(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)

    try:
        contest_id = int((callback.data or "").split(":")[1])
    except (ValueError, IndexError):
        await callback.answer(tr(lang, "bad_params"), show_alert=True)
        return

    contest = await get_contest(contest_id)

    if contest is None:
        await callback.answer(tr(lang, "not_found"), show_alert=True)
        return

    entries = await contest_entries(contest_id)

    document = make_csv(
        f"contest_{contest_id}.csv",
        CSV_HEAD_PARTICIPANTS[lang],
        [
            [
                entry.id,
                cell(split_name_group(entry.student_name)[0]),
                cell(split_name_group(entry.student_name)[1]),
                entry.student_tg_id,
                cell(entry.username),
                fmt_dt(entry.created_at, seconds=True),
            ]
            for entry in entries
        ],
    )

    if callback.message:
        await callback.message.answer_document(
            document,
            caption=tr(
                lang,
                "participants_caption",
                title=esc(contest.title),
                n=len(entries),
            ),
            protect_content=True,
        )

    await callback.answer()


@admin.message(F.text.startswith("/s_"))
async def admin_feedback_detail(message: types.Message):
    lang = await lang_of(message.from_user.id)
    match = re.fullmatch(r"/s_(\d+)", (message.text or "").strip())

    if not match:
        await message.answer(tr(lang, "bad_number"))
        return

    feedback = await get_feedback(int(match.group(1)))

    if feedback is None:
        await message.answer(
            tr(lang, "not_found"),
            protect_content=True,
        )
        return

    await message.answer(
        detail_text(lang, feedback),
        protect_content=True,
    )


@admin.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def admin_date_search(message: types.Message):
    query = parse_date_query(message.text or "")

    if query is None:
        return

    lang = await lang_of(message.from_user.id)
    year, month, day = query

    text, markup = await build_list(
        lang, "a", year, month, day, 0, 0, "d"
    )

    await message.answer(
        text,
        reply_markup=markup,
        protect_content=True,
    )


# ---------- admin: callbacklar (ro'yxat, yil, oy, kun, hafta kuni) ----------

@admin.callback_query(F.data.startswith("l:"))
async def admin_list_callback(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)

    try:
        _, kind, year, month, day, wd, page, order = (
            callback.data or ""
        ).split(":")

        year, month, day, wd, page = map(
            int, (year, month, day, wd, page)
        )

        if order not in {"a", "d"} or kind not in KIND_ICON:
            raise ValueError("Noto'g'ri parametr")

        text, markup = await build_list(
            lang, kind, year, month, day, wd, page, order
        )

        if callback.message:
            await safe_edit(callback.message, text, markup)

        await callback.answer()

    except (ValueError, TypeError):
        await callback.answer(
            tr(lang, "bad_params"),
            show_alert=True,
        )


@admin.callback_query(F.data.startswith("sec:"))
async def admin_section_callback(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)
    kind = (callback.data or "").split(":")[-1]

    if kind not in KIND_ICON:
        await callback.answer(tr(lang, "bad_params"), show_alert=True)
        return

    if callback.message:
        await safe_edit(
            callback.message,
            section_text(lang, kind),
            section_kb(lang, kind),
        )

    await callback.answer()


@admin.callback_query(F.data.startswith("td:"))
async def admin_today_callback(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)
    kind = (callback.data or "").split(":")[-1]

    if kind not in KIND_ICON:
        await callback.answer(tr(lang, "bad_params"), show_alert=True)
        return

    today = now_local()
    text, markup = await build_list(
        lang, kind, today.year, today.month, today.day, 0, 0, "d"
    )

    if callback.message:
        await safe_edit(callback.message, text, markup)

    await callback.answer()


@admin.callback_query(F.data.startswith("y:"))
async def admin_years_callback(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)
    kind = (callback.data or "").split(":")[-1]

    if kind not in KIND_ICON:
        await callback.answer(tr(lang, "bad_params"), show_alert=True)
        return

    text, markup = await build_years(lang, kind)

    if callback.message:
        await safe_edit(callback.message, text, markup)

    await callback.answer()


@admin.callback_query(F.data.startswith("m:"))
async def admin_months_callback(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)

    try:
        _, kind, year = (callback.data or "").split(":")
        year = int(year)

        if kind not in KIND_ICON:
            raise ValueError("Noto'g'ri tur")

        text, markup = await build_months(lang, kind, year)

        if callback.message:
            await safe_edit(callback.message, text, markup)

        await callback.answer()

    except (ValueError, TypeError):
        await callback.answer(
            tr(lang, "bad_params"),
            show_alert=True,
        )


@admin.callback_query(F.data.startswith("d:"))
async def admin_days_callback(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)

    try:
        _, kind, year, month = (callback.data or "").split(":")
        year, month = int(year), int(month)

        if kind not in KIND_ICON or not 1 <= month <= 12:
            raise ValueError("Noto'g'ri parametr")

        text, markup = await build_days(lang, kind, year, month)

        if callback.message:
            await safe_edit(callback.message, text, markup)

        await callback.answer()

    except (ValueError, TypeError):
        await callback.answer(
            tr(lang, "bad_params"),
            show_alert=True,
        )


@admin.callback_query(F.data.startswith("w:"))
async def admin_weekdays_callback(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)

    try:
        _, kind, year, month = (callback.data or "").split(":")
        year, month = int(year), int(month)

        if kind not in KIND_ICON:
            raise ValueError("Noto'g'ri tur")

        text, markup = await build_weekdays(lang, kind, year, month)

        if callback.message:
            await safe_edit(callback.message, text, markup)

        await callback.answer()

    except (ValueError, TypeError):
        await callback.answer(
            tr(lang, "bad_params"),
            show_alert=True,
        )


@admin.callback_query(F.data == "noop")
async def admin_noop(callback: types.CallbackQuery):
    await callback.answer()


@admin.callback_query(F.data.startswith("x:"))
async def admin_export_csv(callback: types.CallbackQuery):
    lang = await lang_of(callback.from_user.id)

    try:
        _, kind, year, month, day, wd = (
            callback.data or ""
        ).split(":")

        year, month, day, wd = map(int, (year, month, day, wd))

        if kind not in KIND_ICON:
            raise ValueError("Noto'g'ri tur")

        rows = await fetch_all(kind, year, month, day, wd)

        data_rows = [
            [
                feedback.id,
                tr(lang, DB_KIND_KEY.get(feedback.kind, "kind_f")),
                fmt_dt(feedback.created_at, seconds=True),
                cell(split_name_group(feedback.student_name)[0]),
                cell(split_name_group(feedback.student_name)[1]),
                feedback.student_tg_id,
                cell(feedback.username),
                cell(feedback.text),
            ]
            for feedback in rows
        ]

        filename = f"export_{kind}_{year or 'all'}_{month}_{day}_{wd}.csv"
        document = make_csv(filename, CSV_HEAD[lang], data_rows)

        caption = (
            f"{title_for(lang, year, month, day, wd)} — {len(rows)}"
        )

        if callback.message:
            await callback.message.answer_document(
                document,
                caption=caption,
                protect_content=True,
            )

        await callback.answer()

    except (ValueError, TypeError):
        await callback.answer(
            tr(lang, "bad_params"),
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

    def student_commands(code: str) -> list[BotCommand]:
        return [
            BotCommand(command="start", description=tr(code, "cmd_start")),
            BotCommand(command="name", description=tr(code, "cmd_name")),
            BotCommand(command="lang", description=tr(code, "cmd_lang")),
            BotCommand(command="id", description=tr(code, "cmd_id")),
        ]

    try:
        await bot.set_my_commands(student_commands(DEFAULT_LANG))

        for code in LANGS:
            await bot.set_my_commands(
                student_commands(code),
                language_code=code,
            )
    except Exception:
        logging.exception("Buyruqlarni o'rnatib bo'lmadi")

    for admin_id in ADMIN_IDS:
        await set_admin_commands(bot, admin_id, await lang_of(admin_id))

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
    except KeyboardInterrupt:
        print("Bot to'xtatildi.")
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"Xatolik: {e.code}")
        else:
            print("Bot to'xtatildi.")