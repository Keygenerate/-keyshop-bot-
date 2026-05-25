#!/usr/bin/env python3
"""
KeyShop Telegram Bot v4.4 - SUB-ADMIN SUPPORT
Changes:
- Main admin can add/remove sub-admins
- Sub-admins can ONLY approve/reject/cancel payments
- Sub-admins have NO access to admin panel, products, settings, etc.
- Force channel join feature retained
- Edit Product feature retained
- Admin cancel + auto clear retained
- Bot lock retained
"""

import logging
import os
import sys
import sqlite3
import json
import requests
import uuid
import re
from typing import Dict, List, Optional
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, ConversationHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USER_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]

FORCE_JOIN_CHANNELS = os.environ.get("FORCE_CHANNELS", "").split(",") if os.environ.get("FORCE_CHANNELS") else []

DB_FILE = "keyshop_bot.db"
UPI_ID = os.environ.get("UPI_ID")
UPI_NAME = os.environ.get("UPI_NAME")
PAYMENT_TIMEOUT_MINUTES = 30

FIXED_API_URL = os.environ.get("API_URL")

DURATION_MAP = {
    "1 Day": "1 DaYS",
    "3 Days": "3 DaYS", 
    "7 Days": "7 DaYS",
    "10 Days": "10 DaYS",
    "14 Days": "14 DaYS",
    "15 Days": "15 DaYS",
    "20 Days": "20 DaYS",
    "30 Days": "30 DaYS"
}

API_TO_DISPLAY = {v: k for k, v in DURATION_MAP.items()}
AVAILABLE_DURATIONS = list(DURATION_MAP.keys())

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("CREATE TABLE IF NOT EXISTS admin_config (id INTEGER PRIMARY KEY, api_key TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, pid TEXT UNIQUE, name TEXT, description TEXT, original_price REAL, category TEXT, stock_info TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS product_durations (id INTEGER PRIMARY KEY, pid TEXT, duration TEXT, admin_price REAL, original_price REAL, stock INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1, UNIQUE(pid, duration), FOREIGN KEY(pid) REFERENCES products(pid))")
    c.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, user_id INTEGER, username TEXT, pid TEXT, product_name TEXT, price_paid REAL, duration TEXT, payment_status TEXT DEFAULT 'pending', payment_utr TEXT, key_delivered TEXT, order_status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS auto_clear_settings (id INTEGER PRIMARY KEY, clear_after_hours INTEGER DEFAULT 24, enabled INTEGER DEFAULT 1, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("INSERT OR IGNORE INTO auto_clear_settings (id, clear_after_hours, enabled) VALUES (1, 24, 1)")
    c.execute("CREATE TABLE IF NOT EXISTS bot_lock_settings (id INTEGER PRIMARY KEY, is_locked INTEGER DEFAULT 0, locked_by INTEGER, locked_at TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("INSERT OR IGNORE INTO bot_lock_settings (id, is_locked, locked_by) VALUES (1, 0, NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS sub_admins (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, added_by INTEGER, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT, first_name TEXT, last_name TEXT, is_reseller INTEGER DEFAULT 0)")

    conn.commit()
    conn.close()
    logger.info('Database initialized!')

class Database:
    def __init__(self):
        self.db_file = DB_FILE
    def conn(self):
        return sqlite3.connect(self.db_file)

    def set_api_key(self, api_key):
        db = self.conn()
        c = db.cursor()
        c.execute('DELETE FROM admin_config')
        c.execute('INSERT INTO admin_config (api_key) VALUES (?)', (api_key,))
        db.commit()
        db.close()

    def get_config(self):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT api_key FROM admin_config LIMIT 1')
        row = c.fetchone()
        db.close()
        if row:
            return {'api_key': row[0], 'api_url': FIXED_API_URL}
        return None

    # ========== SUB-ADMIN METHODS ==========
    def add_sub_admin(self, user_id, added_by):
        db = self.conn()
        c = db.cursor()
        c.execute('INSERT OR REPLACE INTO sub_admins (user_id, added_by) VALUES (?, ?)', (user_id, added_by))
        db.commit()
        db.close()

    def remove_sub_admin(self, user_id):
        db = self.conn()
        c = db.cursor()
        c.execute('DELETE FROM sub_admins WHERE user_id = ?', (user_id,))
        db.commit()
        db.close()

    def is_sub_admin(self, user_id):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT 1 FROM sub_admins WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        db.close()
        return row is not None

    def get_sub_admins(self):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT user_id, added_by, added_at FROM sub_admins ORDER BY added_at DESC')
        rows = c.fetchall()
        db.close()
        return [{'user_id': r[0], 'added_by': r[1], 'added_at': r[2]} for r in rows]

    def is_any_admin(self, user_id):
        """Check if user is main admin OR sub-admin"""
        return user_id in ADMIN_USER_IDS or self.is_sub_admin(user_id)

    def is_main_admin(self, user_id):
        """Only main admin"""
        return user_id in ADMIN_USER_IDS

    # ========== BOT LOCK METHODS ==========
    def is_bot_locked(self):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT is_locked FROM bot_lock_settings WHERE id = 1')
        row = c.fetchone()
        db.close()
        return bool(row[0]) if row else False

    def set_bot_lock(self, locked, admin_id=None):
        db = self.conn()
        c = db.cursor()
        if locked:
            c.execute('UPDATE bot_lock_settings SET is_locked = 1, locked_by = ?, locked_at = CURRENT_TIMESTAMP WHERE id = 1', (admin_id,))
        else:
            c.execute('UPDATE bot_lock_settings SET is_locked = 0, locked_by = NULL, locked_at = NULL WHERE id = 1')
        db.commit()
        db.close()

    def get_bot_lock_status(self):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT is_locked, locked_by, locked_at FROM bot_lock_settings WHERE id = 1')
        row = c.fetchone()
        db.close()
        if row:
            return {'is_locked': bool(row[0]), 'locked_by': row[1], 'locked_at': row[2]}
        return {'is_locked': False, 'locked_by': None, 'locked_at': None}

    def update_product(self, pid, name=None, description=None, category=None):
        db = self.conn()
        c = db.cursor()
        updates = []
        params = []
        if name is not None:
            updates.append('name = ?')
            params.append(name)
        if description is not None:
            updates.append('description = ?')
            params.append(description)
        if category is not None:
            updates.append('category = ?')
            params.append(category)
        if updates:
            params.append(pid)
            query = f"UPDATE products SET {', '.join(updates)} WHERE pid = ?"
            c.execute(query, params)
            db.commit()
        db.close()

    def add_manual_product(self, pid, name, description, category):
        db = self.conn()
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO products (pid, name, description, category) VALUES (?, ?, ?, ?)", (pid, name, description, category))
        db.commit()
        db.close()

    def set_duration_price(self, pid, duration, price):
        db = self.conn()
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO product_durations (pid, duration, admin_price, is_active) VALUES (?, ?, ?, 1)", (pid, duration, price))
        db.commit()
        db.close()

    def get_duration_prices(self, pid):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT duration, admin_price, original_price, stock, is_active FROM product_durations WHERE pid = ? ORDER BY CASE duration WHEN "1 Day" THEN 1 WHEN "3 Days" THEN 2 WHEN "7 Days" THEN 3 WHEN "15 Days" THEN 4 WHEN "30 Days" THEN 5 ELSE 6 END', (pid,))
        rows = c.fetchall()
        db.close()
        return [{'duration': r[0], 'admin_price': r[1], 'original_price': r[2], 'stock': r[3], 'is_active': r[4]} for r in rows]

    def get_active_durations(self, pid):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT duration, admin_price FROM product_durations WHERE pid = ? AND is_active = 1 AND admin_price > 0', (pid,))
        rows = c.fetchall()
        db.close()
        return [{'duration': r[0], 'price': r[1]} for r in rows]

    def toggle_duration(self, pid, duration):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT is_active FROM product_durations WHERE pid = ? AND duration = ?', (pid, duration))
        row = c.fetchone()
        if row:
            new_status = 0 if row[0] == 1 else 1
            c.execute('UPDATE product_durations SET is_active = ? WHERE pid = ? AND duration = ?', (new_status, pid, duration))
            db.commit()
        db.close()

    def save_products(self, products):
        db = self.conn()
        c = db.cursor()
        for p in products:
            pid = str(p.get('pid', p.get('id', '')))
            name = p.get('name', 'Unknown')
            desc = p.get('description', '')
            cat = p.get('category', 'General')
            stock_info = json.dumps(p.get('stock', {})) if isinstance(p.get('stock'), dict) else str(p.get('stock', ''))
            c.execute("INSERT OR REPLACE INTO products (pid, name, description, category, stock_info) VALUES (?, ?, ?, ?, ?)", (pid, name, desc, cat, stock_info))
            durations = p.get('durations', [])
            if not durations and p.get('duration'):
                durations = [{'duration': p.get('duration'), 'price': p.get('price', 0), 'stock': p.get('stock', 0)}]
            for d in durations:
                dur = d.get('duration', '1 Day')
                price = float(d.get('price', 0))
                stock = int(d.get('stock', 0))
                c.execute("INSERT OR REPLACE INTO product_durations (pid, duration, original_price, stock, is_active) VALUES (?, ?, ?, ?, 1)", (pid, dur, price, stock))
        db.commit()
        db.close()

    def get_products(self):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT * FROM products ORDER BY name')
        rows = c.fetchall()
        db.close()
        return [{'id': r[0], 'pid': r[1], 'name': r[2], 'description': r[3], 'category': r[5], 'stock_info': r[6]} for r in rows]

    def get_product(self, pid):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT * FROM products WHERE pid = ?', (pid,))
        r = c.fetchone()
        db.close()
        if r:
            return {'id': r[0], 'pid': r[1], 'name': r[2], 'description': r[3], 'category': r[5], 'stock_info': r[6]}
        return None

    def delete_product(self, pid):
        db = self.conn()
        c = db.cursor()
        c.execute('DELETE FROM product_durations WHERE pid = ?', (pid,))
        c.execute('DELETE FROM products WHERE pid = ?', (pid,))
        db.commit()
        db.close()

    def create_order(self, user_id, username, pid, product_name, price, duration):
        db = self.conn()
        c = db.cursor()
        order_id = f'KS{uuid.uuid4().hex[:10].upper()}'
        c.execute("INSERT INTO orders (order_id, user_id, username, pid, product_name, price_paid, duration) VALUES (?, ?, ?, ?, ?, ?, ?)", (order_id, user_id, username, pid, product_name, price, duration))
        db.commit()
        db.close()
        return order_id

    def get_order(self, order_id):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,))
        r = c.fetchone()
        db.close()
        if r:
            return {'order_id': r[1], 'user_id': r[2], 'username': r[3], 'pid': r[4], 'product_name': r[5], 'price_paid': r[6], 'duration': r[7], 'payment_status': r[8], 'payment_utr': r[9], 'key_delivered': r[10], 'order_status': r[11]}
        return None

    def update_payment(self, order_id, status, utr=None):
        db = self.conn()
        c = db.cursor()
        if utr:
            c.execute('UPDATE orders SET payment_status = ?, payment_utr = ? WHERE order_id = ?', (status, utr, order_id))
        else:
            c.execute('UPDATE orders SET payment_status = ? WHERE order_id = ?', (status, order_id))
        db.commit()
        db.close()

    def update_order(self, order_id, status, key=None):
        db = self.conn()
        c = db.cursor()
        if key:
            c.execute('UPDATE orders SET order_status = ?, key_delivered = ? WHERE order_id = ?', (status, key, order_id))
        else:
            c.execute('UPDATE orders SET order_status = ? WHERE order_id = ?', (status, order_id))
        db.commit()
        db.close()

    def get_user_orders(self, user_id):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
        rows = c.fetchall()
        db.close()
        return [{'order_id': r[1], 'product_name': r[5], 'price_paid': r[6], 'duration': r[7], 'payment_status': r[8], 'order_status': r[11]} for r in rows]

    def get_auto_clear_settings(self):
        db = self.conn()
        c = db.cursor()
        c.execute('SELECT clear_after_hours, enabled FROM auto_clear_settings WHERE id = 1')
        row = c.fetchone()
        db.close()
        if row:
            return {'hours': row[0], 'enabled': bool(row[1])}
        return {'hours': 24, 'enabled': True}

    def set_auto_clear_settings(self, hours, enabled):
        db = self.conn()
        c = db.cursor()
        c.execute('UPDATE auto_clear_settings SET clear_after_hours = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1', (hours, 1 if enabled else 0))
        db.commit()
        db.close()

    def clear_old_orders(self):
        settings = self.get_auto_clear_settings()
        if not settings['enabled']:
            return 0
        db = self.conn()
        c = db.cursor()
        hours = settings['hours']
        c.execute("DELETE FROM orders WHERE created_at < datetime('now', '-{} hours') AND order_status IN ('completed', 'cancelled', 'cancelled_by_user', 'rejected')".format(hours))
        deleted = c.rowcount
        db.commit()
        db.close()
        return deleted

    def get_pending_orders(self):
        db = self.conn()
        c = db.cursor()
        c.execute("SELECT order_id, user_id, username, product_name, price_paid, payment_utr, duration, created_at FROM orders WHERE payment_status = 'pending_verification' ORDER BY created_at DESC")
        rows = c.fetchall()
        db.close()
        return [{'order_id': r[0], 'user_id': r[1], 'username': r[2], 'product_name': r[3], 'price_paid': r[4], 'payment_utr': r[5], 'duration': r[6], 'created_at': r[7]} for r in rows]

    def cancel_order(self, order_id):
        db = self.conn()
        c = db.cursor()
        c.execute("UPDATE orders SET payment_status = 'cancelled_by_admin', order_status = 'cancelled' WHERE order_id = ?", (order_id,))
        db.commit()
        db.close()

    def save_user(self, user_id, username, first_name, last_name):
        db = self.conn()
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)", (user_id, username, first_name, last_name))
        db.commit()
        db.close()

