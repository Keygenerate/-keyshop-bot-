#!/usr/bin/env python3
"""
KeyShop Telegram Bot v5.3 - FULL FEATURES
Features:
- QR Payment (UPI QR code)
- Manual Admin Verify (UTR + Screenshot upload)
- Job Queue Fix (weak reference error fixed)
- Manual Product Add
- Webhook deployment for Render
"""

import logging
import os
import sys
import sqlite3
import json
import requests
import asyncio
from collections import deque
import uuid
import re
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, ConversationHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
from aiohttp import web
import qrcode
from PIL import Image, ImageDraw, ImageFont
import io

LOG_BUFFER = deque(maxlen=200)

class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_USER_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]
FORCE_JOIN_CHANNELS = os.environ.get("FORCE_CHANNELS", "").split(",") if os.environ.get("FORCE_CHANNELS") else []
DB_FILE = "keyshop_bot.db"
UPI_ID = os.environ.get("UPI_ID")
UPI_NAME = os.environ.get("UPI_NAME")
FIXED_API_URL = os.environ.get("API_URL")

DURATION_MAP = {
    "1 Day": "1 DaYS", "3 Days": "3 DaYS", "7 Days": "7 DaYS",
    "10 Days": "10 DaYS", "14 Days": "14 DaYS", "15 Days": "15 DaYS",
    "20 Days": "20 DaYS", "30 Days": "30 DaYS"
}
AVAILABLE_DURATIONS = list(DURATION_MAP.keys())

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout), MemoryLogHandler()]
)
logger = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS admin_config (id INTEGER PRIMARY KEY, api_key TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, pid TEXT UNIQUE, name TEXT, description TEXT, category TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS product_durations (id INTEGER PRIMARY KEY, pid TEXT, duration TEXT, admin_price REAL, is_active INTEGER DEFAULT 1, UNIQUE(pid, duration))")
    c.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, order_id TEXT UNIQUE, user_id INTEGER, username TEXT, pid TEXT, product_name TEXT, price_paid REAL, duration TEXT, payment_status TEXT DEFAULT 'pending', payment_utr TEXT, payment_screenshot TEXT, key_delivered TEXT, order_status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS auto_clear_settings (id INTEGER PRIMARY KEY, clear_after_hours INTEGER DEFAULT 24, enabled INTEGER DEFAULT 1)")
    c.execute("INSERT OR IGNORE INTO auto_clear_settings (id, clear_after_hours, enabled) VALUES (1, 24, 1)")
    c.execute("CREATE TABLE IF NOT EXISTS bot_lock_settings (id INTEGER PRIMARY KEY, is_locked INTEGER DEFAULT 0, locked_by INTEGER)")
    c.execute("INSERT OR IGNORE INTO bot_lock_settings (id, is_locked, locked_by) VALUES (1, 0, NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS sub_admins (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, added_by INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, username TEXT, first_name TEXT, last_name TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS utr_blacklist (id INTEGER PRIMARY KEY, utr TEXT UNIQUE, reason TEXT, added_by INTEGER, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS utr_used (id INTEGER PRIMARY KEY, utr TEXT UNIQUE, order_id TEXT, user_id INTEGER, used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS fraud_alerts (id INTEGER PRIMARY KEY, order_id TEXT, user_id INTEGER, reason TEXT, details TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS user_rate_limits (id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, last_order_at TIMESTAMP, order_count INTEGER DEFAULT 0)")
    conn.commit()
    conn.close()

class Database:
    def __init__(self):
        self.db_file = DB_FILE
    def conn(self):
        return sqlite3.connect(self.db_file)
    def set_api_key(self, api_key):
        db = self.conn(); c = db.cursor()
        c.execute('DELETE FROM admin_config')
        c.execute('INSERT INTO admin_config (api_key) VALUES (?)', (api_key,))
        db.commit(); db.close()
    def get_config(self):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT api_key FROM admin_config LIMIT 1')
        row = c.fetchone(); db.close()
        return {'api_key': row[0], 'api_url': FIXED_API_URL} if row else None
    def add_sub_admin(self, user_id, added_by):
        db = self.conn(); c = db.cursor()
        c.execute('INSERT OR REPLACE INTO sub_admins (user_id, added_by) VALUES (?, ?)', (user_id, added_by))
        db.commit(); db.close()
    def remove_sub_admin(self, user_id):
        db = self.conn(); c = db.cursor()
        c.execute('DELETE FROM sub_admins WHERE user_id = ?', (user_id,))
        db.commit(); db.close()
    def is_sub_admin(self, user_id):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT 1 FROM sub_admins WHERE user_id = ?', (user_id,))
        row = c.fetchone(); db.close()
        return row is not None
    def get_sub_admins(self):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT user_id, added_by FROM sub_admins')
        rows = c.fetchall(); db.close()
        return [{'user_id': r[0], 'added_by': r[1]} for r in rows]
    def is_any_admin(self, user_id):
        return user_id in ADMIN_USER_IDS or self.is_sub_admin(user_id)
    def is_main_admin(self, user_id):
        return user_id in ADMIN_USER_IDS
    def is_bot_locked(self):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT is_locked FROM bot_lock_settings WHERE id = 1')
        row = c.fetchone(); db.close()
        return bool(row[0]) if row else False
    def set_bot_lock(self, locked, admin_id=None):
        db = self.conn(); c = db.cursor()
        if locked:
            c.execute('UPDATE bot_lock_settings SET is_locked = 1, locked_by = ? WHERE id = 1', (admin_id,))
        else:
            c.execute('UPDATE bot_lock_settings SET is_locked = 0, locked_by = NULL WHERE id = 1')
        db.commit(); db.close()
    def get_bot_lock_status(self):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT is_locked, locked_by FROM bot_lock_settings WHERE id = 1')
        row = c.fetchone(); db.close()
        return {'is_locked': bool(row[0]), 'locked_by': row[1]} if row else {'is_locked': False, 'locked_by': None}
    def update_product(self, pid, name=None, description=None, category=None):
        db = self.conn(); c = db.cursor()
        updates, params = [], []
        if name: updates.append('name = ?'); params.append(name)
        if description is not None: updates.append('description = ?'); params.append(description)
        if category: updates.append('category = ?'); params.append(category)
        if updates:
            params.append(pid)
            c.execute(f"UPDATE products SET {', '.join(updates)} WHERE pid = ?", params)
            db.commit()
        db.close()
    def add_manual_product(self, pid, name, description, category):
        db = self.conn(); c = db.cursor()
        c.execute("INSERT OR REPLACE INTO products (pid, name, description, category) VALUES (?, ?, ?, ?)", (pid, name, description, category))
        db.commit(); db.close()
    def set_duration_price(self, pid, duration, price):
        db = self.conn(); c = db.cursor()
        c.execute("INSERT OR REPLACE INTO product_durations (pid, duration, admin_price, is_active) VALUES (?, ?, ?, 1)", (pid, duration, price))
        db.commit(); db.close()
    def get_duration_prices(self, pid):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT duration, admin_price, is_active FROM product_durations WHERE pid = ?', (pid,))
        rows = c.fetchall(); db.close()
        return [{'duration': r[0], 'admin_price': r[1], 'is_active': r[2]} for r in rows]
    def get_active_durations(self, pid):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT duration, admin_price FROM product_durations WHERE pid = ? AND is_active = 1 AND admin_price > 0', (pid,))
        rows = c.fetchall(); db.close()
        return [{'duration': r[0], 'price': r[1]} for r in rows]
    def toggle_duration(self, pid, duration):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT is_active FROM product_durations WHERE pid = ? AND duration = ?', (pid, duration))
        row = c.fetchone()
        if row:
            new_status = 0 if row[0] == 1 else 1
            c.execute('UPDATE product_durations SET is_active = ? WHERE pid = ? AND duration = ?', (new_status, pid, duration))
            db.commit()
        db.close()
    def save_products(self, products):
        db = self.conn(); c = db.cursor()
        for p in products:
            pid = str(p.get('pid', p.get('id', '')))
            name = p.get('name', 'Unknown')
            desc = p.get('description', '')
            cat = p.get('category', 'General')
            c.execute("INSERT OR REPLACE INTO products (pid, name, description, category) VALUES (?, ?, ?, ?)", (pid, name, desc, cat))
            durations = p.get('durations', [])
            if not durations and p.get('duration'):
                durations = [{'duration': p.get('duration'), 'price': p.get('price', 0)}]
            for d in durations:
                dur = d.get('duration', '1 Day')
                price = float(d.get('price', 0))
                c.execute("INSERT OR REPLACE INTO product_durations (pid, duration, admin_price, is_active) VALUES (?, ?, ?, 1)", (pid, dur, price))
        db.commit(); db.close()
    def get_products(self):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT * FROM products ORDER BY name')
        rows = c.fetchall(); db.close()
        return [{'id': r[0], 'pid': r[1], 'name': r[2], 'description': r[3], 'category': r[4]} for r in rows]
    def get_product(self, pid):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT * FROM products WHERE pid = ?', (pid,))
        r = c.fetchone(); db.close()
        return {'id': r[0], 'pid': r[1], 'name': r[2], 'description': r[3], 'category': r[4]} if r else None
    def delete_product(self, pid):
        db = self.conn(); c = db.cursor()
        c.execute('DELETE FROM product_durations WHERE pid = ?', (pid,))
        c.execute('DELETE FROM products WHERE pid = ?', (pid,))
        db.commit(); db.close()
    def create_order(self, user_id, username, pid, product_name, price, duration):
        db = self.conn(); c = db.cursor()
        order_id = f'KS{uuid.uuid4().hex[:10].upper()}'
        c.execute("INSERT INTO orders (order_id, user_id, username, pid, product_name, price_paid, duration) VALUES (?, ?, ?, ?, ?, ?, ?)", (order_id, user_id, username, pid, product_name, price, duration))
        db.commit(); db.close()
        return order_id
    def get_order(self, order_id):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,))
        r = c.fetchone(); db.close()
        if r:
            return {'order_id': r[1], 'user_id': r[2], 'username': r[3], 'pid': r[4], 'product_name': r[5], 'price_paid': r[6], 'duration': r[7], 'payment_status': r[8], 'payment_utr': r[9], 'payment_screenshot': r[10], 'key_delivered': r[11], 'order_status': r[12]}
        return None
    def update_payment(self, order_id, status, utr=None, screenshot=None):
        db = self.conn(); c = db.cursor()
        if utr and screenshot:
            c.execute('UPDATE orders SET payment_status = ?, payment_utr = ?, payment_screenshot = ? WHERE order_id = ?', (status, utr, screenshot, order_id))
        elif utr:
            c.execute('UPDATE orders SET payment_status = ?, payment_utr = ? WHERE order_id = ?', (status, utr, order_id))
        elif screenshot:
            c.execute('UPDATE orders SET payment_status = ?, payment_screenshot = ? WHERE order_id = ?', (status, screenshot, order_id))
        else:
            c.execute('UPDATE orders SET payment_status = ? WHERE order_id = ?', (status, order_id))
        db.commit(); db.close()
    def update_order(self, order_id, status, key=None):
        db = self.conn(); c = db.cursor()
        if key:
            c.execute('UPDATE orders SET order_status = ?, key_delivered = ? WHERE order_id = ?', (status, key, order_id))
        else:
            c.execute('UPDATE orders SET order_status = ? WHERE order_id = ?', (status, order_id))
        db.commit(); db.close()
    def get_user_orders(self, user_id):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
        rows = c.fetchall(); db.close()
        return [{'order_id': r[1], 'product_name': r[5], 'price_paid': r[6], 'duration': r[7], 'payment_status': r[8], 'order_status': r[12]} for r in rows]
    def get_auto_clear_settings(self):
        db = self.conn(); c = db.cursor()
        c.execute('SELECT clear_after_hours, enabled FROM auto_clear_settings WHERE id = 1')
        row = c.fetchone(); db.close()
        return {'hours': row[0], 'enabled': bool(row[1])} if row else {'hours': 24, 'enabled': True}
    def set_auto_clear_settings(self, hours, enabled):
        db = self.conn(); c = db.cursor()
        c.execute('UPDATE auto_clear_settings SET clear_after_hours = ?, enabled = ? WHERE id = 1', (hours, 1 if enabled else 0))
        db.commit(); db.close()
    def clear_old_orders(self):
        settings = self.get_auto_clear_settings()
        if not settings['enabled']: return 0
        db = self.conn(); c = db.cursor()
        hours = settings['hours']
        c.execute("DELETE FROM orders WHERE created_at < datetime('now', '-{} hours') AND order_status IN ('completed', 'cancelled', 'cancelled_by_user', 'rejected')".format(hours))
        deleted = c.rowcount; db.commit(); db.close()
        return deleted
    def get_pending_orders(self):
        db = self.conn(); c = db.cursor()
        c.execute("SELECT order_id, user_id, username, product_name, price_paid, payment_utr, payment_screenshot, duration, created_at FROM orders WHERE payment_status = 'pending_verification' ORDER BY created_at DESC")
        rows = c.fetchall(); db.close()
        return [{'order_id': r[0], 'user_id': r[1], 'username': r[2], 'product_name': r[3], 'price_paid': r[4], 'payment_utr': r[5], 'payment_screenshot': r[6], 'duration': r[7], 'created_at': r[8]} for r in rows]
    def cancel_order(self, order_id):
        db = self.conn(); c = db.cursor()
        c.execute("UPDATE orders SET payment_status = 'cancelled_by_admin', order_status = 'cancelled' WHERE order_id = ?", (order_id,))
        db.commit(); db.close()
    def save_user(self, user_id, username, first_name, last_name):
        db = self.conn(); c = db.cursor()
        c.execute("INSERT OR REPLACE INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)", (user_id, username, first_name, last_name))
        db.commit(); db.close()
    def blacklist_utr(self, utr, reason, added_by):
        db = self.conn(); c = db.cursor()
        c.execute("INSERT OR REPLACE INTO utr_blacklist (utr, reason, added_by) VALUES (?, ?, ?)", (utr, reason, added_by))
        db.commit(); db.close()
    def is_utr_blacklisted(self, utr):
        db = self.conn(); c = db.cursor()
        c.execute("SELECT 1 FROM utr_blacklist WHERE utr = ?", (utr,))
        row = c.fetchone(); db.close()
        return row is not None
    def is_utr_used(self, utr):
        db = self.conn(); c = db.cursor()
        c.execute("SELECT order_id, user_id, used_at FROM utr_used WHERE utr = ?", (utr,))
        row = c.fetchone(); db.close()
        return {'order_id': row[0], 'user_id': row[1], 'used_at': row[2]} if row else None
    def mark_utr_used(self, utr, order_id, user_id):
        db = self.conn(); c = db.cursor()
        c.execute("INSERT INTO utr_used (utr, order_id, user_id) VALUES (?, ?, ?)", (utr, order_id, user_id))
        db.commit(); db.close()
    def add_fraud_alert(self, order_id, user_id, reason, details):
        db = self.conn(); c = db.cursor()
        c.execute("INSERT INTO fraud_alerts (order_id, user_id, reason, details) VALUES (?, ?, ?, ?)", (order_id, user_id, reason, details))
        db.commit(); db.close()
    def get_fraud_alerts(self, status='open'):
        db = self.conn(); c = db.cursor()
        c.execute("SELECT * FROM fraud_alerts WHERE status = ? ORDER BY created_at DESC", (status,))
        rows = c.fetchall(); db.close()
        return [{'id': r[0], 'order_id': r[1], 'user_id': r[2], 'reason': r[3], 'details': r[4], 'status': r[5], 'created_at': r[6]} for r in rows]
    def resolve_fraud_alert(self, alert_id):
        db = self.conn(); c = db.cursor()
        c.execute("UPDATE fraud_alerts SET status = 'resolved' WHERE id = ?", (alert_id,))
        db.commit(); db.close()
    def get_user_rate_limit(self, user_id):
        db = self.conn(); c = db.cursor()
        c.execute("SELECT last_order_at, order_count FROM user_rate_limits WHERE user_id = ?", (user_id,))
        row = c.fetchone(); db.close()
        return {'last_order_at': row[0], 'order_count': row[1]} if row else None
    def update_user_rate_limit(self, user_id):
        db = self.conn(); c = db.cursor()
        now = datetime.now().isoformat()
        c.execute("INSERT OR REPLACE INTO user_rate_limits (user_id, last_order_at, order_count) VALUES (?, ?, COALESCE((SELECT order_count FROM user_rate_limits WHERE user_id = ?), 0) + 1)", (user_id, now, user_id))
        db.commit(); db.close()
    def reset_user_rate_limit(self, user_id):
        db = self.conn(); c = db.cursor()
        c.execute("DELETE FROM user_rate_limits WHERE user_id = ?", (user_id,))
        db.commit(); db.close()

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
            logger.info(f"API REQUEST: URL={self.api_url}, DATA={json.dumps(data)}")
            response = requests.post(self.api_url, data=data, timeout=30)
            logger.info(f"API RESPONSE: Status={response.status_code}")
            raw_text = response.text
            logger.info(f"RAW RESPONSE: {raw_text[:2000]}")
            if raw_text.strip().startswith('<'):
                logger.error("API returned HTML instead of JSON!")
                return None, f"HTML Response: {raw_text[:500]}"
            try:
                result = response.json()
                logger.info(f"PARSED JSON: {json.dumps(result, indent=2)[:1000]}")
                return result, None
            except json.JSONDecodeError as e:
                logger.error(f"JSON Parse Error: {e}")
                return None, f"JSON Parse Error: {e}\nRaw: {raw_text[:500]}"
        except Exception as e:
            logger.error(f"API Request Error: {e}")
            return None, str(e)
    def fetch_products(self):
        result, error = self._make_request({'api_key': self.api_key, 'action': 'products'})
        if error: return []
        if isinstance(result, list): return result
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
        if error: return None
        return result

