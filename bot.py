import os
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import telebot
import gspread
from google.oauth2.service_account import Credentials
import requests
import json
import base64

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
SHEET_ID = os.environ.get('SHEET_ID')                      # شيت الإيرادات
EXPENSES_SHEET_ID = os.environ.get('EXPENSES_SHEET_ID')    # شيت المصاريف
TRANSFER_NUMBER = os.environ.get('TRANSFER_NUMBER', '01152596770')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.6-flash')
# اختياري: أرقام شات تليجرام المسموح لها، مفصولة بفاصلة. لو فاضي أي حد يقدر يستخدم البوت
ALLOWED_CHAT_IDS = {s.strip() for s in os.environ.get('ALLOWED_CHAT_IDS', '').split(',') if s.strip()}

TZ = ZoneInfo('Africa/Cairo')
BUILDINGS = ['سبا 1', 'سبا 2', 'سبا 3']
LOG_SHEET = 'سجل المدفوعات'
EXPENSES_LOG = 'سجل المصاريف'
EXPENSE_CATEGORIES = ['كهرباء', 'مياه', 'غاز', 'إنترنت', 'صيانة وسباكة', 'نظافة', 'مرتبات ومشرفات',
                      'إيجار للمالك', 'عمولة سمسار', 'أثاث ومشتريات', 'نثريات', 'أخرى']

RECIPIENT_NAME_AR = 'يوسف منصور'
RECIPIENT_NAME_EN = 'yousef mansour'
RECIPIENT_INITIALS = ['y', 'm', 'a', 'm']

bot = telebot.TeleBot(TELEGRAM_TOKEN)
_client = None


# ===================== أدوات =====================

def gclient():
    global _client
    if _client is None:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        _client = gspread.authorize(creds)
    return _client


def get_sheet():
    return gclient().open_by_key(SHEET_ID)


def now_cairo():
    now = datetime.now(TZ)
    return now.strftime('%Y-%m-%d'), now.strftime('%H:%M')


def to_western_digits(s):
    s = str(s)
    s = s.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))
    return s.replace('٬', ',').replace('٫', '.')


def to_number(s):
    """'1,400' أو '١٤٠٠' أو '1400 جنيه' -> 1400.0 ، ولو مش رقم -> None"""
    s = to_western_digits(s)
    m = re.search(r'\d[\d,]*(\.\d+)?', s)
    if not m:
        return None
    try:
        return float(m.group().replace(',', ''))
    except ValueError:
        return None


def fmt(x):
    try:
        x = float(x)
        return f'{x:,.0f}' if x == int(x) else f'{x:,.2f}'
    except (TypeError, ValueError):
        return str(x)


def norm(s):
    s = to_western_digits(s)
    s = re.sub(r'[\u064B-\u0652\u0640]', '', s)
    s = re.sub('[أإآ]', 'ا', s).replace('ة', 'ه').replace('ى', 'ي')
    return re.sub(r'\s+', ' ', s).strip().lower()


def normalize_method(t):
    n = norm(t)
    insta = bool(re.search(r'انستا|instapay|insta', n))
    cash = bool(re.search(r'كاش|نقدي|cash', n)) and 'فودافون' not in n
    if insta and cash:
        return 'كاش وانستا باي'
    if insta:
        return 'انستا باي'
    if re.search(r'فودافون|محفظه|اورانج|اتصالات|wallet', n):
        return 'محفظة'
    if re.search(r'تحويل|بنك', n):
        return 'تحويل بنكي'
    if cash:
        return 'كاش'
    return t.strip() or 'غير محدد'


def allowed(message):
    return not ALLOWED_CHAT_IDS or str(message.chat.id) in ALLOWED_CHAT_IDS


# ===================== Gemini =====================

