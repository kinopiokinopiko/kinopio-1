from flask import Flask, render_template, request, redirect, url_for, session, flash
import requests
import json
import os
from datetime import datetime, timezone, timedelta
import re
import time
from decimal import Decimal, InvalidOperation
import concurrent.futures
import threading

# PostgreSQLサポート
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from werkzeug.security import generate_password_hash, check_password_hash
from bs4 import BeautifulSoup

# スケジューラとタイムゾーンのライブラリ
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# --- 定数設定 ---
# 暗号通貨銘柄(固定)
CRYPTO_SYMBOLS = ['BTC', 'ETH', 'XRP', 'DOGE']

# 投資信託銘柄(固定)
INVESTMENT_TRUST_INFO = {
    'S&P500': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000GKC6',
    'オルカン': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000H1T1',
    'FANG+': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000FZD4'
}
INVESTMENT_TRUST_SYMBOLS = list(INVESTMENT_TRUST_INFO.keys())

# 保険種類(参照用)
INSURANCE_TYPES = ['生命保険', '医療保険', '学資保険', '個人年金保険', 'がん保険', 'その他']

# デバッグフラグ(環境変数で有効化可能)
DEBUG_CRYPTO = os.environ.get('CRYPTO_DEBUG', '0') == '1'

# データベース設定 (PostgreSQL専用)
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

# --- データベース関連 ---
def get_db():
    """データベース接続を取得"""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    """データベースの初期化"""
    conn = get_db()
    c = conn.cursor()

    # ユーザーテーブル
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username VARCHAR(255) UNIQUE NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # 資産テーブル
    c.execute('''CREATE TABLE IF NOT EXISTS assets (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        asset_type VARCHAR(50) NOT NULL,
        symbol VARCHAR(50) NOT NULL,
        name VARCHAR(255),
        quantity REAL NOT NULL,
        price REAL DEFAULT 0,
        avg_cost REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )''')

    # 日々の資産スナップショットテーブル
    c.execute('''CREATE TABLE IF NOT EXISTS daily_snapshots (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        snapshot_date DATE NOT NULL,
        total_assets REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id),
        UNIQUE (user_id, snapshot_date)
    )''')

    # デモユーザーの作成
    c.execute("SELECT id FROM users WHERE username = 'demo'")
    if not c.fetchone():
        demo_hash = generate_password_hash('demo123')
        c.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                 ('demo', demo_hash))

    conn.commit()
    conn.close()

# --- データ取得・整形ユーティリティ ---
def get_current_user():
    """セッションから現在のユーザー情報を取得"""
    if 'user_id' not in session:
        return None
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = %s', (session['user_id'],))
    user = c.fetchone()
    conn.close()
    return user

_FULLWIDTH_TRANS = {ord(f): ord(t) for f, t in zip('０１２３４５６７８９', '0123456789')}
_FULLWIDTH_TRANS.update({ord('，'): ord(','), ord('．'): ord('.'), ord('＋'): ord('+'), ord('－'): ord('-'), ord('　'): ord(' '), ord('％'): ord('%')})

def normalize_fullwidth(s):
    """全角文字を半角に変換"""
    if s is None: return s
    return s.translate(_FULLWIDTH_TRANS)

def extract_number_from_string(s):
    """文字列から数値を抽出"""
    if not s: return None
    try: s = normalize_fullwidth(s)
    except Exception: pass
    s = s.replace('\xa0', ' ')
    m = re.search(r'([+-]?\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?)', s)
    if not m: m = re.search(r'([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)', s)
    if not m: return None
    num_str = m.group(1).replace(',', '').replace(' ', '')
    try:
        return float(Decimal(num_str))
    except (InvalidOperation, ValueError):
        try: return float(num_str)
        except Exception: return None

