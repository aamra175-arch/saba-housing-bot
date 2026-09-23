import os
import re
import logging
from datetime import datetime
import telebot
import gspread
from google.oauth2.service_account import Credentials
import requests
import json
import base64

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
SHEET_ID = os.environ.get('SHEET_ID')
TRANSFER_NUMBER = os.environ.get('TRANSFER_NUMBER', '01152596770')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

bot = telebot.TeleBot(TELEGRAM_TOKEN)

def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)

def verify_receipt(photo_file_id):
    try:
        file_info = bot.get_file(photo_file_id)
        file_path = file_info.file_path
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        img_response = requests.get(file_url)
        img_base64 = base64.b64encode(img_response.content).decode('utf-8')

        # تحديد نوع الصورة تلقائياً
        if file_path.lower().endswith('.png'):
            mime_type = 'image/png'
        elif file_path.lower().endswith('.webp'):
            mime_type = 'image/webp'
        else:
            mime_type = 'image/jpeg'

        prompt = f"""أنت مساعد للتحقق من إيصالات الدفع.
افحص هذه الصورة وأجب بـ JSON فقط بهذا الشكل بدون أي نص إضافي:
{{"is_receipt": true, "transfer_number_found": true, "amount": "المبلغ", "reason": ""}}

رقم التحويل المطلوب: {TRANSFER_NUMBER}

تحقق من:
1. هل الصورة إيصال دفع حقيقي (انستاباي أو كاش أو تحويل بنكي أو screenshot لتحويل)؟
2. هل يحتوي على رقم {TRANSFER_NUMBER}؟ (للانستاباي والتحويل فقط - للكاش اجعل transfer_number_found = true تلقائياً)
3. ما هو المبلغ الموجود في الإيصال بالأرقام فقط؟"""

        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": img_base64
                            }
                        },
                        {"text": prompt}
                    ]
                }],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 200
                }
            }
        )

        result = response.json()
        logger.info(f"Gemini response: {result}")

        if 'candidates' not in result:
            logger.error(f"Gemini error: {result}")
            return None

        text = result['candidates'][0]['content']['parts'][0]['text']
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return data
        return None
    except Exception as e:
        logger.error(f"Error verifying receipt: {e}")
        return None

def check_duplicate_receipt(amount, date):
    try:
        spreadsheet = get_sheet()
        try:
            log_ws = spreadsheet.worksheet('سجل المدفوعات')
            data = log_ws.get_all_values()
            for row in data[1:]:
                if len(row) >= 7 and row[0] == date and str(row[6]) == str(amount):
                    return True
        except:
            pass
        return False
    except:
        return False

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

        try:
            rent = float(str(student['rent']).replace(',', ''))
            paid = float(str(amount).replace(',', ''))
            diff = rent - paid
        except:
            rent = 0
            paid = 0
            diff = 0

        ws.update_cell(student['row'], 5, paid)
        ws.update_cell(student['row'], 6, diff if diff > 0 else 0)
        ws.update_cell(student['row'], 7, '✅ دفع كامل' if diff <= 0 else f'⚠️ دفع جزئي - متبقي {diff:.0f}')

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
            diff_text = "✅ دفع كامل" if diff <= 0 else f"⚠️ متبقي: {diff:.0f} جنيه"
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

    bot.reply_to(message, "⏳ بتحقق من الإيصال...")

    photo_file_id = message.photo[-1].file_id
    result = verify_receipt(photo_file_id)

    if result is None:
        bot.reply_to(message, "⚠️ مقدرتش أتحقق من الإيصال، حاول تاني")
        return

    if not result.get('is_receipt'):
        bot.reply_to(message, f"❌ الصورة دي مش إيصال دفع\n{result.get('reason', '')}")
        return

    if not result.get('transfer_number_found'):
        bot.reply_to(message, f"❌ الإيصال مش بيحتوي على رقم التحويل {TRANSFER_NUMBER}")
        return

    receipt_amount = result.get('amount')
    now = datetime.now()
    date = now.strftime('%Y-%m-%d')
    if receipt_amount and check_duplicate_receipt(receipt_amount, date):
        bot.reply_to(message, "❌ الإيصال ده اتسجل قبل كده!")
        return

    user_states[chat_id]['step'] = 'waiting_amount'
    user_states[chat_id]['photo'] = photo_file_id
    user_states[chat_id]['receipt_amount'] = receipt_amount

    amount_hint = f"\n💡 المبلغ في الإيصال: {receipt_amount} جنيه" if receipt_amount else ""
    bot.reply_to(message, f"✅ الإيصال تمام!{amount_hint}\n\n💰 ادخل المبلغ المدفوع:\nمثال: 1500")

if __name__ == '__main__':
    logger.info("Bot started...")
    bot.polling(none_stop=True)
