BOT_TOKEN = "8956626468:AAGU1tYdoVSxaWpACkZeDTS2rn7fB015zug"
ADMIN_IDS = {6932479965, 6823530810, 639844452}

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)

DB_PATH = os.environ.get("QUIZ_DB_PATH", "quiz.db")

router = Router()
scheduler = AsyncIOScheduler()


class RememberUserMiddleware(BaseMiddleware):
    """Botga yozgan har bir odamning user_id/username'ini fon rejimida saqlab boradi."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None:
            db_upsert_user(user.id, user.username)
        return await handler(event, data)


router.message.outer_middleware(RememberUserMiddleware())


def resolve_target(raw: str) -> str | int:
    text = raw.strip()

    if text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        text = "@" + text.split("t.me/")[-1].strip("/")
    elif text.startswith("t.me/"):
        text = "@" + text.split("t.me/")[-1].strip("/")

    if text.lstrip("-").isdigit():
        return int(text)

    if not text.startswith("@"):
        text = "@" + text

    user_id = db_find_user_id_by_username(text)
    if user_id is not None:
        return user_id

    return text


# ---------------------------------------------------------------------------
# Ma'lumotlar bazasi
# ---------------------------------------------------------------------------
def db_init() -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                                                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                     question TEXT NOT NULL,
                                                     answer TEXT NOT NULL,
                                                     origin_chat_id INTEGER
            )
            """
        )
        try:
            con.execute("ALTER TABLE questions ADD COLUMN origin_chat_id INTEGER")
        except Exception:
            pass
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                                                 chat_id INTEGER NOT NULL,
                                                 message_id INTEGER NOT NULL,
                                                 question_id INTEGER NOT NULL,
                                                 PRIMARY KEY (chat_id, message_id)
                )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                                                 user_id INTEGER PRIMARY KEY,
                                                 username TEXT
            )
            """
        )
        # Rejalashtirilgan postlar uchun jadval
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                                                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                           channel TEXT NOT NULL,
                                                           question_id INTEGER NOT NULL,
                                                           photo_id TEXT,
                                                           publish_at TEXT NOT NULL,
                                                           status TEXT DEFAULT 'pending'
            )
            """
        )
        con.commit()


def db_upsert_user(user_id: int, username: str | None) -> None:
    if not username:
        return
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username.lower()),
        )
        con.commit()


def db_find_user_id_by_username(username: str) -> int | None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT user_id FROM users WHERE username = ?",
            (username.lower().lstrip("@"),),
        ).fetchone()
        return row[0] if row else None


def db_add_post(chat_id: int, message_id: int, question_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "INSERT OR REPLACE INTO posts (chat_id, message_id, question_id) VALUES (?, ?, ?)",
            (chat_id, message_id, question_id),
        )
        con.commit()


def db_find_question_by_post(chat_id: int, message_id: int) -> int | None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT question_id FROM posts WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ).fetchone()
        return row[0] if row else None


def db_get_post_message_id(chat_id: int, question_id: int) -> int | None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT message_id FROM posts WHERE chat_id = ? AND question_id = ? "
            "ORDER BY message_id ASC LIMIT 1",
            (chat_id, question_id),
        ).fetchone()
        return row[0] if row else None


def db_add_question(question: str, answer: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute(
            "INSERT INTO questions (question, answer) VALUES (?, ?)",
            (question, answer),
        )
        con.commit()
        return cur.lastrowid


def db_get_question_by_id(qid: int) -> tuple[str, str] | None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT question, answer FROM questions WHERE id = ?", (qid,)
        ).fetchone()
        return (row[0], row[1]) if row else None


def db_get_answer(qid: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT answer FROM questions WHERE id = ?", (qid,)
        ).fetchone()
        return row[0] if row else None


def db_set_origin_channel(qid: int, chat_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "UPDATE questions SET origin_chat_id = ? WHERE id = ? AND origin_chat_id IS NULL",
            (chat_id, qid),
        )
        con.commit()


