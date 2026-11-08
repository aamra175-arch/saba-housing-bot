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
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
SHEET_ID = os.environ.get('SHEET_ID')                      # شيت الإيرادات
EXPENSES_SHEET_ID = os.environ.get('EXPENSES_SHEET_ID')    # شيت المصاريف
TRANSFER_NUMBER = os.environ.get('TRANSFER_NUMBER', '01152596770')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
# موديل Claude: لو مش متحدد، البوت بيختار أحدث Sonnet متاح على الحساب
CLAUDE_MODEL = os.environ.get('CLAUDE_MODEL', '')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
# موديل Groq اللي بيقرا صور (حسب docs/vision في Groq). لو مش متاح البوت بيدوّر على غيره.
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'qwen/qwen3.8-27b')
# مين يقرا الإيصالات الأول: groq أو gemini أو claude. الباقيين بيبقوا احتياطي.
PRIMARY_AI = os.environ.get('PRIMARY_AI', 'claude').strip().lower()
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.6-flash')
# موديل احتياطي لو الأساسي زحمة (503) بعد كذا محاولة
GEMINI_FALLBACK_MODEL = os.environ.get('GEMINI_FALLBACK_MODEL', 'gemini-3.8-flash')
# اختياري: أرقام شات تليجرام المسموح لها، مفصولة بفاصلة. لو فاضي أي حد يقدر يستخدم البوت
ALLOWED_CHAT_IDS = {s.strip() for s in os.environ.get('ALLOWED_CHAT_IDS', '').split(',') if s.strip()}

TZ = ZoneInfo('Africa/Cairo')
BUILDINGS = ['سبا 1', 'سبا 2', 'سبا 3']
LOG_SHEET = 'سجل المدفوعات'
EXPENSES_LOG = 'سجل المصاريف'
EXPENSE_CATEGORIES = ['كهرباء', 'مياه', 'غاز', 'إنترنت', 'صيانة وسباكة', 'نظافة', 'مرتبات ومشرفات',
                      'إيجار للمالك', 'عمولة سمسار', 'أثاث ومشتريات', 'نثريات', 'أخرى']

# المستلمين المقبولين في الإيصال: أي رقم من دول، أو أي اسم من دول
ACCEPTED_NUMBERS = list(dict.fromkeys(
    [TRANSFER_NUMBER, '01152596793', '01030004184']
    + [n.strip() for n in os.environ.get('EXTRA_TRANSFER_NUMBERS', '').split(',') if n.strip()]))
ACCEPTED_RECIPIENTS = [
    # (الاسم بالعربي، الاسم بالإنجليزي وطرق كتابته، أول حرف من كل جزء للاسم المخفي بالنجوم، مثال للاسم المخفي)
    ('يوسف منصور', 'Yousef Mansour', 'y m a m', 'yousef m******* a** m***'),
    ('عبدالرحمن محمد ابوعمره محمود',
     'Abdelrahman Mohamed Abouamra Mahmoud (أو Abdulrahman / Abdel Rahman ، Mohammed ، Abu Amra / Abo Amra)',
     'a m a m', 'abdelrahman m****** a******* m******'),
]
RECIPIENT_NAME_AR = ' أو '.join(r[0] for r in ACCEPTED_RECIPIENTS)

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


# ===================== قراءة الإيصالات (Groq / Gemini / Claude) =====================

MAX_RECEIPT_AGE_DAYS = int(os.environ.get('MAX_RECEIPT_AGE_DAYS', '3'))


_claude_model_cache = None