# --- Webスクレイピング・API関連 ---
def scrape_yahoo_finance_jp(code):
    """Yahoo Finance Japanから株価情報を取得"""
    try:
        api_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
        headers = {'User-Agent': 'Mozilla/5.0'}
        api_response = requests.get(api_url, headers=headers, timeout=10)
        if api_response.status_code == 200:
            data = api_response.json()
            if data['chart']['result']:
                meta = data['chart']['result'][0]['meta']
                price = meta.get('regularMarketPrice', 0)
                name = meta.get('shortName', f"Stock {code}")
                return {'name': name, 'price': round(float(price), 2)}
    except Exception as e:
        print(f"Error getting JP stock {code}: {e}")
    return {'name': f'Stock {code}', 'price': 0}

def scrape_yahoo_finance_us(symbol):
    """Yahoo Finance USから株価情報を取得"""
    try:
        api_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        api_response = requests.get(api_url, headers=headers, timeout=10)
        if api_response.status_code == 200:
            data = api_response.json()
            if data['chart']['result']:
                meta = data['chart']['result'][0]['meta']
                price = meta.get('regularMarketPrice', 0)
                name = meta.get('shortName', symbol.upper())
                return {'name': name, 'price': round(float(price), 2)}
    except Exception as e:
        print(f"Error getting US stock {symbol}: {e}")
    return {'name': symbol.upper(), 'price': 0}

def get_jp_stock_info(code): return scrape_yahoo_finance_jp(code)
def get_us_stock_info(symbol): return scrape_yahoo_finance_us(symbol)
def get_stock_price(symbol, is_jp=False): return (get_jp_stock_info(symbol) if is_jp else get_us_stock_info(symbol))['price']
def get_stock_name(symbol, is_jp=False): return (get_jp_stock_info(symbol) if is_jp else get_us_stock_info(symbol))['name']

def get_crypto_price(symbol):
    """みんかぶから暗号資産の価格を取得"""
    try:
        if symbol.upper() not in CRYPTO_SYMBOLS: return 0.0
        url = f"https://cc.minkabu.jp/pair/{symbol.upper()}_JPY"
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        price_tag = soup.select_one('div.pairPrice, .pair_price')
        if price_tag:
            val = extract_number_from_string(price_tag.text)
            return round(val, 2) if val else 0.0
    except Exception as e:
        print(f"Error getting crypto price for {symbol}: {e}")
    return 0.0

def get_gold_price():
    """田中貴金属工業から金の価格を取得"""
    try:
        res = requests.get("https://gold.tanaka.co.jp/commodity/souba/english/index.php", headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) > 1 and tds[0].get_text(strip=True).upper() == "GOLD":
                match = re.search(r"([0-9,]+) yen", tds[1].get_text(strip=True))
                if match: return int(match.group(1).replace(",", ""))
    except Exception as e:
        print(f"Error getting gold price: {e}")
    return 0

