# --- ライブラリのインポート ---
from flask import Flask, render_template, request, redirect, url_for, session, flash
import os
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import re
from decimal import Decimal, InvalidOperation

# --- 非同期処理のためのライブラリ ---
import asyncio
import httpx
from bs4 import BeautifulSoup

# --- PostgreSQLサポート ---
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, execute_values
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

# --- 定数定義 ---
CRYPTO_SYMBOLS = ['BTC', 'ETH', 'XRP', 'DOGE']
INVESTMENT_TRUST_INFO = {
    'S&P500': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000GKC6',
    'オルカン': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000H1T1',
    'FANG+': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000FZD4'
}
INVESTMENT_TRUST_SYMBOLS = list(INVESTMENT_TRUST_INFO.keys())
DATABASE_URL = os.environ.get('DATABASE_URL')

if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

USE_POSTGRES = DATABASE_URL is not None and POSTGRES_AVAILABLE

# --- データベース関連関数 ---
def get_db():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    else:
        conn = sqlite3.connect('portfolio.db')
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    if USE_POSTGRES:
        c.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username VARCHAR(255) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS assets (id SERIAL PRIMARY KEY, user_id INTEGER, asset_type VARCHAR(50) NOT NULL, symbol VARCHAR(50) NOT NULL, name VARCHAR(255), quantity REAL NOT NULL, price REAL DEFAULT 0, avg_cost REAL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users (id))''')
        c.execute("SELECT id FROM users WHERE username = 'demo'")
        if not c.fetchone():
            c.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", ('demo', generate_password_hash('demo123')))
    else:
        c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS assets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, asset_type TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, quantity REAL NOT NULL, price REAL DEFAULT 0, avg_cost REAL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users (id))''')
        c.execute("SELECT id FROM users WHERE username = 'demo'")
        if not c.fetchone():
            c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ('demo', generate_password_hash('demo123')))
    conn.commit()
    conn.close()

# --- Flask アプリの初期化 ---
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-for-production')

# --- ユーティリティ関数 ---
_FULLWIDTH_TRANS = str.maketrans('０１２３４５６７８９，．＋－　％', '0123456789,.+- %')
def normalize_fullwidth(s): return s.translate(_FULLWIDTH_TRANS) if s else s

def extract_number_from_string(s):
    if not s: return None
    try: s = normalize_fullwidth(s)
    except: pass
    s = s.replace('\xa0', ' ')
    m = re.search(r'([+-]?\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?)', s)
    if not m: m = re.search(r'([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)', s)
    if not m: return None
    num_str = m.group(1).replace(',', '').replace(' ', '')
    try: return float(Decimal(num_str))
    except (InvalidOperation, ValueError):
        try: return float(num_str)
        except: return None

# --- ▼▼▼ 非同期処理のコア部分 ▼▼▼ ---

HTTP_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
TIMEOUT_CONFIG = httpx.Timeout(15.0, connect=20.0)

async def fetch_url_content(client, url):
    try:
        response = await client.get(url, headers=HTTP_HEADERS, timeout=TIMEOUT_CONFIG, follow_redirects=True)
        response.raise_for_status()
        return response.text
    except httpx.RequestError as e:
        print(f"HTTP Request Error for {url}: {e}")
        return None

async def get_stock_info_async(client, symbol, is_jp=False):
    code = f"{symbol}.T" if is_jp else symbol.upper()
    api_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}"
    default_name = f"Stock {symbol}"
    try:
        response = await client.get(api_url, headers=HTTP_HEADERS, timeout=TIMEOUT_CONFIG)
        if response.status_code == 200:
            data = response.json()
            meta = data.get('chart', {}).get('result', [{}])[0].get('meta', {})
            price = meta.get('regularMarketPrice') or meta.get('previousClose') or 0
            name = meta.get('shortName') or meta.get('longName') or default_name
            return {'name': name, 'price': round(float(price), 2)}
    except Exception as e:
        print(f"Error getting stock {symbol}: {e}")
    return {'name': default_name, 'price': 0}

async def get_crypto_price_async(client, symbol):
    symbol = (symbol or '').upper()
    if symbol not in CRYPTO_SYMBOLS: return 0.0
    url = f"https://cc.minkabu.jp/pair/{symbol}_JPY"
    try:
        text = await fetch_url_content(client, url)
        if not text: return 0.0
        matches = re.findall(r'"(?:last|price|ltp)"\s*:\s*"?([0-9\.,Ee+\-]+)"?', text)
        for m in matches:
            if val := extract_number_from_string(m): return round(val, 2)
    except Exception as e: print(f"Error getting crypto price for {symbol}: {e}")
    return 0.0

