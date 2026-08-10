"""
Savol-javob kanal boti.

TUZATISHLAR (1-bosqich):
1. BOT_TOKEN va ADMIN_IDS endi kod ichida emas, .env faylidan o'qiladi.
2. Foydalanuvchi kiritgan matn (savol/javob) Telegramga yuborishdan oldin
   HTML uchun xavfsiz qilib "escape" qilinadi - aks holda matnda < > & kabi
   belgilar bo'lsa, xabar yuborilmay xatolik chiqarardi.
3. Ishlatilmayotgan "o'lik" callback handler (check:...) olib tashlandi -
   uni yaratadigan hech qanday tugma yo'q edi.
4. Matn uzunliklari Telegram limitlariga moslab tekshiriladi:
   - caption (rasm ostidagi matn) - 1024 belgi
   - oddiy xabar matni - 4096 belgi
   Limitdan oshsa, foydalanuvchiga tushunarli xabar bilan qaytariladi
   (jim-jimgina kesib yubormaymiz - bu ma'lumot yo'qolishiga olib kelardi).
5. is_subscribed funksiyasidagi xatolik endi logga yoziladi - shunda nima
   uchun tekshiruv ishlamayotgani (masalan, bot kanalda admin emasligi)
   ko'rinib turadi.
6. Kanal to'g'ri kiritilgan-kiritilmaganligi ENDI BIRINCHI QADAMDAYOQ
   tekshiriladi (bot o'sha yerda mavjudligini ko'radi) - oldin bu faqat
   eng oxirida, savol-javob to'liq kiritilgandan keyin aniqlanardi.
"""

import asyncio
import html
import logging
import os
import sqlite3
from contextlib import closing

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, StateFilter
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

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # dotenv o'rnatilmagan bo'lsa ham, .env fayli bo'lmasa ham ishlashda davom etamiz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("quizbot")

# ---------------------------------------------------------------------------
# Sozlamalar - endi barchasi muhit o'zgaruvchilaridan o'qiladi
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi! '.env' faylini yarating (namuna: .env.example) "
        "va BOT_TOKEN=... qatorini kiriting, yoki muhit o'zgaruvchisi sifatida o'rnating."
    )

_admin_ids_raw = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()}
if not ADMIN_IDS:
    logger.warning(
        "ADMIN_IDS bo'sh! Hech kim /post buyrug'idan foydalana olmaydi. "
        ".env faylida ADMIN_IDS=123,456 kabi kiriting."
    )

DB_PATH = os.environ.get("QUIZ_DB_PATH", "quiz.db")

# Telegram limitlari (rasmiy hujjatlardan)
MAX_CAPTION_LEN = 1024
MAX_MESSAGE_LEN = 4096

router = Router()


def esc(text: str) -> str:
    """Foydalanuvchi kiritgan matnni Telegram HTML rejimi uchun xavfsiz qiladi.
    Buni unutish - eng ko'p uchraydigan xato manbalaridan biri: agar admin
    savol matnida masalan '2 < 5' deb yozsa, HTML rejimida bu tag boshlanishi
    deb noto'g'ri talqin qilinib, xabar yuborilmay xato beradi."""
    return html.escape(text)


