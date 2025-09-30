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

# 投資信託銘柄（固定）
INVESTMENT_TRUST_INFO = {
    'S&P500': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000GKC6',
    'オルカン': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000H1T1',
    'FANG+': 'https://www.rakuten-sec.co.jp/web/fund/detail/?ID=JP90C000FZD4'
}
INVESTMENT_TRUST_SYMBOLS = list(INVESTMENT_TRUST_INFO.keys())


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

def get_investment_trust_price(symbol):
    """楽天証券から投資信託の基準価額を取得"""
    if symbol not in INVESTMENT_TRUST_INFO:
        print(f"Unsupported investment trust symbol: {symbol}")
        return 0.0

    url = INVESTMENT_TRUST_INFO[symbol]
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, 'html.parser')

        # === ▼▼▼ ここからが修正箇所です ▼▼▼ ===
        # 「基準価額」というテキストを含むテーブルヘッダー(th)を探す
        th = soup.find('th', string=re.compile(r'\s*基準価額\s*'))
        
        if th:
            # thの直後にあるテーブルデータ(td)要素を取得
            td = th.find_next_sibling('td')
            if td:
                # td要素からテキストをすべて取得する (例: "36,175 円 前日比 +15円")
                price_text = td.get_text(strip=True)
                
                # 既存の数値抽出関数を使って、テキストから最初の数値を抜き出す
                price = extract_number_from_string(price_text)
                
                if price is not None:
                    # 抽出した数値を返す
                    return price
        # === ▲▲▲ ここまでが修正箇所です ▲▲▲ ===

        # 上記の方法で取得できなかった場合のエラー表示
        print(f"Could not find the price for {symbol} on the page. The website structure may have changed.")
        return 0.0

    except Exception as e:
        print(f"Error scraping investment trust price for {symbol}: {e}")
        return 0.0

    except Exception as e:
        print(f"Error scraping investment trust price for {symbol}: {e}")
        return 0.0


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

@app.route('/dashboard')
def dashboard():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    c = conn.cursor()
    
    asset_types = ['jp_stock', 'us_stock', 'cash', 'gold', 'crypto', 'investment_trust']
    assets = {}

    for asset_type in asset_types:
        if USE_POSTGRES:
            c.execute('''SELECT * FROM assets WHERE user_id = %s AND asset_type = %s''', 
                      (user['id'], asset_type))
        else:
            c.execute('''SELECT * FROM assets WHERE user_id = ? AND asset_type = ?''', 
                      (user['id'], asset_type))
        assets[asset_type] = c.fetchall()
        
    conn.close()
    
    # 日本株
    jp_stocks = assets['jp_stock']
    jp_total = sum(s['quantity'] * s['price'] for s in jp_stocks)
    jp_cost = sum(s['quantity'] * s['avg_cost'] for s in jp_stocks)
    jp_profit = jp_total - jp_cost

    # 米国株
    us_stocks = assets['us_stock']
    usd_jpy = get_usd_jpy_rate()
    us_total_usd = sum(s['quantity'] * s['price'] for s in us_stocks)
    us_cost_usd = sum(s['quantity'] * s['avg_cost'] for s in us_stocks)
    us_profit_usd = us_total_usd - us_cost_usd
    us_total_jpy = us_total_usd * usd_jpy
    us_profit_jpy = us_profit_usd * usd_jpy

    # 現金
    cash_items = assets['cash']
    cash_total = sum(i['quantity'] for i in cash_items)
    
    # 金
    gold_items = assets['gold']
    gold_total = sum(i['quantity'] * i['price'] for i in gold_items)
    gold_cost = sum(i['quantity'] * i['avg_cost'] for i in gold_items)
    gold_profit = gold_total - gold_cost

    # 暗号資産
    crypto_items = assets['crypto']
    crypto_total = sum(i['quantity'] * i['price'] for i in crypto_items)
    crypto_cost = sum(i['quantity'] * i['avg_cost'] for i in crypto_items)
    crypto_profit = crypto_total - crypto_cost

    # 投資信託
    investment_trust_items = assets['investment_trust']
    it_total = sum((i['quantity'] * i['price'] / 10000) for i in investment_trust_items)
    it_cost = sum((i['quantity'] * i['avg_cost'] / 10000) for i in investment_trust_items)
    it_profit = it_total - it_cost

    # 全資産合計
    total_assets = jp_total + us_total_jpy + cash_total + gold_total + crypto_total + it_total
    total_profit = jp_profit + us_profit_jpy + gold_profit + crypto_profit + it_profit

    return render_template(
        'dashboard.html', 
        user_name=session.get('username', ''),
        jp_stocks=jp_stocks,
        jp_total=jp_total,
        jp_profit=jp_profit,
        us_stocks=us_stocks,
        us_total_usd=us_total_usd,
        us_total_jpy=us_total_jpy,
        us_profit_jpy=us_profit_jpy,
        cash_items=cash_items,
        cash_total=cash_total,
        gold_items=gold_items,
        gold_total=gold_total,
        gold_profit=gold_profit,
        crypto_items=crypto_items,
        crypto_total=crypto_total,
        crypto_profit=crypto_profit,
        investment_trust_items=investment_trust_items,
        investment_trust_total=it_total,
        investment_trust_profit=it_profit,
        total_assets=total_assets,
        total_profit=total_profit
    )


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
        'crypto': {'title': '暗号資産', 'symbol_label': '銘柄', 'quantity_label': '数量'},
        'investment_trust': {'title': '投資信託', 'symbol_label': '銘柄', 'quantity_label': '保有数量(口)'}
    }
    
    info = type_info.get(asset_type, type_info['jp_stock'])
    
    return render_template(
        'manage_assets.html', 
        assets=assets, 
        asset_type=asset_type, 
        info=info, 
        crypto_symbols=CRYPTO_SYMBOLS,
        investment_trust_symbols=INVESTMENT_TRUST_SYMBOLS
    )

