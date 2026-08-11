import os
import sqlite3
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    MessageOriginChannel
)
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Env yuklash
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Scheduler bazada saqlanadigan qilindi (rejalashtirilgan postlar o'chib ketmaydi)
jobstores = {
    'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
}
scheduler = AsyncIOScheduler(jobstores=jobstores)

# Database setup
conn = sqlite3.connect("quiz.db", check_same_thread=False)
cursor = conn.cursor()

def init_db():
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS questions (
                                                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                            channel_id TEXT DEFAULT '',
                                                            question_text TEXT,
                                                            media_type TEXT DEFAULT 'text',
                                                            file_id TEXT,
                                                            answer_text TEXT NOT NULL,
                                                            options TEXT DEFAULT ''
                   )
                   ''')

    cursor.execute("PRAGMA table_info(questions)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'channel_id' not in columns:
        cursor.execute("ALTER TABLE questions ADD COLUMN channel_id TEXT DEFAULT ''")

    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS posts (
                                                        chat_id TEXT NOT NULL,
                                                        message_id INTEGER NOT NULL,
                                                        question_id INTEGER NOT NULL,
                                                        PRIMARY KEY (chat_id, message_id)
                       )
                   ''')
    conn.commit()

init_db()

# FSM States
class AddQuestion(StatesGroup):
    waiting_channel = State()
    waiting_question = State()
    waiting_answer = State()
    waiting_schedule = State()

class RepostQuestion(StatesGroup):
    waiting_target_channel = State()

def get_post_keyboard(q_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Javobni bilish", callback_data=f"get_ans_{q_id}")]
    ])

# Forward qilingan kanallar uchun esa to'g'ridan-to'g'ri asl postga olib boradigan URL tugma
async def get_repost_keyboard(q_id: int):
    cursor.execute("SELECT chat_id, message_id FROM posts WHERE question_id=? ORDER BY message_id ASC LIMIT 1", (q_id,))
    row = cursor.fetchone()
    if row:
        p_chat_id, p_msg_id = row
        try:
            chat = await bot.get_chat(p_chat_id)
            if chat.username:
                return InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔍 Javobni bilish", url=f"https://t.me/{chat.username}/{p_msg_id}")]
                ])
            else:
                clean_id = str(p_chat_id).replace("-100", "")
                return InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔍 Javobni bilish", url=f"https://t.me/c/{clean_id}/{p_msg_id}")]
                ])
        except Exception:
            pass
    return get_post_keyboard(q_id)