def pick_claude_model():
    """بيرجّع موديل Claude: من CLAUDE_MODEL لو متحدد، وإلا أحدث Sonnet متاح على الحساب."""
    global _claude_model_cache
    if CLAUDE_MODEL:
        return CLAUDE_MODEL
    if _claude_model_cache:
        return _claude_model_cache
    r = requests.get('https://api.anthropic.com/v1/models?limit=100',
                     headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'}, timeout=30)
    data = r.json().get('data', [])
    ids = [m.get('id', '') for m in data]
    logger.info(f"Claude models available: {ids}")
    for family in ('sonnet', 'opus', 'haiku'):
        for mid in ids:
            if family in mid:
                _claude_model_cache = mid
                return mid
    if ids:
        _claude_model_cache = ids[0]
        return ids[0]
    raise RuntimeError(f"No Claude models available: {r.text[:300]}")


def call_claude(prompt, mime_type, img_base64):
    """بيرجّع (النص، None) لو نجح، أو (None, 'quota'/'busy'/'nokey')."""
    if not ANTHROPIC_API_KEY:
        return None, 'nokey'
    for attempt in range(1, 4):
        try:
            model = pick_claude_model()
            r = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01',
                         'content-type': 'application/json'},
                json={
                    'model': model,
                    'max_tokens': 1000,
                    'temperature': 0,
                    'messages': [{'role': 'user', 'content': [
                        {'type': 'image', 'source': {'type': 'base64', 'media_type': mime_type, 'data': img_base64}},
                        {'type': 'text', 'text': prompt},
                    ]}],
                },
                timeout=90)
            result = r.json()
        except Exception as e:
            logger.error(f"Claude request failed (attempt {attempt}): {e}")
            result = None
        if result and result.get('type') == 'message':
            text = ''.join(b.get('text', '') for b in result.get('content', []) if b.get('type') == 'text')
            logger.info(f"Claude response ({result.get('model')}): {text}")
            return text, None
        logger.error(f"Claude error (attempt {attempt}): {result}")
        err = (result or {}).get('error', {}) if isinstance(result, dict) else {}
        msg = str(err.get('message', '')).lower()
        if 'credit' in msg or 'billing' in msg:
            return None, 'quota'
        if err.get('type') in ('authentication_error', 'permission_error', 'invalid_request_error', 'not_found_error'):
            return None, 'busy'
        if attempt < 3:
            time.sleep(2 * attempt)
    return None, 'busy'


_groq_models_cache = None


def groq_vision_models():
    """الموديلات اللي هنجربها على Groq بالترتيب: GROQ_MODEL لو متحدد، وبعدها الموديلات اللي باين إنها بتقرا صور."""
    global _groq_models_cache
    if _groq_models_cache is None:
        try:
            r = requests.get('https://api.groq.com/openai/v1/models',
                             headers={'Authorization': f'Bearer {GROQ_API_KEY}'}, timeout=30)
            ids = [m.get('id', '') for m in r.json().get('data', []) if m.get('active', True)]
        except Exception as e:
            logger.error(f"Groq models list failed: {e}")
            ids = []
        logger.info(f"Groq models available: {ids}")
        hints = ('vision', 'llama-4', 'scout', 'maverick', '-vl', 'vl-', 'qwen2.5-vl', 'qwen3.8', 'gemma-3', 'pixtral', 'kimi')
        vision = [m for m in ids if any(h in m.lower() for h in hints)]
        # الأكبر غالبا أدق في القراية
        vision.sort(key=lambda m: (('maverick' not in m), ('90b' not in m), m))
        _groq_models_cache = vision
    models = list(_groq_models_cache)
    if GROQ_MODEL:
        models = [GROQ_MODEL] + [m for m in models if m != GROQ_MODEL]
    return models