@app.route('/add_asset', methods=['POST'])
def add_asset():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_type = request.form['asset_type']
    symbol = request.form['symbol'].strip()
    if asset_type in ['us_stock', 'crypto']:
        symbol = symbol.upper()

    name = request.form.get('name', '').strip()
    quantity = float(request.form['quantity'])
    avg_cost = float(request.form.get('avg_cost', 0)) if request.form.get('avg_cost') else 0
    
    price = 0
    if asset_type == 'gold':
        price = get_gold_price()
        if not name: name = "金 (Gold)"
    elif asset_type == 'crypto':
        if symbol not in CRYPTO_SYMBOLS:
            flash('対応していない暗号資産です', 'error')
            return redirect(url_for('manage_assets', asset_type='crypto'))
        price = get_crypto_price(symbol)
        name = name or symbol
    elif asset_type == 'investment_trust':
        if symbol not in INVESTMENT_TRUST_SYMBOLS:
            flash('対応していない投資信託です', 'error')
            return redirect(url_for('manage_assets', asset_type='investment_trust'))
        price = get_investment_trust_price(symbol)
        name = name or symbol
    elif asset_type != 'cash':
        is_jp = (asset_type == 'jp_stock')
        try:
            stock_info = get_jp_stock_info(symbol) if is_jp else get_us_stock_info(symbol)
            price = stock_info['price']
            if not name: name = stock_info['name']
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
        # 既存アセットに数量を追加する場合
        old_quantity = existing['quantity'] or 0
        old_avg_cost = existing['avg_cost'] or 0
        new_total_quantity = old_quantity + quantity
        
        if new_total_quantity > 0 and avg_cost > 0:
            # 加重平均で新しい平均取得単価を計算
            new_avg_cost = ((old_quantity * old_avg_cost) + (quantity * avg_cost)) / new_total_quantity
        else:
            new_avg_cost = old_avg_cost if old_avg_cost > 0 else avg_cost
        
        update_name = name if name else existing.get('name', symbol)

        if USE_POSTGRES:
            c.execute('''UPDATE assets SET quantity = %s, price = %s, name = %s, avg_cost = %s
                        WHERE id = %s''', (new_total_quantity, price, update_name, new_avg_cost, existing['id']))
        else:
            c.execute('''UPDATE assets SET quantity = ?, price = ?, name = ?, avg_cost = ?
                        WHERE id = ?''', (new_total_quantity, price, update_name, new_avg_cost, existing['id']))
        
        flash(f'{symbol} を更新しました', 'success')

    elif existing and asset_type == 'cash':
        # 現金は単純に上書き
        if USE_POSTGRES:
            c.execute('''UPDATE assets SET quantity = %s WHERE id = %s''', (quantity, existing['id']))
        else:
            c.execute('''UPDATE assets SET quantity = ? WHERE id = ?''', (quantity, existing['id']))
        flash(f'{symbol} を更新しました', 'success')
    else:
        # 新規追加
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
        'crypto': {'title': '暗号資産', 'symbol_label': '銘柄', 'quantity_label': '数量'},
        'investment_trust': {'title': '投資信託', 'symbol_label': '銘柄', 'quantity_label': '保有数量(口)'}
    }
    
    info = type_info.get(asset['asset_type'], type_info['jp_stock'])
    
    return render_template('edit_asset.html', asset=asset, info=info)