def db_get_origin_channel(qid: int) -> int | None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT origin_chat_id FROM questions WHERE id = ?", (qid,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None


def db_add_scheduled_post(channel: str, qid: int, photo_id: str | None, publish_at: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute(
            "INSERT INTO scheduled_posts (channel, question_id, photo_id, publish_at) VALUES (?, ?, ?, ?)",
            (str(channel), qid, photo_id, publish_at),
        )
        con.commit()
        return cur.lastrowid


def db_mark_scheduled_done(scheduled_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "UPDATE scheduled_posts SET status = 'sent' WHERE id = ?",
            (scheduled_id,),
        )
        con.commit()


# ---------------------------------------------------------------------------
# Post yuborish funksiyasi (Hozir yoki Rejalashtirilgan vaqtda chaqiriladi)
# ---------------------------------------------------------------------------
async def publish_post_to_channel(bot: Bot, channel: str | int, qid: int, photo_id: str | None) -> bool:
    data = db_get_question_by_id(qid)
    if not data:
        return False
    question, _ = data

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Javobni bilish", callback_data=f"ans:{qid}"
                )
            ]
        ]
    )

    if photo_id:
        sent = await bot.send_photo(
            chat_id=channel,
            photo=photo_id,
            caption=question,
            reply_markup=keyboard,
        )
    else:
        sent = await bot.send_message(
            chat_id=channel, text=question, reply_markup=keyboard
        )

    db_add_post(sent.chat.id, sent.message_id, qid)
    db_set_origin_channel(qid, sent.chat.id)
    return True


async def scheduled_job(bot: Bot, scheduled_id: int, channel: str, qid: int, photo_id: str | None):
    try:
        success = await publish_post_to_channel(bot, channel, qid, photo_id)
        if success:
            db_mark_scheduled_done(scheduled_id)
            logging.info(f"Rejalashtirilgan post #{scheduled_id} kanalga joylandi.")
    except Exception as e:
        logging.error(f"Rejalashtirilgan post joylashda xatolik: {e}")


# ---------------------------------------------------------------------------
# Admin: savol qo'shish va kanalga post qilish (FSM)
# ---------------------------------------------------------------------------
class AddQuestion(StatesGroup):
    waiting_channel = State()
    waiting_question = State()
    waiting_answer = State()
    waiting_publish_option = State()
    waiting_schedule_time = State()


class RepostQuestion(StatesGroup):
    waiting_target_channel = State()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Salom! Bot ishga tushdi.\n"
        " Post joylash uchun — /post buyrugini yuboring."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Hozir hech qanday jarayon yo'q.")
        return
    await state.clear()
    await message.answer("❌ Jarayon bekor qilindi. Qaytadan /post bilan boshlashingiz mumkin.")


@router.message(Command("post"))
async def cmd_post(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AddQuestion.waiting_channel)
    await message.answer(
        "Qaysi kanalga post qilamiz?\n"
        "Kanal username yoki chat_id yuboring (masalan @mychannel yoki -1001234567890).\n"
        "Bot o'sha kanalda admin bo'lishi shart."
    )


@router.message(StateFilter(AddQuestion.waiting_channel))
async def get_channel(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Iltimos, kanal username yoki chat_id yuboring.")
        return

    channel = resolve_target(raw)

    await state.update_data(channel=channel)
    await state.set_state(AddQuestion.waiting_question)
    await message.answer(
        f"Kanal: {channel}\n"
        "Endi savol matnini yuboring.\n"
        "Oddiy matn yoki rasm (pastida savol matni/caption bilan) yuborishingiz mumkin."
    )


@router.message(StateFilter(AddQuestion.waiting_question))
async def get_question(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption
    if not text:
        await message.answer(
            "Iltimos, savol matnini yuboring (rasm bilan yubormoqchi bo'lsangiz, "
            "rasm ostiga matn/caption qo'shing)."
        )
        return

    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id

    await state.update_data(question=text, photo_id=photo_id)
    await state.set_state(AddQuestion.waiting_answer)
    await message.answer("Endi javobni yuboring:")


@router.message(StateFilter(AddQuestion.waiting_answer))
async def get_answer(message: Message, state: FSMContext) -> None:
    answer = message.text or message.caption
    if not answer:
        await message.answer("Iltimos, javobni matn ko'rinishida yuboring.")
        return

    data = await state.get_data()
    question = data["question"]
    qid = db_add_question(question, answer)

    await state.update_data(qid=qid)
    await state.set_state(AddQuestion.waiting_publish_option)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Hozir joylash", callback_data="publish:now")],
            [InlineKeyboardButton(text="📅 Rejalashtirish", callback_data="publish:schedule")],
        ]
    )
    await message.answer("Post tayyor! Qachon joylaymiz?", reply_markup=kb)