def call_groq(prompt, mime_type, img_base64):
    """بيرجّع (النص، None) لو نجح، أو (None, 'quota'/'busy')."""
    if not GROQ_API_KEY:
        return None, 'nokey'
    models = groq_vision_models()
    if not models:
        return None, 'busy'
    quota_hit = False
    for model in models[:4]:
        body = {
            'model': model,
            'temperature': 0,
            'max_tokens': 1000,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': prompt},
                {'type': 'image_url', 'image_url': {'url': f'data:{mime_type};base64,{img_base64}'}},
            ]}],
        }
        if model.startswith('qwen/'):
            # من غير تفكير (أسرع وبيوفر من الحد اليومي)، والرد JSON على طول
            body['reasoning_effort'] = 'none'
            body['response_format'] = {'type': 'json_object'}
        for attempt in range(1, 4):
            try:
                r = requests.post(
                    'https://api.groq.com/openai/v1/chat/completions',
                    headers={'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'},
                    json=body,
                    timeout=90)
                result = r.json()
            except Exception as e:
                logger.error(f"Groq request failed (attempt {attempt}, {model}): {e}")
                result = None
            if result and result.get('choices'):
                text = result['choices'][0].get('message', {}).get('content') or ''
                logger.info(f"Groq response ({model}): {text}")
                return text, None
            logger.error(f"Groq error (attempt {attempt}, {model}): {result}")
            err = (result or {}).get('error', {}) if isinstance(result, dict) else {}
            code = str(err.get('code', '')) + ' ' + str(err.get('type', '')) + ' ' + str(err.get('message', '')).lower()
            if 'rate_limit' in code or 'quota' in code or 'tokens per day' in code or 'requests per day' in code:
                # لو حد الدقيقة: Groq بيقول "try again in Xs"، نستنى ونعيد بدل ما نعتبره خلص
                wait = re.search(r'try again in (?:(\d+)m)?([\d.]+)s', code)
                secs = (int(wait.group(1) or 0) * 60 + float(wait.group(2))) if wait else None
                if secs is not None and secs <= 40 and 'per day' not in code and attempt < 3:
                    time.sleep(secs + 1)
                    continue
                quota_hit = True
                break
            if result is not None:
                break  # خطأ من الموديل نفسه (مثلا مابيقراش صور): نجرب الموديل اللي بعده
            time.sleep(2)
    return None, ('quota' if quota_hit else 'busy')


def call_gemini(prompt, mime_type, img_base64):
    """بيرجّع (النص، None) لو نجح، أو (None, 'quota'/'busy')."""
    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": img_base64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 1000,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0}
        }
    }
    # Gemini ساعات بيرد 503 (زحمة) أو الاتصال بيتقطع: نحاول كذا مرة بالموديل الأساسي.
    # لو رد 429 (الحد اليومي خلص) أو 404 (الموديل مش متاح)، ننقل على طول للموديل الاحتياطي.
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
        models.append(GEMINI_FALLBACK_MODEL)
    result = None
    quota_hit = False
    for model in models:
        for attempt in range(1, 4):
            try:
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=60
                )
                result = response.json()
            except Exception as e:
                logger.error(f"Gemini request failed (attempt {attempt}, {model}): {e}")
                result = None
            if result and 'candidates' in result:
                break
            logger.error(f"Gemini error (attempt {attempt}, {model}): {result}")
            code = (result or {}).get('error', {}).get('code') if isinstance(result, dict) else None
            if code == 429:
                quota_hit = True
                break
            if code in (400, 403, 404):
                break
            if attempt < 3:
                time.sleep(2 * attempt)
        if result and 'candidates' in result:
            break

    logger.info(f"Gemini response: {result}")
    if not result or 'candidates' not in result:
        return None, ('quota' if quota_hit else 'busy')
    return result['candidates'][0]['content']['parts'][0]['text'], None