# Vaqt tugmalari
time_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⚡️ Hozirning o'zida")],
        [KeyboardButton(text="📆 Sanani kiritish")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Siz admin emassiz.")
        return
    await message.answer(
        "👋 **Salom! Bot ishga tushdi.**\n\n"
        "Post joylash uchun — /post buyrug'ini yuboring.\n"
        "Jarayonni bekor qilish uchun — /cancel yuboring.",
        parse_mode="Markdown"
    )

@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Amaliyot bekor qilindi.", reply_markup=ReplyKeyboardRemove())

@router.message(Command("post"))
async def cmd_post(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AddQuestion.waiting_channel)
    await message.answer(
        "Qaysi kanalga post qilamiz?\n"
        "Kanal username yoki chat_id yuboring (masalan @mychannel yoki -1001234567890).\n"
        "Bot o'sha kanalda admin bo'lishi shart.",
        reply_markup=ReplyKeyboardRemove()
    )

@router.message(AddQuestion.waiting_channel)
async def process_channel(message: types.Message, state: FSMContext):
    channel = message.text.strip()
    if "t.me/" in channel:
        channel = "@" + channel.split("t.me/")[-1].replace("/", "")
    await state.update_data(channel=channel)
    await state.set_state(AddQuestion.waiting_question)
    await message.answer(f"✅ Kanal: {channel}\nEndi savol matnini yuboring.\nOddiy matn yoki rasm yuborishingiz mumkin.")

@router.message(AddQuestion.waiting_question)
async def process_question(message: types.Message, state: FSMContext):
    data = {}
    if message.photo:
        data['media_type'] = 'photo'
        data['file_id'] = message.photo[-1].file_id
        data['question_text'] = message.caption if message.caption else ""
    else:
        data['media_type'] = 'text'
        data['file_id'] = None
        data['question_text'] = message.text

    await state.update_data(question_data=data)
    await state.set_state(AddQuestion.waiting_answer)
    await message.answer("Endi javobni yuboring:")

@router.message(AddQuestion.waiting_answer)
async def process_answer(message: types.Message, state: FSMContext):
    answer = message.text.strip()
    await state.update_data(answer=answer)
    await state.set_state(AddQuestion.waiting_schedule)

    await message.answer(
        "⏰ **Postni joylash vaqtini tanlang:**",
        parse_mode="Markdown",
        reply_markup=time_keyboard
    )

async def publish_post_to_channel(channel_id: str, question_data: dict, answer: str):
    try:
        local_conn = sqlite3.connect("quiz.db")
        local_cursor = local_conn.cursor()
        local_cursor.execute(
            "INSERT INTO questions (channel_id, question_text, media_type, file_id, answer_text) VALUES (?, ?, ?, ?, ?)",
            (channel_id, question_data['question_text'], question_data['media_type'], question_data['file_id'], answer)
        )
        local_conn.commit()
        q_id = local_cursor.lastrowid
        local_conn.close()

        reply_markup = get_post_keyboard(q_id)

        if question_data['media_type'] == 'photo':
            sent = await bot.send_photo(chat_id=channel_id, photo=question_data['file_id'], caption=question_data['question_text'], reply_markup=reply_markup)
        else:
            sent = await bot.send_message(chat_id=channel_id, text=question_data['question_text'], reply_markup=reply_markup)

        save_conn = sqlite3.connect("quiz.db")
        save_cursor = save_conn.cursor()
        save_cursor.execute("INSERT OR REPLACE INTO posts (chat_id, message_id, question_id) VALUES (?, ?, ?)",
                            (str(sent.chat.id), sent.message_id, q_id))
        save_conn.commit()
        save_conn.close()
    except Exception as e:
        logging.error(f"Rejalashtirilgan postni yuborishda xatolik: {e}")

@router.message(AddQuestion.waiting_schedule)
async def process_schedule(message: types.Message, state: FSMContext):
    text = message.text.strip()

    if text == "📆 Sanani kiritish":
        await message.answer(
            "✍️ **Post joylanishi kerak bo'lgan vaqtni kiriting:**\n\n"
            "Misol uchun:\n"
            "• `18:30` — bugun soat 18:30 da\n"
            "• `15.08 18:30` — 15-avgust soat 18:30 da\n"
            "• `2026-08-15 18:30` — to'liq sana va vaqt",
            parse_mode="Markdown"
        )
        return

    data = await state.get_data()
    channel = data['channel']
    q_data = data['question_data']
    ans = data['answer']

    now = datetime.now()

    if text in ["/hozir", "⚡️ Hozirning o'zida"]:
        await publish_post_to_channel(channel, q_data, ans)
        await message.answer("✅ Savol kanalga darhol joylandi.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    target_time = None
    formats = [
        ("%H:%M", "time_only"),
        ("%d.%m %H:%M", "short_date"),
        ("%d-%m %H:%M", "short_date"),
        ("%Y-%m-%d %H:%M", "full_date"),
        ("%d.%m.%Y %H:%M", "full_date_dot")
    ]

    for fmt, fmt_type in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt_type == "time_only":
                target_time = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
                if target_time < now:
                    target_time += timedelta(days=1)
            elif fmt_type == "short_date":
                target_time = parsed.replace(year=now.year, second=0, microsecond=0)
                if target_time < now:
                    target_time = target_time.replace(year=now.year + 1)
            else:
                target_time = parsed
            break
        except ValueError:
            continue

    if not target_time:
        await message.answer(
            "⚠️ Vaqt formati noto'g'ri kiritildi.\n\n"
            "Iltimos, tugmalardan birini bosing yoki vaqtni quyidagicha yuboring:\n"
            "• `18:30` (bugun)\n"
            "• `15.08 18:30` (sana va vaqt)",
            reply_markup=time_keyboard
        )
        return

    scheduler.add_job(
        publish_post_to_channel,
        'date',
        run_date=target_time,
        args=[channel, q_data, ans]
    )

    await message.answer(
        f"⏳ **Post muvaffaqiyatli rejalashtirildi!**\n"
        f"📅 Joylanish vaqti: `{target_time.strftime('%Y-%m-%d %H:%M')}`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.clear()

# --- FORWARD QILISH ---
@router.message(F.forward_origin, StateFilter(None))
async def on_forwarded_post(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return

    origin_chat_id = str(origin.chat.id)
    origin_message_id = origin.message_id

    cursor.execute("SELECT question_id FROM posts WHERE chat_id = ? AND message_id = ?", (origin_chat_id, origin_message_id))
    row = cursor.fetchone()

    if not row:
        await message.answer("❌ Bu post bizning bazamizda topilmadi.")
        return

    q_id = row[0]
    await state.update_data(origin_chat_id=origin_chat_id, origin_message_id=origin_message_id, q_id=q_id)
    await state.set_state(RepostQuestion.waiting_target_channel)
    await message.answer("✅ Savol topildi! Qaysi kanalga joylaymiz? (Kanal username yuboring):")

@router.message(StateFilter(RepostQuestion.waiting_target_channel))
async def on_repost_target_channel(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Iltimos, kanal username yuboring.")
        return

    target_channel = raw
    if "t.me/" in target_channel:
        target_channel = "@" + target_channel.split("t.me/")[-1].replace("/", "")

    data = await state.get_data()
    origin_chat_id = data["origin_chat_id"]
    origin_message_id = data["origin_message_id"]
    q_id = data["q_id"]

    reply_markup = await get_repost_keyboard(q_id)

    try:
        copied = await bot.copy_message(
            chat_id=target_channel,
            from_chat_id=origin_chat_id,
            message_id=origin_message_id,
            reply_markup=reply_markup
        )
        cursor.execute("INSERT OR REPLACE INTO posts (chat_id, message_id, question_id) VALUES (?, ?, ?)",
                       (str(copied.chat.id), copied.message_id, q_id))
        conn.commit()

        await message.answer(f"✅ Post {target_channel} kanaliga tarqatildi!")
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

    await state.clear()

async def check_subscription(chat_id: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
    except TelegramBadRequest:
        pass
    return False

# "Javobni bilish" bosilganda faqat popup orqali alert chiqarish
@router.callback_query(F.data.startswith("get_ans_"))
async def handle_get_answer(call: types.CallbackQuery):
    q_id = int(call.data.split("_")[2])
    user_id = call.from_user.id
    chat_id = str(call.message.chat.id)
    message_id = call.message.message_id

    cursor.execute("SELECT channel_id, answer_text FROM questions WHERE id=?", (q_id,))
    row = cursor.fetchone()

    if not row:
        cursor.execute("SELECT question_id FROM posts WHERE chat_id=? AND message_id=?", (chat_id, message_id))
        p_row = cursor.fetchone()
        if p_row:
            cursor.execute("SELECT channel_id, answer_text FROM questions WHERE id=?", (p_row[0],))
            row = cursor.fetchone()

    if not row:
        await call.answer("Savol topilmadi.", show_alert=True)
        return

    channel_id, answer_text = row[0], row[1]

    if channel_id:
        is_subbed = await check_subscription(channel_id, user_id)
        if not is_subbed:
            # Kanalga hech qanday xabar yuborilmaydi, faqat alert chiqadi
            await call.answer("Javobni bilish uchun kanalga obuna bo'ling", show_alert=True)
            return

    await call.answer(f"✅ To'g'ri javob: {answer_text}", show_alert=True)

async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())