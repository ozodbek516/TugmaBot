BOT_TOKEN = "8956626468:AAGU1tYdoVSxaWpACkZeDTS2rn7fB015zug"
ADMIN_IDS = {6932479965, 6823530810, 639844452}

import asyncio
import logging
import os
import sqlite3
from contextlib import closing

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

logging.basicConfig(level=logging.INFO)

DB_PATH = os.environ.get("QUIZ_DB_PATH", "quiz.db")

router = Router()


class RememberUserMiddleware(BaseMiddleware):
    """Botga yozgan har bir odamning user_id/username'ini fon rejimida saqlab boradi.
    OUTER middleware sifatida ro'yxatdan o'tkazilgan - shuning uchun HAR QANDAY
    kiruvchi xabar uchun ishlaydi, hatto hech qanday handler unga mos kelmasa ham
    (masalan /start kabi oddiy buyruqlar uchun ham doim ishlaydi)."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None:
            db_upsert_user(user.id, user.username)
        return await handler(event, data)


# OUTER middleware - observer.trigger'ning ENG TASHQI qatlamida ishlaydi,
# shuning uchun filtrlardan qat'i nazar har doim chaqiriladi.
router.message.outer_middleware(RememberUserMiddleware())


def resolve_target(raw: str) -> str | int:
    """Admin yozgan manzilni (@channel, https://t.me/..., -100.., raqamli ID,
    yoki avval botga yozgan odamning username'i) haqiqiy chat_id/@username'ga aylantiradi."""
    text = raw.strip()

    if text.startswith("https://t.me/") or text.startswith("http://t.me/"):
        text = "@" + text.split("t.me/")[-1].strip("/")
    elif text.startswith("t.me/"):
        text = "@" + text.split("t.me/")[-1].strip("/")

    # Faqat raqam (yoki -100 bilan boshlanuvchi) - bu chat_id, o'zgartirmasdan qaytaramiz
    if text.lstrip("-").isdigit():
        return int(text)

    if not text.startswith("@"):
        text = "@" + text

    # Avval botga yozgan (shaxsiy) foydalanuvchi bo'lsa, uning saqlangan ID'sini qaytaramiz -
    # chunki Telegram shaxsiy chatlarga @username orqali yozishga ruxsat bermaydi.
    user_id = db_find_user_id_by_username(text)
    if user_id is not None:
        return user_id

    # Aks holda kanal/guruh username'i sifatida qaytaramiz
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
    """Berilgan kanaldagi shu savolga tegishli postning message_id'sini topadi."""
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


def db_get_answer(qid: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT answer FROM questions WHERE id = ?", (qid,)
        ).fetchone()
        return row[0] if row else None


def db_set_origin_channel(qid: int, chat_id: int) -> None:
    """Savol birinchi marta qaysi kanalga joylangan bo'lsa, o'shani saqlab qo'yamiz.
    Keyinchalik post boshqa joyga ko'chirilsa ham, obuna aynan shu kanaldan tekshiriladi."""
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


# ---------------------------------------------------------------------------
# Admin: savol qo'shish va kanalga post qilish (FSM)
# ---------------------------------------------------------------------------
class AddQuestion(StatesGroup):
    waiting_channel = State()
    waiting_question = State()
    waiting_answer = State()


class RepostQuestion(StatesGroup):
    waiting_target_channel = State()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    # Bu handler har doim /start uchun ishlaydi va foydalanuvchiga tasdiqlovchi
    # xabar beradi - shu bilan birga outer middleware uning username/ID'sini
    # avtomatik bazaga saqlab qo'yadi (keyinchalik admin uni username orqali
    # topib, post yuborishi mumkin bo'lishi uchun).
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
    # Ham oddiy matn, ham rasm ostidagi caption'ni qabul qiladi
    text = message.text or message.caption
    if not text:
        await message.answer(
            "Iltimos, savol matnini yuboring (rasm bilan yubormoqchi bo'lsangiz, "
            "rasm ostiga matn/caption qo'shing)."
        )
        return

    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id  # eng katta o'lchamdagi rasm

    await state.update_data(question=text, photo_id=photo_id)
    await state.set_state(AddQuestion.waiting_answer)
    await message.answer("Endi javobni yuboring:")


@router.message(StateFilter(AddQuestion.waiting_answer))
async def get_answer(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    channel = data["channel"]
    question = data["question"]
    photo_id = data.get("photo_id")

    # Javob ham matn, ham rasm caption ko'rinishida bo'lishi mumkin
    answer = message.text or message.caption
    if not answer:
        await message.answer("Iltimos, javobni matn ko'rinishida yuboring.")
        return

    qid = db_add_question(question, answer)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Javobni bilish", callback_data=f"ans:{qid}"
                )
            ]
        ]
    )

    try:
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
        # Bu xabar qaysi savolga tegishli ekanini saqlab qo'yamiz -
        # keyinchalik shu postni boshqa kanalga "qayta joylashtirish" uchun kerak.
        db_add_post(sent.chat.id, sent.message_id, qid)
        db_set_origin_channel(qid, sent.chat.id)
        await message.answer("✅ Savol kanalga joylandi.")
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

    await state.clear()