def verify_receipt(photo_file_id):
    try:
        file_info = bot.get_file(photo_file_id)
        file_path = file_info.file_path
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        img_response = requests.get(file_url, timeout=30)
        img_base64 = base64.b64encode(img_response.content).decode('utf-8')

        if file_path.lower().endswith('.png'):
            mime_type = 'image/png'
        elif file_path.lower().endswith('.webp'):
            mime_type = 'image/webp'
        else:
            mime_type = 'image/jpeg'

        prompt = f"""أنت مساعد للتحقق من إيصالات الدفع.
افحص هذه الصورة وأجب بـ JSON فقط بهذا الشكل بدون أي نص إضافي ولا markdown:
{{"is_receipt": true, "recipient_verified": true, "amount": "المبلغ بالأرقام فقط", "reason": ""}}

تحقق من:
1. هل الصورة إيصال دفع حقيقي؟ مقبول: انستاباي، كاش، تحويل بنكي، SMS تحويل، صورة رسالة تحويل بنكي.
2. تحقق من اسم المستلم - مقبول أي من التالي:
   - رقم انستاباي: {TRANSFER_NUMBER}
   - اسم عربي يحتوي على: {RECIPIENT_NAME_AR}
   - اسم إنجليزي يبدأ بـ: y m a m (مع نجوم بين الحروف مثل: yousef m******* a** m***)
   - إذا الاسم مخفي جزئياً بنجوم (*) تحقق أن أول حرف من كل جزء هو: y ثم m ثم a ثم m
   - للكاش: recipient_verified = true تلقائياً
3. اقرأ المبلغ بالأرقام فقط بدون كلمة جنيه أو EGP.

إذا مش قادر تتحقق من أي معلومة اجعل is_receipt = false واكتب السبب في reason.
أجب بـ JSON فقط."""

        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": mime_type, "data": img_base64}},
                        {"text": prompt}
                    ]
                }],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 1000,
                    "thinkingConfig": {"thinkingBudget": 0}
                }
            },
            timeout=60
        )

        result = response.json()
        logger.info(f"Gemini response: {result}")

        if 'candidates' not in result:
            logger.error(f"Gemini error: {result}")
            return None

        text = result['candidates'][0]['content']['parts'][0]['text']
        text = text.strip().replace('```json', '').replace('```', '').strip()
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return None
    except Exception as e:
        logger.error(f"Error verifying receipt: {e}")
        return None


# ===================== الطالبات =====================

def find_students(query):
    """بيدوّر في العماير التلاتة. لو فيه تطابق كامل بيرجعه، غير كده كل اللي فيهم كل كلمات البحث."""
    q = norm(query)
    building = None
    m = re.search(r'سبا\s*([123])', q)
    if m:
        building = f'سبا {m.group(1)}'
        q = norm(q.replace(m.group(0), ' '))
    if not q:
        return []
    words = q.split(' ')
    exact, partial = [], []
    spreadsheet = get_sheet()
    for sheet_name in ([building] if building else BUILDINGS):
        try:
            rows = spreadsheet.worksheet(sheet_name).get_all_values()
        except Exception as e:
            logger.error(f"Error reading {sheet_name}: {e}")
            continue
        for i, row in enumerate(rows[1:], start=2):
            if len(row) < 4 or not row[2].strip() or to_number(row[3]) is None:
                continue  # لازم اسم ومبلغ تأمين (المشرفات والسراير الفاضية بتتشال)
            name_n = norm(row[2])
            s = {
                'sheet': sheet_name, 'row': i, 'name': row[2].strip(),
                'apt': row[0].strip(), 'room': row[1].strip(),
                'rent': to_number(row[3]),
            }
            if name_n == q:
                exact.append(s)
            elif all((' ' + name_n + ' ').find(' ' + w) != -1 for w in words):
                partial.append(s)
    return exact or partial


def read_student_status(student):
    """بيقرا المدفوع والمتبقي والحالة اللي الشيت حسبها من سجل المدفوعات."""
    ws = get_sheet().worksheet(student['sheet'])
    vals = ws.get(f"D{student['row']}:G{student['row']}")
    row = (vals[0] if vals else []) + ['', '', '', '']
    return {'due': to_number(row[0]), 'paid': to_number(row[1]) or 0,
            'remaining': to_number(row[2]), 'status': row[3]}


