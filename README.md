# KeyShop Bot v4.4 - WEB ENABLED + UPTIMEROBOT READY

## 🔒 100% Secure - NO Sensitive Data in Code

## 🌐 NEW: Web Server Added!
Ab UptimeRobot se ping kar ke bot 24/7 chala sakte ho!

## 📁 Files Included
- `advanced_fixed_final_v4.4.py` - Main bot with web server
- `requirements.txt` - Python dependencies (Flask added)
- `Procfile` - Render web process config
- `.gitignore` - Files to ignore

## 🚀 Deploy on Render (FREE 24/7!)

### Step 1: Create GitHub Repo
1. Go to [github.com](https://github.com)
2. Click **+** → **New repository**
3. Name: `keyshop-bot`
4. Select **Public**
5. Upload all 4 files
6. Commit

### Step 2: Deploy on Render
1. Go to [render.com](https://render.com)
2. Sign up with GitHub
3. **New** → **Web Service**
4. Connect your GitHub repo
5. Settings:
   - **Name**: keyshop-bot
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python advanced_fixed_final_v4.4.py`
6. Click **Create Web Service**

### Step 3: Add Environment Variables
```
Dashboard → Your Service → Environment → Add Environment Variable
```

| Variable | Your Value |
|----------|-----------|
| `BOT_TOKEN` | BotFather se naya token |
| `UPI_ID` | `8084866858@fam` |
| `UPI_NAME` | `KeyShop Payments` |
| `ADMIN_USER_IDS` | `8468583207` |
| `API_URL` | `https://adminpanels.shop/api/reseller_v1.php` |
| `FORCE_CHANNELS` | `@paid_pannel88` |

### Step 4: Get Your Render URL
```
Dashboard → Your Service → URL (e.g., https://keyshop-bot.onrender.com)
```

### Step 5: Setup UptimeRobot
1. Go to [uptimerobot.com](https://uptimerobot.com)
2. Sign up free
3. **Add New Monitor**
   - Type: HTTP(s)
   - Name: KeyShop Bot
   - URL: `https://keyshop-bot.onrender.com/health`
   - Interval: 5 minutes
4. Click **Create Monitor**

✅ Done! Bot ab 24/7 chalega! UptimeRobot har 5 min mein ping karega!

## 🌐 Web Endpoints

| URL | What it does |
|-----|-------------|
| `https://your-app.onrender.com/` | Bot status page |
| `https://your-app.onrender.com/health` | Health check (UptimeRobot pings this) |
| `https://your-app.onrender.com/webhook` | Telegram webhook |

## ✅ Bot Features
- Force channel join
- Sub-admin support
- Bot lock/unlock
- Edit product
- Auto clear old orders
- Payment verification
- 100% secure codebase
- 24/7 uptime with UptimeRobot!