db = Database()

# ==================== FORCE CHANNEL JOIN CHECK ====================
async def check_channel_membership(user_id: int, bot) -> tuple:
    if not FORCE_JOIN_CHANNELS:
        return True, []
    missing = []
    for channel in FORCE_JOIN_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status in ['left', 'kicked']:
                missing.append(channel)
        except Exception as e:
            logger.error(f"Channel check error for {channel}: {e}")
            missing.append(channel)
    return len(missing) == 0, missing

async def send_join_required_message(update: Update, context: ContextTypes.DEFAULT_TYPE, missing_channels: list):
    kb = []
    for ch in missing_channels:
        ch_name = ch.replace("@", "").replace("-100", "")
        if ch.startswith("@"):
            kb.append([InlineKeyboardButton(f"🔔 Join {ch}", url=f"https://t.me/{ch_name}")])
        else:
            kb.append([InlineKeyboardButton("🔔 Join Required Channel", callback_data="join_channel_help")])
    kb.append([InlineKeyboardButton("✅ I've Joined - Check Again", callback_data="check_join")])
    kb.append([InlineKeyboardButton("🏠 Main Menu", callback_data="back")])
    text = "⚠️ *Access Denied!*\n\nBhai channel join kar lo isi pe sare hacks ke update aur apk milenge *plz join* bhailog:\n\n"
    for ch in missing_channels:
        text += f"• `{ch}`\n"
    text += "\n👆 Sab channels join karo, phir *✅ I've Joined* button click karo!"
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def require_channel_join(handler_func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if db.is_any_admin(user.id):
            return await handler_func(update, context)
        is_member, missing = await check_channel_membership(user.id, context.bot)
        if not is_member:
            await send_join_required_message(update, context, missing)
            return
        return await handler_func(update, context)
    return wrapper

# ==================== BOT LOCK CHECK ====================
async def send_bot_locked_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🔒 *Bot Under Maintenance*\n\nBot is currently locked by admin.\nOnly admin can use the bot right now.\n\n🙏 Please try again later!"
    kb = [[InlineKeyboardButton("🔄 Try Again", callback_data="back")]]
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def require_bot_unlocked(handler_func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if db.is_any_admin(user.id):
            return await handler_func(update, context)
        if db.is_bot_locked():
            await send_bot_locked_message(update, context)
            return
        return await handler_func(update, context)
    return wrapper

class ResellerAPI:
    def __init__(self, api_key, api_url):
        self.api_key = api_key
        self.api_url = api_url

    def _make_request(self, data):
        try:
            logger.info(f"🔵 API REQUEST: URL={self.api_url}, DATA={json.dumps(data)}")
            response = requests.post(self.api_url, data=data, timeout=30)
            logger.info(f"🟢 API RESPONSE: Status={response.status_code}")
            raw_text = response.text
            logger.info(f"📄 RAW RESPONSE: {raw_text[:2000]}")
            if raw_text.strip().startswith('<'):
                logger.error("❌ API returned HTML instead of JSON!")
                return None, f"HTML Response: {raw_text[:500]}"
            try:
                result = response.json()
                logger.info(f"📋 PARSED JSON: {json.dumps(result, indent=2)[:1000]}")
                return result, None
            except json.JSONDecodeError as e:
                logger.error(f"❌ JSON Parse Error: {e}")
                return None, f"JSON Parse Error: {e}\nRaw: {raw_text[:500]}"
        except Exception as e:
            logger.error(f"❌ API Request Error: {e}")
            return None, str(e)

    def fetch_products(self):
        result, error = self._make_request({'api_key': self.api_key, 'action': 'products'})
        if error:
            return []
        if isinstance(result, list):
            return result
        elif isinstance(result, dict):
            if result.get('status') == 'success':
                return result.get('products', result.get('data', []))
        return []

    def buy_key(self, product_id, duration='1 Day'):
        api_duration = DURATION_MAP.get(duration, duration)
        result, error = self._make_request({'api_key': self.api_key, 'action': 'buy', 'product_id': str(product_id), 'duration': api_duration})
        if error:
            return {'status': 'error', 'message': error, 'raw_error': True, 'api_duration': api_duration}
        if not result:
            return {'status': 'error', 'message': 'Empty response', 'api_duration': api_duration}
        result['api_duration'] = api_duration
        return result

    def check_balance(self):
        result, error = self._make_request({'api_key': self.api_key, 'action': 'balance'})
        if error:
            return None
        return result

class PaymentVerifier:
    def generate_msg(self, order_id, amount, product_name, duration):
        return f"""
🛒 *Order Details*
━━━━━━━━━━━━━━━━━━
📦 Product: `{product_name}`
⏱️ Duration: {duration}
💰 Amount: ₹{amount:.2f}
🆔 Order ID: `{order_id}`

💳 *Payment Instructions:*
1️⃣ Send ₹{amount:.2f} to UPI ID:
   `{UPI_ID}`

2️⃣ *IMPORTANT:* Add note/remarks:
   `Order {order_id}`

3️⃣ After payment, share your UTR/Reference number

⏰ Payment expires in {PAYMENT_TIMEOUT_MINUTES} minutes

✅ Key will be delivered automatically after verification!
"""

# ==================== CONVERSATION STATES ====================
SET_API_KEY, SET_PRICE, ENTER_UTR, ADD_PID, ADD_NAME, ADD_DESC, ADD_CAT, ADD_PRICE = range(8)
EDIT_NAME, EDIT_DESC, EDIT_CAT = range(8, 11)
SET_CLEAR_HOURS = 11
ADD_SUB_ADMIN = 12

# ==================== KEY EXTRACTION ====================
def extract_key_from_response(result):
    if not isinstance(result, dict):
        return None
    logger.info(f"Extracting key from: {json.dumps(result, indent=2)[:2000]}")
    direct_key_fields = ['key', 'license', 'serial', 'serial_key', 'activation_key', 'code', 'product_key', 'license_key', 'token', 'password', 'pin', 'activation_code', 'product_code', 'credentials', 'account', 'login', 'email', 'username', 'link', 'url', 'cookie', 'session', 'refresh_token', 'access_token']
    for k in direct_key_fields:
        if k in result and result[k]:
            val = result[k]
            if isinstance(val, str) and len(val) > 2:
                logger.info(f"Found key in field '{k}': {val[:50]}...")
                return val
            elif isinstance(val, dict):
                dict_str = json.dumps(val, indent=2)
                if len(dict_str) > 5:
                    return dict_str
            elif isinstance(val, list) and val:
                list_str = json.dumps(val, indent=2)
                if len(list_str) > 5:
                    return list_str
    nested_wrappers = ['data', 'result', 'response', 'output', 'payload', 'body']
    for wrapper in nested_wrappers:
        if wrapper in result:
            wrapped = result[wrapper]
            if isinstance(wrapped, dict):
                for k in direct_key_fields:
                    if k in wrapped and wrapped[k]:
                        val = wrapped[k]
                        if isinstance(val, str) and len(val) > 2:
                            return val
                        elif isinstance(val, (dict, list)):
                            return json.dumps(val, indent=2)
            elif isinstance(wrapped, list) and wrapped:
                return json.dumps(wrapped, indent=2)
            elif isinstance(wrapped, str) and len(wrapped) > 5:
                return wrapped
    text_fields = ['message', 'msg', 'description', 'detail', 'info', 'note', 'remark']
    for tf in text_fields:
        if tf in result and result[tf]:
            val = str(result[tf])
            if len(val) > 5:
                return val
    for k, v in result.items():
        if isinstance(v, str) and len(v) > 10 and k not in ['status', 'success', 'error', 'error_code', 'code', 'message']:
            if any(c in v for c in ['-', '_', ':', '.']) or len(v) > 20:
                return v
    logger.warning("No specific key field found, returning full response")
    return json.dumps(result, indent=2)[:2000]

# ==================== VERIFY & DELIVER ====================
async def verify_and_deliver(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
    order = db.get_order(order_id)
    if not order:
        return False, '❌ Order not found'
    if order['key_delivered']:
        return True, '✅ Already delivered'
    cfg = db.get_config()
    if not cfg:
        return False, '❌ API not configured'
    api = ResellerAPI(cfg['api_key'], cfg['api_url'])
    api_duration = DURATION_MAP.get(order['duration'], order['duration'])
    processing_msg = f"""🔄 *Processing Order...*

🆔 `{order_id}`
📦 {order['product_name']}
⏱️ {order['duration']} (API: `{api_duration}`)
👤 {order['username'] or order['user_id']}

⏳ API se key fetch kar raha hoon..."""
    if update.message:
        status_msg = await update.message.reply_text(processing_msg, parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        status_msg = await update.callback_query.message.reply_text(processing_msg, parse_mode=ParseMode.MARKDOWN)
    else:
        status_msg = None
    result = api.buy_key(order['pid'], order['duration'])
    if result.get('raw_error'):
        error_msg = result.get('message', 'Unknown error')
        api_dur = result.get('api_duration', 'unknown')
        try:
            await context.bot.send_message(
                order['user_id'],
                f"""⚠️ *Payment Verified but Key Delivery Failed*

✅ Order: `{order_id}`
📦 {order['product_name']}
⏱️ {order['duration']}
💰 ₹{order['price_paid']:.2f}

❌ Error: {error_msg}

👨‍💼 Admin manually key deliver karega.
🙏 Please wait 5-10 minutes.""",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
        if status_msg:
            debug_info = f"""❌ *Failed!*

🆔 `{order_id}`
📦 PID: `{order['pid']}`
⏱️ Sent: `{api_dur}`

❌ Error: `{error_msg}`

📋 Check `bot_debug.log` for full details.

💡 Tips:
• API key sahi hai?
• Duration format match karta hai?
• API mein stock hai?"""
            await status_msg.edit_text(debug_info, parse_mode=ParseMode.MARKDOWN)
        return False, f'❌ {error_msg}'
    is_success = False
    success_reason = ""
    if result.get('status') == 'success':
        is_success = True
        success_reason = "status=success"
    elif result.get('success') == True:
        is_success = True
        success_reason = "success=true"
    elif result.get('error_code') == 0:
        is_success = True
        success_reason = "error_code=0"
    elif result.get('code') == 200:
        is_success = True
        success_reason = "code=200"
    elif result.get('status_code') == 200:
        is_success = True
        success_reason = "status_code=200"
    elif result.get('stat') == 'OK':
        is_success = True
        success_reason = "stat=OK"
    elif any(k in result for k in ['key', 'license', 'serial', 'serial_key', 'activation_key', 'code', 'token', 'password', 'pin', 'credentials', 'account', 'login', 'email']):
        is_success = True
        success_reason = "key_field_found"
    elif isinstance(result.get('data'), dict) and any(k in result['data'] for k in ['key', 'license', 'serial', 'code', 'token', 'password', 'username', 'email']):
        is_success = True
        success_reason = "nested_key_found"
    elif 'response' in result and result['response']:
        is_success = True
        success_reason = "response_field"
    elif 'message' in result and isinstance(result['message'], str) and len(result['message']) > 10:
        msg = result['message'].lower()
        if any(k in msg for k in ['key', 'license', 'serial', 'code', 'password', 'token']):
            is_success = True
            success_reason = "message_contains_key"
    logger.info(f"API Success Check: {is_success} (reason: {success_reason})")
    if is_success:
        key = extract_key_from_response(result)
        if key and len(key) > 5:
            db.update_order(order_id, 'completed', key)
            db.update_payment(order_id, 'verified')
            try:
                await context.bot.send_message(
                    order['user_id'],
                    f"""🎉 *Payment Verified & Key Delivered!*

✅ Order: `{order_id}`
📦 {order['product_name']}
⏱️ {order['duration']}
💰 ₹{order['price_paid']:.2f}

🔑 *Your Key:*
`{key}`

⚠️ *IMPORTANT:* Save this key immediately!

🙏 Thank you!""",
                    parse_mode=ParseMode.MARKDOWN
                )
                await context.bot.send_message(
                    order['user_id'],
                    '👆 Click to go back',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🏠 Main Menu', callback_data='back')]])
                )
            except Exception as e:
                logger.error(f'Delivery failed: {e}')
            if status_msg:
                await status_msg.edit_text(f"✅ *Order Completed!*\n\n🆔 `{order_id}`\n🔑 Key delivered to user.", parse_mode=ParseMode.MARKDOWN)
            return True, f'✅ Key: {key[:30]}...'
        else:
            raw = json.dumps(result, indent=2)
            if status_msg:
                await status_msg.edit_text(f"⚠️ *Key format unknown!*\n\n🆔 `{order_id}`\n\n📋 Raw Response:\n```{raw[:400]}```", parse_mode=ParseMode.MARKDOWN)
            return False, f'⚠️ Unknown key format\n{raw[:300]}'
    else:
        error_msg = result.get('message', result.get('error_msg', result.get('error', 'Unknown error')))
        api_dur = result.get('api_duration', 'unknown')
        try:
            await context.bot.send_message(
                order['user_id'],
                f"""⚠️ *Payment Verified but Key Delivery Failed*

✅ Order: `{order_id}`
📦 {order['product_name']}
⏱️ {order['duration']}
💰 ₹{order['price_paid']:.2f}

❌ Error: {error_msg}

👨‍💼 Admin manually key deliver karega.
🙏 Please wait.""",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
        if status_msg:
            debug_info = f"""❌ *API Error!*

🆔 `{order_id}`
📦 PID: `{order['pid']}`
⏱️ Sent to API: `{api_dur}`

❌ {error_msg}

📋 Check `bot_debug.log` for full response.

💡 Common fixes:
• API key check karo
• Duration format sahi hai?
• Stock available hai?
• API URL sahi hai?"""
            await status_msg.edit_text(debug_info, parse_mode=ParseMode.MARKDOWN)
        return False, f'❌ API Error: {error_msg}'

# ==================== HANDLERS ====================
@require_channel_join
@require_bot_unlocked
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.save_user(user.id, user.username, user.first_name, user.last_name)
    is_main = db.is_main_admin(user.id)
    is_sub = db.is_sub_admin(user.id)
    text = f"""👋 Welcome *{user.first_name}* to KeyShop Bot!

🔑 Your one-stop shop for instant digital keys

Choose an option below:"""
    kb = [
        [InlineKeyboardButton('🛒 Shop Now', callback_data='shop_now')],
        [InlineKeyboardButton('📦 My Orders', callback_data='my_orders')],
        [InlineKeyboardButton('👤 Profile', callback_data='profile')],
        [InlineKeyboardButton('🆘 Support', callback_data='support')],
    ]
    if is_main:
        kb.append([InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')])
    if is_sub:
        kb.append([InlineKeyboardButton('💰 Payment Panel', callback_data='subadmin_panel')])
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def shop_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = db.get_products()
    if not products:
        await query.edit_message_text('😕 No products available.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='back')]]))
        return
    kb = []
    for p in products:
        pid = p['pid']
        name = p['name']
        kb.append([InlineKeyboardButton(f'📦 {name}', callback_data=f'prod_{pid}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='back')])
    await query.edit_message_text('🛒 *Select a Product:*\n\nPrices vary by duration. Tap to see options!', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def product_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.replace('prod_', '')
    p = db.get_product(pid)
    if not p:
        await query.edit_message_text('❌ Product not found!')
        return
    durations = db.get_active_durations(pid)
    if not durations:
        await query.edit_message_text('⚠️ This product has no active durations.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='shop_now')]]))
        return
    context.user_data['pid'] = pid
    context.user_data['pname'] = p['name']
    msg = f"""
📦 *{p['name']}*
━━━━━━━━━━━━━━━━━━
📝 {p['description'] or 'No description'}

⏱️ *Select Duration:*
"""
    kb = []
    for d in durations:
        price = d['price']
        dur = d['duration']
        kb.append([InlineKeyboardButton(f'⏱️ {dur} - ₹{price:.2f}', callback_data=f'dur_{dur}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='shop_now')])
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def select_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    duration = query.data.replace('dur_', '')
    pid = context.user_data.get('pid')
    pname = context.user_data.get('pname')
    if not pid or not pname:
        await query.edit_message_text('❌ Session expired. Start again with /start')
        return
    durations = db.get_active_durations(pid)
    price = None
    for d in durations:
        if d['duration'] == duration:
            price = d['price']
            break
    if not price:
        await query.edit_message_text('❌ Price not set for this duration.')
        return
    context.user_data['duration'] = duration
    context.user_data['price'] = price
    api_dur = DURATION_MAP.get(duration, duration)
    msg = f"""
📦 *{pname}*
━━━━━━━━━━━━━━━━━━
⏱️ Duration: {duration}
💰 Price: ₹{price:.2f}

Click 'Buy Now' to proceed.
"""
    kb = [
        [InlineKeyboardButton('💳 Buy Now', callback_data=f'buy_{pid}')],
        [InlineKeyboardButton('🔙 Back', callback_data=f'prod_{pid}')]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    pid = context.user_data.get('pid')
    price = context.user_data.get('price')
    pname = context.user_data.get('pname')
    duration = context.user_data.get('duration', '1 Day')
    if not all([pid, price, pname]):
        await query.edit_message_text('❌ Session expired. Start again with /start')
        return
    order_id = db.create_order(user.id, user.username, pid, pname, price, duration)
    context.user_data['order_id'] = order_id
    pv = PaymentVerifier()
    msg = pv.generate_msg(order_id, price, pname, duration)
    kb = [
        [InlineKeyboardButton("✅ I've Paid - Enter UTR", callback_data='enter_utr')],
        [InlineKeyboardButton('❌ Cancel', callback_data='cancel')]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def enter_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text('📝 Enter your UTR/Reference number:\n\nFind it in your UPI app payment history.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Cancel', callback_data='cancel')]]))
    return ENTER_UTR

async def process_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    utr = update.message.text.strip()
    order_id = context.user_data.get('order_id')
    if not order_id:
        await update.message.reply_text('❌ Session expired. /start se shuru karo.')
        return ConversationHandler.END
    order = db.get_order(order_id)
    if not order:
        await update.message.reply_text('❌ Order not found!')
        return ConversationHandler.END
    db.update_payment(order_id, 'pending_verification', utr)
    await update.message.reply_text(f"✅ *UTR Received!*\n\n🆔 Order: `{order_id}`\n🔢 UTR: `{utr}`\n\n⏳ Payment verification pending...", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🏠 Main Menu', callback_data='back')]]))
    # Notify ALL admins (main + sub-admins)
    all_admins = ADMIN_USER_IDS.copy()
    sub_admins = db.get_sub_admins()
    for sa in sub_admins:
        if sa['user_id'] not in all_admins:
            all_admins.append(sa['user_id'])
    for admin_id in all_admins:
        try:
            admin_kb = [
                [InlineKeyboardButton('✅ Verify & Deliver', callback_data=f'admin_verify_{order_id}')],
                [InlineKeyboardButton('❌ Reject', callback_data=f'admin_reject_{order_id}')],
                [InlineKeyboardButton('🚫 Cancel Order', callback_data=f'admin_cancel_{order_id}')]
            ]
            await context.bot.send_message(
                admin_id,
                f"🔔 *New Payment!*\n\n🆔 `{order_id}`\n👤 {update.effective_user.username or update.effective_user.first_name}\n💰 ₹{order['price_paid']:.2f}\n⏱️ {order['duration']}\n🔢 `{utr}`\n📦 {order['product_name']}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(admin_kb)
            )
        except Exception as e:
            logger.error(f'Admin notify failed: {e}')
    return ConversationHandler.END

# ==================== SUB-ADMIN PANEL ====================
async def subadmin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    kb = [
        [InlineKeyboardButton('⏳ Pending Payments', callback_data='subadmin_pending')],
        [InlineKeyboardButton('🔙 Back', callback_data='back')]
    ]
    await query.edit_message_text(
        "💰 *Payment Panel*\n\nSub-Admin Access\n\nYou can only:\n✅ Approve payments\n❌ Reject payments\n🚫 Cancel orders\n\nSelect option:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )

async def subadmin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    db_conn = db.conn()
    c = db_conn.cursor()
    c.execute("SELECT order_id, user_id, username, product_name, price_paid, payment_utr, duration FROM orders WHERE payment_status = 'pending_verification'")
    rows = c.fetchall()
    db_conn.close()
    if not rows:
        await query.edit_message_text('✅ No pending payments!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='subadmin_panel')]]))
        return
    msg = '⏳ *Pending Payments:*\n\n'
    for r in rows:
        msg += f"🆔 `{r[0]}`\n👤 {r[2] or r[1]}\n📦 {r[3]}\n⏱️ {r[6]}\n💰 ₹{r[4]:.2f}\n🔢 `{r[5] or 'N/A'}`\n/verify_{r[0]} | /reject_{r[0]} | /cancel_{r[0]}\n\n"
    kb = [[InlineKeyboardButton('🔙 Back', callback_data='subadmin_panel')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ==================== ADMIN PANEL ====================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    lock_status = db.get_bot_lock_status()
    lock_emoji = "🔴 LOCKED" if lock_status['is_locked'] else "🟢 UNLOCKED"
    sub_admins = db.get_sub_admins()
    sub_count = len(sub_admins)
    kb = [
        [InlineKeyboardButton('🔑 Set API Key', callback_data='set_api')],
        [InlineKeyboardButton('➕ Add Product Manually', callback_data='add_product')],
        [InlineKeyboardButton('✏️ Edit Product', callback_data='edit_product_list')],
        [InlineKeyboardButton('🔄 Fetch from API', callback_data='fetch')],
        [InlineKeyboardButton('📋 View Products', callback_data='view_products')],
        [InlineKeyboardButton('💰 Set Duration Prices', callback_data='set_dur_prices')],
        [InlineKeyboardButton('⏳ Pending Payments', callback_data='pending')],
        [InlineKeyboardButton(f'👥 Sub-Admins ({sub_count})', callback_data='manage_subadmins')],
        [InlineKeyboardButton('🧹 Auto Clear Settings', callback_data='auto_clear_settings')],
        [InlineKeyboardButton(f'🔒 Bot Lock ({lock_emoji})', callback_data='bot_lock_toggle')],
        [InlineKeyboardButton('🔙 Back', callback_data='back')]
    ]
    cfg = db.get_config()
    status = '✅ Configured' if cfg else '❌ Not Configured'
    await query.edit_message_text(f"🔐 *Admin Panel*\n\nAPI: {status}\n🔗 `{FIXED_API_URL}`\n👥 Sub-Admins: {sub_count}\n\nSelect option:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ==================== MANAGE SUB-ADMINS ====================
async def manage_subadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    sub_admins = db.get_sub_admins()
    msg = "👥 *Sub-Admins Management*\n\n"
    if sub_admins:
        msg += "Current Sub-Admins:\n"
        for sa in sub_admins:
            msg += f"• `{sa['user_id']}` (Added: {sa['added_at']})\n"
    else:
        msg += "No sub-admins added yet.\n"
    msg += "\nSub-admins can ONLY:\n✅ Approve payments\n❌ Reject payments\n🚫 Cancel orders\n\nThey CANNOT access admin panel, products, settings, etc."
    kb = [
        [InlineKeyboardButton('➕ Add Sub-Admin', callback_data='add_subadmin')],
        [InlineKeyboardButton('🗑️ Remove Sub-Admin', callback_data='remove_subadmin_list')],
        [InlineKeyboardButton('🔙 Back', callback_data='admin')]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def add_subadmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        "➕ *Add Sub-Admin*\n\nEnter the Telegram User ID of the person you want to make sub-admin.\n\nThey will be able to:\n✅ Approve payments\n❌ Reject payments\n🚫 Cancel orders\n\nBut they CANNOT:\n❌ Add/edit products\n❌ Change prices\n❌ Access admin settings\n❌ Lock/unlock bot\n\nEnter User ID:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Cancel', callback_data='manage_subadmins')]])
    )
    return ADD_SUB_ADMIN

async def save_subadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await update.message.reply_text('❌ Unauthorized!')
        return ConversationHandler.END
    try:
        new_admin_id = int(update.message.text.strip())
        if new_admin_id <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text('❌ Invalid User ID! Enter a valid number.')
        return ADD_SUB_ADMIN
    if new_admin_id in ADMIN_USER_IDS:
        await update.message.reply_text('❌ This user is already a main admin!')
        return ADD_SUB_ADMIN
    if db.is_sub_admin(new_admin_id):
        await update.message.reply_text('❌ This user is already a sub-admin!')
        return ADD_SUB_ADMIN
    db.add_sub_admin(new_admin_id, user.id)
    kb = [
        [InlineKeyboardButton('➕ Add Another', callback_data='add_subadmin')],
        [InlineKeyboardButton('👥 Manage Sub-Admins', callback_data='manage_subadmins')],
        [InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]
    ]
    await update.message.reply_text(
        f"✅ *Sub-Admin Added!*\n\n🆔 `{new_admin_id}`\n\nThis user can now approve/reject payments only.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def remove_subadmin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    sub_admins = db.get_sub_admins()
    if not sub_admins:
        await query.edit_message_text(
            "📭 No sub-admins to remove!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='manage_subadmins')]])
        )
        return
    kb = []
    for sa in sub_admins:
        kb.append([InlineKeyboardButton(f"🗑️ Remove {sa['user_id']}", callback_data=f"removesub_{sa['user_id']}")])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='manage_subadmins')])
    await query.edit_message_text(
        "🗑️ *Remove Sub-Admin*\n\nSelect sub-admin to remove:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )

async def remove_subadmin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    target_id = int(query.data.replace('removesub_', ''))
    db.remove_sub_admin(target_id)
    await query.edit_message_text(
        f"🗑️ *Sub-Admin Removed!*\n\n🆔 `{target_id}`\n\nThis user no longer has admin access.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('👥 Manage Sub-Admins', callback_data='manage_subadmins')]]),
        parse_mode=ParseMode.MARKDOWN
    )

# ==================== MANUAL ADD PRODUCT ====================
async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "➕ *Add Product Manually*\n\nStep 1/4: Enter Product ID (PID)\n\nExample: `62` or `netflix_1`\n\nYe wahi PID hoga jo API mein hai.",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_PID

async def add_product_pid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = update.message.text.strip()
    if not pid:
        await update.message.reply_text('❌ PID cannot be empty!')
        return ADD_PID
    context.user_data['add_pid'] = pid
    await update.message.reply_text(
        "Step 2/4: Enter Product Name\n\nExample: `Netflix Premium` or `Spotify Family`",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_NAME

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text('❌ Name cannot be empty!')
        return ADD_NAME
    context.user_data['add_name'] = name
    await update.message.reply_text(
        "Step 3/4: Enter Description (or send . to skip)\n\nExample: `4K UHD, 4 Screens`",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_DESC

async def add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc == '.':
        desc = ''
    context.user_data['add_desc'] = desc
    await update.message.reply_text(
        "Step 4/4: Enter Category\n\nExample: `Streaming`, `VPN`, `Gaming`",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_CAT

async def add_product_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    pid = context.user_data.get('add_pid')
    name = context.user_data.get('add_name')
    desc = context.user_data.get('add_desc', '')
    db.add_manual_product(pid, name, desc, cat)
    kb = []
    for dur in AVAILABLE_DURATIONS:
        kb.append([InlineKeyboardButton(f'💰 Set Price for {dur}', callback_data=f'setdur_{pid}_{dur}')])
    kb.append([InlineKeyboardButton('✅ Done - Back to Admin', callback_data='admin')])
    await update.message.reply_text(
        f"✅ *Product Added!*\n\n📦 {name}\n🆔 `{pid}`\n\nAb har duration ka price set karo:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

# ==================== SET DURATION PRICE ====================
async def set_duration_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.replace('setdur_', '')

    # FIX: Split from the RIGHT side using rsplit
    # Duration is always at the end and contains a space (e.g., "1 Day", "30 Days")
    # So we split from right by space first, then handle the underscore part
    # Actually, the format is: {pid}_{duration}
    # Duration is one of AVAILABLE_DURATIONS which we know
    # So we can find the duration by checking from the end

    found_duration = None
    pid = None

    # Check each available duration to find which one is at the end
    for dur in AVAILABLE_DURATIONS:
        if data.endswith('_' + dur):
            found_duration = dur
            pid = data[:-(len(dur) + 1)]  # Remove "_{duration}" from end
            break

    if not found_duration:
        logger.error(f"set_duration_price_start: Could not parse duration from: {data}")
        await query.edit_message_text('❌ Invalid data format!')
        return

    duration = found_duration
    logger.info(f"set_duration_price_start: Looking for pid={pid}, duration={duration}")
    p = db.get_product(pid)
    if not p:
        logger.error(f"set_duration_price_start: Product not found for pid={pid}")
        all_products = db.get_products()
        logger.info(f"Available products: {[p['pid'] for p in all_products]}")
        await query.edit_message_text(f'❌ Product not found! PID: `{pid}`', parse_mode=ParseMode.MARKDOWN)
        return
    context.user_data['price_pid'] = pid
    context.user_data['price_dur'] = duration
    dur_data = db.get_duration_prices(pid)
    current = 0
    for d in dur_data:
        if d['duration'] == duration:
            current = d['admin_price'] or 0
            break
    api_dur = DURATION_MAP.get(duration, duration)
    await query.edit_message_text(
        f"💰 *Set Price for {duration}*\n\n📦 {p['name']}\n🆔 `{pid}`\n📝 API Format: `{api_dur}`\n\nCurrent Price: ₹{current:.2f}\n\nEnter new price (in ₹):",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_PRICE

async def save_duration_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text('❌ Invalid price! Enter a number greater than 0.')
        return ADD_PRICE
    pid = context.user_data.get('price_pid')
    duration = context.user_data.get('price_dur')
    if not pid or not duration:
        await update.message.reply_text('❌ Session expired!')
        return ConversationHandler.END
    db.set_duration_price(pid, duration, price)
    p = db.get_product(pid)
    api_dur = DURATION_MAP.get(duration, duration)
    kb = []
    for dur in AVAILABLE_DURATIONS:
        kb.append([InlineKeyboardButton(f'💰 Set Price for {dur}', callback_data=f'setdur_{pid}_{dur}')])
    kb.append([InlineKeyboardButton('✅ Done - Back to Admin', callback_data='admin')])
    await update.message.reply_text(
        f"✅ *Price Updated!*\n\n📦 {p['name']}\n⏱️ {duration} (`{api_dur}`)\n💰 ₹{price:.2f}\n\nAur durations set karo ya Done click karo:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

# ==================== VIEW PRODUCTS ====================
async def view_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = db.get_products()
    if not products:
        await query.edit_message_text('📭 No products found!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]))
        return
    msg = '📋 *All Products:*\n\n'
    for p in products:
        pid = p['pid']
        durs = db.get_duration_prices(pid)
        dur_text = ''
        for d in durs:
            status = '✅' if d['is_active'] and d['admin_price'] else '❌'
            api_dur = DURATION_MAP.get(d['duration'], d['duration'])
            dur_text += f"\n   {status} {d['duration']} ({api_dur}): ₹{d['admin_price'] or 0:.2f}"
        msg += f"📦 *{p['name']}*\n🆔 `{pid}`{dur_text}\n\n"
    kb = [[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ==================== SET DURATION PRICES LIST ====================
async def set_dur_prices_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = db.get_products()
    if not products:
        await query.edit_message_text('❌ No products!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]))
        return
    kb = []
    for p in products:
        pid = p['pid']
        kb.append([InlineKeyboardButton(f"✏️ {p['name']}", callback_data=f'editdur_{pid}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='admin')])
    await query.edit_message_text('💰 *Select Product to Set Duration Prices:*', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def edit_product_durations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.replace('editdur_', '')
    logger.info(f"edit_product_durations: Looking for pid={pid}")
    p = db.get_product(pid)
    if not p:
        logger.error(f"edit_product_durations: Product not found for pid={pid}")
        all_products = db.get_products()
        logger.info(f"Available products: {[p['pid'] for p in all_products]}")
        await query.edit_message_text(f'❌ Product not found! PID: `{pid}`', parse_mode=ParseMode.MARKDOWN)
        return
    kb = []
    for dur in AVAILABLE_DURATIONS:
        dur_data = db.get_duration_prices(pid)
        current = 0
        active = False
        for d in dur_data:
            if d['duration'] == dur:
                current = d['admin_price'] or 0
                active = d['is_active'] == 1
                break
        api_dur = DURATION_MAP.get(dur, dur)
        status = '✅' if active and current > 0 else '❌'
        kb.append([InlineKeyboardButton(f'{status} {dur} ({api_dur}) - ₹{current:.2f}', callback_data=f'setdur_{pid}_{dur}')])
        kb.append([InlineKeyboardButton(f'🔄 Toggle {dur}', callback_data=f'toggle_{pid}_{dur}')])
    kb.append([InlineKeyboardButton('🗑️ Delete Product', callback_data=f'delprod_{pid}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='set_dur_prices')])
    await query.edit_message_text(
        f"✏️ *{p['name']}*\n🆔 `{pid}`\n\nDuration prices:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )

async def toggle_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.replace('toggle_', '')

    # FIX: Same parsing logic as set_duration_price_start
    found_duration = None
    pid = None

    for dur in AVAILABLE_DURATIONS:
        if data.endswith('_' + dur):
            found_duration = dur
            pid = data[:-(len(dur) + 1)]
            break

    if not found_duration:
        logger.error(f"toggle_duration_callback: Could not parse duration from: {data}")
        return

    duration = found_duration
    db.toggle_duration(pid, duration)
    await edit_product_durations(update, context)

async def delete_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.replace('delprod_', '')
    p = db.get_product(pid)
    if p:
        db.delete_product(pid)
        await query.edit_message_text(
            f"🗑️ *Product Deleted!*\n\n📦 {p['name']}\n🆔 `{pid}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_text('❌ Product not found!')

# ==================== EXISTING HANDLERS ====================
async def set_api_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(f"🔑 Enter your *API Access Token*:\n\n🔗 URL: `{FIXED_API_URL}`", parse_mode=ParseMode.MARKDOWN)
    return SET_API_KEY

async def save_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    db.set_api_key(key)
    await update.message.reply_text(f"✅ *API Configured!*\n\n🔑 `{key[:15]}...`\n🔗 `{FIXED_API_URL}`", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]))
    return ConversationHandler.END

async def fetch_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cfg = db.get_config()
    if not cfg:
        await query.edit_message_text('❌ API not configured!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]))
        return
    await query.edit_message_text('🔄 Fetching products...')
    api = ResellerAPI(cfg['api_key'], cfg['api_url'])
    products = api.fetch_products()
    if products:
        db.save_products(products)
        all_p = db.get_products()
        await query.edit_message_text(f"✅ *Products Fetched!*\n\n📦 Total: {len(all_p)}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]))
    else:
        await query.edit_message_text('❌ Failed to fetch!\n\n📋 Check `bot_debug.log` for details.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]))

# ==================== BOT LOCK HANDLER ====================
async def bot_lock_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    current = db.get_bot_lock_status()
    new_locked = not current['is_locked']
    db.set_bot_lock(new_locked, user.id if new_locked else None)
    if new_locked:
        await query.answer('🔒 Bot Locked!', show_alert=True)
    else:
        await query.answer('🔓 Bot Unlocked!', show_alert=True)
    await admin_panel(update, context)

# ==================== ADMIN VERIFY/REJECT/CANCEL ====================
async def admin_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    order_id = query.data.replace('admin_verify_', '')
    await query.edit_message_text(f'🔄 Processing `{order_id}`...', parse_mode=ParseMode.MARKDOWN)
    success, msg = await verify_and_deliver(update, context, order_id)
    final_kb = [[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]
    if not db.is_main_admin(user.id):
        final_kb = [[InlineKeyboardButton('💰 Payment Panel', callback_data='subadmin_panel')]]
    if success:
        await query.edit_message_text(f'✅ *Delivered!*\n\n🆔 `{order_id}`\n\n{msg}', parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(final_kb))
    else:
        await query.edit_message_text(f'⚠️ *Result*\n\n🆔 `{order_id}`\n\n{msg}', parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(final_kb))

async def admin_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    order_id = query.data.replace('admin_reject_', '')
    order = db.get_order(order_id)
    if not order:
        await query.edit_message_text('❌ Order not found!')
        return
    db.update_payment(order_id, 'rejected')
    db.update_order(order_id, 'cancelled')
    try:
        await context.bot.send_message(order['user_id'], f"❌ *Payment Rejected*\n\nOrder: `{order_id}`\nReason: Not verified", parse_mode=ParseMode.MARKDOWN)
    except:
        pass
    kb = [[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]
    if not db.is_main_admin(user.id):
        kb = [[InlineKeyboardButton('💰 Payment Panel', callback_data='subadmin_panel')]]
    await query.edit_message_text(f'❌ Order `{order_id}` rejected.', parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    order_id = query.data.replace('admin_cancel_', '')
    order = db.get_order(order_id)
    if not order:
        await query.edit_message_text('Order not found!')
        return
    db.cancel_order(order_id)
    try:
        msg = "Order Cancelled by Admin" + chr(10) + chr(10) + "Order: " + order_id + chr(10) + "Product: " + order['product_name'] + chr(10) + "Amount: Rs " + str(order['price_paid']) + chr(10) + chr(10) + "Your order has been cancelled by admin. Contact support for help."
        await context.bot.send_message(order['user_id'], msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error('Cancel notify failed: ' + str(e))
    msg2 = "Order Cancelled!" + chr(10) + chr(10) + "ID: " + order_id + chr(10) + "Product: " + order['product_name'] + chr(10) + "User: " + str(order['username'] or order['user_id'])
    kb = [[InlineKeyboardButton('Admin Panel', callback_data='admin')]]
    if not db.is_main_admin(user.id):
        kb = [[InlineKeyboardButton('Payment Panel', callback_data='subadmin_panel')]]
    await query.edit_message_text(msg2, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

# ==================== COMMAND HANDLERS ====================
async def cmd_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await update.message.reply_text('❌ Unauthorized!')
        return
    cmd_text = update.message.text.strip()
    match = re.search(r'/verify_(.+)', cmd_text)
    if not match:
        await update.message.reply_text('❌ Usage: /verify_ORDERID')
        return
    order_id = match.group(1).strip()
    await update.message.reply_text(f'🔄 Processing `{order_id}`...', parse_mode=ParseMode.MARKDOWN)
    success, msg = await verify_and_deliver(update, context, order_id)
    if success:
        await update.message.reply_text(f'✅ *Success!*\n\n🆔 `{order_id}`\n\n{msg}', parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f'⚠️ *Result:*\n\n🆔 `{order_id}`\n\n{msg}', parse_mode=ParseMode.MARKDOWN)

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await update.message.reply_text('❌ Unauthorized!')
        return
    cmd_text = update.message.text.strip()
    match = re.search(r'/reject_(.+)', cmd_text)
    if not match:
        await update.message.reply_text('❌ Usage: /reject_ORDERID')
        return
    order_id = match.group(1).strip()
    order = db.get_order(order_id)
    if not order:
        await update.message.reply_text('❌ Order not found!')
        return
    db.update_payment(order_id, 'rejected')
    db.update_order(order_id, 'cancelled')
    try:
        await context.bot.send_message(order['user_id'], f"❌ *Payment Rejected*\n\nOrder: `{order_id}`", parse_mode=ParseMode.MARKDOWN)
    except:
        pass
    await update.message.reply_text(f'❌ Order `{order_id}` rejected.', parse_mode=ParseMode.MARKDOWN)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await update.message.reply_text('Unauthorized!')
        return
    cmd_text = update.message.text.strip()
    match = re.search(r'/cancel_(.+)', cmd_text)
    if not match:
        await update.message.reply_text('Usage: /cancel_ORDERID')
        return
    order_id = match.group(1).strip()
    order = db.get_order(order_id)
    if not order:
        await update.message.reply_text('Order not found!')
        return
    db.cancel_order(order_id)
    try:
        msg = "Order Cancelled by Admin" + chr(10) + chr(10) + "Order: " + order_id + chr(10) + "Product: " + order['product_name'] + chr(10) + "Amount: Rs " + str(order['price_paid']) + chr(10) + chr(10) + "Your order has been cancelled by admin. Contact support for help."
        await context.bot.send_message(order['user_id'], msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error('Cancel notify failed: ' + str(e))
    await update.message.reply_text('Order ' + order_id + ' cancelled by admin.', parse_mode=ParseMode.MARKDOWN)

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await update.message.reply_text('Unauthorized!')
        return
    try:
        with open('bot_debug.log', 'r') as f:
            lines = f.readlines()
            last_response = []
            for line in reversed(lines):
                if 'RAW RESPONSE' in line or 'PARSED JSON' in line:
                    last_response.append(line.strip())
                if len(last_response) > 20:
                    break
            if last_response:
                msg = "Last API Responses (most recent first):\n\n"
                for line in reversed(last_response):
                    msg += line + "\n"
                await update.message.reply_text(msg[:4000])
            else:
                await update.message.reply_text("No API responses found in log.")
    except Exception as e:
        await update.message.reply_text(f"Error reading log: {e}")

async def pending_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    db_conn = db.conn()
    c = db_conn.cursor()
    c.execute("SELECT order_id, user_id, username, product_name, price_paid, payment_utr, duration FROM orders WHERE payment_status = 'pending_verification'")
    rows = c.fetchall()
    db_conn.close()
    if not rows:
        await query.edit_message_text('✅ No pending payments!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]))
        return
    msg = '⏳ *Pending Payments:*\n\n'
    for r in rows:
        msg += f"🆔 `{r[0]}`\n👤 {r[2] or r[1]}\n📦 {r[3]}\n⏱️ {r[6]}\n💰 ₹{r[4]:.2f}\n🔢 `{r[5] or 'N/A'}`\n/verify_{r[0]} | /reject_{r[0]}\n\n"
    kb = [[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    orders = db.get_user_orders(user.id)
    if not orders:
        await query.edit_message_text('📭 No orders yet!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='back')]]))
        return
    msg = '📦 *Your Orders:*\n\n'
    for o in orders[:10]:
        emoji = '✅' if o['order_status'] == 'completed' else '⏳'
        msg += f"{emoji} *{o['product_name']}*\n   ID: `{o['order_id']}`\n   ⏱️ {o['duration']}\n   ₹{o['price_paid']:.2f}\n   {o['order_status']}\n\n"
    kb = [[InlineKeyboardButton('🔙 Back', callback_data='back')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    msg = f"""
👤 *Your Profile*
━━━━━━━━━━━━━━━━━━
🆔 `{user.id}`
👤 {user.first_name} {user.last_name or ''}
📛 @{user.username or 'N/A'}
"""
    kb = [[InlineKeyboardButton('🔙 Back', callback_data='back')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg = """
🆘 *Support Center*
━━━━━━━━━━━━━━━━━━
📧 Email:
💬 Telegram: @Personreplybot

Common Issues:
• Payment pending? Wait 5-10 min
• Key issue? Share Order ID
"""
    kb = [[InlineKeyboardButton('🔙 Back', callback_data='back')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await start(update, context)

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    oid = context.user_data.get('order_id')
    if oid:
        db.update_order(oid, 'cancelled_by_user')
        db.update_payment(oid, 'cancelled')
    context.user_data.clear()
    await query.edit_message_text('❌ Order cancelled.\n\nStart again anytime!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🏠 Main Menu', callback_data='back')]]))

# ==================== EDIT PRODUCT FEATURE ====================
async def edit_product_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    products = db.get_products()
    if not products:
        await query.edit_message_text('📭 No products found!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]]))
        return
    kb = []
    for p in products:
        kb.append([InlineKeyboardButton(f'✏️ {p["name"]}', callback_data=f'editprod_{p["pid"]}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='admin')])
    await query.edit_message_text('✏️ *Select Product to Edit:*', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def edit_product_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    pid = query.data.replace('editprod_', '')
    p = db.get_product(pid)
    if not p:
        await query.edit_message_text('❌ Product not found!')
        return
    context.user_data['edit_pid'] = pid
    msg = f"""✏️ *Edit Product*

📦 *{p['name']}*
🆔 `{pid}`
📝 {p['description'] or 'No description'}
📂 Category: {p['category'] or 'N/A'}

What do you want to edit?"""
    kb = [
        [InlineKeyboardButton('📝 Edit Name', callback_data='edit_field_name')],
        [InlineKeyboardButton('📄 Edit Description', callback_data='edit_field_desc')],
        [InlineKeyboardButton('📂 Edit Category', callback_data='edit_field_cat')],
        [InlineKeyboardButton('💰 Edit Duration Prices', callback_data=f'editdur_{pid}')],
        [InlineKeyboardButton('🗑️ Delete Product', callback_data=f'delprod_{pid}')],
        [InlineKeyboardButton('🔙 Back', callback_data='edit_product_list')]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def edit_field_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('❌ Unauthorized!', show_alert=True)
        return
    await query.answer()
    field = query.data.replace('edit_field_', '')
    pid = context.user_data.get('edit_pid')
    if not pid:
        await query.edit_message_text('❌ Session expired!')
        return
    p = db.get_product(pid)
    if not p:
        await query.edit_message_text('❌ Product not found!')
        return
    context.user_data['edit_field'] = field
    field_names = {'name': 'Product Name', 'desc': 'Description', 'cat': 'Category'}
    current_values = {'name': p['name'], 'desc': p['description'] or 'No description', 'cat': p['category'] or 'N/A'}
    await query.edit_message_text(
        f"✏️ *Edit {field_names.get(field, field)}*\n\n📦 {p['name']}\n\nCurrent: `{current_values[field]}`\n\nEnter new {field_names.get(field, field)}:",
        parse_mode=ParseMode.MARKDOWN
    )
    if field == 'name':
        return EDIT_NAME
    elif field == 'desc':
        return EDIT_DESC
    elif field == 'cat':
        return EDIT_CAT
    return ConversationHandler.END

async def save_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    pid = context.user_data.get('edit_pid')
    if not pid:
        await update.message.reply_text('❌ Session expired!')
        return ConversationHandler.END
    if not new_name:
        await update.message.reply_text('❌ Name cannot be empty!')
        return EDIT_NAME
    db.update_product(pid, name=new_name)
    p = db.get_product(pid)
    kb = [
        [InlineKeyboardButton('✏️ Edit Again', callback_data=f'editprod_{pid}')],
        [InlineKeyboardButton('📋 View All Products', callback_data='view_products')],
        [InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]
    ]
    await update.message.reply_text(
        f"✅ *Name Updated!*\n\n📦 `{p['name']}`\n🆔 `{pid}`",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def save_edit_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_desc = update.message.text.strip()
    pid = context.user_data.get('edit_pid')
    if not pid:
        await update.message.reply_text('❌ Session expired!')
        return ConversationHandler.END
    if new_desc == '.':
        new_desc = ''
    db.update_product(pid, description=new_desc)
    p = db.get_product(pid)
    kb = [
        [InlineKeyboardButton('✏️ Edit Again', callback_data=f'editprod_{pid}')],
        [InlineKeyboardButton('📋 View All Products', callback_data='view_products')],
        [InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]
    ]
    await update.message.reply_text(
        f"✅ *Description Updated!*\n\n📦 {p['name']}\n📝 `{p['description'] or 'No description'}`",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def save_edit_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_cat = update.message.text.strip()
    pid = context.user_data.get('edit_pid')
    if not pid:
        await update.message.reply_text('❌ Session expired!')
        return ConversationHandler.END
    if not new_cat:
        await update.message.reply_text('❌ Category cannot be empty!')
        return EDIT_CAT
    db.update_product(pid, category=new_cat)
    p = db.get_product(pid)
    kb = [
        [InlineKeyboardButton('✏️ Edit Again', callback_data=f'editprod_{pid}')],
        [InlineKeyboardButton('📋 View All Products', callback_data='view_products')],
        [InlineKeyboardButton('🔐 Admin Panel', callback_data='admin')]
    ]
    await update.message.reply_text(
        f"✅ *Category Updated!*\n\n📦 {p['name']}\n📂 `{p['category']}`",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

# ==================== AUTO CLEAR SETTINGS ====================
async def auto_clear_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    settings = db.get_auto_clear_settings()
    status = 'Enabled' if settings['enabled'] else 'Disabled'
    msg = "🧹 *Auto Clear Settings*" + chr(10) + chr(10) + "Current Status: " + status + chr(10) + "Clear After: " + str(settings['hours']) + " hours" + chr(10) + chr(10) + "Completed/Cancelled/Rejected orders older than " + str(settings['hours']) + " hours will be automatically deleted from customer history." + chr(10) + chr(10) + "Select option:"
    kb = [
        [InlineKeyboardButton('⏱️ Set Clear Hours', callback_data='set_clear_hours')],
        [InlineKeyboardButton('🔄 Toggle ON/OFF', callback_data='toggle_auto_clear')],
        [InlineKeyboardButton('🧹 Manual Clear Now', callback_data='manual_clear')],
        [InlineKeyboardButton('🔙 Back', callback_data='admin')]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def toggle_auto_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    settings = db.get_auto_clear_settings()
    new_enabled = not settings['enabled']
    db.set_auto_clear_settings(settings['hours'], new_enabled)
    status = 'Enabled' if new_enabled else 'Disabled'
    await query.answer('Auto Clear ' + status, show_alert=True)
    await auto_clear_settings_menu(update, context)

async def set_clear_hours_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    settings = db.get_auto_clear_settings()
    msg = "⏱️ *Set Auto Clear Hours*" + chr(10) + chr(10) + "Current: " + str(settings['hours']) + " hours" + chr(10) + chr(10) + "Enter new value (in hours):" + chr(10) + chr(10) + "Examples:" + chr(10) + "- 24 = 1 day" + chr(10) + "- 168 = 1 week" + chr(10) + "- 720 = 30 days" + chr(10) + "- 0 = Disable auto clear"
    await query.edit_message_text(
        msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Cancel', callback_data='auto_clear_settings')]])
    )
    return SET_CLEAR_HOURS

async def save_clear_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = int(update.message.text.strip())
        if hours < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text('❌ Invalid! Enter a number (0 or greater).')
        return SET_CLEAR_HOURS
    enabled = hours > 0
    db.set_auto_clear_settings(hours, enabled)
    status = 'Enabled' if enabled else 'Disabled'
    kb = [[InlineKeyboardButton('🧹 Auto Clear Settings', callback_data='auto_clear_settings')]]
    msg = "✅ *Settings Updated!*" + chr(10) + chr(10) + "Clear After: " + str(hours) + " hours" + chr(10) + "Status: " + status
    await update.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def manual_clear_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    deleted = db.clear_old_orders()
    kb = [[InlineKeyboardButton('🧹 Auto Clear Settings', callback_data='auto_clear_settings')]]
    msg = "✅ *Manual Clear Complete!*" + chr(10) + chr(10) + "Deleted " + str(deleted) + " old orders from history."
    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )

# ==================== CHECK JOIN AGAIN HANDLER ====================
async def check_join_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    is_member, missing = await check_channel_membership(user.id, context.bot)
    if is_member:
        await query.edit_message_text(
            "✅ *Channel Join Verified!*\n\nAb aap bot use kar sakte ho! 🎉",
            parse_mode=ParseMode.MARKDOWN
        )
        await start(update, context)
    else:
        await send_join_required_message(update, context, missing)

# ==================== WEB SERVER ====================
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response = json.dumps({"status": "ok", "bot": "running", "timestamp": datetime.now().isoformat()})
        self.wfile.write(response.encode())
    def log_message(self, format, *args):
        pass

def run_web_server():
    try:
        server = HTTPServer(('0.0.0.0', 10000), HealthHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"Web server error: {e}")

# ==================== AUTO CLEAR BACKGROUND TASK ====================
async def auto_clear_task(context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    while True:
        try:
            await asyncio.sleep(3600)
            deleted = db.clear_old_orders()
            if deleted > 0:
                logger.info(f"Auto-clear: Deleted {deleted} old orders")
                for admin_id in ADMIN_USER_IDS:
                    try:
                        await context.bot.send_message(
                            admin_id,
                            f"🧹 Auto Clear Report: {deleted} old orders deleted from history."
                        )
                    except:
                        pass
        except Exception as e:
            logger.error(f"Auto-clear task error: {e}")
            await asyncio.sleep(3600)

# ==================== MAIN ====================
def main():
    init_db()

    # Start health check server in background
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    print("🌐 Health check server on port 10000")

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversations
    api_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_api_start, pattern='^set_api$')],
        states={SET_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_api_key)]},
        fallbacks=[CommandHandler('cancel', start)]
    )
    add_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_product_start, pattern='^add_product$')],
        states={
            ADD_PID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_pid)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_desc)],
            ADD_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_cat)]
        },
        fallbacks=[CommandHandler('cancel', start)]
    )
    dur_price_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(set_duration_price_start, pattern='^setdur_'),
            CallbackQueryHandler(set_duration_price_start, pattern='^editdur_')
        ],
        states={ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_duration_price)]},
        fallbacks=[CommandHandler('cancel', start)]
    )
    utr_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(enter_utr, pattern='^enter_utr$')],
        states={ENTER_UTR: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_utr)]},
        fallbacks=[CallbackQueryHandler(cancel_order, pattern='^cancel$'), CommandHandler('cancel', start)]
    )
    edit_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_field_start, pattern='^edit_field_')],
        states={
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit_name)],
            EDIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit_desc)],
            EDIT_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit_cat)]
        },
        fallbacks=[CommandHandler('cancel', start)]
    )
    auto_clear_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_clear_hours_start, pattern='^set_clear_hours$')],
        states={SET_CLEAR_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_clear_hours)]},
        fallbacks=[CommandHandler('cancel', start)]
    )
    subadmin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_subadmin_start, pattern='^add_subadmin$')],
        states={ADD_SUB_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_subadmin)]},
        fallbacks=[CommandHandler('cancel', start)]
    )

    # Handlers
    app.add_handler(CommandHandler('start', start))
    app.add_handler(api_conv)
    app.add_handler(add_product_conv)
    app.add_handler(dur_price_conv)
    app.add_handler(utr_conv)
    app.add_handler(edit_product_conv)
    app.add_handler(auto_clear_conv)
    app.add_handler(subadmin_conv)

    app.add_handler(CallbackQueryHandler(bot_lock_toggle, pattern='^bot_lock_toggle$'))
    app.add_handler(CallbackQueryHandler(admin_verify_callback, pattern='^admin_verify_'))
    app.add_handler(CallbackQueryHandler(admin_reject_callback, pattern='^admin_reject_'))
    app.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern='^admin_cancel_'))
    app.add_handler(CallbackQueryHandler(product_details, pattern='^prod_'))
    app.add_handler(CallbackQueryHandler(select_duration, pattern='^dur_'))
    app.add_handler(CallbackQueryHandler(buy_product, pattern='^buy_'))
    app.add_handler(CallbackQueryHandler(toggle_duration_callback, pattern='^toggle_'))
    app.add_handler(CallbackQueryHandler(delete_product_callback, pattern='^delprod_'))
    app.add_handler(CallbackQueryHandler(edit_product_durations, pattern='^editdur_'))
    app.add_handler(CallbackQueryHandler(edit_product_list, pattern='^edit_product_list$'))
    app.add_handler(CallbackQueryHandler(edit_product_menu, pattern='^editprod_'))
    app.add_handler(CallbackQueryHandler(auto_clear_settings_menu, pattern='^auto_clear_settings$'))
    app.add_handler(CallbackQueryHandler(toggle_auto_clear, pattern='^toggle_auto_clear$'))
    app.add_handler(CallbackQueryHandler(manual_clear_now, pattern='^manual_clear$'))
    app.add_handler(CallbackQueryHandler(subadmin_panel, pattern='^subadmin_panel$'))
    app.add_handler(CallbackQueryHandler(subadmin_pending, pattern='^subadmin_pending$'))
    app.add_handler(CallbackQueryHandler(manage_subadmins, pattern='^manage_subadmins$'))
    app.add_handler(CallbackQueryHandler(remove_subadmin_list, pattern='^remove_subadmin_list$'))
    app.add_handler(CallbackQueryHandler(remove_subadmin_callback, pattern='^removesub_'))
    app.add_handler(CallbackQueryHandler(check_join_again, pattern='^check_join$'))
    app.add_handler(CallbackQueryHandler(shop_now, pattern='^shop_now$'))
    app.add_handler(CallbackQueryHandler(my_orders, pattern='^my_orders$'))
    app.add_handler(CallbackQueryHandler(profile, pattern='^profile$'))
    app.add_handler(CallbackQueryHandler(support, pattern='^support$'))
    app.add_handler(CallbackQueryHandler(back_main, pattern='^back$'))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern='^cancel$'))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin$'))
    app.add_handler(CallbackQueryHandler(fetch_products, pattern='^fetch$'))
    app.add_handler(CallbackQueryHandler(view_products, pattern='^view_products$'))
    app.add_handler(CallbackQueryHandler(set_dur_prices_list, pattern='^set_dur_prices$'))
    app.add_handler(CallbackQueryHandler(pending_payments, pattern='^pending$'))
    app.add_handler(CommandHandler('verify', cmd_verify))
    app.add_handler(CommandHandler('reject', cmd_reject))
    app.add_handler(CommandHandler('cancel', cmd_cancel))
    app.add_handler(CommandHandler('debug', cmd_debug))

    print('KeyShop Bot v4.5 - WEBHOOK ONLY')
    print('API Endpoint: ' + str(FIXED_API_URL))

    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(auto_clear_task, interval=3600, first=60)

    # WEBHOOK ONLY - NO POLLING
    render_port = int(os.environ.get("PORT", "10000"))
    webhook_url = os.environ.get("WEBHOOK_URL")

    if not webhook_url:
        print("❌ WEBHOOK_URL not set! Cannot start.")
        sys.exit(1)

    print(f"🚀 Webhook: {webhook_url} | Port: {render_port}")

    app.run_webhook(
        listen="0.0.0.0",
        port=render_port,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == '__main__':
    main()