def record_payment(student, amount, pay_type, date, time_str, note=''):
    """بيضيف سطر في سجل المدفوعات بس. المدفوع والمتبقي والحالة بتتحسب في الشيت بمعادلات."""
    try:
        log_ws = get_sheet().worksheet(LOG_SHEET)
        log_ws.append_row(
            [date, time_str, student['name'], student['sheet'],
             f"{student['apt']} - {student['room']}", amount, pay_type, 'تليجرام', note],
            value_input_option='USER_ENTERED', table_range='A1')
        return True
    except Exception as e:
        logger.error(f"Error recording payment: {e}")
        return False


def check_duplicate(student, date):
    try:
        data = get_sheet().worksheet(LOG_SHEET).get_all_values()
        return any(len(r) >= 4 and r[0] == date and r[2] == student['name'] and r[3] == student['sheet']
                   for r in data[1:])
    except Exception:
        return False


def check_duplicate_receipt(amount, date):
    try:
        data = get_sheet().worksheet(LOG_SHEET).get_all_values()
        return any(len(r) >= 6 and r[0] == date and to_number(r[5]) == amount for r in data[1:])
    except Exception:
        return False


# ===================== المصاريف =====================

def parse_expense(text):
    """مصروف سبا 1 كهرباء 850 كاش عداد شقة 3"""
    t = to_western_digits(text)
    t = re.sub(r'^\s*(مصروف|مصاريف|صرف)\s*', '', t)
    building = 'عام'
    m = re.search(r'سبا\s*([123])', t)
    if m:
        building = f'سبا {m.group(1)}'
        t = t.replace(m.group(0), ' ')
    elif re.search(r'\bعام\b', t):
        t = re.sub(r'\bعام\b', ' ', t, count=1)

    m = re.search(r'\d[\d,]*(\.\d+)?', t)
    if not m:
        return None, 'مش لاقي مبلغ في الرسالة.'
    amount = float(m.group().replace(',', ''))
    t = (t[:m.start()] + ' ' + t[m.end():])

    category = 'أخرى'
    tokens = t.split()
    found = False
    for c in EXPENSE_CATEGORIES[:-1]:
        stem = norm(c).split(' ')[0][:4]
        for i, tok in enumerate(tokens):
            if norm(tok).startswith(stem):
                category = c
                tokens.pop(i)
                found = True
                break
        if found:
            break
    t = ' '.join(tokens)

    method = 'كاش'
    for word, label in [('انستا', 'انستا باي'), ('كاش', 'كاش'), ('محفظ', 'محفظة'), ('فودافون', 'محفظة'), ('تحويل', 'تحويل بنكي')]:
        if word in norm(t):
            method = label
            t = re.sub(word + r'\S*(\s*باي)?', ' ', t, count=1)
            break

    desc = re.sub(r'\s+', ' ', t).strip(' -،,')
    return {'building': building, 'category': category, 'amount': amount, 'method': method, 'desc': desc}, None


def record_expense(exp, who):
    if not EXPENSES_SHEET_ID:
        return False, 'متغير EXPENSES_SHEET_ID مش متظبط على السيرفر.'
    try:
        ws = gclient().open_by_key(EXPENSES_SHEET_ID).worksheet(EXPENSES_LOG)
        date, _ = now_cairo()
        ws.append_row([date, exp['building'], exp['category'], exp['desc'], exp['amount'],
                       exp['method'], who, 'تليجرام', ''],
                      value_input_option='USER_ENTERED', table_range='A1')
        return True, None
    except Exception as e:
        logger.error(f"Error recording expense: {e}")
        return False, 'حصل خطأ وأنا بسجل في شيت المصاريف.'


# ===================== الرسائل =====================

user_states = {}

