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

# PostgreSQLサポート
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

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

def get_gold_price():
    """金価格を取得（デフォルト値を返す）"""
    try:
        # 簡易実装: 固定値または外部APIから取得
        return 10000  # 仮の金価格（円/g）
    except Exception as e:
        print(f"Error getting gold price: {e}")
        return 10000

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
    
    total_assets = jp_total + us_total_jpy + cash_total + gold_total
    total_cost = jp_cost_total + (us_cost_total_usd * usd_jpy) + cash_total + gold_cost_total
    total_profit = total_assets - total_cost
    
    return render_template('dashboard.html', 
                         jp_stocks=jp_stocks, us_stocks=us_stocks, 
                         cash_items=cash_items, gold_items=gold_items,
                         jp_total=jp_total, jp_profit=jp_profit,
                         us_total_usd=us_total_usd, us_total_jpy=us_total_jpy, 
                         us_profit_usd=us_profit_usd, us_profit_jpy=us_profit_jpy,
                         cash_total=cash_total, gold_total=gold_total, 
                         gold_profit=gold_profit,
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
        'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'}
    }
    
    info = type_info.get(asset_type, type_info['jp_stock'])
    
    return render_template('manage_assets.html', assets=assets, asset_type=asset_type, info=info)

@app.route('/add_asset', methods=['POST'])
def add_asset():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_type = request.form['asset_type']
    symbol = request.form['symbol'].strip().upper()
    name = request.form.get('name', '').strip()
    quantity = float(request.form['quantity'])
    avg_cost = float(request.form.get('avg_cost', 0)) if request.form.get('avg_cost') else 0
    
    price = 0
    if asset_type == 'gold':
        price = get_gold_price()
        if not name:
            name = "金 (Gold)"
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
        'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'}
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
            pass
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
    
    conn.commit()
    conn.close()
    
    flash('全ての価格を更新しました', 'success')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
