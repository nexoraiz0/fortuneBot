import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

# ---------------------------------------------------------------------------
# НАСТРОЙКИ (меняй смело под себя)
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения!")

# путь к базе данных.
# ВАЖНО: на Railway контейнер эфемерный — при каждом деплое файловая система
# пересоздаётся с нуля. Чтобы база НЕ обнулялась, нужно подключить Volume
# и указать DB_PATH внутри него (см. README.md, шаг 4).
DB_PATH = os.getenv("DB_PATH", "bot.db")

# звёзды начисляются ЗА ЛЮБОЙ прокрут (выигрышный или нет)
STARS_PER_SPIN = 2

# билет начисляется ТОЛЬКО за выигрышные комбинации (777 или BAR BAR BAR),
# звёзды за них — те же STARS_PER_SPIN, отдельно ничего доп. не даётся
TICKET_ON_WIN = 1

# минимальная пауза между прокрутами одного юзера, сек (антиспам)
SPIN_COOLDOWN = 3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("slot-bot")

# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ (sqlite)
# ---------------------------------------------------------------------------


def db_connect() -> sqlite3.Connection:
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id  INTEGER PRIMARY KEY,
                username TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spins (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                ts       INTEGER NOT NULL,
                stars    INTEGER NOT NULL DEFAULT 0,
                ticket   INTEGER NOT NULL DEFAULT 0,
                combo    TEXT
            )
            """
        )


def upsert_user(user_id: int, name: str) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO users(user_id, username) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, name),
        )


def get_last_spin_ts(user_id: int) -> float:
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT ts FROM spins WHERE user_id=? ORDER BY ts DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row[0] if row else 0


def add_spin(user_id: int, stars: int, ticket: int, combo: str) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO spins(user_id, ts, stars, ticket, combo) VALUES (?,?,?,?,?)",
            (user_id, int(time.time()), stars, ticket, combo),
        )


def week_start_ts() -> int:
    """Начало текущей недели: понедельник 00:00 по МСК (UTC+3), в unix-времени."""
    msk = timezone(timedelta(hours=3))
    now = datetime.now(msk)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(monday.timestamp())


def get_weekly_top(limit: int = 5):
    start = week_start_ts()
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT u.username,
                   COALESCE(SUM(s.stars), 0)  AS stars,
                   COUNT(s.id)                AS spins,
                   COALESCE(SUM(s.ticket), 0) AS tickets
            FROM users u
            JOIN spins s ON s.user_id = u.user_id
            WHERE s.ts >= ?
            GROUP BY u.user_id
            HAVING spins > 0
            ORDER BY stars DESC, tickets DESC, spins DESC
            LIMIT ?
            """,
            (start, limit),
        ).fetchall()
    return rows


# ---------------------------------------------------------------------------
# СЛОТ: определение комбинации по значению dice.value (1..64)
# ---------------------------------------------------------------------------
# Формула Telegram для эмодзи 🎰: три "тройки" одинаковых символов лежат на
# значениях 1 / 22 / 43 / 64:
#   1  -> BAR BAR BAR
#   22 -> виноград x3   (не награждаем)
#   43 -> лимон x3      (не награждаем)
#   64 -> 7 7 7 (джекпот)


def check_combo(value: int):
    """Возвращает название комбинации, если это 777 или BAR BAR BAR, иначе None."""
    if value == 64:
        return "777"
    if value == 1:
        return "BAR BAR BAR"
    return None


# ---------------------------------------------------------------------------
# Склонение числительных (звезда/звезды/звёзд и т.д.)
# ---------------------------------------------------------------------------


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def stars_word(n: int) -> str:
    return plural(n, "звезда", "звезды", "звёзд")


def spins_word(n: int) -> str:
    return plural(n, "прокрут", "прокрута", "прокрутов")


def tickets_word(n: int) -> str:
    return plural(n, "билет", "билета", "билетов")


# ---------------------------------------------------------------------------
# Текст топа
# ---------------------------------------------------------------------------