@router.callback_query(StateFilter(AddQuestion.waiting_publish_option), F.data == "publish:now")
async def publish_now_callback(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    channel = data["channel"]
    qid = data["qid"]
    photo_id = data.get("photo_id")

    try:
        await publish_post_to_channel(bot, channel, qid, photo_id)
        await callback.message.edit_text("✅ Savol kanalga joylandi.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Xatolik: {e}")

    await state.clear()


@router.callback_query(StateFilter(AddQuestion.waiting_publish_option), F.data == "publish:schedule")
async def schedule_option_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddQuestion.waiting_schedule_time)
    await callback.message.edit_text(
        "📅 Post qaysi vaqtda chiqsin?\n\n"
        "Format: `YYYY-MM-DD HH:MM` (masalan: `2026-08-10 18:30`)\n"
        "Vaqtni kiriting:"
    )


@router.message(StateFilter(AddQuestion.waiting_schedule_time))
async def set_schedule_time(message: Message, state: FSMContext, bot: Bot) -> None:
    time_str = (message.text or "").strip()
    try:
        run_date = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        if run_date <= datetime.now():
            await message.answer("❌ Kiritilgan vaqt o'tib ketgan. Kelajakdagi vaqtni kiriting:")
            return
    except ValueError:
        await message.answer(
            "❌ Vaqt formati xato! Iltimos, to'g'ri formatda kiriting:\n"
            "`YYYY-MM-DD HH:MM` (masalan: `2026-08-10 18:30`)"
        )
        return

    data = await state.get_data()
    channel = data["channel"]
    qid = data["qid"]
    photo_id = data.get("photo_id")

    scheduled_id = db_add_scheduled_post(str(channel), qid, photo_id, time_str)

    scheduler.add_job(
        scheduled_job,
        "date",
        run_date=run_date,
        args=[bot, scheduled_id, channel, qid, photo_id],
    )

    await message.answer(f"⏳ Post rejalashtirildi! Sana va vaqt: **{time_str}**")
    await state.clear()


# ---------------------------------------------------------------------------
# Postni boshqa kanalga "qayta joylashtirish"
# ---------------------------------------------------------------------------
@router.message(F.forward_origin, StateFilter(None))
async def on_forwarded_post(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return

    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return

    origin_chat_id = origin.chat.id
    origin_message_id = origin.message_id

    qid = db_find_question_by_post(origin_chat_id, origin_message_id)
    if qid is None:
        await message.answer(
            "Bu post bizning bazamizda topilmadi (bot orqali joylanmagan bo'lishi mumkin)."
        )
        return

    await state.update_data(
        origin_chat_id=origin_chat_id,
        origin_message_id=origin_message_id,
        qid=qid,
    )
    await state.set_state(RepostQuestion.waiting_target_channel)
    await message.answer(
        "✅ Savol topildi.\n"
        "Endi shu postni qaysi kanalga qayta joylashtirishni xohlaysiz?\n"
        "Kanal username yoki chat_id yuboring (masalan @mychannel)."
    )


@router.message(StateFilter(RepostQuestion.waiting_target_channel))
async def on_repost_target_channel(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Iltimos, kanal username yoki chat_id yuboring.")
        return

    channel = resolve_target(raw)

    data = await state.get_data()
    origin_chat_id = data["origin_chat_id"]
    origin_message_id = data["origin_message_id"]
    qid = data["qid"]

    origin_link = await origin_post_link(bot, origin_chat_id, qid)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Javobni bilish", url=origin_link)]
        ]
    )

    try:
        copied = await bot.copy_message(
            chat_id=channel,
            from_chat_id=origin_chat_id,
            message_id=origin_message_id,
            reply_markup=keyboard,
        )
        target_chat = await bot.get_chat(channel)
        db_add_post(target_chat.id, copied.message_id, qid)
        await message.answer(f"✅ Post {channel} ga tugmasi bilan joylandi.")
    except Exception as e:
        err_text = str(e)
        if "chat not found" in err_text.lower():
            await message.answer(
                "❌ Chat topilmadi. Buning ikki sababi bo'lishi mumkin:\n\n"
                "1️⃣ Agar bu KANAL yoki GURUH bo'lsa — bot o'sha yerda admin "
                "ekanligini va username to'g'ri yozilganini tekshiring.\n\n"
                "2️⃣ Agar bu ODAM bo'lsa — bot unga birinchi yoza olmaydi."
            )
        else:
            await message.answer(f"❌ Xatolik: {e}")

    await state.clear()


