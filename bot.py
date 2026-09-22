import os
import re
import logging
from datetime import datetime
import telebot
import gspread
from google.oauth2.service_account import Credentials
import requests
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
SHEET_ID = os.environ.get('SHEET_ID')
TRANSFER_NUMBER = os.environ.get('TRANSFER_NUMBER', '01152596770')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')

bot = telebot.TeleBot(TELEGRAM_TOKEN)

def get_sheet():
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
                        }
            except:
                continue
        return None
    except Exception as e:
        logger.error(f"Error: {e}")
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
            log_ws.append_row(['التاريخ', 'الوقت', 'الاسم', 'العمارة', 'الوحدة', 'المبلغ', 'نوع الدفع'])
        log_ws.append_row([date, time_str, student['name'], student['sheet'], student['unit'], amount, pay_type])
        return True
    except Exception as e:
        logger.error(f"Error: {e}")
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
        "👋 أهلاً في بوت سبا للإيجارات!\n\n"
        "📌 طريقة الاستخدام:\n"
        "1️⃣ اكتب اسم الطالب الرباعي\n"
        "2️⃣ بعدين ابعت صورة الإيصال مع المبلغ ونوع الدفع\n\n"
        "ابدأ بكتابة اسم الطالب 👇"
    )

@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()

    if chat_id in user_states and user_states[chat_id].get('step') == 'waiting_details':
        parts = text.split(',')
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ ابعت المبلغ ونوع الدفع بالشكل ده:\nمثال: 1500, انستاباي")
            return
        amount = parts[0].strip()
        pay_type = parts[1].strip()
        student = user_states[chat_id]['student']
        now = datetime.now()
        date = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M')
        if check_duplicate(student['name'], date):
            bot.reply_to(message, f"⚠️ {student['name']} دفع النهاردة قبل كده!")
            del user_states[chat_id]
            return
        success = record_payment(student, amount, pay_type, date, time_str)
        if success:
            bot.reply_to(message,
                f"✅ تم تسجيل الدفع!\n\n"
                f"👤 {student['name']}\n"
                f"🏢 {student['sheet']}\n"
                f"💰 {amount} جنيه\n"
                f"💳 {pay_type}\n"
                f"📅 {date} - {time_str}"
            )
        else:
            bot.reply_to(message, "⚠️ حصل خطأ، حاول تاني")
        del user_states[chat_id]
        return

    student = find_student(text)
    if student:
        user_states[chat_id] = {'student': student, 'step': 'waiting_photo'}
        bot.reply_to(message,
            f"✅ تم العثور على الطالب:\n\n"
            f"👤 {student['name']}\n"
            f"🏢 {student['sheet']}\n"
            f"🚪 {student['unit']}\n"
            f"💰 {student['rent']} جنيه\n\n"
            f"📸 ابعت صورة الإيصال"
        )
    else:
        bot.reply_to(message, f"❌ مش لاقي: {text}\nتأكد من الاسم الرباعي")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    if chat_id not in user_states or user_states[chat_id].get('step') != 'waiting_photo':
        bot.reply_to(message, "⚠️ ابعت اسم الطالب الأول")
        return
    user_states[chat_id]['step'] = 'waiting_details'
    user_states[chat_id]['photo'] = message.photo[-1].file_id
    bot.reply_to(message,
        "✅ استلمت الإيصال!\n\n"
        "دلوقتي ابعت المبلغ ونوع الدفع:\n"
        "مثال: 1500, انستاباي\n"
        "أو: 1500, كاش\n"
        "أو: 1500, تحويل بنكي"
    )

if __name__ == '__main__':
    logger.info("Bot started...")
    bot.polling(none_stop=True)