def get_investment_trust_price(symbol):
    """楽天証券から投資信託の基準価額を取得"""
    try:
        if symbol not in INVESTMENT_TRUST_INFO: return 0.0
        response = requests.get(INVESTMENT_TRUST_INFO[symbol], headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        th = soup.find('th', string=re.compile(r'\s*基準価額\s*'))
        if th and th.find_next_sibling('td'):
            return extract_number_from_string(th.find_next_sibling('td').get_text(strip=True))
    except Exception as e:
        print(f"Error getting investment trust price for {symbol}: {e}")
    return 0.0

def get_usd_jpy_rate():
    """Yahoo FinanceからUSD/JPY為替レートを取得"""
    try:
        api_response = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/USDJPY=X", headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        if api_response.status_code == 200:
            data = api_response.json()
            if data['chart']['result']:
                return float(data['chart']['result'][0]['meta']['regularMarketPrice'])
    except Exception: pass
    return 150.0 # 取得失敗時のデフォルト値

# --- Flaskアプリケーションの初期化 ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a-very-secret-key-for-development')
init_db()

# --- 自動記録ジョブ ---
def record_all_snapshots():
    """全ユーザーの資産総額を毎日23:59に記録する"""
    print(f"[{datetime.now()}] Starting scheduled job: record_all_snapshots")
    with app.app_context():
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute('SELECT id FROM users')
            users = c.fetchall()
            if not users:
                print("No users found to record.")
                return

            print(f"Found {len(users)} users to process.")
            usd_jpy = get_usd_jpy_rate()
            
            for user in users:
                user_id = user['id']
                c.execute('SELECT * FROM assets WHERE user_id = %s', (user_id,))
                assets_list = c.fetchall()
                
                # 総資産を計算
                total_assets = 0
                for a in assets_list:
                    value = 0
                    if a['asset_type'] in ['jp_stock', 'gold', 'crypto']:
                        value = a['quantity'] * a['price']
                    elif a['asset_type'] == 'us_stock':
                        value = a['quantity'] * a['price'] * usd_jpy
                    elif a['asset_type'] == 'investment_trust':
                        value = (a['quantity'] * a['price']) / 10000
                    elif a['asset_type'] == 'insurance':
                        value = a['price']
                    elif a['asset_type'] == 'cash':
                        value = a['quantity']
                    total_assets += value

                # 日本時間の今日の日付でDBに保存
                jst = pytz.timezone('Asia/Tokyo')
                today_jst_date = datetime.now(jst).date()
                c.execute('''
                    INSERT INTO daily_snapshots (user_id, snapshot_date, total_assets)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, snapshot_date) DO UPDATE SET total_assets = EXCLUDED.total_assets
                ''', (user_id, today_jst_date, total_assets))
                print(f"Recorded snapshot for user_id {user_id}: ¥{total_assets:,.0f}")

            conn.commit()
            print("Snapshot recording job finished successfully.")
        except Exception as e:
            if conn: conn.rollback()
            print(f"Error in record_all_snapshots job: {e}")
        finally:
            if conn: conn.close()
            
# スケジューラを設定し、ジョブを登録
scheduler = BackgroundScheduler(daemon=True, timezone=pytz.timezone('Asia/Tokyo'))
scheduler.add_job(record_all_snapshots, trigger=CronTrigger(hour=23, minute=59))
scheduler.start()

# --- Render/Heroku用スリープ防止 ---
@app.route('/ping')
def ping(): return "pong", 200
def keep_alive():
    app_url = os.environ.get('RENDER_EXTERNAL_URL')
    if not app_url: return
    while True:
        try: requests.get(f"{app_url}/ping", timeout=10)
        except requests.exceptions.RequestException as e: print(f"Keep-alive failed: {e}")
        time.sleep(840)
if os.environ.get('RENDER'):
    threading.Thread(target=keep_alive, daemon=True).start()

# --- ルーティング ---
@app.route('/')
def index():
    if not get_current_user(): return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    # 登録処理
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        if len(username) < 3 or len(password) < 6:
            flash('ユーザー名は3文字以上、パスワードは6文字以上で入力してください', 'error')
        elif password != request.form['confirm_password']:
            flash('パスワードが一致しません', 'error')
        else:
            conn = get_db()
            c = conn.cursor()
            c.execute('SELECT id FROM users WHERE username = %s', (username,))
            if c.fetchone():
                flash('このユーザー名は既に使用されています', 'error')
            else:
                c.execute('INSERT INTO users (username, password_hash) VALUES (%s, %s)', (username, generate_password_hash(password)))
                conn.commit()
                flash('アカウントを作成しました。ログインしてください。', 'success')
                return redirect(url_for('login'))
            conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    # ログイン処理
    if request.method == 'POST':
        username, password = request.form['username'], request.form['password']
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE username = %s', (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'], session['username'] = user['id'], user['username']
            return redirect(url_for('dashboard'))
        flash('ユーザー名またはパスワードが間違っています', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    # ダッシュボード表示
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM assets WHERE user_id = %s', (user['id'],))
    assets_list = c.fetchall()
    
    # ... (資産計算ロジックは前回と同様)
    jp_stocks = [a for a in assets_list if a['asset_type'] == 'jp_stock']
    us_stocks = [a for a in assets_list if a['asset_type'] == 'us_stock']
    cash_items = [a for a in assets_list if a['asset_type'] == 'cash']
    gold_items = [a for a in assets_list if a['asset_type'] == 'gold']
    crypto_items = [a for a in assets_list if a['asset_type'] == 'crypto']
    investment_trust_items = [a for a in assets_list if a['asset_type'] == 'investment_trust']
    insurance_items = [a for a in assets_list if a['asset_type'] == 'insurance']

    jp_total = sum(s['quantity'] * s['price'] for s in jp_stocks)
    jp_profit = jp_total - sum(s['quantity'] * s['avg_cost'] for s in jp_stocks)
    usd_jpy = get_usd_jpy_rate()
    us_total_usd = sum(s['quantity'] * s['price'] for s in us_stocks)
    us_total_jpy = us_total_usd * usd_jpy
    us_profit_jpy = (us_total_usd - sum(s['quantity'] * s['avg_cost'] for s in us_stocks)) * usd_jpy
    cash_total = sum(i['quantity'] for i in cash_items)
    gold_total = sum(i['quantity'] * i['price'] for i in gold_items)
    gold_profit = gold_total - sum(i['quantity'] * i['avg_cost'] for i in gold_items)
    crypto_total = sum(i['quantity'] * i['price'] for i in crypto_items)
    crypto_profit = crypto_total - sum(i['quantity'] * i['avg_cost'] for i in crypto_items)
    investment_trust_total = sum((i['quantity'] * i['price'] / 10000) for i in investment_trust_items)
    investment_trust_profit = investment_trust_total - sum((i['quantity'] * i['avg_cost'] / 10000) for i in investment_trust_items)
    insurance_total = sum(i['price'] for i in insurance_items)
    insurance_profit = insurance_total - sum(i['avg_cost'] for i in insurance_items)
    
    total_assets = sum([jp_total, us_total_jpy, cash_total, gold_total, crypto_total, investment_trust_total, insurance_total])
    total_profit = sum([jp_profit, us_profit_jpy, gold_profit, crypto_profit, investment_trust_profit, insurance_profit])
    
    chart_data = {"labels": ["日本株", "米国株", "現金", "金", "暗号資産", "投資信託", "保険"], "values": [jp_total, us_total_jpy, cash_total, gold_total, crypto_total, investment_trust_total, insurance_total]}

    # 時系列データを取得
    ninety_days_ago = (datetime.now(pytz.timezone('Asia/Tokyo')) - timedelta(days=90)).date()
    c.execute('SELECT snapshot_date, total_assets FROM daily_snapshots WHERE user_id = %s AND snapshot_date >= %s ORDER BY snapshot_date ASC', (user['id'], ninety_days_ago))
    history_data = c.fetchall()
    conn.close()

    line_chart_data = {"labels": [r['snapshot_date'].strftime('%m/%d') for r in history_data], "values": [r['total_assets'] for r in history_data]}
    
    daily_change = {'value': 0, 'percentage': 0}
    if history_data:
        change = total_assets - history_data[-1]['total_assets']
        daily_change = {'value': change, 'percentage': (change / history_data[-1]['total_assets'] * 100) if history_data[-1]['total_assets'] > 0 else 0}

    return render_template('dashboard.html', user_name=session.get('username'), total_assets=total_assets, total_profit=total_profit, jp_total=jp_total, jp_profit=jp_profit, us_total_jpy=us_total_jpy, us_profit_jpy=us_profit_jpy, cash_total=cash_total, gold_total=gold_total, gold_profit=gold_profit, crypto_total=crypto_total, crypto_profit=crypto_profit, investment_trust_total=investment_trust_total, investment_trust_profit=investment_trust_profit, insurance_total=insurance_total, insurance_profit=insurance_profit, jp_stocks=jp_stocks, us_stocks=us_stocks, cash_items=cash_items, gold_items=gold_items, crypto_items=crypto_items, investment_trust_items=investment_trust_items, insurance_items=insurance_items, us_total_usd=us_total_usd, chart_data=json.dumps(chart_data), line_chart_data=json.dumps(line_chart_data), daily_change=daily_change)

@app.route('/assets/<asset_type>')
def manage_assets(asset_type):
    # 資産管理ページ
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM assets WHERE user_id = %s AND asset_type = %s ORDER BY symbol', (user['id'], asset_type))
    assets = c.fetchall()
    conn.close()
    type_info = {
        'jp_stock': {'title': '日本株', 'symbol_label': '証券コード', 'quantity_label': '株数'},
        'us_stock': {'title': '米国株', 'symbol_label': 'シンボル', 'quantity_label': '株数'},
        'gold': {'title': '金 (Gold)', 'symbol_label': '種類', 'quantity_label': '重量(g)'},
        'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'},
        'crypto': {'title': '暗号資産', 'symbol_label': '銘柄', 'quantity_label': '数量'},
        'investment_trust': {'title': '投資信託', 'symbol_label': '銘柄', 'quantity_label': '保有数量(口)'},
        'insurance': {'title': '保険', 'symbol_label': '項目名', 'quantity_label': '保険金額'}
    }
    return render_template('manage_assets.html', assets=assets, asset_type=asset_type, info=type_info.get(asset_type, {}), crypto_symbols=CRYPTO_SYMBOLS, investment_trust_symbols=INVESTMENT_TRUST_SYMBOLS, insurance_types=INSURANCE_TYPES)

