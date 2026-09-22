import os
import re
import logging
from datetime import datetime
import telebot
from telebot import types
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image
import pytesseract
import requests
from io import BytesIO

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
SHEET_ID = os.environ.get('SHEET_ID')
TRANSFER_NUMBER = os.environ.get('TRANSFER_NUMBER', '01152596770')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')

bot = telebot.TeleBot(TELEGRAM_TOKEN)

def get_sheet():
    import json
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)

def find_student(name):
    try:
        spreadsheet = get_sheet()
        sheets = ['سبا 1', 'سبا 2', 'سبا 3']
        for sheet_name in sheets:
            try:
                ws = spreadsheet.worksheet(sheet_name)
                data = ws.get_all_values()
                for i, row in enumerate(data):
                    if len(row) >= 2 and name.strip() in row[1].strip():
                        return {
                            'sheet': sheet_name,
                            'row': i + 1,
                            'name': row[1],
                            'unit': row[2] if len(row) > 2 else '',
                            'rent': row[3] if len(row) > 3 else '',
                            'status': row[4] if len(row) > 4 else ''
                        }
            except:
                continue
        return None
    except Exception as e:
        logger.error(f"Error finding student: {e}")
        return None

def record_payment(student, amount, pay_type, date, time_str):
    try:
        spreadsheet = get_sheet()
        ws = spreadsheet.worksheet(student['sheet'])
        ws.update_cell(student['row'], 5, '✅ دفع')
        try:
            log_ws = spreadsheet.worksheet('سجل المدفوعات')
        except:
            log_ws = spreadsheet.add_worksheet('سجل المدفوعات', 1000, 10)
            log_ws.append_row(['التاريخ', 'الوقت', 'الاسم', 'العمارة', 'الوحدة', 'المبلغ', 'نوع الدفع', 'الحالة'])
        log_ws.append_row([date, time_str, student['name'], student['sheet'], student['unit'], amount, pay_type, '✅ مقبول'])
        return True
    except Exception as e:
        logger.error(f"Error recording payment: {e}")
        return False

def check_duplicate(name, date):
    try:
        spreadsheet = get_sheet()
        try:
            log_ws = spreadsheet.worksheet('سجل المدفوعات')
            data = log_ws.get_all_values()
            for row in data[1:]:
                if len(row) >= 3 and row[2] == name and row[0] == date:
                    return True
        except:
            pass
        return False
    except:
        return False

user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message,
        "👋 أهلاً بك في بوت سبا للإيجارات!\n\n"
        "📌 طريقة الاستخدام:\n"
        "1️⃣ ابعت اسم الطالب الرباعي\n"
        "2️⃣ بعدين ابعت صورة الإيصال\n\n"
        "ابدأ بكتابة اسم الطالب 👇"
    )

@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    name = message.text.strip()
    student = find_student(name)
    if student:
        user_states[chat_id] = {'student': student, 'step': 'waiting_photo'}
        bot.reply_to(message,
            f"✅ تم العثور على الطالب:\n\n"
            f"👤 الاسم: {student['name']}\n"
            f"🏢 العمارة: {student['sheet']}\n"
            f"🚪 الوحدة: {student['unit']}\n"
            f"💰 الإيجار: {student['rent']} جنيه\n\n"
            f"📸 ابعت صورة الإيصال دلوقتي"
        )
    else:
        bot.reply_to(message,
            f"❌ مش لاقي الطالب: {name}\n\n"
            "تأكد من الاسم الرباعي وحاول تاني"
        )

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    if chat_id not in user_states or user_states[chat_id].get('step') != 'waiting_photo':
        bot.reply_to(message, "⚠️ ابعت اسم الطالب الأول قبل الإيصال")
        return
    student = user_states[chat_id]['student']
    bot.reply_to(message, "⏳ جاري التحقق من الإيصال...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        response = requests.get(file_url)
        img = Image.open(BytesIO(response.content))
        text = pytesseract.image_to_string(img, lang='ara+eng')
        text_lower = text.lower()
        transfer_found = TRANSFER_NUMBER in text or TRANSFER_NUMBER.replace('0', '') in text
        amounts = re.findall(r'\b(\d{3,5})\b', text)
        amount = amounts[0] if amounts else 'غير معروف'
        if 'instapay' in text_lower or 'انستا' in text:
            pay_type = 'انستاباي'
        elif 'vodafone' in text_lower or 'فودافون' in text:
            pay_type = 'فودافون كاش'
        else:
            pay_type = 'تحويل بنكي'
        now = datetime.now()
        date = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M')
        if check_duplicate(student['name'], date):
            bot.reply_to(message,
                f"⚠️ تحذير: يبدو إن {student['name']} دفع النهاردة قبل كده!\n"
                "تأكد إنه مش إيصال متكرر"
            )
            del user_states[chat_id]
            return
        if not transfer_found:
            bot.reply_to(message,
                f"❌ الإيصال مرفوض!\n\n"
                f"رقم المستلم مش صح\n"
                f"لازم التحويل يكون على: {TRANSFER_NUMBER}"
            )
            del user_states[chat_id]
            return
        success = record_payment(student, amount, pay_type, date, time_str)
        if success:
            bot.reply_to(message,
                f"✅ تم قبول الإيصال وتسجيل الدفع!\n\n"
                f"👤 الاسم: {student['name']}\n"
                f"🏢 العمارة: {student['sheet']}\n"
                f"💰 المبلغ: {amount} جنيه\n"
                f"💳 نوع الدفع: {pay_type}\n"
                f"📅 التاريخ: {date}\n"
                f"⏰ الوقت: {time_str}"
            )
        else:
            bot.reply_to(message, "⚠️ حصل خطأ في التسجيل، حاول تاني")
    except Exception as e:
        logger.error(f"Error processing photo: {e}")
        bot.reply_to(message, "❌ مش قادر أقرأ الإيصال، تأكد إن الصورة واضحة وحاول تاني")
    if chat_id in user_states:
        del user_states[chat_id]

if __name__ == '__main__':
    logger.info("Bot started...")
    bot.polling(none_stop=True)