def verify_receipt(photo_file_id):
    """بيبعت صورة الإيصال لـ Gemini ويرجّع البيانات اللي قراها منها، أو None لو حصل خطأ."""
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

        today, _ = now_cairo()
        numbers_text = '، '.join(ACCEPTED_NUMBERS)
        names_text = '\n'.join(
            f"   - اسم عربي فيه: {ar} ، أو بالإنجليزي: {en} (ومقبول الاسم المخفي جزئيا بنجوم لو أول حرف من كل جزء بالترتيب: {ini}، مثال: {masked})"
            for ar, en, ini, masked in ACCEPTED_RECIPIENTS)
        prompt = f"""أنت مراجع حسابات بتتحقق من إيصالات دفع إيجار. تاريخ النهارده {today} بتوقيت القاهرة.
افحص الصورة وأجب بـ JSON فقط بالشكل ده:
{{"is_receipt": true, "recipient_verified": true, "recipient_found": "", "amount": "", "date": "", "time": "", "reference": "", "payment_method": "", "tampering_suspected": false, "tampering_reason": "", "reason": ""}}

1. is_receipt: true فقط لو الصورة إيصال أو رسالة تأكيد لعملية دفع تمت بنجاح (انستاباي، تحويل بنكي، محفظة، رسالة SMS من البنك، إيصال كاش مكتوب). false لو صورة عادية، أو طلب تحويل لسه ما تمش، أو عملية فاشلة أو مرفوضة.
2. recipient_found: اكتب اسم أو رقم المستلم زي ما هو مكتوب في الإيصال بالظبط.
   recipient_verified = true فقط لو المستلم واحد من دول:
   - رقم انستاباي أو موبايل من دول: {numbers_text}
{names_text}
   لو المستلم مش ظاهر أو مختلف: recipient_verified = false. ماتخلطش بين المرسل والمستلم.
3. amount: المبلغ المحول بالأرقام الإنجليزي فقط (مثال 1400)، من غير عملة ولا رسوم.
4. date: تاريخ العملية بصيغة YYYY-MM-DD. الإيصالات المصرية بتكتب اليوم قبل الشهر (01/11/2026 يعني 1 نوفمبر 2026). لو مكتوب النهارده أو Today استخدم {today}. لو اليوم والشهر مكتوبين من غير سنة استخدم سنة النهارده. لو التاريخ مش ظاهر اكتب "".
5. time: وقت العملية بصيغة HH:MM (24 ساعة) لو ظاهر، غير كده "".
6. reference: رقم العملية أو Reference أو Transaction ID أو رقم المرجع زي ما هو مكتوب، غير كده "".
7. payment_method: واحد من: انستا باي، تحويل بنكي، محفظة، كاش.
8. tampering_suspected: افحص الصورة كويس بحثا عن أي تعديل، سواء بالذكاء الاصطناعي أو ببرامج تعديل الصور أو بأي طريقة:
   - خط أو حجم أو لون أو سُمك مختلف في المبلغ أو التاريخ أو الوقت أو الاسم أو الرقم عن باقي الإيصال.
   - أرقام أو حروف مش على نفس السطر، أو المسافات بينها مش منتظمة.
   - مناطق مغبّشة أو ممسوحة أو متلزقة، أو خلفية لونها مختلف حوالين نص معين.
   - علامات توليد بالذكاء الاصطناعي: نص مشوّه أو مالوش معنى، تفاصيل غير منطقية، شعارات مش مظبوطة.
   - تفاصيل متناقضة جوه الإيصال، زي تاريخ أو وقت مش راكب مع بعض، أو شكل التطبيق مش مطابق للأصلي.
   - صورة متقصوصة بحيث تخبي بيانات أساسية.
   اجعل tampering_suspected = true لو فيه علامة واضحة على التعديل، واكتب في tampering_reason إيه اللي لاحظته بالعربي. لو الإيصال طبيعي اجعلها false. ماتحكمش بالتعديل لمجرد إن الصورة مش واضحة أو مضغوطة.
9. reason: لو is_receipt أو recipient_verified = false اكتب السبب بالعربي في جملة قصيرة.
أجب بـ JSON فقط."""

        # الأساسي حسب PRIMARY_AI، ولو فشل نجرب الباقيين اللي مفاتيحهم موجودة.
        providers = [('groq', call_groq, GROQ_API_KEY), ('gemini', call_gemini, GEMINI_API_KEY),
                     ('claude', call_claude, ANTHROPIC_API_KEY)]
        providers.sort(key=lambda p: p[0] != PRIMARY_AI)
        text, errors = None, []
        for name, fn, key in providers:
            if not key:
                continue
            text, err = fn(prompt, mime_type, img_base64)
            if text is not None:
                break
            logger.info(f"{name} failed ({err})")
            errors.append(err)
        if text is None:
            return {'_error': 'quota' if 'quota' in errors else 'busy'}
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return None
    except Exception as e:
        logger.error(f"Error verifying receipt: {e}")
        return None