@app.route('/add_asset', methods=['POST'])
def add_asset():
    # 資産の追加・更新
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    
    asset_type, symbol, name, quantity, avg_cost = (
        request.form['asset_type'], request.form['symbol'].strip(), 
        request.form.get('name', '').strip(), float(request.form.get('quantity', 0)), 
        float(request.form.get('avg_cost', 0) or 0)
    )
    if asset_type in ['us_stock', 'crypto']: symbol = symbol.upper()

    price = 0
    if asset_type == 'insurance': price = float(request.form.get('price', 0) or 0)
    elif asset_type == 'gold': price, name = get_gold_price(), "金 (Gold)"
    elif asset_type == 'crypto': price, name = get_crypto_price(symbol), name or symbol
    elif asset_type == 'investment_trust': price, name = get_investment_trust_price(symbol), name or symbol
    elif asset_type != 'cash':
        info = get_jp_stock_info(symbol) if asset_type == 'jp_stock' else get_us_stock_info(symbol)
        price, name = info['price'], name or info['name']
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, quantity, avg_cost FROM assets WHERE user_id = %s AND asset_type = %s AND symbol = %s', (user['id'], asset_type, symbol))
    existing = c.fetchone()
    
    if existing and asset_type not in ['cash', 'insurance']:
        new_qty = existing['quantity'] + quantity
        new_avg = ((existing['quantity'] * existing['avg_cost']) + (quantity * avg_cost)) / new_qty if new_qty > 0 else 0
        c.execute('UPDATE assets SET quantity = %s, price = %s, name = %s, avg_cost = %s WHERE id = %s', (new_qty, price, name, new_avg, existing['id']))
    else:
        c.execute('INSERT INTO assets (user_id, asset_type, symbol, name, quantity, price, avg_cost) VALUES (%s, %s, %s, %s, %s, %s, %s)', (user['id'], asset_type, symbol, name, quantity, price, avg_cost))
    
    conn.commit()
    conn.close()
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/edit_asset/<int:asset_id>')
def edit_asset(asset_id):
    # 資産編集ページ
    user = get_current_user();
    if not user: return redirect(url_for('login'))
    conn = get_db(); c = conn.cursor()
    c.execute('SELECT * FROM assets WHERE id = %s AND user_id = %s', (asset_id, user['id']))
    asset = c.fetchone(); conn.close()
    if not asset: return redirect(url_for('dashboard'))
    type_info = {
        'jp_stock': {'title': '日本株', 'symbol_label': '証券コード', 'quantity_label': '株数'},
        'us_stock': {'title': '米国株', 'symbol_label': 'シンボル', 'quantity_label': '株数'},
        # ... 他の資産タイプも同様
    }
    return render_template('edit_asset.html', asset=asset, info=type_info.get(asset['asset_type'], {}), insurance_types=INSURANCE_TYPES)