# ---------------------------------------------------------------------------
# Postni boshqa kanalga "qayta joylashtirish" (forward orqali tanib olish)
# ---------------------------------------------------------------------------
@router.message(F.forward_origin, StateFilter(None))
async def on_forwarded_post(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return

    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return  # oddiy foydalanuvchidan forward - bizga aloqasi yo'q

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

    # Bu yerda MUHIM farq: qayta joylangan (forward/copy) nusxada tugma
    # CALLBACK emas, balki oddiy URL tugma bo'ladi - bosilgan zahoti,
    # hech qanday server tekshiruvisiz, TO'G'RIDAN-TO'G'RI asosiy postga
    # olib boradi. U yerda odam a'zo bo'lmasa, Telegram O'ZI "JOIN CHANNEL"
    # taklifini avtomatik ko'rsatadi - bizga buni qo'lda qilish shart emas.
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
                "2️⃣ Agar bu ODAM (shaxsiy chat) bo'lsa — bot unga birinchi "
                "yoza olmaydi. O'sha odam avval botga (@sadjchbot) o'zi "
                "yozib, /start bosishi shart. Shundan keyingina qayta urinib ko'ring."
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
    """Imkon bo'lsa, savol birinchi joylangan XABARNING O'ZIGA havola qaytaradi
    (shunda foydalanuvchi to'g'ridan-to'g'ri o'sha postdagi tugmani ko'radi va bosa oladi).
    Bo'lmasa, oddiy kanal havolasiga tushamiz."""
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

    # Har doim savol BIRINCHI joylangan (asosiy) kanalga obunani tekshiramiz -
    # post boshqa kanalga ko'chirilgan/joylashtirilgan bo'lsa ham farqi yo'q.
    origin_chat_id = db_get_origin_channel(qid)
    if origin_chat_id is None:
        # Fallback: eski yozuvlar uchun - agar sabab bo'lmasa, hozirgi kanaldan foydalanamiz
        origin_chat_id = callback.message.chat.id

    if await is_subscribed(bot, origin_chat_id, user_id):
        answer = db_get_answer(qid)
        if answer is None:
            await callback.answer("Savol topilmadi.", show_alert=True)
            return
        # Telegram alert (popup) matni ~200 belgigacha cheklangan.
        # Qisqa javob - popup, uzun javob - shaxsiy xabar.
        if len(answer) <= 190:
            await callback.answer(answer, show_alert=True)
        else:
            await callback.answer()
            await bot.send_message(chat_id=user_id, text=f"✅ Javob:\n\n{answer}")
        return

    # Obuna bo'lmagan bo'lsa - hech qanday shaxsiy xabar yubormaymiz.
    # Faqat POPUP (alert) orqali, aynan shu joyning o'zida obuna so'raymiz.
    # Telegram matn ichidagi havolani odatda avtomatik bosiladigan qilib ko'rsatadi.
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
        # Xabar (message) matni uchun Telegram cheklovi 4096 belgi,
        # shuning uchun bu yerda uzunlik odatda muammo emas.
        await callback.message.edit_text(text[:4096])
    else:
        await callback.answer(
            "❌ Siz hali obuna bo'lmadingiz. Obuna bo'lib, qayta tekshiring.",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
async def main() -> None:
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())