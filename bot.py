import re
import random
from cryptography.fernet import Fernet
import logging
from datetime import date
from decouple import config
from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    CallbackContext,
    CommandHandler,
    MessageHandler,
    filters,
    Application,
    CallbackQueryHandler,
    ConversationHandler,
    PicklePersistence
)
from bott.database import (
    search_table_by_tg_id,
    insert_data,
    delete_from_table
)
from bott.portal import (
    login_to_portal,
    get_profile,
    get_grades
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WCU = 'WCU'

KEY = config('SECRET_KEY').encode()

LOGGED_BUTTONS: list = [
    [KeyboardButton("Grade Report")],
    [KeyboardButton("View Profile")],
    [KeyboardButton("Delete Account")],
]

AGREE, CAMPUS, STUDENT_ID = range(3)
GRADE_REPORT = range(3, 4)
MATH_QUESTION, ACCOUNT_DELETED = range(4, 6)

persistence = PicklePersistence(filepath='bot_dat')

application = Application.builder().token(config('TELEGRAM_BOT_TOKEN')).persistence(persistence).build()


def encrypt_data(data: str, key: bytes) -> bytes:
    fernet = Fernet(key)
    return fernet.encrypt(data.encode())

def decrypt_data(encrypted_data: bytes, key: bytes) -> str:
    fernet = Fernet(key)
    return fernet.decrypt(encrypted_data).decode()

def is_user_id_valid(user_id: str) -> bool:
    user_id = user_id.upper()
    pattern = r'^[A-Z]{3}/\d{4}/\d{2}'
    return bool(re.match(pattern, user_id))

def generate_math_question() -> tuple:
    a = random.randint(1, 10)
    b = random.randint(1, 10)
    operation = random.choice(["+", "-", "*", "/"])
    if operation == "+": result = a + b
    elif operation == "-": result = a - b
    elif operation == "*": result = a * b
    else: result = a // b
    return f"What is {a} {operation} {b}?", result

async def math_question(update: Update, context: CallbackContext) -> int:
    question, correct_answer = generate_math_question()
    answers = [correct_answer, correct_answer+1, correct_answer-1]
    random.shuffle(answers)
    keyboard = [[InlineKeyboardButton(str(a), callback_data=f"answer_{a}")] for a in answers]
    await update.message.reply_text(question, reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['correct_answer'] = correct_answer
    return ACCOUNT_DELETED

async def handle_math_answer(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    user_answer = int(query.data.split('_')[1])
    correct_answer = context.user_data.get('correct_answer')
    if user_answer == correct_answer:
        delete_from_table(query.from_user.id)
        await query.edit_message_text("✅ Account deleted successfully!")
        return ConversationHandler.END

    question, new_answer = generate_math_question()
    answers = [new_answer, new_answer+1, new_answer-1]
    random.shuffle(answers)
    keyboard = [[InlineKeyboardButton(str(a), callback_data=f"answer_{a}")] for a in answers]
    await query.edit_message_text(
        f"❌ Incorrect answer. Try again:\n\n{question}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data['correct_answer'] = new_answer
    return ACCOUNT_DELETED

async def ask_for_password(update: Update, context: CallbackContext) -> int:
    msg = await update.message.reply_text(
        "🔒 Please enter your password to view the grade report:",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data['password_msg_id'] = msg.message_id
    return GRADE_REPORT

async def get_password(update: Update, context: CallbackContext) -> int:
    try:
        password = update.message.text
        tg_id = update.message.from_user.id
        registered = search_table_by_tg_id(tg_id)

        if not registered:
            await update.message.reply_text(
                "Viewing grade report is not available for unregistered users.\n/start here",
                reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END

        reg_tg_id, reg_id, reg_name, reg_campus, reg_date = registered

        profile = get_profile(
            campus=decrypt_data(reg_campus, KEY),
            student_id=decrypt_data(reg_id, KEY),
            password=password
        )

        if profile == "It seems you are a graduate, so I am skipping your profile and showing your grade report below.":
            await update.message.reply_text(
                "🎓 Congratulations! You are a graduate. 🎓\n\n"
                "Grade report is only available for active students.\n\n"
                "Thank you for using WCU Robot! 🎉"
            )
            return ConversationHandler.END

        elif isinstance(profile, tuple):
            await context.bot.send_photo(
                update.effective_chat.id,
                photo=profile[0],
                caption=profile[1]
            )

        grades = get_grades(
            campus=decrypt_data(reg_campus, KEY),
            student_id=decrypt_data(reg_id, KEY),
            password=password
        )

        semesters = []
        current_semester = []
        for line in grades:
            current_semester.append(line)
            if "Academic Status" in line:
                semesters.append("\n".join(current_semester))
                current_semester = []

        if semesters:
            semesters[-1] += "\n\nThis bot was Made by @Esubaalew"

        context.user_data['semesters'] = semesters
        context.user_data['current_page'] = 0

        await send_semester(update, context)
        return ConversationHandler.END

    except Exception as e:
        logging.error(f"Error in get_password: {e}")
        await update.message.reply_text(
            "An error occurred. Please try again later."
        )
        return ConversationHandler.END

async def send_semester(update: Update, context: CallbackContext) -> None:
    semesters = context.user_data.get('semesters', [])
    current_page = context.user_data.get('current_page', 0)
    total_pages = len(semesters)

    if not semesters:
        await update.message.reply_text("No grade information available")
        return

    buttons = []
    if total_pages > 1:
        if current_page > 0: buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data="prev"))
        if current_page < total_pages-1: buttons.append(InlineKeyboardButton("Next ➡️", callback_data="next"))

    footer = f"\n\n📄 Page {current_page+1} of {total_pages}"
    message_text = semesters[current_page] + footer

    if 'semester_message_id' in context.user_data:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['semester_message_id'],
                text=message_text,
                reply_markup=InlineKeyboardMarkup([buttons]) if buttons else None
            )
            return
        except Exception as e:
            print(f"Error editing message: {e}")

    msg = await update.message.reply_text(
        message_text,
        reply_markup=InlineKeyboardMarkup([buttons]) if buttons else None
    )
    context.user_data['semester_message_id'] = msg.message_id

async def handle_page_navigation(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    current_page = context.user_data.get('current_page', 0)
    if query.data == "prev" and current_page > 0: context.user_data['current_page'] -= 1
    elif query.data == "next": context.user_data['current_page'] += 1
    await send_semester(update, context)

async def view_profile(update: Update, context: CallbackContext) -> int:
    tg_id = update.message.from_user.id
    registered = search_table_by_tg_id(tg_id)
    if registered:
        reg_tg_id, reg_id, reg_name, reg_campus, reg_date = registered
        portal_id = decrypt_data(reg_id, KEY)
        telegram_name = decrypt_data(reg_name, KEY)
        portal_name = decrypt_data(reg_campus, KEY)
        registration_date = decrypt_data(reg_date, KEY)

        profile_message = (
            "📄 **Your Profile Information**\n\n"
            f"🆔 **Telegram ID**: `{reg_tg_id}`\n"
            f"📋 **Portal ID**: `{portal_id}`\n"
            f"👤 **Telegram Name**: {telegram_name}\n"
            f"🏫 **Campus**: {portal_name}\n"
            f"📅 **Date of Registration**: {registration_date}\n\n"
            "You can use the buttons below to explore more features!"
        )
        await update.message.reply_text(
            profile_message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardMarkup(
                LOGGED_BUTTONS,
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
    else:
        await update.message.reply_text(
            "❌ **Profile Unavailable**\nYou are not registered.\nUse /start to register.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=ReplyKeyboardRemove()
        )
    return ConversationHandler.END

# ---------------- Start Function ----------------
async def start(update: Update, context: CallbackContext) -> int:
    tg_id = update.message.from_user.id
    registered = search_table_by_tg_id(tg_id)
    WELCOME_IMAGE_URL = "https://portal.wcu.edu.et/wcu-welcome.jpg"
    NEW_USER_IMAGE_URL = "https://portal.wcu.edu.et/wcu-new.jpg"

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

        if registered:
            reg_tg_id, reg_id, reg_name, reg_campus, reg_date = registered
            welcome_message = f"👋 Welcome back, {decrypt_data(reg_name, KEY)}!\n\nSend your password to see your report."
            try:
                await update.message.reply_photo(
                    WELCOME_IMAGE_URL,
                    caption=welcome_message,
                    reply_markup=ReplyKeyboardMarkup(
                        LOGGED_BUTTONS,
                        resize_keyboard=True,
                        one_time_keyboard=True
                    )
                )
            except Exception:
                await update.message.reply_text(
                    welcome_message,
                    reply_markup=ReplyKeyboardMarkup(LOGGED_BUTTONS, resize_keyboard=True)
                )
            return ConversationHandler.END
        else:
            welcome_message = (
                "👋 Welcome to WCU Robot!\n\n"
                "Before using the bot, please read /policy and agree to our terms."
            )
            keyboard = [[InlineKeyboardButton("✅ AGREE", callback_data="agree")],
                        [InlineKeyboardButton("❌ DISAGREE", callback_data="disagree")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await update.message.reply_photo(
                    NEW_USER_IMAGE_URL,
                    caption=welcome_message,
                    reply_markup=reply_markup
                )
            except Exception:
                await update.message.reply_text(welcome_message, reply_markup=reply_markup)
            return AGREE
    except Exception as e:
        logger.error(f"Error in start function: {e}")
        await update.message.reply_text("👋 Welcome to WCU Robot!\nSomething went wrong. Try again.")
        return ConversationHandler.END

# ----------------- Remaining Handlers (cancel, registration, choose_campus, get_student_id, filters) -----------------
# You can keep the rest of your bot.py unchanged, just make sure all references to AAU are WCU

