import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import escape as html_escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
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
# и указать DB_PATH внутри него.
DB_PATH = os.getenv("DB_PATH", "bot.db")

# звёзды начисляются ЗА ЛЮБОЙ прокрут (выигрышный или нет)
STARS_PER_SPIN = 2

# билет начисляется ТОЛЬКО за выигрышные комбинации (777 или BAR BAR BAR)
TICKET_ON_WIN = 1

# сколько дней длится один турнир
TOURNAMENT_DAYS = 7

# username'ы (без @, регистр не важен), чьи прокруты НЕ учитываются в топе
ADMIN_USERNAMES = {"raivens1", "nexoraizfuck", "mtl_sr"}

# призы за места — просто текст, меняй как нужно
PRIZE_1 = "Эксклюзивный NFT 🎁"
PRIZE_2 = "100"
PRIZE_3 = "50"

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
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


def add_spin(user_id: int, stars: int, ticket: int, combo: str) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO spins(user_id, ts, stars, ticket, combo) VALUES (?,?,?,?,?)",
            (user_id, int(time.time()), stars, ticket, combo),
        )


def get_setting(key: str):
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_setting(key: str, value) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# ---------------------------------------------------------------------------
# ТУРНИР
# ---------------------------------------------------------------------------


def start_tournament() -> int:
    now = int(time.time())
    set_setting("tournament_start", now)
    set_setting("tournament_end", "")     # сбрасываем время завершения
    set_setting("tournament_active", 1)
    return now


def end_tournament() -> None:
    set_setting("tournament_end", int(time.time()))
    set_setting("tournament_active", 0)


def get_tournament_state():
    """Возвращает (start_ts | None, end_ts | None, active: bool)."""
    start = get_setting("tournament_start")
    end = get_setting("tournament_end")
    active = get_setting("tournament_active")
    start_ts = int(start) if start else None
    end_ts = int(end) if end else None
    is_active = bool(active) and active == "1"
    return start_ts, end_ts, is_active