@app.route('/update_asset', methods=['POST'])
def update_asset():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_id = request.form['asset_id']
    symbol = request.form['symbol'].strip()
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
    if asset_type in ['us_stock', 'crypto']:
        symbol = symbol.upper()

    # 価格取得ロジックは add_asset と同様
    price = 0
    if asset_type == 'gold':
        price = get_gold_price()
        if not name: name = "金 (Gold)"
    elif asset_type == 'crypto':
        if symbol not in CRYPTO_SYMBOLS:
            flash('対応していない暗号資産です', 'error')
            conn.close()
            return redirect(url_for('manage_assets', asset_type='crypto'))
        price = get_crypto_price(symbol)
        if not name: name = symbol
    elif asset_type == 'investment_trust':
        if symbol not in INVESTMENT_TRUST_SYMBOLS:
            flash('対応していない投資信託です', 'error')
            conn.close()
            return redirect(url_for('manage_assets', asset_type='investment_trust'))
        price = get_investment_trust_price(symbol)
        if not name: name = symbol
    elif asset_type != 'cash':
        is_jp = (asset_type == 'jp_stock')
        try:
            stock_info = get_jp_stock_info(symbol) if is_jp else get_us_stock_info(symbol)
            price = stock_info['price']
            if not name: name = stock_info['name']
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
        return ('Unauthorized', 401)
    
    asset_type = request.form.get('asset_type')
    if not asset_type:
        return ('Bad Request', 400)

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
    
    assets_to_update = c.fetchall()
    
    update_funcs = {
        'gold': get_gold_price,
        'crypto': get_crypto_price,
        'jp_stock': lambda symbol: get_stock_price(symbol, is_jp=True),
        'us_stock': lambda symbol: get_stock_price(symbol, is_jp=False),
        'investment_trust': get_investment_trust_price
    }

    price_func = update_funcs.get(asset_type)
    if not price_func:
        conn.close()
        return ('Invalid asset type', 400)

    for asset in assets_to_update:
        try:
            # gold は symbol を引数に取らないので場合分け
            price = price_func() if asset_type == 'gold' else price_func(asset['symbol'])
            if price is not None and price > 0:
                if USE_POSTGRES:
                    c.execute('UPDATE assets SET price = %s WHERE id = %s', (price, asset['id']))
                else:
                    c.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
            time.sleep(0.5)  # レート制限対策
        except Exception as e:
            print(f"Failed to update price for {asset['symbol']} ({asset_type}): {e}")
            
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

    asset_types_to_update = ['jp_stock', 'us_stock', 'gold', 'crypto', 'investment_trust']

    for asset_type in asset_types_to_update:
        if USE_POSTGRES:
            c.execute('SELECT id, symbol FROM assets WHERE user_id = %s AND asset_type = %s', (user['id'], asset_type))
        else:
            c.execute('SELECT id, symbol FROM assets WHERE user_id = ? AND asset_type = ?', (user['id'], asset_type))
        
        assets = c.fetchall()

        for asset in assets:
            price = 0
            try:
                if asset_type == 'jp_stock':
                    price = get_stock_price(asset['symbol'], is_jp=True)
                elif asset_type == 'us_stock':
                    price = get_stock_price(asset['symbol'], is_jp=False)
                elif asset_type == 'gold':
                    price = get_gold_price()
                elif asset_type == 'crypto':
                    price = get_crypto_price(asset['symbol'])
                elif asset_type == 'investment_trust':
                    price = get_investment_trust_price(asset['symbol'])

                if price > 0:
                    if USE_POSTGRES:
                        c.execute('UPDATE assets SET price = %s WHERE id = %s', (price, asset['id']))
                    else:
                        c.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
                
                time.sleep(1) # APIへの負荷軽減
            except Exception as e:
                print(f"Error updating all prices for {asset['symbol']} ({asset_type}): {e}")
                pass
    
    conn.commit()
    conn.close()
    
    flash('全ての価格を更新しました', 'success')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
