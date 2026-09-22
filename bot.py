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
        ws.update_cell(student['row'], 5, '✅ دفع')
        try:
            log_ws = spreadsheet.worksheet('سجل المدفوعات')
        except:
            log_ws = spreadsheet.add_worksheet('سجل المدفوعات', 1000, 10)
            log_ws.append_row(['التاريخ', 'الوقت', 'الاسم', 'العمارة', 'الوحدة', 'المبلغ', 'نوع الدفع'])
        log_ws.append_row([date, time_str, student['name'], student['sheet'], student['unit'],