async def auto_clear_qr_message(context: ContextTypes.DEFAULT_TYPE):
    """Auto-delete QR message and send main menu after timeout"""
    job_data = context.job.data
    chat_id = job_data.get('chat_id')
    message_id = job_data.get('message_id')
    order_id = job_data.get('order_id')

    if not chat_id or not message_id:
        return

    try:
        # Delete the QR message
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.info(f"Auto-clear: Could not delete QR message: {e}")

    # Cancel the order if still pending
    if order_id:
        db.update_order(order_id, 'cancelled_by_user')
        db.update_payment(order_id, 'cancelled')

    # Send main menu
    try:
        kb = [
            [InlineKeyboardButton('🛒 Shop Now', callback_data='shop_now')],
            [InlineKeyboardButton('📦 My Orders', callback_data='my_orders')],
            [InlineKeyboardButton('👤 Profile', callback_data='profile')],
            [InlineKeyboardButton('🆘 Support', callback_data='support')],
        ]
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏰ *Order Auto-Cancelled*\n\nYour payment session expired. Please start again!",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Auto-clear: Failed to send main menu: {e}")

class PaymentVerifier:
    def generate_upi_url(self, order_id, amount):
        if not UPI_ID or not UPI_NAME:
            return None
        pa = UPI_ID.strip()
        pn = UPI_NAME.strip()
        am = f"{amount:.2f}"
        cu = "INR"
        tn = f"Order {order_id}"
        tr = order_id
        return f"upi://pay?pa={pa}&pn={pn}&am={am}&cu={cu}&tn={tn}&tr={tr}"
    def generate_qr_image(self, order_id, amount, product_name):
        upi_url = self.generate_upi_url(order_id, amount)
        if not upi_url: return None
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
        qr.add_data(upi_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        try:
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
                small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
            except:
                font = ImageFont.load_default()
                small_font = font
            qr_width, qr_height = qr_img.size
            text_height = 100
            new_img = Image.new('RGB', (qr_width, qr_height + text_height), 'white')
            new_img.paste(qr_img, (0, 0))
            draw = ImageDraw.Draw(new_img)
            text_y = qr_height + 10
            # FIX: Use textbbox for centering instead of anchor (for older Pillow versions)
            try:
                # Try anchor method first (Pillow >= 8.0)
                draw.text((qr_width//2, text_y), f"Rs {amount:.2f}", fill="black", font=font, anchor="mm")
                draw.text((qr_width//2, text_y + 30), f"Order: {order_id}", fill="black", font=small_font, anchor="mm")
                draw.text((qr_width//2, text_y + 55), "Scan with any UPI App", fill="green", font=small_font, anchor="mm")
            except TypeError:
                # Fallback for older Pillow versions
                def draw_centered_text(y, text, font, fill):
                    bbox = draw.textbbox((0, 0), text, font=font)
                    text_width = bbox[2] - bbox[0]
                    x = (qr_width - text_width) // 2
                    draw.text((x, y), text, fill=fill, font=font)
                draw_centered_text(text_y, f"Rs {amount:.2f}", font, "black")
                draw_centered_text(text_y + 30, f"Order: {order_id}", small_font, "black")
                draw_centered_text(text_y + 55, "Scan with any UPI App", small_font, "green")
            qr_img = new_img
        except Exception as e:
            logger.warning(f"QR text overlay failed: {e}, using plain QR")
        buffer = io.BytesIO()
        qr_img.save(buffer, format='PNG')
        buffer.seek(0)
        return buffer
    def generate_payment_msg(self, order_id, amount, product_name, duration):
        lines = [
            "💳 *PAYMENT DETAILS*", "",
            f"📦 Product: {product_name}",
            f"⏱ Duration: {duration}",
            f"💰 Amount: Rs {amount:.2f}",
            f"🆔 Order ID: `{order_id}`", "",
            "━━━━━━━━━━━━━━━━━━", "",
            "📲 *Steps:*",
            "1️⃣ Scan QR code above",
            "2️⃣ Pay exact amount",
            "3️⃣ Save UTR/Reference number",
            "4️⃣ Click 'I Have Paid' below", "",
            "⚠️ _Pay exact amount only!_"
        ]
        return "\n".join(lines)

# ==================== CONVERSATION STATES ====================
SET_API_KEY, SET_PRICE, ENTER_UTR, UPLOAD_SCREENSHOT, ADD_PID, ADD_NAME, ADD_DESC, ADD_CAT, ADD_PRICE = range(9)
EDIT_NAME, EDIT_DESC, EDIT_CAT = range(9, 12)
SET_CLEAR_HOURS = 12
ADD_SUB_ADMIN = 13

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
                if len(dict_str) > 5: return dict_str
            elif isinstance(val, list) and val:
                list_str = json.dumps(val, indent=2)
                if len(list_str) > 5: return list_str
    nested_wrappers = ['data', 'result', 'response', 'output', 'payload', 'body']
    for wrapper in nested_wrappers:
        if wrapper in result:
            wrapped = result[wrapper]
            if isinstance(wrapped, dict):
                for k in direct_key_fields:
                    if k in wrapped and wrapped[k]:
                        val = wrapped[k]
                        if isinstance(val, str) and len(val) > 2: return val
                        elif isinstance(val, (dict, list)): return json.dumps(val, indent=2)
            elif isinstance(wrapped, list) and wrapped: return json.dumps(wrapped, indent=2)
            elif isinstance(wrapped, str) and len(wrapped) > 5: return wrapped
    text_fields = ['message', 'msg', 'description', 'detail', 'info', 'note', 'remark']
    for tf in text_fields:
        if tf in result and result[tf]:
            val = str(result[tf])
            if len(val) > 5: return val
    for k, v in result.items():
        if isinstance(v, str) and len(v) > 10 and k not in ['status', 'success', 'error', 'error_code', 'code', 'message']:
            if any(c in v for c in ['-', '_', ':', '.']) or len(v) > 20: return v
    logger.warning("No specific key field found, returning full response")
    return json.dumps(result, indent=2)[:2000]

# ==================== VERIFY & DELIVER ====================
async def verify_and_deliver(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
    order = db.get_order(order_id)
    if not order:
        return False, 'Order not found'
    if order['key_delivered']:
        return True, 'Already delivered'
    cfg = db.get_config()
    if not cfg:
        return False, 'API not configured'
    api = ResellerAPI(cfg['api_key'], cfg['api_url'])
    api_duration = DURATION_MAP.get(order['duration'], order['duration'])
    processing_msg = f"⏳ Processing Order...\n\n🆔 ID: `{order_id}`\n📦 Product: {order['product_name']}\n⏱ Duration: {order['duration']} (API: `{api_duration}`)\n👤 User: {order['username'] or order['user_id']}\n\n🔑 Fetching key from API..."
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
                f"⚠️ *Payment Verified but Key Delivery Failed*\n\n🆔 Order: `{order_id}`\n📦 Product: {order['product_name']}\n⏱ Duration: {order['duration']}\n💰 Amount: Rs {order['price_paid']:.2f}\n\n❌ Error: {error_msg}\n\n👨‍💼 Admin will deliver key manually.\n⏰ Please wait 5-10 minutes.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
        if status_msg:
            debug_info = f"❌ *Failed!*\n\n🆔 ID: `{order_id}`\n🔢 PID: `{order['pid']}`\n📤 Sent: `{api_dur}`\n\n❌ Error: `{error_msg}`\n\n💡 Use /debug command for details.\n\n🔧 Tips:\n• API key correct?\n• Duration format matches?\n• Stock available?"
            await status_msg.edit_text(debug_info, parse_mode=ParseMode.MARKDOWN)
        return False, f'{error_msg}'
    is_success = False
    success_reason = ""
    if result.get('status') == 'success':
        is_success = True; success_reason = "status=success"
    elif result.get('success') == True:
        is_success = True; success_reason = "success=true"
    elif result.get('error_code') == 0:
        is_success = True; success_reason = "error_code=0"
    elif result.get('code') == 200:
        is_success = True; success_reason = "code=200"
    elif result.get('status_code') == 200:
        is_success = True; success_reason = "status_code=200"
    elif result.get('stat') == 'OK':
        is_success = True; success_reason = "stat=OK"
    elif any(k in result for k in ['key', 'license', 'serial', 'serial_key', 'activation_key', 'code', 'token', 'password', 'pin', 'credentials', 'account', 'login', 'email']):
        is_success = True; success_reason = "key_field_found"
    elif isinstance(result.get('data'), dict) and any(k in result['data'] for k in ['key', 'license', 'serial', 'code', 'token', 'password', 'username', 'email']):
        is_success = True; success_reason = "nested_key_found"
    elif 'response' in result and result['response']:
        is_success = True; success_reason = "response_field"
    elif 'message' in result and isinstance(result['message'], str) and len(result['message']) > 10:
        msg = result['message'].lower()
        if any(k in msg for k in ['key', 'license', 'serial', 'code', 'password', 'token']):
            is_success = True; success_reason = "message_contains_key"
    logger.info(f"API Success Check: {is_success} (reason: {success_reason})")
    if is_success:
        key = extract_key_from_response(result)
        if key and len(key) > 5:
            db.update_order(order_id, 'completed', key)
            db.update_payment(order_id, 'verified')
            try:
                await context.bot.send_message(
                    order['user_id'],
                    f"✅ *Payment Verified & Key Delivered!*\n\n🆔 Order: `{order_id}`\n📦 Product: {order['product_name']}\n⏱ Duration: {order['duration']}\n💰 Amount: Rs {order['price_paid']:.2f}\n\n🔑 *Your Key:*\n`{key}`\n\n⚠️ *IMPORTANT: Save this key immediately!*\n\n🙏 Thank you!",
                    parse_mode=ParseMode.MARKDOWN
                )
                await context.bot.send_message(
                    order['user_id'],
                    'Click to go back',
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🏠 Main Menu', callback_data='back')]])
                )
            except Exception as e:
                logger.error(f'Delivery failed: {e}')
            if status_msg:
                await status_msg.edit_text(f"✅ *Order Completed!*\n\n🆔 ID: `{order_id}`\n🔑 Key delivered to user.", parse_mode=ParseMode.MARKDOWN)
            return True, f'Key: {key[:30]}...'
        else:
            raw = json.dumps(result, indent=2)
            if status_msg:
                await status_msg.edit_text(f"⚠️ *Key format unknown!*\n\n🆔 ID: `{order_id}`\n\n📄 Raw Response:\n```{raw[:400]}```", parse_mode=ParseMode.MARKDOWN)
            return False, f'Unknown key format\n{raw[:300]}'
    else:
        error_msg = result.get('message', result.get('error_msg', result.get('error', 'Unknown error')))
        api_dur = result.get('api_duration', 'unknown')
        try:
            await context.bot.send_message(
                order['user_id'],
                f"⚠️ *Payment Verified but Key Delivery Failed*\n\n🆔 Order: `{order_id}`\n📦 Product: {order['product_name']}\n⏱ Duration: {order['duration']}\n💰 Amount: Rs {order['price_paid']:.2f}\n\n❌ Error: {error_msg}\n\n👨‍💼 Admin will deliver key manually.\n⏰ Please wait.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
        if status_msg:
            debug_info = f"❌ *API Error!*\n\n🆔 ID: `{order_id}`\n🔢 PID: `{order['pid']}`\n📤 Sent to API: `{api_dur}`\n\n{error_msg}\n\n💡 Use /debug command for full response.\n\n🔧 Common fixes:\n• Check API key\n• Duration format correct?\n• Stock available?\n• API URL correct?"
            await status_msg.edit_text(debug_info, parse_mode=ParseMode.MARKDOWN)
        return False, f'API Error: {error_msg}'

# ==================== HANDLERS ====================
@require_channel_join
@require_bot_unlocked
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.save_user(user.id, user.username, user.first_name, user.last_name)
    is_main = db.is_main_admin(user.id)
    is_sub = db.is_sub_admin(user.id)
    text = f"👋 Welcome *{user.first_name}* to *KeyShop Bot!*\n\n🛒 Your one-stop shop for instant digital keys\n\nChoose an option below:"
    kb = [
        [InlineKeyboardButton('🛒 Shop Now', callback_data='shop_now')],
        [InlineKeyboardButton('📦 My Orders', callback_data='my_orders')],
        [InlineKeyboardButton('👤 Profile', callback_data='profile')],
        [InlineKeyboardButton('🆘 Support', callback_data='support')],
    ]
    if is_main:
        kb.append([InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')])
    if is_sub:
        kb.append([InlineKeyboardButton('💰 Payment Panel', callback_data='subadmin_panel')])

    # FIX: Smart start - handles text msg, photo caption, and deleted messages
    if update.message:
        # Direct /start command
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    elif update.callback_query:
        query = update.callback_query
        try:
            # Try editing text first
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.info(f"edit_message_text failed in start: {e}")
            try:
                # Try editing caption (for photo messages)
                await query.edit_message_caption(caption=text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            except Exception as e2:
                logger.info(f"edit_message_caption also failed in start: {e2}")
                # Message was deleted or other issue - send new message
                try:
                    await query.delete_message()
                except:
                    pass
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode=ParseMode.MARKDOWN
                )

@require_channel_join
@require_bot_unlocked
async def shop_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = db.get_products()
    if not products:
        await query.edit_message_text('❌ No products available.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='back')]]))
        return
    kb = []
    for p in products:
        kb.append([InlineKeyboardButton(f"{p['name']}", callback_data=f'prod_{p["pid"]}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='back')])
    await query.edit_message_text('🛒 *Select a Product:*\n\n💡 Prices vary by duration. Tap to see options!', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

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
        await query.edit_message_text('❌ This product has no active durations.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='shop_now')]]))
        return
    context.user_data['pid'] = pid
    context.user_data['pname'] = p['name']
    msg = f"\n📦 *{p['name']}*\n━━━━━━━━━━━━━━━━━━\n{p['description'] or 'No description'}\n\n⏱ *Select Duration:*\n"
    kb = []
    for d in durations:
        kb.append([InlineKeyboardButton(f"{d['duration']} - Rs {d['price']:.2f}", callback_data=f"dur_{d['duration']}")])
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
        await query.edit_message_text('⚠️ Session expired. Start again with /start')
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
    msg = f"\n📦 *{pname}*\n━━━━━━━━━━━━━━━━━━\n⏱ Duration: {duration}\n💰 Price: Rs {price:.2f}\n\n✅ Click *'Buy Now'* to proceed.\n"
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
        await query.edit_message_text('⚠️ Session expired. Start again with /start')
        return
    order_id = db.create_order(user.id, user.username, pid, pname, price, duration)
    context.user_data['order_id'] = order_id
    msg = f"💳 *Payment Details*\n\n⏱ Plan: {duration}\n💰 Price: Rs {price:.2f}\n🆔 Order: `{order_id}`\n\n👇 Click below to get QR code:"
    kb = [
        [InlineKeyboardButton("📱 Pay via QR Code", callback_data=f'show_qr_{order_id}')],
        [InlineKeyboardButton('❌ Cancel', callback_data='cancel')]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def show_qr_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = context.user_data.get('order_id')
    price = context.user_data.get('price')
    pname = context.user_data.get('pname')
    duration = context.user_data.get('duration', '1 Day')
    if not order_id:
        try:
            await query.edit_message_text('⚠️ Session expired. Start again with /start')
        except:
            await context.bot.send_message(chat_id=query.message.chat_id, text='⚠️ Session expired. Start again with /start')
        return
    pv = PaymentVerifier()
    qr_buffer = pv.generate_qr_image(order_id, price, pname)
    msg = pv.generate_payment_msg(order_id, price, pname, duration)
    kb = [
        [InlineKeyboardButton("✅ I Have Paid - Enter UTR", callback_data='enter_utr')],
        [InlineKeyboardButton('❌ Cancel Order', callback_data='cancel')]
    ]
    if qr_buffer:
        try:
            qr_buffer.seek(0)
            await query.delete_message()
            sent_msg = await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=InputFile(qr_buffer.getvalue(), filename="payment.png"),
                caption=msg,
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN
            )
            # Schedule auto-clear after 5 minutes (300 seconds)
            try:
                job_queue = context.application.job_queue
                if job_queue:
                    job_queue.run_once(
                        auto_clear_qr_message,
                        when=300,  # 5 minutes
                        data={
                            'chat_id': query.message.chat_id,
                            'message_id': sent_msg.message_id,
                            'order_id': order_id
                        },
                        name=f"auto_clear_{order_id}"
                    )
                    logger.info(f"Auto-clear scheduled for order {order_id} in 5 minutes")
            except Exception as e:
                logger.warning(f"Could not schedule auto-clear: {e}")
        except Exception as e:
            logger.error(f"QR send failed: {e}")
            await query.edit_message_text(msg + "\n\n❌ QR generation failed. Use UPI ID manually.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text(msg + "\n\n⚠️ QR not available. Use UPI ID manually.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def enter_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # CRITICAL FIX: End any existing conversation to prevent stuck state
    # This ensures user can re-enter UTR even if previous attempt failed
    if context.user_data.get('order_id'):
        # Keep order_id but clear any conversation-related flags
        pass  # order_id is needed, don't clear it

    text = """📝 *Enter your UTR/Reference number:*

Find it in your UPI app payment history (PhonePe, GPay, Paytm).

📌 Example: `123456789012`

Or send payment screenshot if UTR not available."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('📸 Upload Screenshot Instead', callback_data='upload_screenshot')],
        [InlineKeyboardButton('❌ Cancel', callback_data='cancel')]
    ])
    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"enter_utr edit failed: {e}")
        try:
            await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception as e2:
            logger.error(f"enter_utr send_message also failed: {e2}")
    return ENTER_UTR

async def process_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle case where user sends a command during UTR entry
    text = update.message.text.strip()
    user = update.effective_user

    # If user sends /start or any command, end conversation and let command handler take over
    if text.startswith('/'):
        context.user_data.clear()
        await update.message.reply_text(
            "⚠️ *Order cancelled.*\n\nYou started a new command. Please use /start to begin again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    utr = text
    order_id = context.user_data.get('order_id')
    if not order_id:
        await update.message.reply_text('⚠️ Session expired. Start with /start')
        return ConversationHandler.END
    order = db.get_order(order_id)
    if not order:
        await update.message.reply_text('❌ Order not found!')
        return ConversationHandler.END
    
    # ===== SECURITY CHECKS =====
    
    # 1. UTR Format Validation (10-22 alphanumeric)
    if not re.match(r'^[A-Za-z0-9]{10,22}$', utr):
        await update.message.reply_text(
            "❌ *Invalid UTR Format!*\n\n"
            "📝 UTR should be:\n"
            "• 10-22 characters\n"
            "• Only letters and numbers\n"
            "• No spaces or special characters\n\n"
            "📌 Found in your UPI app payment history.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('🔄 Try Again', callback_data='enter_utr')],
                [InlineKeyboardButton('📸 Upload Screenshot', callback_data='upload_screenshot')],
                [InlineKeyboardButton('🏠 Main Menu', callback_data='back')]
            ])
        )
        return ENTER_UTR
    
    # 2. Check if UTR is blacklisted
    if db.is_utr_blacklisted(utr):
        await update.message.reply_text(
            "🚫 *FRAUD ALERT!*\n\n"
            "This UTR has been flagged as fraudulent.\n"
            "Your account has been reported to admin.",
            parse_mode=ParseMode.MARKDOWN
        )
        db.add_fraud_alert(order_id, user.id, 'Blacklisted UTR used', f'UTR: {utr}')
        await notify_admins_fraud(context, order_id, user, f'Blacklisted UTR: {utr}')
        return ConversationHandler.END
    
    # 3. Check if UTR already used
    used_info = db.is_utr_used(utr)
    if used_info:
        await update.message.reply_text(
            f"⚠️ *UTR Already Used!*\n\n"
            f"This UTR was already used for order: `{used_info['order_id']}`\n"
            f"🕐 Used at: {used_info['used_at']}\n\n"
            f"Each payment needs a unique UTR.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('📸 Upload Screenshot', callback_data='upload_screenshot')],
                [InlineKeyboardButton('🏠 Main Menu', callback_data='back')]
            ])
        )
        db.add_fraud_alert(order_id, user.id, 'Reused UTR', f'UTR: {utr}, Original: {used_info["order_id"]}')
        await notify_admins_fraud(context, order_id, user, f'Reused UTR: {utr}')
        return ConversationHandler.END
    
    # 4. Rate Limit Check (max 3 orders per 10 minutes)
    rate_info = db.get_user_rate_limit(user.id)
    if rate_info and rate_info['last_order_at']:
        last_time = datetime.fromisoformat(rate_info['last_order_at'])
        if datetime.now() - last_time < timedelta(minutes=10) and rate_info['order_count'] >= 3:
            await update.message.reply_text(
                "⏳ *Rate Limit Exceeded!*\n\n"
                "You can only place 3 orders per 10 minutes.\n"
                "Please wait before trying again.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🏠 Main Menu', callback_data='back')]])
            )
            return ConversationHandler.END
    
    # 5. Mark UTR as used
    db.mark_utr_used(utr, order_id, user.id)
    db.update_user_rate_limit(user.id)
    
    # Save UTR to order
    db.update_payment(order_id, 'pending_verification', utr=utr)
    
    await update.message.reply_text(
        f"✅ *UTR Received & Verified!*\n\n"
        f"🆔 Order: `{order_id}`\n"
        f"📝 UTR: `{utr}`\n"
        f"🔒 Security: Passed\n\n"
        f"⏳ Admin will verify and deliver key soon.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🏠 Main Menu', callback_data='back')]])
    )
    await notify_admins_payment(context, order_id, utr, None, update.effective_user)
    return ConversationHandler.END
async def upload_screenshot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = """📸 *Upload Payment Screenshot*

Please send the payment screenshot/photo from your UPI app.

This will help admin verify your payment faster."""
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('❌ Cancel', callback_data='cancel')]])
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    return UPLOAD_SCREENSHOT

async def process_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get('order_id')
    if not order_id:
        await update.message.reply_text('⚠️ Session expired. Start with /start')
        return ConversationHandler.END
    order = db.get_order(order_id)
    if not order:
        await update.message.reply_text('❌ Order not found!')
        return ConversationHandler.END
    photo = update.message.photo[-1]
    file = await photo.get_file()
    screenshot_path = f"screenshots/{order_id}.jpg"
    os.makedirs("screenshots", exist_ok=True)
    await file.download_to_drive(screenshot_path)
    db.update_payment(order_id, 'pending_verification', screenshot=screenshot_path)

    # Cancel auto-clear job since user has paid
    try:
        job_queue = context.application.job_queue
        if job_queue:
            for job in job_queue.jobs():
                if job.name == f"auto_clear_{order_id}":
                    job.schedule_removal()
                    logger.info(f"Auto-clear cancelled for order {order_id} (user uploaded screenshot)")
                    break
    except Exception as e:
        logger.warning(f"Could not cancel auto-clear job: {e}")

    await update.message.reply_text(
        f"✅ *Screenshot Uploaded!*\n\n🆔 Order: `{order_id}`\n📸 Screenshot saved.\n\n⏳ Wait only maximum 1min payment verified key delivered 😊.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🏠 Main Menu', callback_data='back')]])
    )
    await notify_admins_payment(context, order_id, "Screenshot uploaded", screenshot_path, update.effective_user)
    return ConversationHandler.END

async def notify_admins_payment(context, order_id, utr_or_note, screenshot_path, user):
    order = db.get_order(order_id)
    if not order: return
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
                [InlineKeyboardButton('🗑 Cancel Order', callback_data=f'admin_cancel_{order_id}')]
            ]
            msg = f"🔔 *New Payment!*\n\n🆔 ID: `{order_id}`\n👤 User: {user.username or user.first_name}\n💰 Amount: Rs {order['price_paid']:.2f}\n⏱ Duration: {order['duration']}\n📝 UTR/Note: `{utr_or_note}`\n📦 Product: {order['product_name']}"
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, 'rb') as f:
                    await context.bot.send_photo(
                        admin_id, photo=f, caption=msg,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=InlineKeyboardMarkup(admin_kb)
                    )
            else:
                await context.bot.send_message(
                    admin_id, msg,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(admin_kb)
                )
        except Exception as e:
            logger.error(f'Admin notify failed: {e}')

async def notify_admins_fraud(context, order_id, user, reason):
    all_admins = ADMIN_USER_IDS.copy()
    for admin_id in all_admins:
        try:
            msg = f"🚨 *FRAUD ALERT!*\n\n🆔 Order: `{order_id}`\n👤 User: {user.username or user.first_name} (`{user.id}`)\n⚠️ Reason: {reason}\n\n🛡️ Action: Auto-blocked"
            await context.bot.send_message(
                admin_id, msg,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('🔍 View Fraud Alerts', callback_data='fraud_alerts')],
                    [InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]
                ])
            )
        except Exception as e:
            logger.error(f'Fraud notify failed: {e}')

# ==================== SUB-ADMIN PANEL ====================
async def subadmin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    kb = [
        [InlineKeyboardButton('⏳ Pending Payments', callback_data='subadmin_pending')],
        [InlineKeyboardButton('🔙 Back', callback_data='back')]
    ]
    await query.edit_message_text("💰 *Payment Panel*\n\n👤 Sub-Admin Access\n\nYou can only:\n• Approve payments\n• Reject payments\n• Cancel orders\n\nSelect option:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def subadmin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    db_conn = db.conn(); c = db_conn.cursor()
    c.execute("SELECT order_id, user_id, username, product_name, price_paid, payment_utr, payment_screenshot, duration FROM orders WHERE payment_status = 'pending_verification'")
    rows = c.fetchall(); db_conn.close()
    if not rows:
        await query.edit_message_text('✅ No pending payments!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='subadmin_panel')]]))
        return
    msg = '⏳ *Pending Payments:*\n\n'
    for r in rows:
        has_ss = "📸" if r[6] else ""
        msg += f"🆔 `{r[0]}` {has_ss}\n👤 {r[2] or r[1]}\n📦 {r[3]}\n⏱ {r[7]}\n💰 Rs {r[4]:.2f}\n📝 `{r[5] or 'N/A'}`\n/verify_{r[0]} | /reject_{r[0]} | /cancel_{r[0]}\n\n"
    kb = [[InlineKeyboardButton('🔙 Back', callback_data='subadmin_panel')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ==================== ADMIN PANEL ====================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    lock_status = db.get_bot_lock_status()
    lock_emoji = "🔒 LOCKED" if lock_status['is_locked'] else "🔓 UNLOCKED"
    sub_admins = db.get_sub_admins()
    sub_count = len(sub_admins)
    fraud_count = len(db.get_fraud_alerts('open'))
    fraud_alert = f' 🚨{fraud_count}' if fraud_count > 0 else ''
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
        [InlineKeyboardButton(f'🔐 Bot Lock ({lock_emoji})', callback_data='bot_lock_toggle')],
        [InlineKeyboardButton('🔙 Back', callback_data='back')]
    ]
    cfg = db.get_config()
    status = '✅ Configured' if cfg else '❌ Not Configured'
    await query.edit_message_text(f"⚙️ *Admin Panel*\n\n🔑 API: {status}\n🌐 URL: `{FIXED_API_URL}`\n👥 Sub-Admins: {sub_count}\n🛡️ Fraud Alerts: {fraud_count}\n\nSelect option:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ==================== MANAGE SUB-ADMINS ====================
async def manage_subadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    sub_admins = db.get_sub_admins()
    msg = "👥 *Sub-Admins Management*\n\n"
    if sub_admins:
        msg += "Current Sub-Admins:\n"
        for sa in sub_admins:
            msg += f"• `{sa['user_id']}`\n"
    else:
        msg += "No sub-admins added yet.\n"
    msg += "\n🔰 Sub-admins can ONLY:\n• Approve payments\n• Reject payments\n• Cancel orders\n\n🚫 They CANNOT access admin panel, products, settings, etc."
    kb = [
        [InlineKeyboardButton('➕ Add Sub-Admin', callback_data='add_subadmin')],
        [InlineKeyboardButton('➖ Remove Sub-Admin', callback_data='remove_subadmin_list')],
        [InlineKeyboardButton('🔙 Back', callback_data='admin')]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def add_subadmin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    await query.edit_message_text("➕ *Add Sub-Admin*\n\nEnter the Telegram User ID of the person you want to make sub-admin.\n\nThey will be able to:\n• Approve payments\n• Reject payments\n• Cancel orders\n\nBut they CANNOT:\n• Add/edit products\n• Change prices\n• Access admin settings\n• Lock/unlock bot\n\n📝 Enter User ID:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Cancel', callback_data='manage_subadmins')]]))
    return ADD_SUB_ADMIN

async def save_subadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await update.message.reply_text('Unauthorized!')
        return ConversationHandler.END
    try:
        new_admin_id = int(update.message.text.strip())
        if new_admin_id <= 0: raise ValueError
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
        [InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]
    ]
    await update.message.reply_text(f"✅ *Sub-Admin Added!*\n\n🆔 ID: `{new_admin_id}`\n\nThis user can now approve/reject payments only.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def remove_subadmin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    sub_admins = db.get_sub_admins()
    if not sub_admins:
        await query.edit_message_text("No sub-admins to remove!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='manage_subadmins')]]))
        return
    kb = []
    for sa in sub_admins:
        kb.append([InlineKeyboardButton(f"➖ Remove {sa['user_id']}", callback_data=f"removesub_{sa['user_id']}")])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='manage_subadmins')])
    await query.edit_message_text("➖ *Remove Sub-Admin*\n\nSelect sub-admin to remove:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def remove_subadmin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    target_id = int(query.data.replace('removesub_', ''))
    db.remove_sub_admin(target_id)
    await query.edit_message_text(f"✅ *Sub-Admin Removed!*\n\n🆔 ID: `{target_id}`\n\nThis user no longer has admin access.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('👥 Manage Sub-Admins', callback_data='manage_subadmins')]]), parse_mode=ParseMode.MARKDOWN)

# ==================== MANUAL ADD PRODUCT ====================
async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("➕ *Add Product Manually*\n\n*Step 1/4:* Enter Product ID (PID)\n\n📌 Example: `62` or `netflix_1`\n\nThis should match the PID in API.", parse_mode=ParseMode.MARKDOWN)
    return ADD_PID

async def add_product_pid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = update.message.text.strip()
    if not pid:
        await update.message.reply_text('❌ PID cannot be empty!')
        return ADD_PID
    context.user_data['add_pid'] = pid
    await update.message.reply_text("*Step 2/4:* Enter Product Name\n\n📌 Example: `Netflix Premium` or `Spotify Family`", parse_mode=ParseMode.MARKDOWN)
    return ADD_NAME

async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text('❌ Name cannot be empty!')
        return ADD_NAME
    context.user_data['add_name'] = name
    await update.message.reply_text("*Step 3/4:* Enter Description (or send `.` to skip)\n\n📌 Example: `4K UHD, 4 Screens`", parse_mode=ParseMode.MARKDOWN)
    return ADD_DESC

async def add_product_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc == '.': desc = ''
    context.user_data['add_desc'] = desc
    await update.message.reply_text("*Step 4/4:* Enter Category\n\n📌 Example: `Streaming`, `VPN`, `Gaming`", parse_mode=ParseMode.MARKDOWN)
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
    await update.message.reply_text(f"✅ *Product Added!*\n\n📦 Product: {name}\n🔢 PID: `{pid}`\n\nNow set prices for each duration:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# ==================== SET DURATION PRICE ====================
async def set_duration_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    # Handle BOTH 'setdur_' and 'editdur_' prefixes
    if data.startswith('setdur_'):
        data = data.replace('setdur_', '', 1)
    elif data.startswith('editdur_'):
        data = data.replace('editdur_', '', 1)
    found_duration = None
    pid = None
    for dur in AVAILABLE_DURATIONS:
        if data.endswith('_' + dur):
            found_duration = dur
            pid = data[:-(len(dur) + 1)]
            break
    if not found_duration:
        logger.error(f"set_duration_price_start: Could not parse duration from: {data}")
        await query.edit_message_text('❌ Invalid data format!')
        return
    duration = found_duration
    logger.info(f"set_duration_price_start: pid={pid}, duration={duration}")
    p = db.get_product(pid)
    if not p:
        logger.error(f"Product not found for pid={pid}")
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
    await query.edit_message_text(f"💰 *Set Price for {duration}*\n\n📦 Product: {p['name']}\n🔢 PID: `{pid}`\n📤 API Format: `{api_dur}`\n\n💵 Current Price: Rs {current:.2f}\n\n📝 Enter new price (in Rs):", parse_mode=ParseMode.MARKDOWN)
    return ADD_PRICE

async def save_duration_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text('❌ Invalid price! Enter a number greater than 0.')
        return ADD_PRICE
    pid = context.user_data.get('price_pid')
    duration = context.user_data.get('price_dur')
    if not pid or not duration:
        await update.message.reply_text('⚠️ Session expired!')
        return ConversationHandler.END
    db.set_duration_price(pid, duration, price)
    p = db.get_product(pid)
    api_dur = DURATION_MAP.get(duration, duration)
    kb = []
    for dur in AVAILABLE_DURATIONS:
        kb.append([InlineKeyboardButton(f'💰 Set Price for {dur}', callback_data=f'setdur_{pid}_{dur}')])
    kb.append([InlineKeyboardButton('✅ Done - Back to Admin', callback_data='admin')])
    await update.message.reply_text(f"✅ *Price Updated!*\n\n📦 Product: {p['name']}\n⏱ Duration: {duration} (`{api_dur}`)\n💰 Price: Rs {price:.2f}\n\nSet more durations or click Done:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# ==================== VIEW PRODUCTS ====================
async def view_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = db.get_products()
    if not products:
        await query.edit_message_text('❌ No products found!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]))
        return
    msg = '📋 *All Products:*\n\n'
    for p in products:
        pid = p['pid']
        durs = db.get_duration_prices(pid)
        dur_text = ''
        for d in durs:
            status = '✅' if d['is_active'] and d['admin_price'] else '❌'
            api_dur = DURATION_MAP.get(d['duration'], d['duration'])
            dur_text += f"\n   {status} {d['duration']} ({api_dur}): Rs {d['admin_price'] or 0:.2f}"
        msg += f"📦 {p['name']}\n🔢 `{pid}`{dur_text}\n\n"
    kb = [[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ==================== SET DURATION PRICES LIST ====================
async def set_dur_prices_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = db.get_products()
    if not products:
        await query.edit_message_text('❌ No products!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]))
        return
    kb = []
    for p in products:
        kb.append([InlineKeyboardButton(f"✏️ Edit {p['name']}", callback_data=f'editdur_{p["pid"]}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='admin')])
    await query.edit_message_text('💰 *Select Product to Set Duration Prices:*', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def edit_product_durations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.replace('editdur_', '')
    logger.info(f"edit_product_durations: pid={pid}")
    p = db.get_product(pid)
    if not p:
        logger.error(f"Product not found for pid={pid}")
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
        kb.append([InlineKeyboardButton(f'{status} {dur} ({api_dur}) - Rs {current:.2f}', callback_data=f'setdur_{pid}_{dur}')])
        kb.append([InlineKeyboardButton(f'🔄 Toggle {dur}', callback_data=f'toggle_{pid}_{dur}')])
    kb.append([InlineKeyboardButton('🗑 Delete Product', callback_data=f'delprod_{pid}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='set_dur_prices')])
    await query.edit_message_text(f"✏️ *Edit {p['name']}*\n🔢 `{pid}`\n\n💰 Duration prices:", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def toggle_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.replace('toggle_', '', 1)
    found_duration = None
    pid = None
    for dur in AVAILABLE_DURATIONS:
        if data.endswith('_' + dur):
            found_duration = dur
            pid = data[:-(len(dur) + 1)]
            break
    if not found_duration:
        logger.error(f"toggle_duration_callback: Could not parse from: {data}")
        await query.edit_message_text('❌ Invalid data format!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Back', callback_data='admin')]]))
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
        await query.edit_message_text(f"✅ *Product Deleted!*\n\n📦 {p['name']}\n🔢 `{pid}`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]), parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text('❌ Product not found!')

# ==================== EXISTING HANDLERS ====================
async def set_api_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(f"🔑 *Enter your API Access Token:*\n\n🌐 URL: `{FIXED_API_URL}`", parse_mode=ParseMode.MARKDOWN)
    return SET_API_KEY

async def save_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    db.set_api_key(key)
    await update.message.reply_text(f"✅ *API Configured!*\n\n🔑 Key: `{key[:15]}...`\n🌐 URL: `{FIXED_API_URL}`", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]))
    return ConversationHandler.END

async def fetch_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cfg = db.get_config()
    if not cfg:
        await query.edit_message_text('❌ API not configured!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]))
        return
    await query.edit_message_text('🔄 Fetching products...')
    api = ResellerAPI(cfg['api_key'], cfg['api_url'])
    products = api.fetch_products()
    if products:
        db.save_products(products)
        all_p = db.get_products()
        await query.edit_message_text(f"✅ *Products Fetched!*\n\n📦 Total: {len(all_p)}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]))
    else:
        await query.edit_message_text('❌ Failed to fetch!\n\n💡 Use /debug command for details.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]))

# ==================== BOT LOCK HANDLER ====================
async def bot_lock_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
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
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer('⏳ Processing...', show_alert=False)
    order_id = query.data.replace('admin_verify_', '')
    # FIX: Use reply_text for photo messages (edit_message_text fails on photos)
    processing_msg = await query.message.reply_text(f'⏳ Processing `{order_id}`...', parse_mode=ParseMode.MARKDOWN)
    success, msg = await verify_and_deliver(update, context, order_id)
    final_kb = [[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]
    if not db.is_main_admin(user.id):
        final_kb = [[InlineKeyboardButton('💰 Payment Panel', callback_data='subadmin_panel')]]
    try:
        await processing_msg.delete()
    except:
        pass
    if success:
        await query.message.reply_text(f"✅ *Delivered!*\n\n🆔 ID: `{order_id}`\n\n{msg}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(final_kb))
    else:
        await query.message.reply_text(f"📄 *Result*\n\n🆔 ID: `{order_id}`\n\n{msg}", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(final_kb))
async def admin_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer('❌ Rejected!', show_alert=True)
    order_id = query.data.replace('admin_reject_', '')
    order = db.get_order(order_id)
    if not order:
        await query.message.reply_text('❌ Order not found!')
        return
    db.update_payment(order_id, 'rejected')
    db.update_order(order_id, 'cancelled')
    try:
        await context.bot.send_message(order['user_id'], f"❌ *Payment Rejected*\n\n🆔 Order: `{order_id}`\n📝 Reason: Not verified", parse_mode=ParseMode.MARKDOWN)
    except:
        pass
    kb = [[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]
    if not db.is_main_admin(user.id):
        kb = [[InlineKeyboardButton('💰 Payment Panel', callback_data='subadmin_panel')]]
    await query.message.reply_text(f'❌ Order `{order_id}` rejected.', parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
async def admin_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer('🗑 Cancelled!', show_alert=True)
    order_id = query.data.replace('admin_cancel_', '')
    order = db.get_order(order_id)
    if not order:
        await query.message.reply_text('❌ Order not found!')
        return
    db.cancel_order(order_id)
    try:
        msg = f"🗑 *Order Cancelled by Admin*\n\n🆔 Order: {order_id}\n📦 Product: {order['product_name']}\n💰 Amount: Rs {order['price_paid']}\n\nYour order has been cancelled by admin. Contact support for help."
        await context.bot.send_message(order['user_id'], msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f'Cancel notify failed: {e}')
    msg2 = f"🗑 *Order Cancelled!*\n\n🆔 ID: {order_id}\n📦 Product: {order['product_name']}\n👤 User: {order['username'] or order['user_id']}"
    kb = [[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]
    if not db.is_main_admin(user.id):
        kb = [[InlineKeyboardButton('💰 Payment Panel', callback_data='subadmin_panel')]]
    await query.message.reply_text(msg2, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
# ==================== COMMAND HANDLERS ====================
async def cmd_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await update.message.reply_text('Unauthorized!')
        return
    cmd_text = update.message.text.strip()
    match = re.search(r'/verify_(.+)', cmd_text)
    if not match:
        await update.message.reply_text('Usage: /verify_ORDERID')
        return
    order_id = match.group(1).strip()
    await update.message.reply_text(f'⏳ Processing `{order_id}`...', parse_mode=ParseMode.MARKDOWN)
    success, msg = await verify_and_deliver(update, context, order_id)
    if success:
        await update.message.reply_text(f"✅ *Success!*\n\n🆔 ID: `{order_id}`\n\n{msg}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"📄 *Result:*\n\n🆔 ID: `{order_id}`\n\n{msg}", parse_mode=ParseMode.MARKDOWN)

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_any_admin(user.id):
        await update.message.reply_text('Unauthorized!')
        return
    cmd_text = update.message.text.strip()
    match = re.search(r'/reject_(.+)', cmd_text)
    if not match:
        await update.message.reply_text('Usage: /reject_ORDERID')
        return
    order_id = match.group(1).strip()
    order = db.get_order(order_id)
    if not order:
        await update.message.reply_text('❌ Order not found!')
        return
    db.update_payment(order_id, 'rejected')
    db.update_order(order_id, 'cancelled')
    try:
        await context.bot.send_message(order['user_id'], f"❌ *Payment Rejected*\n\n🆔 Order: `{order_id}`", parse_mode=ParseMode.MARKDOWN)
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
        await update.message.reply_text('❌ Order not found!')
        return
    db.cancel_order(order_id)
    try:
        msg = f"🗑 *Order Cancelled by Admin*\n\n🆔 Order: {order_id}\n📦 Product: {order['product_name']}\n💰 Amount: Rs {order['price_paid']}\n\nYour order has been cancelled by admin. Contact support for help."
        await context.bot.send_message(order['user_id'], msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f'Cancel notify failed: {e}')
    await update.message.reply_text(f'🗑 Order {order_id} cancelled by admin.', parse_mode=ParseMode.MARKDOWN)

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await update.message.reply_text('Unauthorized!')
        return
    if not LOG_BUFFER:
        await update.message.reply_text("No logs captured yet. Bot running clean!")
        return
    api_logs = [line for line in LOG_BUFFER if 'RAW RESPONSE' in line or 'PARSED JSON' in line or 'API REQUEST' in line or 'API RESPONSE' in line]
    if api_logs:
        msg = "📋 *Last API Responses:*\n\n"
        for line in api_logs[-20:]:
            msg += line + "\n"
    else:
        msg = "📋 *Recent Logs:*\n\n"
        for line in list(LOG_BUFFER)[-30:]:
            msg += line + "\n"
    await update.message.reply_text(msg[:4000])

async def pending_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    db_conn = db.conn(); c = db_conn.cursor()
    c.execute("SELECT order_id, user_id, username, product_name, price_paid, payment_utr, payment_screenshot, duration FROM orders WHERE payment_status = 'pending_verification'")
    rows = c.fetchall(); db_conn.close()
    if not rows:
        await query.edit_message_text('✅ No pending payments!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]))
        return
    msg = '⏳ *Pending Payments:*\n\n'
    for r in rows:
        has_ss = "📸" if r[6] else ""
        msg += f"🆔 `{r[0]}` {has_ss}\n👤 {r[2] or r[1]}\n📦 {r[3]}\n⏱ {r[7]}\n💰 Rs {r[4]:.2f}\n📝 `{r[5] or 'N/A'}`\n/verify_{r[0]} | /reject_{r[0]}\n\n"
    kb = [[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]
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
        msg += f"{emoji} {o['product_name']}\n   🆔 `{o['order_id']}`\n   ⏱ {o['duration']}\n   💰 Rs {o['price_paid']:.2f}\n   📊 {o['order_status']}\n\n"
    kb = [[InlineKeyboardButton('🔙 Back', callback_data='back')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    msg = f"\n👤 *Your Profile*\n━━━━━━━━━━━━━━━━━━\n🆔 ID: `{user.id}`\n👤 Name: {user.first_name} {user.last_name or ''}\n📱 Username: @{user.username or 'N/A'}\n"
    kb = [[InlineKeyboardButton('🔙 Back', callback_data='back')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

@require_channel_join
@require_bot_unlocked
async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg = "\n🆘 *Support Center*\n━━━━━━━━━━━━━━━━━━\n📧 Email:\n📱 Telegram: @Personreplybot\n\n🔧 Common Issues:\n• Payment pending? Wait 5-10 min\n• Key issue? Share Order ID\n"
    kb = [[InlineKeyboardButton('🔙 Back', callback_data='back')]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Clear any stale order data when going back to main menu
    context.user_data.clear()
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
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    products = db.get_products()
    if not products:
        await query.edit_message_text('❌ No products found!', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]]))
        return
    kb = []
    for p in products:
        kb.append([InlineKeyboardButton(f"✏️ Edit {p['name']}", callback_data=f'editprod_{p["pid"]}')])
    kb.append([InlineKeyboardButton('🔙 Back', callback_data='admin')])
    await query.edit_message_text('✏️ *Select Product to Edit:*', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def edit_product_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    pid = query.data.replace('editprod_', '')
    p = db.get_product(pid)
    if not p:
        await query.edit_message_text('❌ Product not found!')
        return
    context.user_data['edit_pid'] = pid
    msg = f"✏️ *Edit Product*\n\n📦 {p['name']}\n🔢 `{pid}`\n📝 {p['description'] or 'No description'}\n🏷 {p['category'] or 'N/A'}\n\nWhat do you want to edit?"
    kb = [
        [InlineKeyboardButton('✏️ Edit Name', callback_data='edit_field_name')],
        [InlineKeyboardButton('✏️ Edit Description', callback_data='edit_field_desc')],
        [InlineKeyboardButton('✏️ Edit Category', callback_data='edit_field_cat')],
        [InlineKeyboardButton('💰 Edit Duration Prices', callback_data=f'editdur_{pid}')],
        [InlineKeyboardButton('🗑 Delete Product', callback_data=f'delprod_{pid}')],
        [InlineKeyboardButton('🔙 Back', callback_data='edit_product_list')]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def edit_field_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    field = query.data.replace('edit_field_', '')
    pid = context.user_data.get('edit_pid')
    if not pid:
        await query.edit_message_text('⚠️ Session expired!')
        return
    p = db.get_product(pid)
    if not p:
        await query.edit_message_text('❌ Product not found!')
        return
    context.user_data['edit_field'] = field
    field_names = {'name': 'Product Name', 'desc': 'Description', 'cat': 'Category'}
    current_values = {'name': p['name'], 'desc': p['description'] or 'No description', 'cat': p['category'] or 'N/A'}
    await query.edit_message_text(f"✏️ *Edit {field_names.get(field, field)}*\n\n📦 {p['name']}\n\n💾 Current: `{current_values[field]}`\n\n📝 Enter new {field_names.get(field, field)}:", parse_mode=ParseMode.MARKDOWN)
    if field == 'name': return EDIT_NAME
    elif field == 'desc': return EDIT_DESC
    elif field == 'cat': return EDIT_CAT
    return ConversationHandler.END

async def save_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    pid = context.user_data.get('edit_pid')
    if not pid:
        await update.message.reply_text('⚠️ Session expired!')
        return ConversationHandler.END
    if not new_name:
        await update.message.reply_text('❌ Name cannot be empty!')
        return EDIT_NAME
    db.update_product(pid, name=new_name)
    p = db.get_product(pid)
    kb = [
        [InlineKeyboardButton('✏️ Edit Again', callback_data=f'editprod_{pid}')],
        [InlineKeyboardButton('📋 View All Products', callback_data='view_products')],
        [InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]
    ]
    await update.message.reply_text(f"✅ *Name Updated!*\n\n📦 `{p['name']}`\n🔢 `{pid}`", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def save_edit_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_desc = update.message.text.strip()
    pid = context.user_data.get('edit_pid')
    if not pid:
        await update.message.reply_text('⚠️ Session expired!')
        return ConversationHandler.END
    if new_desc == '.': new_desc = ''
    db.update_product(pid, description=new_desc)
    p = db.get_product(pid)
    kb = [
        [InlineKeyboardButton('✏️ Edit Again', callback_data=f'editprod_{pid}')],
        [InlineKeyboardButton('📋 View All Products', callback_data='view_products')],
        [InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]
    ]
    await update.message.reply_text(f"✅ *Description Updated!*\n\n📦 {p['name']}\n📝 `{p['description'] or 'No description'}`", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def save_edit_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_cat = update.message.text.strip()
    pid = context.user_data.get('edit_pid')
    if not pid:
        await update.message.reply_text('⚠️ Session expired!')
        return ConversationHandler.END
    if not new_cat:
        await update.message.reply_text('❌ Category cannot be empty!')
        return EDIT_CAT
    db.update_product(pid, category=new_cat)
    p = db.get_product(pid)
    kb = [
        [InlineKeyboardButton('✏️ Edit Again', callback_data=f'editprod_{pid}')],
        [InlineKeyboardButton('📋 View All Products', callback_data='view_products')],
        [InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]
    ]
    await update.message.reply_text(f"✅ *Category Updated!*\n\n📦 {p['name']}\n🏷 `{p['category']}`", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
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
    status = '✅ Enabled' if settings['enabled'] else '❌ Disabled'
    msg = f"🧹 *Auto Clear Settings*\n\n📊 Current Status: {status}\n⏱ Clear After: {settings['hours']} hours\n\nCompleted/Cancelled/Rejected orders older than {settings['hours']} hours will be automatically deleted.\n\nSelect option:"
    kb = [
        [InlineKeyboardButton('⏱ Set Clear Hours', callback_data='set_clear_hours')],
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
    await query.answer(f'Auto Clear {status}', show_alert=True)
    await auto_clear_settings_menu(update, context)

async def set_clear_hours_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    settings = db.get_auto_clear_settings()
    msg = f"⏱ *Set Auto Clear Hours*\n\n💾 Current: {settings['hours']} hours\n\n📝 Enter new value (in hours):\n\nExamples:\n• 24 = 1 day\n• 168 = 1 week\n• 720 = 30 days\n• 0 = Disable auto clear"
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Cancel', callback_data='auto_clear_settings')]]))
    return SET_CLEAR_HOURS

async def save_clear_hours(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = int(update.message.text.strip())
        if hours < 0: raise ValueError
    except ValueError:
        await update.message.reply_text('❌ Invalid! Enter a number (0 or greater).')
        return SET_CLEAR_HOURS
    enabled = hours > 0
    db.set_auto_clear_settings(hours, enabled)
    status = '✅ Enabled' if enabled else '❌ Disabled'
    kb = [[InlineKeyboardButton('🧹 Auto Clear Settings', callback_data='auto_clear_settings')]]
    msg = f"✅ *Settings Updated!*\n\n⏱ Clear After: {hours} hours\n📊 Status: {status}"
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
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
    msg = f"✅ *Manual Clear Complete!*\n\n🗑 Deleted {deleted} old orders from history."
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ==================== FRAUD ALERTS PANEL ====================
async def fraud_alerts_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer()
    alerts = db.get_fraud_alerts('open')
    if not alerts:
        await query.edit_message_text(
            "🛡️ *Fraud Alerts*\n\n✅ No open fraud alerts!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('🔄 Refresh', callback_data='fraud_alerts')],
                [InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')]
            ])
        )
        return
    msg = f"🛡️ *Fraud Alerts ({len(alerts)} open)*\n\n"
    kb = []
    for alert in alerts[:10]:
        msg += f"🚨 ID: `{alert['id']}`\n👤 User: `{alert['user_id']}`\n⚠️ {alert['reason']}\n📝 {alert['details'][:50]}...\n🕐 {alert['created_at']}\n\n"
        kb.append([InlineKeyboardButton(f"✅ Resolve #{alert['id']}", callback_data=f"resolve_fraud_{alert['id']}")])
    kb.append([InlineKeyboardButton('🔄 Refresh', callback_data='fraud_alerts')])
    kb.append([InlineKeyboardButton('⚙️ Admin Panel', callback_data='admin')])
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

async def resolve_fraud_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not db.is_main_admin(user.id):
        await query.answer('Unauthorized!', show_alert=True)
        return
    await query.answer('✅ Resolved!', show_alert=True)
    alert_id = int(query.data.replace('resolve_fraud_', ''))
    db.resolve_fraud_alert(alert_id)
    await fraud_alerts_panel(update, context)

# ==================== CHECK JOIN AGAIN HANDLER ====================
async def check_join_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    is_member, missing = await check_channel_membership(user.id, context.bot)
    if is_member:
        await query.edit_message_text("✅ *Channel Join Verified!*\n\nYou can now use the bot!", parse_mode=ParseMode.MARKDOWN)
        await start(update, context)
    else:
        await send_join_required_message(update, context, missing)

# ==================== AUTO CLEAR BACKGROUND TASK ====================
async def auto_clear_callback(context: ContextTypes.DEFAULT_TYPE):
    try:
        deleted = db.clear_old_orders()
        if deleted > 0:
            logger.info(f"Auto-clear: Deleted {deleted} old orders")
            for admin_id in ADMIN_USER_IDS:
                try:
                    await context.bot.send_message(admin_id, f"🧹 Auto Clear Report: {deleted} old orders deleted from history.")
                except:
                    pass
    except Exception as e:
        logger.error(f"Auto-clear error: {e}")

# ==================== WEB SERVER (HEALTH CHECK + WEBHOOK) ====================
async def health_check(request):
    """GET / - UptimeRobot health check endpoint"""
    return web.Response(text="KeyShop Bot v5.5 is alive!", status=200)

async def webhook_handler(request):
    """POST /webhook/<token> - Telegram webhook handler"""
    try:
        data = await request.json()
        update = Update.de_json(data, request.app['bot_app'].bot)
        await request.app['bot_app'].process_update(update)
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500)

# ==================== MAIN ====================
async def main_async():
    init_db()

    # Build application
    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    # Add all handlers
    api_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_api_start, pattern='^set_api$')],
        states={SET_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_api_key)]},
        fallbacks=[CommandHandler('cancel', start)],
        per_message=False, per_chat=True
    )
    add_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_product_start, pattern='^add_product$')],
        states={
            ADD_PID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_pid)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_desc)],
            ADD_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_cat)]
        },
        fallbacks=[CommandHandler('cancel', start)],
        per_message=False, per_chat=True
    )
    dur_price_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(set_duration_price_start, pattern='^setdur_')
            # REMOVED: editdur_ from here - it's handled by direct CallbackQueryHandler
            # "Edit Duration Prices" button calls edit_product_durations (shows duration list)
            # Individual "Set Price" buttons in that list call setdur_ (starts conversation)
        ],
        states={ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_duration_price)]},
        fallbacks=[CommandHandler('cancel', start)],
        per_message=False, per_chat=True
    )
    utr_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(enter_utr, pattern='^enter_utr$')],
        states={
            ENTER_UTR: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_utr)],
            UPLOAD_SCREENSHOT: [MessageHandler(filters.PHOTO, process_screenshot)]
        },
        fallbacks=[
            CallbackQueryHandler(enter_utr, pattern='^enter_utr$'),
            CallbackQueryHandler(back_main, pattern='^back$'),
            CallbackQueryHandler(upload_screenshot_start, pattern='^upload_screenshot$'),
            CallbackQueryHandler(cancel_order, pattern='^cancel$'),
            CommandHandler('cancel', start),
            CommandHandler('start', start)
        ],
        per_message=False, per_chat=True
    )
    edit_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_field_start, pattern='^edit_field_')],
        states={
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit_name)],
            EDIT_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit_desc)],
            EDIT_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit_cat)]
        },
        fallbacks=[CommandHandler('cancel', start)],
        per_message=False, per_chat=True
    )
    auto_clear_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_clear_hours_start, pattern='^set_clear_hours$')],
        states={SET_CLEAR_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_clear_hours)]},
        fallbacks=[CommandHandler('cancel', start)],
        per_message=False, per_chat=True
    )
    subadmin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_subadmin_start, pattern='^add_subadmin$')],
        states={ADD_SUB_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_subadmin)]},
        fallbacks=[CommandHandler('cancel', start)],
        per_message=False, per_chat=True
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(api_conv)
    application.add_handler(add_product_conv)
    application.add_handler(dur_price_conv)
    application.add_handler(utr_conv)
    application.add_handler(edit_product_conv)
    application.add_handler(auto_clear_conv)
    application.add_handler(subadmin_conv)

    application.add_handler(CallbackQueryHandler(bot_lock_toggle, pattern='^bot_lock_toggle$'))
    application.add_handler(CallbackQueryHandler(admin_verify_callback, pattern='^admin_verify_'))
    application.add_handler(CallbackQueryHandler(admin_reject_callback, pattern='^admin_reject_'))
    application.add_handler(CallbackQueryHandler(admin_cancel_callback, pattern='^admin_cancel_'))
    application.add_handler(CallbackQueryHandler(product_details, pattern='^prod_'))
    application.add_handler(CallbackQueryHandler(select_duration, pattern='^dur_'))
    application.add_handler(CallbackQueryHandler(buy_product, pattern='^buy_'))
    application.add_handler(CallbackQueryHandler(show_qr_code, pattern='^show_qr_'))
    application.add_handler(CallbackQueryHandler(toggle_duration_callback, pattern='^toggle_'))
    application.add_handler(CallbackQueryHandler(delete_product_callback, pattern='^delprod_'))
    application.add_handler(CallbackQueryHandler(edit_product_durations, pattern='^editdur_'))
    application.add_handler(CallbackQueryHandler(edit_product_list, pattern='^edit_product_list$'))
    application.add_handler(CallbackQueryHandler(edit_product_menu, pattern='^editprod_'))
    application.add_handler(CallbackQueryHandler(auto_clear_settings_menu, pattern='^auto_clear_settings$'))
    application.add_handler(CallbackQueryHandler(toggle_auto_clear, pattern='^toggle_auto_clear$'))
    application.add_handler(CallbackQueryHandler(manual_clear_now, pattern='^manual_clear$'))
    application.add_handler(CallbackQueryHandler(subadmin_panel, pattern='^subadmin_panel$'))
    application.add_handler(CallbackQueryHandler(subadmin_pending, pattern='^subadmin_pending$'))
    application.add_handler(CallbackQueryHandler(manage_subadmins, pattern='^manage_subadmins$'))
    application.add_handler(CallbackQueryHandler(remove_subadmin_list, pattern='^remove_subadmin_list$'))
    application.add_handler(CallbackQueryHandler(remove_subadmin_callback, pattern='^removesub_'))
    application.add_handler(CallbackQueryHandler(check_join_again, pattern='^check_join$'))
    application.add_handler(CallbackQueryHandler(shop_now, pattern='^shop_now$'))
    application.add_handler(CallbackQueryHandler(my_orders, pattern='^my_orders$'))
    application.add_handler(CallbackQueryHandler(profile, pattern='^profile$'))
    application.add_handler(CallbackQueryHandler(support, pattern='^support$'))
    application.add_handler(CallbackQueryHandler(back_main, pattern='^back$'))
    application.add_handler(CallbackQueryHandler(cancel_order, pattern='^cancel$'))
    application.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin$'))
    application.add_handler(CallbackQueryHandler(fetch_products, pattern='^fetch$'))
    application.add_handler(CallbackQueryHandler(view_products, pattern='^view_products$'))
    application.add_handler(CallbackQueryHandler(set_dur_prices_list, pattern='^set_dur_prices$'))
    application.add_handler(CallbackQueryHandler(pending_payments, pattern='^pending$'))
    application.add_handler(CallbackQueryHandler(fraud_alerts_panel, pattern='^fraud_alerts$'))
    application.add_handler(CallbackQueryHandler(resolve_fraud_callback, pattern='^resolve_fraud_'))
    application.add_handler(CommandHandler('verify', cmd_verify))
    application.add_handler(CommandHandler('reject', cmd_reject))
    application.add_handler(CommandHandler('cancel', cmd_cancel))
    application.add_handler(CommandHandler('debug', cmd_debug))

    print('KeyShop Bot v5.5 - RENDER WEBHOOK + FULL FEATURES')
    print('API: ' + str(FIXED_API_URL))

    # Initialize bot application FIRST (CRITICAL FIX for weak reference error)
    await application.initialize()
    await application.start()

    # NOW setup job queue - AFTER application is started
    try:
        from telegram.ext import JobQueue
        job_queue = application.job_queue
        if job_queue:
            job_queue.run_repeating(auto_clear_callback, interval=3600, first=60)
            print("Auto-clear scheduled: Every 60 minutes")
        else:
            print("WARNING: Job queue not available, auto-clear disabled")
            print("TIP: Install with: pip install 'python-telegram-bot[job-queue]'")
    except ImportError:
        print("WARNING: JobQueue not available, auto-clear disabled")
        print("TIP: Install with: pip install 'python-telegram-bot[job-queue]'")

    # Set webhook
    render_port = int(os.environ.get("PORT", "10000"))
    webhook_url = os.environ.get("WEBHOOK_URL")

    if not webhook_url:
        print("WEBHOOK_URL not set!")
        sys.exit(1)

    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{webhook_url}{webhook_path}"

    await application.bot.set_webhook(full_webhook_url, drop_pending_updates=True)
    print(f'Webhook set: {full_webhook_url}')
    print(f'Health check: {webhook_url}/')

    # Create aiohttp web app with BOTH health check AND webhook
    web_app = web.Application()
    web_app['bot_app'] = application

    # Health check for UptimeRobot (GET /)
    web_app.router.add_get('/', health_check)
    # Webhook for Telegram (POST)
    web_app.router.add_post(webhook_path, webhook_handler)

    # Run server
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', render_port)

    print(f'Server starting on port {render_port}')
    await site.start()

    # Keep running forever
    while True:
        await asyncio.sleep(3600)

def main():
    asyncio.run(main_async())

if __name__ == '__main__':
    main()