async def get_gold_price_async(client):
    url = "https://gold.tanaka.co.jp/commodity/souba/english/index.php"
    try:
        text = await fetch_url_content(client, url)
        if not text: return 0
        soup = BeautifulSoup(text, "lxml")
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) > 1 and "GOLD" in tds[0].get_text(strip=True).upper():
                if price_match := re.search(r"([0-9,]+)", tds[1].get_text(strip=True)):
                    return int(price_match.group(1).replace(",", ""))
    except Exception as e: print(f"Error getting gold price: {e}")
    return 0

async def get_investment_trust_price_async(client, symbol):
    if symbol not in INVESTMENT_TRUST_INFO: return 0.0
    url = INVESTMENT_TRUST_INFO[symbol]
    try:
        text = await fetch_url_content(client, url)
        if not text: return 0.0
        soup = BeautifulSoup(text, 'lxml')
        if th := soup.find('th', string=re.compile(r'\s*基準価額\s*')):
            if td := th.find_next_sibling('td'):
                if price := extract_number_from_string(td.get_text(strip=True)):
                    return price
    except Exception as e: print(f"Error scraping investment trust price for {symbol}: {e}")
    return 0.0

async def get_usd_jpy_rate_async(client):
    api_url = "https://query1.finance.yahoo.com/v8/finance/chart/USDJPY=X"
    try:
        response = await client.get(api_url, headers=HTTP_HEADERS, timeout=TIMEOUT_CONFIG)
        if response.status_code == 200:
            meta = response.json().get('chart', {}).get('result', [{}])[0].get('meta', {})
            if price := meta.get('regularMarketPrice'): return float(price)
    except Exception as e: print(f"Error getting USD/JPY rate: {e}")
    return 150.0

async def get_price_for_single_asset(asset_type, symbol=None, get_info=False):
    async with httpx.AsyncClient() as client:
        if asset_type == 'gold': return await get_gold_price_async(client)
        if asset_type == 'crypto': return await get_crypto_price_async(client, symbol)
        if asset_type == 'investment_trust': return await get_investment_trust_price_async(client, symbol)
        if asset_type == 'jp_stock': return await get_stock_info_async(client, symbol, is_jp=True)
        if asset_type == 'us_stock': return await get_stock_info_async(client, symbol, is_jp=False)
        if asset_type == 'usd_jpy': return await get_usd_jpy_rate_async(client)
    return 0 if not get_info else {'name': 'N/A', 'price': 0}

# --- 同期関数のラッパー (既存ルートとの互換性のため) ---
def get_gold_price(): return asyncio.run(get_price_for_single_asset('gold'))
def get_crypto_price(s): return asyncio.run(get_price_for_single_asset('crypto', s))
def get_investment_trust_price(s): return asyncio.run(get_price_for_single_asset('investment_trust', s))
def get_jp_stock_info(s): return asyncio.run(get_price_for_single_asset('jp_stock', s, get_info=True))
def get_us_stock_info(s): return asyncio.run(get_price_for_single_asset('us_stock', s, get_info=True))
def get_stock_price(s, is_jp): return get_jp_stock_info(s)['price'] if is_jp else get_us_stock_info(s)['price']
def get_usd_jpy_rate(): return asyncio.run(get_price_for_single_asset('usd_jpy'))

# --- ルート定義 ---
def get_current_user():
    if 'user_id' not in session: return None
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE id = %s' if USE_POSTGRES else 'SELECT * FROM users WHERE id = ?', (session['user_id'],))
    user = c.fetchone()
    conn.close()
    return user