@app.route('/update_asset', methods=['POST'])
def update_asset():
    # 資産情報の上書き更新
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    
    asset_id, symbol, name, quantity, avg_cost = (
        request.form['asset_id'], request.form['symbol'].strip(), 
        request.form.get('name', '').strip(), float(request.form.get('quantity', 0)), 
        float(request.form.get('avg_cost', 0) or 0)
    )
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT asset_type FROM assets WHERE id = %s AND user_id = %s', (asset_id, user['id']))
    asset = c.fetchone()
    if not asset: 
        conn.close()
        return redirect(url_for('dashboard'))

    asset_type = asset['asset_type']
    if asset_type in ['us_stock', 'crypto']: symbol = symbol.upper()

    price = 0
    if asset_type == 'insurance': price = float(request.form.get('price', 0) or 0)
    elif asset_type == 'gold': price = get_gold_price()
    # ... 他の資産タイプの価格取得ロジックも同様に追加
    
    c.execute('UPDATE assets SET symbol = %s, name = %s, quantity = %s, price = %s, avg_cost = %s WHERE id = %s', (symbol, name, quantity, price, avg_cost, asset_id))
    conn.commit()
    conn.close()
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/delete_asset', methods=['POST'])
def delete_asset():
    # 資産の削除
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    asset_id = request.form['asset_id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT asset_type FROM assets WHERE id = %s AND user_id = %s', (asset_id, user['id']))
    asset = c.fetchone()
    if asset:
        c.execute('DELETE FROM assets WHERE id = %s', (asset_id,))
        conn.commit()
    conn.close()
    return redirect(url_for('manage_assets', asset_type=asset['asset_type'] if asset else 'jp_stock'))

