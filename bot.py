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
                    if len(row) >= 3 and name.strip().lower() in row[2].strip().lower():
                        return {
                            'sheet': sheet_name,
                            'row': i + 1,
                            'name': row[2],
                            'unit': row[1] if len(row) > 1 else '',
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
        
        # حساب الفرق
        try:
            rent = float(str(student['rent']).replace(',', ''))
            paid = float(str(amount).replace(',', ''))
            diff = rent - paid
        except:
            rent = 0
            paid = 0
            diff = 0

        # تحديث الشيت الرئيسي
        ws.update_cell(student['row'], 5, paid)
        ws.update_cell(student['row'], 6, diff if diff > 0 else 0)
        ws.update_cell(student['row'], 7, '✅ دفع كامل' if diff <= 0 else f'⚠️ دفع جزئي - متبقي {diff:.0f}')

        # سجل المدفوعات
        try:
            log_ws = spreadsheet.worksheet('سجل المدفوعات')
        except:
            log_ws = spreadsheet.add_worksheet('سجل المدفوعات', 1000, 10)
            log_ws.append_row(['التاريخ', 'الوقت', 'الاسم', 'العمارة', 'الوحدة', 'الإيجار', 'المبلغ المدفوع', 'الفرق', 'نوع الدفع'])
        
        log_ws.append_row([date, time_str, student['name'], student['sheet'], student['unit'], rent, paid, diff if diff > 0 else 0, pay_type])
        return True, diff
    except Exception as e:
        logger.error(f"Error: {e}")
        return False, 0

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
        "1️⃣ اكتب اسم الطالب\n"
        "2️⃣ ابعت صورة الإيصال\n"
        "3️⃣ ابعت المبلغ\n"
        "4️⃣ ابعت نوع الدفع\n\n"
        "ابدأ بكتابة اسم الطالب 👇"
    )

@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()

    # انتظار المبلغ
    if chat_id in user_states and user_states[chat_id].get('step') == 'waiting_amount':
        try:
            float(text.replace(',', ''))
        except:
            bot.reply_to(message, "⚠️ ادخل المبلغ كرقم فقط\nمثال: 1500")
            return
        user_states[chat_id]['amount'] = text
        user_states[chat_id]['step'] = 'waiting_pay_type'
        bot.reply_to(message, "💳 ادخل نوع الدفع:\nمثال: انستاباي / كاش / تحويل بنكي")
        return

    # انتظار نوع الدفع
    if chat_id in user_states and user_states[chat_id].get('step') == 'waiting_pay_type':
        pay_type = text
        amount = user_states[chat_id]['amount']
        student = user_states[chat_id]['student']
        now = datetime.now()
        date = now.strftime('%Y-%m-%d')
        time_str = now.strftime('%H:%M')

        if check_duplicate(student['name'], date):
            bot.reply_to(message, f"⚠️ {student['name']} دفع النهاردة قبل كده!")
            del user_states[chat_id]
            return

        success, diff = record_payment(student, amount, pay_type, date, time_str)
        if success:
            diff_text = f"✅ دفع كامل" if diff <= 0 else f"⚠️ متبقي: {diff:.0f} جنيه"
            bot.reply_to(message,
                f"✅ تم تسجيل الدفع!\n\n"
                f"👤 {student['name']}\n"
                f"🏢 {student['sheet']}\n"
                f"💰 المدفوع: {amount} جنيه\n"
                f"💳 {pay_type}\n"
                f"📊 {diff_text}\n"
                f"📅 {date} - {time_str}"
            )
        else:
            bot.reply_to(message, "⚠️ حصل خطأ، حاول تاني")
        del user_states[chat_id]
        return

    # البحث عن الطالب
    student = find_student(text)
    if student:
        user_states[chat_id] = {'student': student, 'step': 'waiting_photo'}
        bot.reply_to(message,
            f"✅ تم العثور على الطالب:\n\n"
            f"👤 {student['name']}\n"
            f"🏢 {student['sheet']}\n"
            f"🚪 {student['unit']}\n"
            f"💰 الإيجار: {student['rent']} جنيه\n\n"
            f"📸 ابعت صورة الإيصال"
        )
    else:
        bot.reply_to(message, f"❌ مش لاقي: {text}\nتأكد من الاسم")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id
    if chat_id not in user_states or user_states[chat_id].get('step') != 'waiting_photo':
        bot.reply_to(message, "⚠️ ابعت اسم الطالب الأول")
        return
    user_states[chat_id]['step'] = 'waiting_amount'
    user_states[chat_id]['photo'] = message.photo[-1].file_id
    bot.reply_to(message, "✅ استلمت الإيصال!\n\n💰 ادخل المبلغ المدفوع:\nمثال: 1500")

if __name__ == '__main__':
    logger.info("Bot started...")
    bot.polling(none_stop=True)