@app.route('/')
def index():
    return redirect(url_for('login')) if not get_current_user() else redirect(url_for('dashboard'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        if not username or len(username) < 3 or len(password) < 6 or password != request.form['confirm_password']:
            flash('ユーザー名は3文字以上、パスワードは6文字以上で、パスワード確認が一致している必要があります。', 'error')
            return render_template('register.html')
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM users WHERE username = %s' if USE_POSTGRES else 'SELECT id FROM users WHERE username = ?', (username,))
        if c.fetchone():
            flash('このユーザー名は既に使用されています', 'error')
        else:
            c.execute('INSERT INTO users (username, password_hash) VALUES (%s, %s)' if USE_POSTGRES else 'INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, generate_password_hash(password)))
            conn.commit()
            flash('アカウントを作成しました。ログインしてください。', 'success')
            return redirect(url_for('login'))
        conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username, password = request.form['username'], request.form['password']
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE username = %s' if USE_POSTGRES else 'SELECT * FROM users WHERE username = ?', (username,))
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
    flash('ログアウトしました', 'success')
    return redirect(url_for('login'))

@app.route('/dashboard')
async def dashboard():
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM assets WHERE user_id = %s' if USE_POSTGRES else 'SELECT * FROM assets WHERE user_id = ?', (user['id'],))
    assets_list = c.fetchall()
    conn.close()
    async with httpx.AsyncClient() as client: usd_jpy = await get_usd_jpy_rate_async(client)
    assets = {t: [a for a in assets_list if a['asset_type'] == t] for t in ['jp_stock', 'us_stock', 'cash', 'gold', 'crypto', 'investment_trust']}
    jp_total = sum(s['quantity'] * s['price'] for s in assets['jp_stock'])
    jp_profit = jp_total - sum(s['quantity'] * s['avg_cost'] for s in assets['jp_stock'])
    us_total_usd = sum(s['quantity'] * s['price'] for s in assets['us_stock'])
    us_profit_usd = us_total_usd - sum(s['quantity'] * s['avg_cost'] for s in assets['us_stock'])
    cash_total = sum(i['quantity'] for i in assets['cash'])
    gold_total = sum(i['quantity'] * i['price'] for i in assets['gold'])
    gold_profit = gold_total - sum(i['quantity'] * i['avg_cost'] for i in assets['gold'])
    crypto_total = sum(i['quantity'] * i['price'] for i in assets['crypto'])
    crypto_profit = crypto_total - sum(i['quantity'] * i['avg_cost'] for i in assets['crypto'])
    it_total = sum((i['quantity'] * i['price'] / 10000) for i in assets['investment_trust'])
    it_profit = it_total - sum((i['quantity'] * i['avg_cost'] / 10000) for i in assets['investment_trust'])
    us_total_jpy, us_profit_jpy = us_total_usd * usd_jpy, us_profit_usd * usd_jpy
    total_assets = jp_total + us_total_jpy + cash_total + gold_total + crypto_total + it_total
    total_profit = jp_profit + us_profit_jpy + gold_profit + crypto_profit + it_profit
    return render_template('dashboard.html', **locals())

@app.route('/update_all_prices', methods=['POST'])
async def update_all_prices():
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, symbol, asset_type FROM assets WHERE user_id = %s' if USE_POSTGRES else 'SELECT id, symbol, asset_type FROM assets WHERE user_id = ?', (user['id'],))
    all_assets = c.fetchall()
    if not all_assets:
        conn.close()
        flash('更新対象の資産がありません', 'info')
        return redirect(url_for('dashboard'))
    tasks, updates = [], []
    async with httpx.AsyncClient() as client:
        for asset in all_assets:
            task = None
            if asset['asset_type'] == 'jp_stock': task = asyncio.create_task(get_stock_info_async(client, asset['symbol'], is_jp=True))
            elif asset['asset_type'] == 'us_stock': task = asyncio.create_task(get_stock_info_async(client, asset['symbol'], is_jp=False))
            elif asset['asset_type'] == 'gold': task = asyncio.create_task(get_gold_price_async(client))
            elif asset['asset_type'] == 'crypto': task = asyncio.create_task(get_crypto_price_async(client, asset['symbol']))
            elif asset['asset_type'] == 'investment_trust': task = asyncio.create_task(get_investment_trust_price_async(client, asset['symbol']))
            if task: tasks.append((asset['id'], task))
        results = await asyncio.gather(*(task for _, task in tasks))
    for (asset_id, _), result in zip(tasks, results):
        price = result.get('price', 0) if isinstance(result, dict) else result
        if price and price > 0: updates.append((price, asset_id))
    if updates:
        if USE_POSTGRES: execute_values(c, "UPDATE assets SET price = data.price FROM (VALUES %s) AS data(price, id) WHERE assets.id = data.id", updates)
        else: c.executemany('UPDATE assets SET price = ? WHERE id = ?', updates)
        conn.commit()
    conn.close()
    flash(f'全{len(all_assets)}件の資産価格を更新しました（{len(updates)}件成功）', 'success')
    return redirect(url_for('dashboard'))

@app.route('/assets/<asset_type>')
def manage_assets(asset_type):
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM assets WHERE user_id = %s AND asset_type = %s ORDER BY symbol' if USE_POSTGRES else 'SELECT * FROM assets WHERE user_id = ? AND asset_type = ? ORDER BY symbol', (user['id'], asset_type))
    assets = c.fetchall()
    conn.close()
    type_info = {'jp_stock': {'title': '日本株', 'symbol_label': '証券コード', 'quantity_label': '株数'},'us_stock': {'title': '米国株', 'symbol_label': 'シンボル', 'quantity_label': '株数'},'gold': {'title': '金 (Gold)', 'symbol_label': '種類', 'quantity_label': '重量(g)'},'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'},'crypto': {'title': '暗号資産', 'symbol_label': '銘柄', 'quantity_label': '数量'},'investment_trust': {'title': '投資信託', 'symbol_label': '銘柄', 'quantity_label': '保有数量(口)'}}
    info = type_info.get(asset_type, {})
    return render_template('manage_assets.html', assets=assets, asset_type=asset_type, info=info, crypto_symbols=CRYPTO_SYMBOLS, investment_trust_symbols=INVESTMENT_TRUST_SYMBOLS)

@app.route('/add_asset', methods=['POST'])
def add_asset():
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    form = request.form
    asset_type = form['asset_type']
    symbol = form['symbol'].strip().upper() if asset_type in ['us_stock', 'crypto'] else form['symbol'].strip()
    name = form.get('name', '').strip()
    try:
        quantity = float(form['quantity'])
        avg_cost = float(form.get('avg_cost', 0)) if form.get('avg_cost') else 0
    except (ValueError, TypeError):
        flash('数量または単価の形式が正しくありません。', 'error')
        return redirect(url_for('manage_assets', asset_type=asset_type))
    
    price, stock_info = 0, None
    if asset_type == 'gold': price, name = get_gold_price(), name or "金 (Gold)"
    elif asset_type == 'crypto':
        if symbol not in CRYPTO_SYMBOLS: flash('対応していない暗号資産です', 'error'); return redirect(url_for('manage_assets', asset_type='crypto'))
        price, name = get_crypto_price(symbol), name or symbol
    elif asset_type == 'investment_trust':
        if symbol not in INVESTMENT_TRUST_SYMBOLS: flash('対応していない投資信託です', 'error'); return redirect(url_for('manage_assets', asset_type='investment_trust'))
        price, name = get_investment_trust_price(symbol), name or symbol
    elif asset_type != 'cash':
        stock_info = get_jp_stock_info(symbol) if asset_type == 'jp_stock' else get_us_stock_info(symbol)
        price, name = stock_info['price'], name or stock_info['name']
    
    conn, c = get_db(), conn.cursor()
    c.execute('SELECT id, quantity, avg_cost FROM assets WHERE user_id = %s AND asset_type = %s AND symbol = %s' if USE_POSTGRES else 'SELECT id, quantity, avg_cost FROM assets WHERE user_id = ? AND asset_type = ? AND symbol = ?', (user['id'], asset_type, symbol))
    existing = c.fetchone()
    if existing and asset_type != 'cash':
        old_q, old_ac = existing['quantity'] or 0, existing['avg_cost'] or 0
        new_q = old_q + quantity
        new_ac = ((old_q * old_ac) + (quantity * avg_cost)) / new_q if new_q > 0 and avg_cost > 0 else (old_ac or avg_cost)
        c.execute('UPDATE assets SET quantity = %s, price = %s, name = %s, avg_cost = %s WHERE id = %s' if USE_POSTGRES else 'UPDATE assets SET quantity = ?, price = ?, name = ?, avg_cost = ? WHERE id = ?', (new_q, price, name, new_ac, existing['id']))
    elif asset_type == 'cash' and existing:
         c.execute('UPDATE assets SET quantity = %s WHERE id = %s' if USE_POSTGRES else 'UPDATE assets SET quantity = ? WHERE id = ?', (quantity, existing['id']))
    else:
        c.execute('INSERT INTO assets (user_id, asset_type, symbol, name, quantity, price, avg_cost) VALUES (%s,%s,%s,%s,%s,%s,%s)' if USE_POSTGRES else 'INSERT INTO assets (user_id, asset_type, symbol, name, quantity, price, avg_cost) VALUES (?,?,?,?,?,?,?)', (user['id'], asset_type, symbol, name, quantity, price, avg_cost))
    conn.commit(); conn.close()
    flash(f'{symbol} を追加/更新しました', 'success')
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/edit_asset/<int:asset_id>')
def edit_asset(asset_id):
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM assets WHERE id = %s AND user_id = %s' if USE_POSTGRES else 'SELECT * FROM assets WHERE id = ? AND user_id = ?', (asset_id, user['id']))
    asset = c.fetchone()
    conn.close()
    if not asset:
        flash('資産が見つかりません', 'error')
        return redirect(url_for('dashboard'))
    type_info = {'jp_stock': {'title': '日本株', 'symbol_label': '証券コード', 'quantity_label': '株数'},'us_stock': {'title': '米国株', 'symbol_label': 'シンボル', 'quantity_label': '株数'},'gold': {'title': '金 (Gold)', 'symbol_label': '種類', 'quantity_label': '重量(g)'},'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'},'crypto': {'title': '暗号資産', 'symbol_label': '銘柄', 'quantity_label': '数量'},'investment_trust': {'title': '投資信託', 'symbol_label': '銘柄', 'quantity_label': '保有数量(口)'}}
    info = type_info.get(asset['asset_type'], {})
    return render_template('edit_asset.html', asset=asset, info=info)

@app.route('/update_asset', methods=['POST'])
def update_asset():
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    form = request.form
    asset_id = form['asset_id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT asset_type FROM assets WHERE id = %s AND user_id = %s' if USE_POSTGRES else 'SELECT asset_type FROM assets WHERE id = ? AND user_id = ?', (asset_id, user['id']))
    asset = c.fetchone()
    if not asset:
        flash('資産が見つかりません', 'error')
        conn.close()
        return redirect(url_for('dashboard'))
    asset_type = asset['asset_type']
    symbol = form['symbol'].strip().upper() if asset_type in ['us_stock', 'crypto'] else form['symbol'].strip()
    name = form.get('name', '').strip()
    try:
        quantity = float(form['quantity'])
        avg_cost = float(form.get('avg_cost', 0)) if form.get('avg_cost') else 0
    except (ValueError, TypeError):
        flash('数量または単価の形式が正しくありません。', 'error')
        return redirect(url_for('manage_assets', asset_type=asset_type))
    
    price, stock_info = 0, None
    if asset_type == 'gold': price, name = get_gold_price(), name or "金 (Gold)"
    elif asset_type == 'crypto': price, name = get_crypto_price(symbol), name or symbol
    elif asset_type == 'investment_trust': price, name = get_investment_trust_price(symbol), name or symbol
    elif asset_type != 'cash':
        stock_info = get_jp_stock_info(symbol) if asset_type == 'jp_stock' else get_us_stock_info(symbol)
        price, name = stock_info['price'], name or stock_info['name']
    
    c.execute('UPDATE assets SET symbol=%s, name=%s, quantity=%s, price=%s, avg_cost=%s WHERE id=%s AND user_id=%s' if USE_POSTGRES else 'UPDATE assets SET symbol=?, name=?, quantity=?, price=?, avg_cost=? WHERE id=? AND user_id=?',
              (symbol, name, quantity, price, avg_cost, asset_id, user['id']))
    conn.commit()
    conn.close()
    flash(f'{symbol} を更新しました', 'success')
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/delete_asset', methods=['POST'])
def delete_asset():
    user = get_current_user()
    if not user: return redirect(url_for('login'))
    asset_id = request.form['asset_id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT asset_type, symbol FROM assets WHERE id = %s AND user_id = %s' if USE_POSTGRES else 'SELECT asset_type, symbol FROM assets WHERE id = ? AND user_id = ?', (asset_id, user['id']))
    asset = c.fetchone()
    if asset:
        c.execute('DELETE FROM assets WHERE id = %s AND user_id = %s' if USE_POSTGRES else 'DELETE FROM assets WHERE id = ? AND user_id = ?', (asset_id, user['id']))
        conn.commit()
        flash(f'{asset["symbol"]} を削除しました', 'success')
        asset_type = asset['asset_type']
    else:
        flash('削除に失敗しました', 'error')
        asset_type = 'jp_stock' # fallback
    conn.close()
    return redirect(url_for('manage_assets', asset_type=asset_type))

# --- アプリケーションの起動 ---
init_db()

if __name__ == '__main__':
    # ローカルでテスト実行するには、ターミナルで `hypercorn app:app --reload` を実行してください
    port = int(os.environ.get('PORT', 8000))
    print(f"This is an async app. To run it locally, use the command:\n hypercorn app:app --bind 0.0.0.0:{port} --reload")