HELP = (
    "👋 أهلاً في بوت سبا!\n\n"
    "💰 تسجيل إيراد:\n"
    "1️⃣ اكتب اسم الطالبة (ولو الاسم متكرر زوّد: سبا 2)\n"
    "2️⃣ ابعت صورة الإيصال\n"
    "3️⃣ ابعت نوع الدفع (انستاباي / كاش / تحويل)\n\n"
    "🧾 تسجيل مصروف (رسالة واحدة):\n"
    "مصروف سبا 1 كهرباء 850 كاش عداد شقة 3\n"
    "(من غير عمارة يتسجل على \"عام\")\n\n"
    "📋 حالة طالبة: حالة عزه صلاح\n"
    "❌ إلغاء العملية الحالية: الغاء"
)


@bot.message_handler(commands=['start', 'help'])
def start(message):
    if not allowed(message):
        return
    bot.reply_to(message, HELP)


def describe(s):
    return f"👤 {s['name']}\n🏢 {s['sheet']} - {s['apt']} - {s['room']}\n💰 التأمين: {fmt(s['rent'])} جنيه"


def select_student(message, student):
    chat_id = message.chat.id
    user_states[chat_id] = {'student': student, 'step': 'waiting_photo'}
    try:
        st = read_student_status(student)
        extra = f"\n📊 المدفوع لحد دلوقتي: {fmt(st['paid'])} | المتبقي: {fmt(st['remaining'])}"
    except Exception:
        extra = ''
    bot.reply_to(message, f"✅ تم العثور على الطالبة:\n\n{describe(student)}{extra}\n\n📸 ابعت صورة الإيصال")


@bot.message_handler(content_types=['text'])
def handle_text(message):
    if not allowed(message):
        return
    chat_id = message.chat.id
    text = message.text.strip()
    n = norm(text)
    state = user_states.get(chat_id, {})

    if n in ('الغاء', 'إلغاء', 'cancel'):
        user_states.pop(chat_id, None)
        bot.reply_to(message, "تم الإلغاء.")
        return

    if re.match(r'^(مصروف|مصاريف|صرف)\b', n):
        exp, err = parse_expense(text)
        if err:
            bot.reply_to(message, f"⚠️ {err}\nمثال: مصروف سبا 1 كهرباء 850 كاش عداد شقة 3")
            return
        who = message.from_user.first_name or str(message.from_user.id)
        ok, err = record_expense(exp, who)
        if ok:
            bot.reply_to(message,
                f"✅ تم تسجيل المصروف\n\n🏢 {exp['building']}\n📂 {exp['category']}\n"
                f"💸 {fmt(exp['amount'])} جنيه ({exp['method']})" + (f"\n📝 {exp['desc']}" if exp['desc'] else ''))
        else:
            bot.reply_to(message, f"⚠️ {err}")
        return

    if n.startswith('حاله '):
        found = find_students(text.split(' ', 1)[1])
        if not found:
            bot.reply_to(message, "❌ مش لاقي الاسم ده.")
            return
        lines = []
        for s in found[:8]:
            st = read_student_status(s)
            lines.append(f"{describe(s)}\n📊 المدفوع {fmt(st['paid'])} | المتبقي {fmt(st['remaining'])} | {st['status']}")
        bot.reply_to(message, '\n\n'.join(lines))
        return

    if state.get('step') == 'choosing' and re.fullmatch(r'\d+', to_western_digits(text)):
        idx = int(to_western_digits(text)) - 1
        options = state['options']
        if 0 <= idx < len(options):
            select_student(message, options[idx])
        else:
            bot.reply_to(message, f"اختار رقم من 1 لـ {len(options)}")
        return

    if state.get('step') == 'waiting_pay_type':
        pay_type = normalize_method(text)
        amount = state['amount']
        student = state['student']
        date, time_str = now_cairo()

        if check_duplicate(student, date) and not state.get('confirmed_dup'):
            state['confirmed_dup'] = True
            state['pending_pay_type'] = pay_type
            state['step'] = 'confirm_duplicate'
            bot.reply_to(message, f"⚠️ {student['name']} اتسجلها دفعة النهارده قبل كده.\nلو دي دفعة تانية فعلاً ابعت: تأكيد\nأو ابعت: الغاء")
            return
        finish_payment(message, student, amount, pay_type, date, time_str)
        return

    if state.get('step') == 'confirm_duplicate':
        if n in ('تاكيد', 'تأكيد', 'ايوه', 'نعم'):
            date, time_str = now_cairo()
            finish_payment(message, state['student'], state['amount'], state['pending_pay_type'], date, time_str)
        else:
            bot.reply_to(message, "ابعت: تأكيد ، أو: الغاء")
        return

    found = find_students(text)
    if not found:
        bot.reply_to(message, f"❌ مش لاقي: {text}\nتأكد من الاسم، أو ابعت /help")
    elif len(found) == 1:
        select_student(message, found[0])
    else:
        options = found[:10]
        user_states[chat_id] = {'step': 'choosing', 'options': options}
        lst = '\n'.join(f"{i}. {s['name']} ({s['sheet']} - {s['apt']} - {s['room']})" for i, s in enumerate(options, 1))
        bot.reply_to(message, f"لقيت أكتر من طالبة، ابعت رقم الصح:\n\n{lst}")