# ---------------------------------------------------------------------------
# Obunani tekshirish
# ---------------------------------------------------------------------------
SUBSCRIBED_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}


async def is_subscribed(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in SUBSCRIBED_STATUSES
    except Exception:
        return False


async def channel_invite_link(bot: Bot, chat_id: int) -> str:
    chat = await bot.get_chat(chat_id)
    if chat.username:
        return f"https://t.me/{chat.username}"
    if chat.invite_link:
        return chat.invite_link
    link = await bot.create_chat_invite_link(chat_id=chat_id)
    return link.invite_link


async def origin_post_link(bot: Bot, chat_id: int, qid: int) -> str:
    chat = await bot.get_chat(chat_id)
    message_id = db_get_post_message_id(chat_id, qid)

    if chat.username and message_id:
        return f"https://t.me/{chat.username}/{message_id}"
    if chat.username:
        return f"https://t.me/{chat.username}"
    if chat.invite_link:
        return chat.invite_link
    link = await bot.create_chat_invite_link(chat_id=chat_id)
    return link.invite_link


# ---------------------------------------------------------------------------
# "🔍 Javobni bilish" tugmasi bosilganda
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("ans:"))
async def on_answer_click(callback: CallbackQuery, bot: Bot) -> None:
    qid = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id

    origin_chat_id = db_get_origin_channel(qid)
    if origin_chat_id is None:
        origin_chat_id = callback.message.chat.id

    if await is_subscribed(bot, origin_chat_id, user_id):
        answer = db_get_answer(qid)
        if answer is None:
            await callback.answer("Savol topilmadi.", show_alert=True)
            return
        if len(answer) <= 190:
            await callback.answer(answer, show_alert=True)
        else:
            await callback.answer()
            await bot.send_message(chat_id=user_id, text=f"✅ Javob:\n\n{answer}")
        return

    link = await channel_invite_link(bot, origin_chat_id)
    await callback.answer(
        f"Javobni bilish uchun avval kanalga obuna bo'ling!!!",
        show_alert=True,
    )


# ---------------------------------------------------------------------------
# "✅ Tekshirdim" tugmasi bosilganda
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("check:"))
async def on_check_click(callback: CallbackQuery, bot: Bot) -> None:
    _, qid_str, chat_id_str = callback.data.split(":")
    qid = int(qid_str)
    chat_id = int(chat_id_str)
    user_id = callback.from_user.id

    if await is_subscribed(bot, chat_id, user_id):
        answer = db_get_answer(qid)
        text = f"✅ Javob:\n\n{answer}"
        await callback.message.edit_text(text[:4096])
    else:
        await callback.answer(
            "❌ Siz hali obuna bo'lmadingiz. Obuna bo'lib, qayta tekshiring.",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
async def main() -> None:
    db_init()
    scheduler.start()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())