from flask import Flask, render_template, request, redirect, url_for, session, flash
import requests
import json
import os
from datetime import datetime, timezone, timedelta
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import re
from bs4 import BeautifulSoup
import time
from decimal import Decimal, InvalidOperation

# PostgreSQLサポート
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

# 暗号通貨銘柄（固定）
CRYPTO_SYMBOLS = ['BTC', 'ETH', 'XRP', 'DOGE']

# デバッグフラグ（環境変数で有効化可能）
DEBUG_CRYPTO = os.environ.get('CRYPTO_DEBUG', '0') == '1'

# データベース設定
DATABASE_URL = os.environ.get('DATABASE_URL')

# HerokuのDATABASE_URLは postgres:// で始まるが、psycopg2は postgresql:// が必要
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

USE_POSTGRES = DATABASE_URL is not None and POSTGRES_AVAILABLE


def get_db():
    """データベース接続を取得"""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect('portfolio.db')
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    """データベースの初期化"""
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        # PostgreSQL用のテーブル作成
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
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
        
        # デフォルトユーザー作成
        c.execute("SELECT id FROM users WHERE username = 'demo'")
        if not c.fetchone():
            demo_hash = generate_password_hash('demo123')
            c.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", 
                     ('demo', demo_hash))
    else:
        # SQLite用のテーブル作成
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            asset_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            quantity REAL NOT NULL,
            price REAL DEFAULT 0,
            avg_cost REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )''')
        
        # デフォルトユーザー作成
        c.execute("SELECT id FROM users WHERE username = 'demo'")
        if not c.fetchone():
            demo_hash = generate_password_hash('demo123')
            c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", 
                     ('demo', demo_hash))

    conn.commit()
    conn.close()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this-in-production')


def get_current_user():
    """現在のユーザーを取得"""
    if 'user_id' not in session:
        return None
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('SELECT * FROM users WHERE id = %s', (session['user_id'],))
    else:
        c.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],))
    
    user = c.fetchone()
    conn.close()
    return user


# --- ユーティリティ：文字列正規化・数値抽出 ---

_FULLWIDTH_TRANS = {ord(f): ord(t) for f, t in zip('０１２３４５６７８９', '0123456789')}
_FULLWIDTH_TRANS.update({ord('，'): ord(','), ord('．'): ord('.'), ord('＋'): ord('+'), ord('－'): ord('-'), ord('　'): ord(' '), ord('％'): ord('%')})


def normalize_fullwidth(s):
    if s is None:
        return s
    return s.translate(_FULLWIDTH_TRANS)


def extract_number_from_string(s):
    """文字列中から最初に見つかる妥当な数値を抽出して float を返す（小数点／指数表記対応）"""
    if not s:
        return None
    try:
        s = normalize_fullwidth(s)
    except Exception:
        pass

    s = s.replace('\xa0', ' ')

    # 優先パターン：桁区切りカンマやスペースに対応し、小数および指数表記を許す
    m = re.search(r'([+-]?\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?)', s)
    if not m:
        # 最低限の数値（小数・指数含む）
        m = re.search(r'([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)', s)
    if not m:
        return None

    num_str = m.group(1)
    # カンマと空白の除去
    num_str = num_str.replace(',', '').replace(' ', '')

    try:
        d = Decimal(num_str)
        # float に変換して返す（DBの REAL に合わせるため）
        return float(d)
    except (InvalidOperation, ValueError):
        try:
            return float(num_str)
        except Exception:
            return None


def scrape_yahoo_finance_jp(code):
    """Yahoo Finance APIから日本株の情報を取得"""
    try:
        api_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        session_req = requests.Session()
        api_response = session_req.get(api_url, headers=headers, timeout=10)
        
        if api_response.status_code == 200:
            try:
                data = api_response.json()
                if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                    result = data['chart']['result'][0]
                    
                    price = 0
                    if 'meta' in result:
                        meta = result['meta']
                        price = (meta.get('regularMarketPrice') or 
                                meta.get('previousClose') or 
                                meta.get('chartPreviousClose') or 0)
                    
                    name = f"Stock {code}"
                    if 'meta' in result and 'shortName' in result['meta']:
                        name = result['meta']['shortName']
                    elif 'meta' in result and 'longName' in result['meta']:
                        name = result['meta']['longName']
                    
                    if price > 0:
                        return {'name': name, 'price': round(float(price), 2)}
            except Exception as e:
                print(f"API parsing error for {code}: {e}")
        
        return {'name': f'Stock {code}', 'price': 0}
        
    except Exception as e:
        print(f"Error getting JP stock {code}: {e}")
        return {'name': f'Stock {code}', 'price': 0}

def scrape_yahoo_finance_us(symbol):
    """Yahoo Finance APIから米国株の情報を取得"""
    try:
        api_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        session_req = requests.Session()
        api_response = session_req.get(api_url, headers=headers, timeout=10)
        
        if api_response.status_code == 200:
            try:
                data = api_response.json()
                if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                    result = data['chart']['result'][0]
                    
                    price = 0
                    if 'meta' in result:
                        meta = result['meta']
                        price = (meta.get('regularMarketPrice') or 
                                meta.get('previousClose') or 
                                meta.get('chartPreviousClose') or 0)
                    
                    name = symbol.upper()
                    if 'meta' in result and 'shortName' in result['meta']:
                        name = result['meta']['shortName']
                    elif 'meta' in result and 'longName' in result['meta']:
                        name = result['meta']['longName']
                    
                    if price > 0:
                        return {'name': name, 'price': round(float(price), 2)}
            except Exception as e:
                print(f"API parsing error for {symbol}: {e}")
        
        return {'name': symbol.upper(), 'price': 0}
        
    except Exception as e:
        print(f"Error getting US stock {symbol}: {e}")
        return {'name': symbol.upper(), 'price': 0}

def get_jp_stock_info(code):
    """日本株の情報を取得"""
    return scrape_yahoo_finance_jp(code)

def get_us_stock_info(symbol):
    """米国株の情報を取得"""
    return scrape_yahoo_finance_us(symbol)

def get_stock_price(symbol, is_jp=False):
    """株価を取得"""
    if is_jp:
        return get_jp_stock_info(symbol)['price']
    else:
        return get_us_stock_info(symbol)['price']

def get_stock_name(symbol, is_jp=False):
    """株式名を取得"""
    if is_jp:
        return get_jp_stock_info(symbol)['name']
    else:
        return get_us_stock_info(symbol)['name']
        
def get_crypto_price(symbol):
    """みんかぶ暗号資産から価格をスクレイピング（BTC/ETH/XRP/DOGEに限定）"""
    try:
        symbol = (symbol or '').upper()
        if symbol not in CRYPTO_SYMBOLS:
            # サポート外は0を返す（または例外にしても良い）
            print(f"Unsupported crypto symbol requested: {symbol}")
            return 0.0

        url = f"https://cc.minkabu.jp/pair/{symbol}_JPY"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = response.apparent_encoding
        text = response.text
        # 0) ページ内の JSON（"last" や "price" 等）を探索（指数表記も含む）
        json_matches = re.findall(r'"(?:last|price|lastPrice|close|current|ltp)"\s*:\s*"?([0-9\.,Ee+\-]+)"?', text)
        if json_matches:
            for jm in json_matches:
                val = extract_number_from_string(jm)
                if val is not None and val > 0:
                    if DEBUG_CRYPTO:
                        print(f"[DEBUG] Found price in JSON-like field: {jm} -> {val}")
                    return round(val, 2)

        # 1) 「現在値」の近傍にある「xxx 円」を探す（優先）
        idx = text.find('現在値')
        if idx != -1:
            snippet = text[idx: idx + 700]
            m = re.search(r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d+)?)\s*円', snippet)
            if m:
                try:
                    return float(m.group(1).replace(',', ''))
                except:
                    pass

        # 2) data-price や data-last などの属性（JSで埋めている場合）
        m = re.search(r'data-price=["\']([0-9\.,Ee+\-]+)["\']', text)
        if m:
            val = extract_number_from_string(m.group(1))
            if val is not None:
                return round(val, 2)

        m = re.search(r'"last"\s*:\s*["\']?([0-9\.,Ee+\-]+)["\']?', text)
        if m:
            val = extract_number_from_string(m.group(1))
            if val is not None:
                return round(val, 2)

        # 3) BeautifulSoup を使って、よく使われるクラス／要素を探す
        soup = BeautifulSoup(text, 'html.parser')
        selectors = ['div.pairPrice', '.pairPrice', '.pair_price', 'div.priceWrap', 'div.kv',
                     'span.yen', 'div.stock_price span.yen', 'p.price', 'span.price', 'div.price',
                     'span.value', 'div.value', 'strong', 'b']
        for sel in selectors:
            try:
                tag = soup.select_one(sel)
            except Exception:
                tag = None
            if tag:
                txt = tag.get_text(' ', strip=True)
                val = extract_number_from_string(txt)
                if val is not None and val > 0:
                    if DEBUG_CRYPTO:
                        print(f"[DEBUG] Found price by selector {sel}: {txt} -> {val}")
                    return round(val, 2)

        # 4) ページ中の全ての "xxx 円" を探して妥当な最初の値を取る
        normalized = normalize_fullwidth(text)
        matches = re.findall(r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d+)?)\s*円', normalized)
        for num in matches:
            try:
                val = float(num.replace(',', ''))
                if val > 0:
                    return round(val, 2)
            except:
                continue

        # 5) 指数表記（例: 0.169717e8 等）も探す
        m2 = re.search(r'([0-9\.,]+[eE][+-]?\d+)', text)
        if m2:
            val = extract_number_from_string(m2.group(1))
            if val is not None and val > 0:
                if DEBUG_CRYPTO:
                    print(f"[DEBUG] Found price by scientific notation: {m2.group(1)} -> {val}")
                return round(val, 2)

        # それでも取れなければ0
        if DEBUG_CRYPTO:
            snippet = text[:1200].replace('\n', ' ')
            print(f"[DEBUG] Failed to parse crypto price for {symbol}. Dumping small snippet:\n{snippet}\n--- end snippet ---")
        return 0.0
    except Exception as e:
        print(f"Error getting crypto price for {symbol}: {e}")
        return 0.0

def get_gold_price():
    """金価格を取得（田中貴金属からスクレイピング）"""
    try:
        tanaka_url = "https://gold.tanaka.co.jp/commodity/souba/english/index.php"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        res = requests.get(tanaka_url, headers=headers, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) > 1 and tds[0].get_text(strip=True).upper() == "GOLD":
                price_text = tds[1].get_text(strip=True)
                price_match = re.search(r"([0-9,]+) yen", price_text)
                if price_match:
                    return int(price_match.group(1).replace(",", ""))
        return 0
    except Exception as e:
        print(f"Error getting gold price: {e}")
        return 0

def get_usd_jpy_rate():
    """USD/JPY レートを取得"""
    try:
        api_url = "https://query1.finance.yahoo.com/v8/finance/chart/USDJPY=X"
        headers = {'User-Agent': 'Mozilla/5.0'}
        
        session_req = requests.Session()
        api_response = session_req.get(api_url, headers=headers, timeout=10)
        
        if api_response.status_code == 200:
            data = api_response.json()
            if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                result = data['chart']['result'][0]
                if 'meta' in result and 'regularMarketPrice' in result['meta']:
                    return float(result['meta']['regularMarketPrice'])
        
        return 150.0
    except Exception as e:
        print(f"Error getting USD/JPY rate: {e}")
        return 150.0

# アプリケーション開始時にDB初期化
init_db()

@app.route('/')
def index():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        
        if not username:
            flash('ユーザー名を入力してください', 'error')
        elif len(username) < 3:
            flash('ユーザー名は3文字以上で入力してください', 'error')
        elif len(password) < 6:
            flash('パスワードは6文字以上で入力してください', 'error')
        elif password != confirm_password:
            flash('パスワードが一致しません', 'error')
        else:
            conn = get_db()
            c = conn.cursor()
            
            if USE_POSTGRES:
                c.execute('SELECT id FROM users WHERE username = %s', (username,))
            else:
                c.execute('SELECT id FROM users WHERE username = ?', (username,))
            
            existing_user = c.fetchone()
            
            if existing_user:
                flash('このユーザー名は既に使用されています', 'error')
                conn.close()
            else:
                password_hash = generate_password_hash(password)
                
                if USE_POSTGRES:
                    c.execute('INSERT INTO users (username, password_hash) VALUES (%s, %s)',
                             (username, password_hash))
                else:
                    c.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                             (username, password_hash))
                
                conn.commit()
                conn.close()
                
                flash('アカウントを作成しました。ログインしてください。', 'success')
                return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db()
        c = conn.cursor()
        
        if USE_POSTGRES:
            c.execute('SELECT * FROM users WHERE username = %s', (username,))
        else:
            c.execute('SELECT * FROM users WHERE username = ?', (username,))
        
        user = c.fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('ログインしました', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('ユーザー名またはパスワードが間違っています', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('ログアウトしました', 'success')
    return redirect(url_for('login'))

# ... (imports and other code) ...

@app.route('/dashboard')
def dashboard():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'jp_stock'))
        jp_stocks = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'us_stock'))
        us_stocks = c.fetchall()
        
        c.execute('''SELECT symbol as label, quantity as amount FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'cash'))
        cash_items = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'gold'))
        gold_items = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'crypto'))
        crypto_items = c.fetchall()
    else:
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = "jp_stock"''', (user['id'],))
        jp_stocks = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = "us_stock"''', (user['id'],))
        us_stocks = c.fetchall()
        
        c.execute('''SELECT symbol as label, quantity as amount FROM assets 
                    WHERE user_id = ? AND asset_type = "cash"''', (user['id'],))
        cash_items = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = "gold"''', (user['id'],))
        gold_items = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = "crypto"''', (user['id'],))
        crypto_items = c.fetchall()
    
    conn.close()
    
    # 合計計算
    jp_total = sum(stock['quantity'] * stock['price'] for stock in jp_stocks)
    jp_cost_total = sum(stock['quantity'] * stock['avg_cost'] for stock in jp_stocks)
    jp_profit = jp_total - jp_cost_total
    
    us_total_usd = sum(stock['quantity'] * stock['price'] for stock in us_stocks)
    us_cost_total_usd = sum(stock['quantity'] * stock['avg_cost'] for stock in us_stocks)
    us_profit_usd = us_total_usd - us_cost_total_usd
    usd_jpy = get_usd_jpy_rate()
    us_total_jpy = us_total_usd * usd_jpy
    us_profit_jpy = us_profit_usd * usd_jpy
    
    cash_total = sum(item['amount'] for item in cash_items)
    
    gold_total = sum(item['quantity'] * item['price'] for item in gold_items)
    gold_cost_total = sum(item['quantity'] * item['avg_cost'] for item in gold_items)
    gold_profit = gold_total - gold_cost_total
    
    crypto_total = sum(item['quantity'] * item['price'] for item in crypto_items)
    crypto_cost_total = sum(item['quantity'] * item['avg_cost'] for item in crypto_items)
    crypto_profit = crypto_total - crypto_cost_total
    
    total_assets = jp_total + us_total_jpy + cash_total + gold_total + crypto_total
    total_cost = jp_cost_total + (us_cost_total_usd * usd_jpy) + cash_total + gold_cost_total + crypto_cost_total
    total_profit = total_assets - total_cost
    
    return render_template('dashboard.html', 
                         jp_stocks=jp_stocks, us_stocks=us_stocks, 
                         cash_items=cash_items, gold_items=gold_items, crypto_items=crypto_items,
                         jp_total=jp_total, jp_profit=jp_profit,
                         us_total_usd=us_total_usd, us_total_jpy=us_total_jpy, 
                         us_profit_usd=us_profit_usd, us_profit_jpy=us_profit_jpy,
                         cash_total=cash_total, gold_total=gold_total, 
                         gold_profit=gold_profit,
                         crypto_total=crypto_total, crypto_profit=crypto_profit,
                         total_assets=total_assets, total_profit=total_profit, 
                         usd_jpy=usd_jpy,
                         user_name=session.get('username', '')) # Changed this line
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'jp_stock'))
        jp_stocks = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'us_stock'))
        us_stocks = c.fetchall()
        
        c.execute('''SELECT symbol as label, quantity as amount FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'cash'))
        cash_items = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'gold'))
        gold_items = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s''', (user['id'], 'crypto'))
        crypto_items = c.fetchall()
    else:
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = "jp_stock"''', (user['id'],))
        jp_stocks = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = "us_stock"''', (user['id'],))
        us_stocks = c.fetchall()
        
        c.execute('''SELECT symbol as label, quantity as amount FROM assets 
                    WHERE user_id = ? AND asset_type = "cash"''', (user['id'],))
        cash_items = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = "gold"''', (user['id'],))
        gold_items = c.fetchall()
        
        c.execute('''SELECT symbol, name, quantity, price, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = "crypto"''', (user['id'],))
        crypto_items = c.fetchall()
    
    conn.close()
    
    # 合計計算
    jp_total = sum(stock['quantity'] * stock['price'] for stock in jp_stocks)
    jp_cost_total = sum(stock['quantity'] * stock['avg_cost'] for stock in jp_stocks)
    jp_profit = jp_total - jp_cost_total
    
    us_total_usd = sum(stock['quantity'] * stock['price'] for stock in us_stocks)
    us_cost_total_usd = sum(stock['quantity'] * stock['avg_cost'] for stock in us_stocks)
    us_profit_usd = us_total_usd - us_cost_total_usd
    usd_jpy = get_usd_jpy_rate()
    us_total_jpy = us_total_usd * usd_jpy
    us_profit_jpy = us_profit_usd * usd_jpy
    
    cash_total = sum(item['amount'] for item in cash_items)
    
    gold_total = sum(item['quantity'] * item['price'] for item in gold_items)
    gold_cost_total = sum(item['quantity'] * item['avg_cost'] for item in gold_items)
    gold_profit = gold_total - gold_cost_total
    
    crypto_total = sum(item['quantity'] * item['price'] for item in crypto_items)
    crypto_cost_total = sum(item['quantity'] * item['avg_cost'] for item in crypto_items)
    crypto_profit = crypto_total - crypto_cost_total
    
    total_assets = jp_total + us_total_jpy + cash_total + gold_total + crypto_total
    total_cost = jp_cost_total + (us_cost_total_usd * usd_jpy) + cash_total + gold_cost_total + crypto_cost_total
    total_profit = total_assets - total_cost
    
    return render_template('dashboard.html', 
                         jp_stocks=jp_stocks, us_stocks=us_stocks, 
                         cash_items=cash_items, gold_items=gold_items, crypto_items=crypto_items,
                         jp_total=jp_total, jp_profit=jp_profit,
                         us_total_usd=us_total_usd, us_total_jpy=us_total_jpy, 
                         us_profit_usd=us_profit_usd, us_profit_jpy=us_profit_jpy,
                         cash_total=cash_total, gold_total=gold_total, 
                         gold_profit=gold_profit,
                         crypto_total=crypto_total, crypto_profit=crypto_profit,
                         total_assets=total_assets, total_profit=total_profit, 
                         usd_jpy=usd_jpy,
                         username=session.get('username', ''))

@app.route('/assets/<asset_type>')
def manage_assets(asset_type):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('''SELECT * FROM assets WHERE user_id = %s AND asset_type = %s
                    ORDER BY symbol''', (user['id'], asset_type))
    else:
        c.execute('''SELECT * FROM assets WHERE user_id = ? AND asset_type = ?
                    ORDER BY symbol''', (user['id'], asset_type))
    
    assets = c.fetchall()
    conn.close()
    
    type_info = {
        'jp_stock': {'title': '日本株', 'symbol_label': '証券コード', 'quantity_label': '株数'},
        'us_stock': {'title': '米国株', 'symbol_label': 'シンボル', 'quantity_label': '株数'},
        'gold': {'title': '金 (Gold)', 'symbol_label': '種類', 'quantity_label': '重量(g)'},
        'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'},
        'crypto': {'title': '暗号資産', 'symbol_label': '銘柄', 'quantity_label': '数量'}
    }
    
    info = type_info.get(asset_type, type_info['jp_stock'])
    
    # crypto用の選択肢をテンプレートに渡す（プルダウンチップに使う）
    crypto_symbols = CRYPTO_SYMBOLS
    
    return render_template('manage_assets.html', assets=assets, asset_type=asset_type, info=info, crypto_symbols=crypto_symbols)

@app.route('/add_asset', methods=['POST'])
def add_asset():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_type = request.form['asset_type']
    # symbolは暗号資産だとプルダウン（ラジオ）で渡る想定。既存CSV入力等にも対応。
    symbol = request.form['symbol'].strip().upper()
    name = request.form.get('name', '').strip()
    quantity = float(request.form['quantity'])
    avg_cost = float(request.form.get('avg_cost', 0)) if request.form.get('avg_cost') else 0
    
    price = 0
    if asset_type == 'gold':
        price = get_gold_price()
        if not name:
            name = "金 (Gold)"
    elif asset_type == 'crypto':
        # 暗号資産は限定銘柄のみ受け付ける
        if symbol not in CRYPTO_SYMBOLS:
            flash('対応していない暗号資産です', 'error')
            return redirect(url_for('manage_assets', asset_type='crypto'))
        price = get_crypto_price(symbol)
        name = name or symbol
    elif asset_type != 'cash':
        is_jp = (asset_type == 'jp_stock')
        try:
            price = get_stock_price(symbol, is_jp)
            if not name:
                name = get_stock_name(symbol, is_jp)
        except Exception as e:
            flash(f'価格取得に失敗しました: {symbol}', 'error')
            price = 0
            name = name or symbol
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('''SELECT id, quantity, avg_cost FROM assets 
                    WHERE user_id = %s AND asset_type = %s AND symbol = %s''',
                 (user['id'], asset_type, symbol))
    else:
        c.execute('''SELECT id, quantity, avg_cost FROM assets 
                    WHERE user_id = ? AND asset_type = ? AND symbol = ?''',
                 (user['id'], asset_type, symbol))
    
    existing = c.fetchone()
    
    if existing and asset_type != 'cash':
        old_quantity = existing['quantity']
        old_avg_cost = existing['avg_cost']
        new_total_quantity = old_quantity + quantity
        
        if new_total_quantity > 0 and avg_cost > 0:
            new_avg_cost = ((old_quantity * old_avg_cost) + (quantity * avg_cost)) / new_total_quantity
        else:
            new_avg_cost = old_avg_cost if old_avg_cost > 0 else avg_cost
        
        if USE_POSTGRES:
            c.execute('''UPDATE assets SET quantity = %s, price = %s, name = %s, avg_cost = %s
                        WHERE id = %s''', (new_total_quantity, price, name, new_avg_cost, existing['id']))
        else:
            c.execute('''UPDATE assets SET quantity = ?, price = ?, name = ?, avg_cost = ?
                        WHERE id = ?''', (new_total_quantity, price, name, new_avg_cost, existing['id']))
        
        flash(f'{symbol} を更新しました（数量: {new_total_quantity}）', 'success')
    elif existing and asset_type == 'cash':
        if USE_POSTGRES:
            c.execute('''UPDATE assets SET quantity = %s, price = %s, name = %s
                        WHERE id = %s''', (quantity, price, name, existing['id']))
        else:
            c.execute('''UPDATE assets SET quantity = ?, price = ?, name = ?
                        WHERE id = ?''', (quantity, price, name, existing['id']))
        flash(f'{symbol} を更新しました', 'success')
    else:
        if USE_POSTGRES:
            c.execute('''INSERT INTO assets (user_id, asset_type, symbol, name, quantity, price, avg_cost)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                     (user['id'], asset_type, symbol, name, quantity, price, avg_cost))
        else:
            c.execute('''INSERT INTO assets (user_id, asset_type, symbol, name, quantity, price, avg_cost)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''',
                     (user['id'], asset_type, symbol, name, quantity, price, avg_cost))
        flash(f'{symbol} を追加しました', 'success')
    
    conn.commit()
    conn.close()
    
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/edit_asset/<int:asset_id>')
def edit_asset(asset_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('SELECT * FROM assets WHERE id = %s AND user_id = %s', (asset_id, user['id']))
    else:
        c.execute('SELECT * FROM assets WHERE id = ? AND user_id = ?', (asset_id, user['id']))
    
    asset = c.fetchone()
    conn.close()
    
    if not asset:
        flash('資産が見つかりません', 'error')
        return redirect(url_for('dashboard'))
    
    type_info = {
        'jp_stock': {'title': '日本株', 'symbol_label': '証券コード', 'quantity_label': '株数'},
        'us_stock': {'title': '米国株', 'symbol_label': 'シンボル', 'quantity_label': '株数'},
        'gold': {'title': '金 (Gold)', 'symbol_label': '種類', 'quantity_label': '重量(g)'},
        'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'},
        'crypto': {'title': '暗号資産', 'symbol_label': '銘柄', 'quantity_label': '数量'}
    }
    
    info = type_info.get(asset['asset_type'], type_info['jp_stock'])
    
    return render_template('edit_asset.html', asset=asset, info=info)

