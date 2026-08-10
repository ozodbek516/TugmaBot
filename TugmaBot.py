import os
import sqlite3
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
scheduler = AsyncIOScheduler()

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

    # Agar eski bazada channel_id ustuni bo'lmasa, uni avtomatik qo'shish
    cursor.execute("PRAGMA table_info(questions)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'channel_id' not in columns:
        cursor.execute("ALTER TABLE questions ADD COLUMN channel_id TEXT DEFAULT ''")

    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS user_answers (
                                                               id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                               question_id INTEGER NOT NULL,
                                                               user_id INTEGER NOT NULL,
                                                               user_answer TEXT NOT NULL,
                                                               is_correct INTEGER NOT NULL,
                                                               UNIQUE(question_id, user_id)
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

class UserAnswerState(StatesGroup):
    waiting_for_user_answer = State()

def get_post_keyboard(q_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Javob yuborish", callback_data=f"answer_{q_id}")]
    ])

# Quick time keyboard for admin
time_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⚡️ Hozirning o'zida"), KeyboardButton(text="📅 Ertaga (shu vaqtda)")],
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
        "⏰ **Postni joylash vaqtini tanlang:**\n\n"
        "🔹 Pastdagi tugmalardan birini bosing;\n",
        parse_mode="Markdown",
        reply_markup=time_keyboard
    )

async def publish_post_to_channel(channel_id: str, question_data: dict, answer: str):
    cursor.execute(
        "INSERT INTO questions (channel_id, question_text, media_type, file_id, answer_text) VALUES (?, ?, ?, ?, ?)",
        (channel_id, question_data['question_text'], question_data['media_type'], question_data['file_id'], answer)
    )
    conn.commit()
    q_id = cursor.lastrowid
    reply_markup = get_post_keyboard(q_id)

    if question_data['media_type'] == 'photo':
        await bot.send_photo(chat_id=channel_id, photo=question_data['file_id'], caption=question_data['question_text'], reply_markup=reply_markup)
    else:
        await bot.send_message(chat_id=channel_id, text=question_data['question_text'], reply_markup=reply_markup)

@router.message(AddQuestion.waiting_schedule)
async def process_schedule(message: types.Message, state: FSMContext):
    text = message.text.strip()

    # 0. Sanani kiritish tugmasi bosilganda yo'riqnoma ko'rsatish
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

    # 1. Hozir joylash
    if text in ["/hozir", "⚡️ Hozirning o'zida"]:
        await publish_post_to_channel(channel, q_data, ans)
        await message.answer("✅ Savol kanalga darhol joylandi.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    # 2. Ertaga shu vaqtda
    if text == "📅 Ertaga (shu vaqtda)":
        target_time = now + timedelta(days=1)
        scheduler.add_job(
            publish_post_to_channel,
            'date',
            run_date=target_time,
            args=[channel, q_data, ans]
        )
        await message.answer(
            f"📅 Post ertaga joylashga rejalashtirildi: `{target_time.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.clear()
        return

    # 3. Sana va soatni tahlil qilish (Parsing)
    target_time = None
    formats = [
        ("%H:%M", "time_only"),             # 18:30
        ("%d.%m %H:%M", "short_date"),       # 15.08 18:30
        ("%d-%m %H:%M", "short_date"),       # 15-08 18:30
        ("%Y-%m-%d %H:%M", "full_date"),     # 2026-08-15 18:30
        ("%d.%m.%Y %H:%M", "full_date_dot") # 15.08.2026 18:30
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

# Javobni tekshirish logikasi (Callback handler)
@router.callback_query(F.data.startswith("answer_"))
async def handle_answer_button(call: types.CallbackQuery, state: FSMContext):
    q_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    cursor.execute("SELECT id FROM user_answers WHERE question_id=? AND user_id=?", (q_id, user_id))
    if cursor.fetchone():
        await call.answer("Siz bu savolga allaqachon javob bergansiz!", show_alert=True)
        return

    await state.update_data(active_q_id=q_id)
    await state.set_state(UserAnswerState.waiting_for_user_answer)
    await call.message.answer("✍️ Javobingizni yuboring:")
    await call.answer()

@router.message(UserAnswerState.waiting_for_user_answer)
async def process_user_answer(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q_id = data.get("active_q_id")
    user_id = message.from_user.id
    user_ans = message.text.strip()

    cursor.execute("SELECT answer_text FROM questions WHERE id=?", (q_id,))
    row = cursor.fetchone()
    if not row:
        await message.answer("Savol topilmadi.")
        await state.clear()
        return

    correct_ans = row[0]
    is_correct = 1 if user_ans.lower() == correct_ans.lower() else 0

    try:
        cursor.execute(
            "INSERT INTO user_answers (question_id, user_id, user_answer, is_correct) VALUES (?, ?, ?, ?)",
            (q_id, user_id, user_ans, is_correct)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        await message.answer("Siz bu savolga allaqachon javob beribsiz.")
        await state.clear()
        return

    if is_correct:
        await message.answer("🎉 Barakalla! Javobingiz to'g'ri.")
    else:
        await message.answer(f"❌ Noto'g'ri javob. To'g'ri javob: {correct_ans}")

    await state.clear()

async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())