def parse_receipt_date(s):
    s = to_western_digits(s or '').strip()
    for f in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def norm_ref(s):
    return re.sub(r'[\s\-#:]', '', to_western_digits(s or '')).upper()


def check_receipt_rules(result):
    """بيطبق شروط القبول. بيرجّع (رسالة رفض أو None, بيانات الإيصال)."""
    if not result.get('is_receipt'):
        return f"الصورة دي مش إيصال دفع مقبول.\n{result.get('reason') or ''}".strip(), None
    if result.get('tampering_suspected'):
        why = result.get('tampering_reason') or ''
        return f"الإيصال عليه علامات تعديل.\n{why}\nلو الإيصال سليم، راجعه يدوي وسجّله في الشيت.".strip(), None
    if not result.get('recipient_verified'):
        found = result.get('recipient_found') or 'مش ظاهر'
        return (f"المستلم في الإيصال مش صح.\nالمستلم المكتوب: {found}\n"
                f"المفروض يكون {RECIPIENT_NAME_AR}\nأو رقم من دول: {'، '.join(ACCEPTED_NUMBERS)}"), None

    amount = to_number(result.get('amount'))
    if not amount:
        return "مش قادر أقرأ المبلغ من الإيصال، ابعت صورة أوضح.", None

    rdate = parse_receipt_date(result.get('date'))
    note = ''
    if rdate:
        today = datetime.now(TZ).date()
        age = (today - rdate).days
        if rdate.year != today.year and not (0 <= age <= MAX_RECEIPT_AGE_DAYS):
            # Gemini ساعات بيقرا السنة غلط (مثلا 2020 بدل 2026). لو اليوم والشهر بسنة النهارده
            # (أو السنة اللي فاتت في أول يناير) يطلعوا في المدة المسموحة، نعتبرها قراية غلط للسنة.
            for y in (today.year, today.year - 1):
                try:
                    fixed = rdate.replace(year=y)
                except ValueError:
                    continue
                if 0 <= (today - fixed).days <= MAX_RECEIPT_AGE_DAYS:
                    note = f'سنة الإيصال اتقرت {rdate.year} واتعدلت لـ {y}'
                    rdate = fixed
                    age = (today - rdate).days
                    break
        if age < 0:
            return f"تاريخ الإيصال ({rdate}) بعد النهارده، راجع الإيصال.", None
        if age > MAX_RECEIPT_AGE_DAYS:
            return f"الإيصال قديم: تاريخه {rdate} (من {age} يوم). المقبول آخر {MAX_RECEIPT_AGE_DAYS} أيام بس.", None
    else:
        # الإيصال من غير تاريخ: بيتقبل، وبيتكتب في الملاحظات علشان يتراجع
        note = 'الإيصال من غير تاريخ'

    rtime = (to_western_digits(result.get('time') or '').strip())[:5]
    return None, {
        'amount': amount,
        'date': rdate.isoformat() if rdate else '',
        'time': rtime,
        'reference': (result.get('reference') or '').strip(),
        'recipient': (result.get('recipient_found') or '').strip(),
        'method': normalize_method(result.get('payment_method') or ''),
        'note': note,
    }


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