@app.route('/update_asset', methods=['POST'])
def update_asset():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_id = request.form['asset_id']
    symbol = request.form['symbol'].strip().upper()
    name = request.form.get('name', '').strip()
    quantity = float(request.form['quantity'])
    avg_cost = float(request.form.get('avg_cost', 0)) if request.form.get('avg_cost') else 0
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('SELECT asset_type FROM assets WHERE id = %s AND user_id = %s',
                 (asset_id, user['id']))
    else:
        c.execute('SELECT asset_type FROM assets WHERE id = ? AND user_id = ?',
                 (asset_id, user['id']))
    
    asset = c.fetchone()
    
    if not asset:
        flash('資産が見つかりません', 'error')
        conn.close()
        return redirect(url_for('dashboard'))
    
    asset_type = asset['asset_type']
    
    price = 0
    if asset_type == 'gold':
        price = get_gold_price()
        if not name:
            name = "金 (Gold)"
    elif asset_type == 'crypto':
        if symbol not in CRYPTO_SYMBOLS:
            flash('対応していない暗号資産です', 'error')
            conn.close()
            return redirect(url_for('manage_assets', asset_type='crypto'))
        price = get_crypto_price(symbol)
        if not name:
            name = symbol
    elif asset_type != 'cash':
        is_jp = (asset_type == 'jp_stock')
        try:
            price = get_stock_price(symbol, is_jp)
            if not name:
                name = get_stock_name(symbol, is_jp)
        except Exception as e:
            flash(f'価格取得に失敗しました: {symbol}', 'error')
            price = 0
            name = name or symbol
    
    if USE_POSTGRES:
        c.execute('''UPDATE assets SET symbol = %s, name = %s, quantity = %s, price = %s, avg_cost = %s
                    WHERE id = %s AND user_id = %s''',
                 (symbol, name, quantity, price, avg_cost, asset_id, user['id']))
    else:
        c.execute('''UPDATE assets SET symbol = ?, name = ?, quantity = ?, price = ?, avg_cost = ?
                    WHERE id = ? AND user_id = ?''',
                 (symbol, name, quantity, price, avg_cost, asset_id, user['id']))
    
    conn.commit()
    conn.close()
    
    flash(f'{symbol} を更新しました', 'success')
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/delete_asset', methods=['POST'])
def delete_asset():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_id = request.form['asset_id']
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('SELECT asset_type, symbol FROM assets WHERE id = %s AND user_id = %s',
                 (asset_id, user['id']))
    else:
        c.execute('SELECT asset_type, symbol FROM assets WHERE id = ? AND user_id = ?',
                 (asset_id, user['id']))
    
    asset = c.fetchone()
    
    if asset:
        if USE_POSTGRES:
            c.execute('DELETE FROM assets WHERE id = %s AND user_id = %s', (asset_id, user['id']))
        else:
            c.execute('DELETE FROM assets WHERE id = ? AND user_id = ?', (asset_id, user['id']))
        
        conn.commit()
        flash(f'{asset["symbol"]} を削除しました', 'success')
        asset_type = asset['asset_type']
    else:
        flash('削除に失敗しました', 'error')
        asset_type = 'jp_stock'
    
    conn.close()
    
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/update_prices', methods=['POST'])
def update_prices():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_type = request.form['asset_type']
    
    if asset_type == 'cash':
        return 'OK'
    
    conn = get_db()
    c = conn.cursor()
    
    if USE_POSTGRES:
        c.execute('SELECT id, symbol FROM assets WHERE user_id = %s AND asset_type = %s',
                 (user['id'], asset_type))
    else:
        c.execute('SELECT id, symbol FROM assets WHERE user_id = ? AND asset_type = ?',
                 (user['id'], asset_type))
    
    assets = c.fetchall()
    
    if asset_type == 'gold':
        try:
            gold_price = get_gold_price()
            for asset in assets:
                if USE_POSTGRES:
                    c.execute('UPDATE assets SET price = %s WHERE id = %s', (gold_price, asset['id']))
                else:
                    c.execute('UPDATE assets SET price = ? WHERE id = ?', (gold_price, asset['id']))
                time.sleep(0.5)
        except Exception as e:
            print(f"Error updating gold price: {e}")
    elif asset_type == 'crypto':
        for asset in assets:
            try:
                crypto_price = get_crypto_price(asset['symbol'])
                if DEBUG_CRYPTO:
                    print(f"[DEBUG] Updating {asset['symbol']} price to {crypto_price}")
                if USE_POSTGRES:
                    c.execute('UPDATE assets SET price = %s WHERE id = %s', (crypto_price, asset['id']))
                else:
                    c.execute('UPDATE assets SET price = ? WHERE id = ?', (crypto_price, asset['id']))
                conn.commit()  # 各更新後にコミット
                time.sleep(0.5)  # APIレート制限を考慮
            except Exception as e:
                print(f"Error updating crypto price for {asset['symbol']}: {e}")
                if DEBUG_CRYPTO:
                    import traceback
                    traceback.print_exc()
    else:
        is_jp = (asset_type == 'jp_stock')
        for asset in assets:
            try:
                price = get_stock_price(asset['symbol'], is_jp)
                if USE_POSTGRES:
                    c.execute('UPDATE assets SET price = %s WHERE id = %s', (price, asset['id']))
                else:
                    c.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
                time.sleep(1)
            except Exception as e:
                print(f"Failed to update {asset['symbol']}: {e}")
    
    conn.commit()
    conn.close()
    
    return 'OK'