MEDALS = [
    '<tg-emoji emoji-id="5440539497383087970">🥇</tg-emoji>',
    '<tg-emoji emoji-id="5447203607294265305">🥈</tg-emoji>',
    '<tg-emoji emoji-id="5453902265922376865">🥉</tg-emoji>',
    '<tg-emoji emoji-id="5435882198056060129">4️⃣</tg-emoji>',
    '<tg-emoji emoji-id="5447616284931933807">5️⃣</tg-emoji>',
]

STAR_ICON_TOP3 = '<tg-emoji emoji-id="4940458991772763887">⭐️</tg-emoji>'
STAR_ICON_REST = '<tg-emoji emoji-id="5924870095925942277">⭐️</tg-emoji>'
STAR_WORD_ICON = '<tg-emoji emoji-id="4940458991772763887">⭐️</tg-emoji>'
SLOT_ICON = '<tg-emoji emoji-id="5915833712368424979">🎰</tg-emoji>'
STARSTRUCK_ICON = '<tg-emoji emoji-id="4952118595325790401">🤩</tg-emoji>'
CUP_ICON = '<tg-emoji emoji-id="5388773012478659078">🏆</tg-emoji>'


def build_top_text(rows) -> str:
    if not rows:
        return (
            f"{CUP_ICON} <b>ТОП-5 НЕДЕЛИ (МСК)</b>\n\n"
            "Пока никто не крутил слот на этой неделе 🙃"
        )

    lines = [f"{CUP_ICON} <b>ТОП-5 НЕДЕЛИ (МСК)</b>", ""]
    for i, (username, stars, spins, tickets) in enumerate(rows):
        star_icon = STAR_ICON_TOP3 if i < 3 else STAR_ICON_REST
        name = username or "Без имени"
        lines.append(f"{MEDALS[i]} {name}")
        lines.append(f"{star_icon} {stars} {STAR_WORD_ICON}{stars_word(stars)}")
        lines.append(f"{SLOT_ICON} {spins} {spins_word(spins)}")
        lines.append(f"{STARSTRUCK_ICON} {tickets} {tickets_word(tickets)}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# ХЕНДЛЕРЫ
# ---------------------------------------------------------------------------

dp = Dispatcher()


def is_top_word(message: Message) -> bool:
    return bool(message.text) and message.text.strip().lower() == "топ"


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🎰 <b>Слот-машина</b>\n\n"
        "Просто отправь в чат эмодзи-кубик 🎰 (иконка вложений → "
        "«кубик» → слот-машина) — бот сам увидит результат броска и, "
        "если выпадет <b>777</b> или <b>BAR BAR BAR</b>, начислит тебе "
        "звёзды и билет.\n\n"
        "Напиши слово <b>топ</b>, чтобы посмотреть таблицу лидеров за неделю."
    )


@dp.message(F.dice.emoji == "🎰")
async def handle_slot(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    upsert_user(user.id, user.full_name)

    last_ts = get_last_spin_ts(user.id)
    if time.time() - last_ts < SPIN_COOLDOWN:
        return  # тихо игнорируем спам-прокруты

    value = message.dice.value
    log.info("Пользователь %s (%s) выбил значение %s", user.id, user.full_name, value)

    combo = check_combo(value)
    ticket = TICKET_ON_WIN if combo else 0

    # звёзды начисляются за ЛЮБОЙ прокрут
    add_spin(user.id, STARS_PER_SPIN, ticket, combo or "-")

    if combo:
        text = (
            f"🎉 {user.full_name}, выпало <b>{combo}</b>!\n"
            f"⭐️ +{STARS_PER_SPIN} {stars_word(STARS_PER_SPIN)}\n"
            f"🎫 +{ticket} {tickets_word(ticket)}"
        )
    else:
        text = (
            f"Мимо, {user.full_name}. Попробуй ещё раз 🎰\n"
            f"⭐️ +{STARS_PER_SPIN} {stars_word(STARS_PER_SPIN)}"
        )

    await message.reply(text)


@dp.message(is_top_word)
async def cmd_top(message: Message) -> None:
    rows = get_weekly_top()
    await message.answer(build_top_text(rows))


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------


async def main() -> None:
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("База данных: %s", os.path.abspath(DB_PATH))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