class RememberUserMiddleware(BaseMiddleware):
    """Botga yozgan har bir odamning user_id/username'ini fon rejimida saqlab boradi.
    OUTER middleware sifatida ro'yxatdan o'tkazilgan - shuning uchun HAR QANDAY
    kiruvchi xabar uchun ishlaydi, hatto hech qanday handler unga mos kelmasa ham."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None:
            db_upsert_user(user.id, user.username)
        return await handler(event, data)


router.message.outer_middleware(RememberUserMiddleware())


def resolve_target(raw: str) -> str | int:
    """Admin yozgan manzilni (@channel, https://t.me/..., -100.., raqamli ID,
    yoki avval botga yozgan odamning username'i) haqiqiy chat_id/@username'ga aylantiradi."""
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
    await message.answer(
        "👋 Salom! Bot ishga tushdi.\n"
        "Post joylash uchun — /post buyrug'ini yuboring.\n"
        "Buyruqlar ro'yxati uchun — /help"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    lines = [
        "<b>Buyruqlar:</b>",
        "/post — kanalga yangi savol-javob joylash",
        "/cancel — joriy jarayonni bekor qilish",
    ]
    if message.from_user.id in ADMIN_IDS:
        lines.append("/list — oxirgi qo'shilgan savollarni ko'rish")
    await message.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Hozir hech qanday jarayon yo'q.")
        return
    await state.clear()
    await message.answer("❌ Jarayon bekor qilindi. Qaytadan /post bilan boshlashingiz mumkin.")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    with closing(sqlite3.connect(DB_PATH)) as con:
        rows = con.execute(
            "SELECT id, question FROM questions ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await message.answer("Hozircha savollar yo'q.")
        return
    lines = ["<b>Oxirgi 10 ta savol:</b>"]
    for qid, question in rows:
        short = question if len(question) <= 60 else question[:57] + "..."
        lines.append(f"#{qid} — {esc(short)}")
    await message.answer("\n".join(lines))


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
async def get_channel(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Iltimos, kanal username yoki chat_id yuboring.")
        return

    channel = resolve_target(raw)

    # TUZATISH: kanal shu yerdayoq tekshiriladi - oldin bu faqat eng oxirida,
    # savol va javob to'liq kiritilgandan keyin aniqlanardi va admin vaqtini
    # behuda sarflardi.
    try:
        chat = await bot.get_chat(channel)
        member = await bot.get_chat_member(chat_id=chat.id, user_id=bot.id)
        if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            await message.answer(
                f"❌ Bot '{channel}' kanalida admin emas. "
                "Botni admin qilib qo'shing va qaytadan /post yuboring."
            )
            return
    except Exception as e:
        await message.answer(
            f"❌ Bu kanalga kira olmadim: {e}\n\n"
            "Tekshiring:\n"
            "1) Username/ID to'g'ri yozilganmi?\n"
            "2) Bot o'sha kanalga admin sifatida qo'shilganmi?\n\n"
            "Qaytadan urinib ko'ring yoki /cancel bilan bekor qiling."
        )
        return

    await state.update_data(channel=channel)
    await state.set_state(AddQuestion.waiting_question)
    await message.answer(
        f"✅ Kanal: {channel}\n"
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
        # TUZATISH: rasm captioni uchun Telegram limiti 1024 belgi -
        # oldin bu tekshirilmasdi va yuborishda kutilmagan xatolik chiqardi.
        if len(text) > MAX_CAPTION_LEN:
            await message.answer(
                f"❌ Rasm ostidagi matn juda uzun ({len(text)} belgi). "
                f"Rasm bilan birga eng ko'pi {MAX_CAPTION_LEN} belgigacha matn bo'lishi mumkin.\n"
                "Matnni qisqartiring yoki rasmsiz, oddiy xabar sifatida yuboring."
            )
            return
    elif len(text) > MAX_MESSAGE_LEN:
        await message.answer(
            f"❌ Savol matni juda uzun ({len(text)} belgi, limit {MAX_MESSAGE_LEN})."
        )
        return

    await state.update_data(question=text, photo_id=photo_id)
    await state.set_state(AddQuestion.waiting_answer)
    await message.answer("Endi javobni yuboring:")


@router.message(StateFilter(AddQuestion.waiting_answer))
async def get_answer(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    channel = data["channel"]
    question = data["question"]
    photo_id = data.get("photo_id")

    answer = message.text or message.caption
    if not answer:
        await message.answer("Iltimos, javobni matn ko'rinishida yuboring.")
        return

    # TUZATISH: javob DM orqali yuborilganda 4096 belgidan oshib ketishi
    # mumkin edi va Telegram xato qaytarardi. Endi oldindan ogohlantiramiz.
    if len(answer) > MAX_MESSAGE_LEN:
        await message.answer(
            f"❌ Javob juda uzun ({len(answer)} belgi, limit {MAX_MESSAGE_LEN}). "
            "Iltimos, qisqartiring."
        )
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

    # TUZATISH: matn HTML rejimida yuborilgani uchun endi escape qilib
    # yuboramiz - aks holda foydalanuvchi "<" yoki "&" kabi belgi yozsa,
    # xabar yuborilmay xato chiqardi.
    safe_question = esc(question)

    try:
        if photo_id:
            sent = await bot.send_photo(
                chat_id=channel,
                photo=photo_id,
                caption=safe_question,
                reply_markup=keyboard,
            )
        else:
            sent = await bot.send_message(
                chat_id=channel, text=safe_question, reply_markup=keyboard
            )
        db_add_post(sent.chat.id, sent.message_id, qid)
        db_set_origin_channel(qid, sent.chat.id)
        await message.answer("✅ Savol kanalga joylandi.")
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

    await state.clear()


# ---------------------------------------------------------------------------
# Postni boshqa kanalga "qayta joylashtirish" (forward orqali tanib olish)
# ---------------------------------------------------------------------------
# ESLATMA: MemoryStorage ishlatilgani uchun bot FSM jarayoni o'rtasida qayta
# ishga tushsa (masalan, server qayta yuklansa), admin boshlagan jarayon
# holati yo'qoladi. Bitta shaxsiy bot uchun bu odatda muammo emas, lekin
# doimiy ishlaydigan katta loyihada RedisStorage kabi saqlovchi storage'ga
# o'tish tavsiya etiladi.
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

    # Bu yerdagi tugma CALLBACK emas, balki oddiy URL tugma bo'ladi - bosilgan
    # zahoti, asosiy postga olib boradi. U yerda odam a'zo bo'lmasa, Telegram
    # o'zi "JOIN CHANNEL" taklifini avtomatik ko'rsatadi.
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
                "yoza olmaydi. O'sha odam avval botga o'zi yozib, /start "
                "bosishi shart. Shundan keyingina qayta urinib ko'ring."
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
    except Exception as e:
        # TUZATISH: xatolik endi jimgina yutilmaydi, logga yoziladi.
        # Aks holda, masalan bot kanalda admin bo'lmay qolsa yoki Telegram
        # API vaqtincha ishlamasa, HAQIQIY obunachilar ham "obuna emassiz"
        # deb ko'rsatilardi va sababi hech qayerda ko'rinmasdi.
        logger.warning(
            "is_subscribed tekshiruvida xatolik (chat_id=%s, user_id=%s): %s",
            chat_id, user_id, e,
        )
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
        # Telegram alert (popup) matni ~200 belgigacha cheklangan.
        if len(answer) <= 190:
            await callback.answer(answer, show_alert=True)
        else:
            # TUZATISH: oldin bu yerda callback.answer() bo'sh (matnsiz)
            # chaqirilardi - ekranda hech qanday bildirishnoma chiqmasdi,
            # foydalanuvchi javob qayerga ketganini tushunmasligi mumkin edi.
            # Endi avval DM yuborishga harakat qilamiz, natijaga qarab
            # BITTA aniq bildirishnoma ko'rsatamiz (Telegram bitta callback
            # so'roviga faqat bitta javob berishga ruxsat beradi).
            # TUZATISH: DM matni Telegram limitidan (4096) oshib ketmasligi
            # uchun kesib yuboriladi - bu yuqorida /post bosqichida allaqachon
            # oldini olingan, lekin qo'shimcha xavfsizlik chorasi sifatida
            # bu yerda ham qoldirildi.
            text = f"✅ Javob:\n\n{esc(answer)}"[:MAX_MESSAGE_LEN]
            try:
                await bot.send_message(chat_id=user_id, text=text)
                await callback.answer(
                    "✅ Javob sizning shaxsiy chatingizga yuborildi.",
                    show_alert=True,
                )
            except Exception as e:
                logger.warning("Foydalanuvchiga DM yuborib bo'lmadi (user_id=%s): %s", user_id, e)
                await callback.answer(
                    "Sizga shaxsiy xabar yubora olmadim. Botga /start bosing va qayta urinib ko'ring.",
                    show_alert=True,
                )
        return

    link = await channel_invite_link(bot, origin_chat_id)
    await callback.answer(
        "Javobni bilish uchun avval kanalga obuna bo'ling!",
        show_alert=True,
    )


# ---------------------------------------------------------------------------
async def main() -> None:
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Bot ishga tushdi.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())