@app.route('/update_all_prices', methods=['POST'])
def update_all_prices():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    c = conn.cursor()
    
    # 日本株の価格更新
    if USE_POSTGRES:
        c.execute('SELECT id, symbol FROM assets WHERE user_id = %s AND asset_type = %s',
                 (user['id'], 'jp_stock'))
    else:
        c.execute('SELECT id, symbol FROM assets WHERE user_id = ? AND asset_type = "jp_stock"',
                 (user['id'],))
    
    jp_assets = c.fetchall()
    
    for asset in jp_assets:
        try:
            price = get_stock_price(asset['symbol'], True)
            if USE_POSTGRES:
                c.execute('UPDATE assets SET price = %s WHERE id = %s', (price, asset['id']))
            else:
                c.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
            time.sleep(1)
        except:
            pass
    
    # 米国株の価格更新
    if USE_POSTGRES:
        c.execute('SELECT id, symbol FROM assets WHERE user_id = %s AND asset_type = %s',
                 (user['id'], 'us_stock'))
    else:
        c.execute('SELECT id, symbol FROM assets WHERE user_id = ? AND asset_type = "us_stock"',
                 (user['id'],))
    
    us_assets = c.fetchall()
    
    for asset in us_assets:
        try:
            price = get_stock_price(asset['symbol'], False)
            if USE_POSTGRES:
                c.execute('UPDATE assets SET price = %s WHERE id = %s', (price, asset['id']))
            else:
                c.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
            time.sleep(1)
        except:
            pass
    
    # 金の価格更新
    if USE_POSTGRES:
        c.execute('SELECT id FROM assets WHERE user_id = %s AND asset_type = %s',
                 (user['id'], 'gold'))
    else:
        c.execute('SELECT id FROM assets WHERE user_id = ? AND asset_type = "gold"',
                 (user['id'],))
    
    gold_assets = c.fetchall()
    
    if gold_assets:
        try:
            gold_price = get_gold_price()
            for asset in gold_assets:
                if USE_POSTGRES:
                    c.execute('UPDATE assets SET price = %s WHERE id = %s', (gold_price, asset['id']))
                else:
                    c.execute('UPDATE assets SET price = ? WHERE id = ?', (gold_price, asset['id']))
        except:
            pass
            
    # 暗号資産の価格更新
    if USE_POSTGRES:
        c.execute('SELECT id, symbol FROM assets WHERE user_id = %s AND asset_type = %s',
                 (user['id'], 'crypto'))
    else:
        c.execute('SELECT id, symbol FROM assets WHERE user_id = ? AND asset_type = "crypto"',
                 (user['id'],))
    
    crypto_assets = c.fetchall()
    
    for asset in crypto_assets:
        try:
            price = get_crypto_price(asset['symbol'])
            if USE_POSTGRES:
                c.execute('UPDATE assets SET price = %s WHERE id = %s', (price, asset['id']))
            else:
                c.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
            time.sleep(1)
        except:
            pass
    
    conn.commit()
    conn.close()
    
    flash('全ての価格を更新しました', 'success')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