def finish_payment(message, student, amount, pay_type, date, time_str):
    chat_id = message.chat.id
    if record_payment(student, amount, pay_type, date, time_str):
        try:
            st = read_student_status(student)
            status = f"📊 إجمالي المدفوع: {fmt(st['paid'])} من {fmt(st['due'])}\n📌 المتبقي: {fmt(st['remaining'])} | {st['status']}"
        except Exception:
            status = ''
        bot.reply_to(message,
            f"✅ تم تسجيل الدفع!\n\n"
            f"👤 {student['name']}\n"
            f"🏢 {student['sheet']} - {student['apt']}\n"
            f"💰 المدفوع: {fmt(amount)} جنيه\n"
            f"💳 {pay_type}\n"
            f"{status}\n"
            f"📅 {date} - {time_str}")
    else:
        bot.reply_to(message, "⚠️ حصل خطأ، حاول تاني")
    user_states.pop(chat_id, None)


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if not allowed(message):
        return
    chat_id = message.chat.id
    if user_states.get(chat_id, {}).get('step') != 'waiting_photo':
        bot.reply_to(message, "⚠️ ابعت اسم الطالبة الأول")
        return

    bot.reply_to(message, "⏳ بتحقق من الإيصال...")
    photo_file_id = message.photo[-1].file_id
    result = verify_receipt(photo_file_id)

    if result is None:
        bot.reply_to(message, "⚠️ مقدرتش أتحقق من الإيصال، حاول تاني")
        return
    if not result.get('is_receipt'):
        bot.reply_to(message, f"❌ الإيصال مرفوض\n{result.get('reason', 'الصورة مش إيصال دفع')}")
        return
    if not result.get('recipient_verified'):
        bot.reply_to(message, "❌ الإيصال مش بيحتوي على اسم أو رقم المستلم الصح")
        return

    amount = to_number(result.get('amount'))
    if not amount:
        bot.reply_to(message, "❌ مش قادر أقرأ المبلغ من الإيصال، ابعت إيصال أوضح")
        return

    date, _ = now_cairo()
    if check_duplicate_receipt(amount, date):
        bot.reply_to(message, "⚠️ فيه دفعة بنفس المبلغ اتسجلت النهارده. لو الإيصال ده مختلف كمّل عادي، غير كده ابعت: الغاء")

    user_states[chat_id].update({'step': 'waiting_pay_type', 'photo': photo_file_id, 'amount': amount})
    bot.reply_to(message,
        f"✅ الإيصال تمام!\n\n💰 المبلغ: {fmt(amount)} جنيه\n\n💳 ادخل نوع الدفع:\nانستاباي / كاش / تحويل بنكي")


if __name__ == '__main__':
    logger.info("Bot started...")
    bot.polling(none_stop=True)