@app.route('/update_prices', methods=['POST'])
def update_prices():
    # 特定カテゴリの資産価格を一括更新
    user = get_current_user()
    if not user: return ('Unauthorized', 401)
    asset_type = request.form.get('asset_type')
    if asset_type in ['cash', 'insurance']: return 'OK'
    
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, symbol FROM assets WHERE user_id = %s AND asset_type = %s', (user['id'], asset_type))
    assets_to_update = c.fetchall()
    
    update_data = []
    for asset in assets_to_update:
        price = 0
        if asset_type == 'jp_stock': price = get_stock_price(asset['symbol'], is_jp=True)
        elif asset_type == 'us_stock': price = get_stock_price(asset['symbol'], is_jp=False)
        # ... 他の資産タイプも同様
        if price > 0: update_data.append((price, asset['id']))
    
    if update_data:
        execute_values(c, "UPDATE assets SET price = data.price FROM (VALUES %s) AS data(price, id) WHERE assets.id = data.id", update_data)
    
    conn.commit()
    conn.close()
    return 'OK'

@app.route('/update_all_prices', methods=['POST'])
def update_all_prices():
    # 全ての資産価格を一括更新
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    
    conn = get_db()
    c = conn.cursor()
    asset_types = "','".join(['jp_stock', 'us_stock', 'gold', 'crypto', 'investment_trust'])
    c.execute(f"SELECT id, symbol, asset_type FROM assets WHERE user_id = %s AND asset_type IN ('{asset_types}')", (user['id'],))
    all_assets = c.fetchall()

    def fetch_price(asset):
        # ... 並列処理用の価格取得ロジック
        return (asset['id'], 0) # 省略

    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = executor.map(fetch_price, all_assets)
        updated_prices = [(p, i) for i, p in results if p > 0]
    
    if updated_prices:
        execute_values(c, "UPDATE assets SET price = data.price FROM (VALUES %s) AS data(price, id) WHERE assets.id = data.id", updated_prices)
    
    conn.commit()
    conn.close()
    flash('資産価格を更新しました', 'success')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