def get_leaderboard(limit: int = 3):
    start_ts, end_ts, _active = get_tournament_state()
    if start_ts is None:
        return []
    window_end = end_ts if end_ts else int(time.time())

    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT u.user_id, u.username,
                   COALESCE(SUM(s.stars), 0)  AS stars,
                   COUNT(s.id)                AS spins,
                   COALESCE(SUM(s.ticket), 0) AS tickets
            FROM users u
            JOIN spins s ON s.user_id = u.user_id
            WHERE s.ts >= ? AND s.ts <= ?
            GROUP BY u.user_id
            HAVING spins > 0
            ORDER BY stars DESC, tickets DESC, spins DESC
            LIMIT ?
            """,
            (start_ts, window_end, limit),
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


def is_admin(user) -> bool:
    if not user or not user.username:
        return False
    return user.username.lstrip("@").lower() in ADMIN_USERNAMES


# ---------------------------------------------------------------------------
# Склонение числительных
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


def days_word(n: int) -> str:
    return plural(n, "день", "дня", "дней")


def hours_word(n: int) -> str:
    return plural(n, "час", "часа", "часов")


# ---------------------------------------------------------------------------
# Текст топа
# ---------------------------------------------------------------------------

FIRE_ICON = '<tg-emoji emoji-id="5404425823220408230">🔥</tg-emoji>'
TEDDY_ICON = '<tg-emoji emoji-id="4940729024956598305">🧸</tg-emoji>'
STARSTRUCK_HEADER_ICON = '<tg-emoji emoji-id="4940581505714882819">🤩</tg-emoji>'
CROWN_ICON = '<tg-emoji emoji-id="5217822164362739968">👑</tg-emoji>'
CUP_PRIZE_ICON = '<tg-emoji emoji-id="5469967260380612012">🏆</tg-emoji>'
ROCKET_ICON = '<tg-emoji emoji-id="5145427681680032825">🚀</tg-emoji>'
STAR_ICON = '<tg-emoji emoji-id="5924870095925942277">⭐️</tg-emoji>'
SEVEN_ICON = '<tg-emoji emoji-id="5443135830883313930">7️⃣</tg-emoji>'
BAR_ICON = '<tg-emoji emoji-id="5388681323516821572">🍫</tg-emoji>'
CHART_ICON = '<tg-emoji emoji-id="5028746137645876535">📈</tg-emoji>'
SLOT_ICON = '<tg-emoji emoji-id="5915833712368424979">🎰</tg-emoji>'
TICKET_ICON = '<tg-emoji emoji-id="4952118595325790401">🤩</tg-emoji>'
HOURGLASS_ICON = '<tg-emoji emoji-id="5213452215527677338">⏳</tg-emoji>'

MEDALS = [
    '<tg-emoji emoji-id="5440539497383087970">🥇</tg-emoji>',
    '<tg-emoji emoji-id="5447203607294265305">🥈</tg-emoji>',
    '<tg-emoji emoji-id="5453902265922376865">🥉</tg-emoji>',
]


def user_link(user_id: int, name: str) -> str:
    safe_name = html_escape(name or "Без имени")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def build_time_left_line() -> str:
    start_ts, end_ts, active = get_tournament_state()

    if start_ts is None:
        return f"{HOURGLASS_ICON} Турнир ещё не запущен. Ждите объявления от администрации!"

    if not active:
        return f"{HOURGLASS_ICON} Турнир завершён! Ждите начала нового 🏁"

    target_end = start_ts + TOURNAMENT_DAYS * 86400
    remaining = target_end - int(time.time())
    if remaining <= 0:
        return f"{HOURGLASS_ICON} Рейтинг обновляется. Турнир вот-вот завершится!"

    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    if days > 0:
        left = f"{days} {days_word(days)} {hours} {hours_word(hours)}"
    else:
        minutes = (remaining % 3600) // 60
        if hours > 0:
            left = f"{hours} {hours_word(hours)} {minutes} мин"
        else:
            left = f"{minutes} мин"
    return f"{HOURGLASS_ICON} Рейтинг обновляется. До конца турнира осталось: {left}!"


def build_top_text() -> str:
    rows = get_leaderboard(limit=3)

    lines = [
        f"{FIRE_ICON} Встречайте ТОП пользователей за эту неделю!",
        "",
        f"{TEDDY_ICON}Испытай свою удачу, вырвись в лидеры и забирай крутые "
        f"призы в соответствии со своим местом:{STARSTRUCK_HEADER_ICON}",
        "",
        f"{CROWN_ICON} 1-е место — {PRIZE_1}",
        f"{CUP_PRIZE_ICON} 2-е место — {PRIZE_2}",
        f"{ROCKET_ICON} 3-е место — {PRIZE_3}",
        "",
        f"{STAR_ICON} Напоминаем: за каждые {SEVEN_ICON}{SEVEN_ICON}{SEVEN_ICON} и "
        f"{BAR_ICON}{BAR_ICON}{BAR_ICON} начисляются билеты. "
        f"Чем больше билетов, тем выше твое место!",
        "",
        f"{CHART_ICON} Текущий рейтинг лидеров:",
    ]

    if not rows:
        lines.append("Пока никто не крутил слот 🙃")
    else:
        for i, (user_id, username, stars, spins, tickets) in enumerate(rows):
            lines.append(f"{MEDALS[i]} {user_link(user_id, username)}")
            lines.append(
                f"{STAR_ICON} {stars} {stars_word(stars)} | "
                f"{SLOT_ICON} {spins} {spins_word(spins)} | "
                f"{TICKET_ICON} {tickets} {tickets_word(tickets)}"
            )
            lines.append("")

    lines.append(build_time_left_line())
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
        "Просто отправь в чат эмодзи-кубик 🎰 (иконка вложений → «кубик» → "
        "слот-машина) — бот сам увидит результат броска и, если выпадет "
        "<b>777</b> или <b>BAR BAR BAR</b>, начислит тебе звёзды и билет.\n\n"
        "Напиши слово <b>топ</b>, чтобы посмотреть текущий рейтинг турнира."
    )


@dp.message(Command("start_tournament"))
async def cmd_start_tournament(message: Message) -> None:
    if not is_admin(message.from_user):
        return
    start_tournament()
    await message.answer(
        f"✅ Турнир запущен! Он продлится {TOURNAMENT_DAYS} "
        f"{days_word(TOURNAMENT_DAYS)}. Напишите «топ», чтобы увидеть рейтинг."
    )


@dp.message(Command("end_tournament"))
async def cmd_end_tournament(message: Message) -> None:
    if not is_admin(message.from_user):
        return
    end_tournament()
    await message.answer("🏁 Турнир завершён. Итоговый рейтинг заморожен — напишите «топ», чтобы его увидеть.")


@dp.message(F.dice.emoji == "🎰")
async def handle_slot(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    if is_admin(user):
        # прокруты админов не учитываются в статистике/топе
        return

    upsert_user(user.id, user.full_name)

    value = message.dice.value
    combo = check_combo(value)
    ticket = TICKET_ON_WIN if combo else 0

    # звёзды начисляются за ЛЮБОЙ прокрут, билет — только за выигрышную комбинацию
    add_spin(user.id, STARS_PER_SPIN, ticket, combo or "-")

    log.info(
        "Пользователь %s (%s): значение=%s комбо=%s +%s звёзд +%s билет(ов)",
        user.id, user.full_name, value, combo, STARS_PER_SPIN, ticket,
    )
    # намеренно ничего не отвечаем в чат — бот просто молча обновляет топ


@dp.message(is_top_word)
async def cmd_top(message: Message) -> None:
    await message.answer(build_top_text())


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