def record_payment(student, amount, pay_type, date, time_str, receipt=None):
    """بيضيف سطر في سجل المدفوعات بس. المدفوع والمتبقي والحالة بتتحسب في الشيت بمعادلات."""
    r = receipt or {}
    try:
        log_ws = get_sheet().worksheet(LOG_SHEET)
        log_ws.append_row(
            [date, time_str, student['name'], student['sheet'],
             f"{student['apt']} - {student['room']}", amount, pay_type, 'تليجرام', r.get('note', ''),
             "'" + r['reference'] if r.get('reference') else '',
             "'" + r['date'] if r.get('date') else '',
             "'" + r['time'] if r.get('time') else '',
             "'" + r['photo_uid'] if r.get('photo_uid') else ''],
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


def find_duplicate_receipt(receipt):
    """بيدوّر في السجل على نفس الإيصال: نفس رقم العملية، أو نفس الصورة، أو نفس المبلغ والتاريخ والوقت."""
    data = get_sheet().worksheet(LOG_SHEET).get_all_values()
    ref = norm_ref(receipt.get('reference'))
    for r in data[1:]:
        r = r + [''] * (13 - len(r))
        if ref and norm_ref(r[9]) == ref:
            return r
        if receipt.get('photo_uid') and r[12] == receipt['photo_uid']:
            return r
        if (not ref and receipt.get('date') and receipt.get('time') and r[10] == receipt['date'] and r[11] == receipt['time']
                and to_number(r[5]) == receipt['amount']):
            return r
    return None


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
    receipt = user_states.get(chat_id, {}).get('receipt')
    if receipt:
        try:
            dup = find_duplicate_receipt(receipt)
        except Exception as e:
            logger.error(f"Error checking duplicates: {e}")
            dup = None
        if dup:
            bot.reply_to(message, f"❌ الإيصال ده اتسجل قبل كده لـ {dup[2]} يوم {dup[0]}. ماتسجلش.")
            user_states.pop(chat_id, None)
            return
    if record_payment(student, amount, pay_type, date, time_str, receipt):
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
    photo = message.photo[-1]
    result = verify_receipt(photo.file_id)
    if result is None or result.get('_error') == 'busy':
        bot.reply_to(message, "⚠️ خدمة قراءة الإيصالات مش بترد دلوقتي (غالبا زحمة). ابعت صورة الإيصال تاني بعد دقيقة.")
        return
    if result.get('_error') == 'quota':
        bot.reply_to(message, "⚠️ الحد اليومي لقراءة الإيصالات خلص. سجّل الدفعة دي يدوي في الشيت، أو ابعت الإيصال بكره.")
        return

    error, receipt = check_receipt_rules(result)
    if error:
        bot.reply_to(message, f"❌ الإيصال مرفوض\n{error}")
        return

    receipt['photo_uid'] = photo.file_unique_id
    try:
        dup = find_duplicate_receipt(receipt)
    except Exception as e:
        logger.error(f"Error checking duplicates: {e}")
        bot.reply_to(message, "⚠️ مقدرتش أراجع السجل، حاول تاني")
        return
    if dup:
        bot.reply_to(message, f"❌ الإيصال مرفوض\nالإيصال ده اتسجل قبل كده لـ {dup[2]} يوم {dup[0]}.")
        return

    user_states[chat_id].update({'step': 'waiting_pay_type', 'amount': receipt['amount'], 'receipt': receipt})
    details = (f"💰 المبلغ: {fmt(receipt['amount'])} جنيه\n"
               f"📅 تاريخ العملية: {receipt['date'] or 'مش مكتوب في الإيصال (هيتكتب في الملاحظات)'}"
               + (f" {receipt['time']}" if receipt['time'] else '') + "\n"
               f"👤 المستلم: {receipt['recipient']}"
               + (f"\n🔢 رقم العملية: {receipt['reference']}" if receipt['reference'] else ''))
    bot.reply_to(message,
        f"✅ الإيصال تمام!\n\n{details}\n\n💳 ادخل نوع الدفع:\nانستاباي / كاش / تحويل بنكي")


if __name__ == '__main__':
    logger.info("Bot started...")
    logger.info(f"Primary AI for receipts: {PRIMARY_AI}")
    if GROQ_API_KEY:
        logger.info(f"Groq vision models: {groq_vision_models()}")
    if ANTHROPIC_API_KEY:
        try:
            logger.info(f"Claude model for receipts: {pick_claude_model()}")
        except Exception as e:
            logger.error(f"Claude startup check failed: {e}")
    else:
        logger.error("ANTHROPIC_API_KEY is not set")
    bot.polling(none_stop=True)
