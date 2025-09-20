from flask import Flask, render_template_string, request, redirect, url_for, session, flash
import requests
import json
import os
from datetime import datetime, timezone, timedelta
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import re
from bs4 import BeautifulSoup
import time

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this')

# データベースの初期化
def init_db():
    conn = sqlite3.connect('portfolio.db')
    c = conn.cursor()
    
    # ユーザーテーブル
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL
    )''')
    
    # まず既存のテーブル構造を確認
    c.execute("PRAGMA table_info(assets)")
    existing_columns = [row[1] for row in c.fetchall()]
    
    if not existing_columns:
        # 新しいテーブルを作成
        c.execute('''CREATE TABLE assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            asset_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            quantity REAL NOT NULL,
            avg_price REAL DEFAULT 0,
            current_price REAL DEFAULT 0,
            total_cost REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )''')
    else:
        # 既存テーブルに新しいカラムを追加
        if 'avg_price' not in existing_columns:
            try:
                c.execute("ALTER TABLE assets ADD COLUMN avg_price REAL DEFAULT 0")
            except:
                pass
        
        if 'current_price' not in existing_columns:
            try:
                c.execute("ALTER TABLE assets ADD COLUMN current_price REAL DEFAULT 0")
            except:
                pass
        
        if 'total_cost' not in existing_columns:
            try:
                c.execute("ALTER TABLE assets ADD COLUMN total_cost REAL DEFAULT 0")
            except:
                pass
        
        if 'updated_at' not in existing_columns:
            try:
                c.execute("ALTER TABLE assets ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            except:
                pass
        
        # 既存のpriceカラムがある場合、current_priceに移行
        if 'price' in existing_columns:
            try:
                c.execute("UPDATE assets SET current_price = price WHERE current_price = 0 AND price > 0")
                c.execute("UPDATE assets SET avg_price = price WHERE avg_price = 0 AND price > 0")
                c.execute("UPDATE assets SET total_cost = quantity * price WHERE total_cost = 0 AND price > 0")
            except Exception as e:
                print(f"Migration error: {e}")
    
    # 取引履歴テーブル
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        asset_id INTEGER,
        transaction_type TEXT NOT NULL,
        symbol TEXT NOT NULL,
        quantity REAL NOT NULL,
        price REAL NOT NULL,
        total_amount REAL NOT NULL,
        transaction_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id),
        FOREIGN KEY (asset_id) REFERENCES assets (id)
    )''')
    
    # デフォルトユーザー作成（開発用）
    c.execute("SELECT id FROM users WHERE username = 'demo'")
    if not c.fetchone():
        demo_hash = generate_password_hash('demo123')
        c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", 
                 ('demo', demo_hash))
    
    conn.commit()
    conn.close()

def get_db():
    """データベース接続を取得"""
    conn = sqlite3.connect('portfolio.db')
    conn.row_factory = sqlite3.Row
    return conn

def get_current_user():
    """現在のユーザーを取得"""
    if 'user_id' not in session:
        return None
    
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    return user

def scrape_yahoo_finance_jp(code):
    """Yahoo Finance APIから日本株の情報を取得"""
    try:
        # まずYahoo Finance APIを試行
        api_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.T"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        session = requests.Session()
        api_response = session.get(api_url, headers=headers, timeout=10)
        
        if api_response.status_code == 200:
            try:
                data = api_response.json()
                if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                    result = data['chart']['result'][0]
                    
                    # 現在価格を取得
                    price = 0
                    if 'meta' in result:
                        meta = result['meta']
                        price = (meta.get('regularMarketPrice') or 
                                meta.get('previousClose') or 
                                meta.get('chartPreviousClose') or 0)
                    
                    # 会社名を取得
                    name = f"Stock {code}"
                    if 'meta' in result and 'shortName' in result['meta']:
                        name = result['meta']['shortName']
                    elif 'meta' in result and 'longName' in result['meta']:
                        name = result['meta']['longName']
                    
                    if price > 0:
                        print(f"API success for {code}: price={price}, name={name}")
                        return {
                            'name': name,
                            'price': round(float(price), 2)
                        }
            except Exception as e:
                print(f"API parsing error for {code}: {e}")
        
        # APIが失敗した場合、スクレイピングにフォールバック
        print(f"Falling back to scraping for {code}")
        url = f"https://finance.yahoo.co.jp/quote/{code}.T"
        response = session.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # より具体的なパターンで株価を検索
        price = 0
        html_text = soup.get_text()
        
        # 1. 最低購入代金から逆算する方法
        min_purchase_match = re.search(r'最低購入代金[:\s]*([0-9,]+)', html_text)
        unit_match = re.search(r'単元株数[:\s]*([0-9,]+)株', html_text)
        
        if min_purchase_match and unit_match:
            try:
                min_purchase = int(min_purchase_match.group(1).replace(',', ''))
                unit_shares = int(unit_match.group(1).replace(',', ''))
                if unit_shares > 0:
                    price = min_purchase / unit_shares
                    print(f"Calculated price from min purchase for {code}: {price}")
            except:
                pass
        
        # 2. 直接的な価格パターンを検索（証券コードを除外）
        if price == 0:
            # 証券コードの数字を避けるため、より具体的なパターンを使用
            price_patterns = [
                rf'現在値[:\s]*([0-9,]+)(?!.*{code})',  # 現在値の後に証券コードがないもの
                r'株価[:\s]*([0-9,]+)円',  # 「円」が明示されているもの
                r'(\d{3,4})\s*円(?!.*コード)',  # 3-4桁の数字の後に「円」、「コード」が後に続かないもの
            ]
            
            for pattern in price_patterns:
                matches = re.findall(pattern, html_text)
                if matches:
                    for match in matches:
                        try:
                            potential_price = float(match.replace(',', ''))
                            # より現実的な株価範囲（50円〜50万円）
                            if 50 <= potential_price <= 500000:
                                # 証券コードと同じでないかチェック
                                if str(int(potential_price)) != code:
                                    price = potential_price
                                    print(f"Found price pattern for {code}: {price}")
                                    break
                        except ValueError:
                            continue
                    if price > 0:
                        break
        
        # 会社名を取得
        name = f"Stock {code}"
        name_match = re.search(rf'([^【\n]+)【{code}】', html_text)
        if name_match:
            name = name_match.group(1).strip()
        
        print(f"Final scraping result for {code}: name={name}, price={price}")
        
        return {
            'name': name,
            'price': round(price, 2) if price else 0
        }
        
    except Exception as e:
        print(f"Error getting JP stock {code}: {e}")
        return {'name': f'Stock {code}', 'price': 0}

def scrape_yahoo_finance_us(symbol):
    """Yahoo Finance APIから米国株の情報を取得"""
    try:
        # Yahoo Finance APIを使用
        api_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        session = requests.Session()
        api_response = session.get(api_url, headers=headers, timeout=10)
        
        if api_response.status_code == 200:
            try:
                data = api_response.json()
                if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                    result = data['chart']['result'][0]
                    
                    # 現在価格を取得
                    price = 0
                    if 'meta' in result:
                        meta = result['meta']
                        price = (meta.get('regularMarketPrice') or 
                                meta.get('previousClose') or 
                                meta.get('chartPreviousClose') or 0)
                    
                    # 会社名を取得
                    name = symbol.upper()
                    if 'meta' in result and 'shortName' in result['meta']:
                        name = result['meta']['shortName']
                    elif 'meta' in result and 'longName' in result['meta']:
                        name = result['meta']['longName']
                    
                    if price > 0:
                        print(f"API success for {symbol}: price={price}, name={name}")
                        return {
                            'name': name,
                            'price': round(float(price), 2)
                        }
            except Exception as e:
                print(f"API parsing error for {symbol}: {e}")
        
        # APIが失敗した場合、スクレイピングにフォールバック
        print(f"Falling back to scraping for {symbol}")
        url = f"https://finance.yahoo.co.jp/quote/{symbol.upper()}"
        response = session.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        price = 0
        html_text = soup.get_text()
        
        # USD価格のより具体的なパターン
        price_patterns = [
            r'現在値[:\s]*\$?([0-9,]+\.?[0-9]*)',
            r'株価[:\s]*\$?([0-9,]+\.?[0-9]*)',
            r'\$([0-9,]+\.?[0-9]*)',
            r'([0-9,]+\.?[0-9]*)\s*(?:USD|ドル)'
        ]
        
        for pattern in price_patterns:
            matches = re.findall(pattern, html_text)
            if matches:
                for match in matches:
                    try:
                        potential_price = float(match.replace(',', ''))
                        # 妥当な株価範囲をチェック（0.1ドル〜10万ドル）
                        if 0.1 <= potential_price <= 100000:
                            price = potential_price
                            print(f"Found scraping price for {symbol}: {price}")
                            break
                    except ValueError:
                        continue
                if price > 0:
                    break
        
        # 会社名を取得
        name = symbol.upper()
        name_match = re.search(rf'([^【\n]+)【{symbol.upper()}】', html_text)
        if name_match:
            name = name_match.group(1).strip()
        
        print(f"Final scraping result for {symbol}: name={name}, price={price}")
        
        return {
            'name': name,
            'price': round(price, 2) if price else 0
        }
        
    except Exception as e:
        print(f"Error getting US stock {symbol}: {e}")
        return {'name': symbol.upper(), 'price': 0}

def get_jp_stock_info(code):
    """日本株の情報を取得（スクレイピング使用）"""
    return scrape_yahoo_finance_jp(code)

def get_us_stock_info(symbol):
    """米国株の情報を取得（スクレイピング使用）"""
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
    """USD/JPY レートを取得（Yahoo Financeからスクレイピング）"""
    try:
        url = "https://finance.yahoo.co.jp/quote/USDJPY=X"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ja-JP,ja;q=0.8,en-US;q=0.5,en;q=0.3'
        }
        
        session = requests.Session()
        response = session.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # レートを取得
        html_text = soup.get_text()
        rate_patterns = [
            r'(\d{2,3}\.\d{1,4})\s*円',  # XXX.XXXX円
            r'USD/JPY[:\s]*(\d{2,3}\.\d{1,4})',
            r'(\d{2,3}\.\d{1,4})\s*JPY',
            r'現在値[:\s]*(\d{2,3}\.\d{1,4})'
        ]
        
        for pattern in rate_patterns:
            matches = re.findall(pattern, html_text)
            if matches:
                for match in matches:
                    try:
                        rate = float(match)
                        if 100 <= rate <= 200:  # 妥当なUSD/JPYレート範囲
                            print(f"Found USD/JPY rate: {rate}")
                            return rate
                    except ValueError:
                        continue
        
        # APIエンドポイントを試行
        try:
            api_url = "https://query1.finance.yahoo.com/v8/finance/chart/USDJPY=X"
            api_response = session.get(api_url, headers=headers, timeout=10)
            if api_response.status_code == 200:
                data = api_response.json()
                if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
                    result = data['chart']['result'][0]
                    if 'meta' in result and 'regularMarketPrice' in result['meta']:
                        rate = float(result['meta']['regularMarketPrice'])
                        print(f"Found API USD/JPY rate: {rate}")
                        return rate
        except:
            pass
        
        print("Failed to get USD/JPY rate, using default")
        return 150.0  # デフォルト値
        
    except Exception as e:
        print(f"Error getting USD/JPY rate: {e}")
        return 150.0

def update_asset_avg_price(asset_id, new_quantity, new_price):
    """平均取得単価を更新"""
    conn = get_db()
    
    # 現在の資産情報を取得
    asset = conn.execute('''
        SELECT quantity, avg_price, total_cost FROM assets WHERE id = ?
    ''', (asset_id,)).fetchone()
    
    if asset:
        # 新しい平均取得単価を計算
        old_quantity = asset['quantity']
        old_avg_price = asset['avg_price']
        old_total_cost = asset['total_cost']
        
        new_total_cost = old_total_cost + (new_quantity * new_price)
        new_total_quantity = old_quantity + new_quantity
        
        if new_total_quantity > 0:
            new_avg_price = new_total_cost / new_total_quantity
        else:
            new_avg_price = 0
            new_total_cost = 0
        
        # データベースを更新
        conn.execute('''
            UPDATE assets SET quantity = ?, avg_price = ?, total_cost = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_total_quantity, new_avg_price, new_total_cost, asset_id))
    
    conn.commit()
    conn.close()

# アプリケーション開始時にDB初期化
init_db()

@app.route('/')
def index():
    """ホームページ"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """ログインページ"""
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('ログインしました', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('ユーザー名またはパスワードが間違っています', 'error')
    
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ログイン - 資産管理</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .login-container { max-width: 400px; margin: 50px auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .form-group { margin-bottom: 20px; }
            label { display: block; margin-bottom: 5px; font-weight: bold; }
            input[type="text"], input[type="password"] { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
            button { width: 100%; padding: 12px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }
            button:hover { background: #0056b3; }
            .alert { padding: 10px; margin-bottom: 20px; border-radius: 4px; }
            .alert-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .demo-info { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; padding: 15px; margin-bottom: 20px; border-radius: 4px; }
        </style>
    </head>
    <body>
        <div class="login-container">
            <h1>資産管理システム</h1>
            <div class="demo-info">
                <strong>デモ用ログイン情報:</strong><br>
                ユーザー名: demo<br>
                パスワード: demo123
            </div>
            
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            
            <form method="post">
                <div class="form-group">
                    <label for="username">ユーザー名</label>
                    <input type="text" id="username" name="username" required>
                </div>
                <div class="form-group">
                    <label for="password">パスワード</label>
                    <input type="password" id="password" name="password" required>
                </div>
                <button type="submit">ログイン</button>
            </form>
        </div>
    </body>
    </html>
    """
    return render_template_string(template)

@app.route('/logout')
def logout():
    """ログアウト"""
    session.clear()
    flash('ログアウトしました', 'success')
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    """メインダッシュボード"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    
    # 各資産タイプの合計を計算
    jp_stocks = conn.execute('''
        SELECT symbol, name, quantity, 
               COALESCE(current_price, 0) as current_price,
               COALESCE(avg_price, 0) as avg_price,
               COALESCE(total_cost, 0) as total_cost
        FROM assets 
        WHERE user_id = ? AND asset_type = "jp_stock"
    ''', (user['id'],)).fetchall()
    
    us_stocks = conn.execute('''
        SELECT symbol, name, quantity,
               COALESCE(current_price, 0) as current_price,
               COALESCE(avg_price, 0) as avg_price,
               COALESCE(total_cost, 0) as total_cost
        FROM assets 
        WHERE user_id = ? AND asset_type = "us_stock"
    ''', (user['id'],)).fetchall()
    
    cash_items = conn.execute('''
        SELECT symbol as label, quantity as amount FROM assets 
        WHERE user_id = ? AND asset_type = "cash"
    ''', (user['id'],)).fetchall()
    
    gold_items = conn.execute('''
        SELECT symbol, name, quantity,
               COALESCE(current_price, 0) as current_price,
               COALESCE(avg_price, 0) as avg_price,
               COALESCE(total_cost, 0) as total_cost
        FROM assets 
        WHERE user_id = ? AND asset_type = "gold"
    ''', (user['id'],)).fetchall()
    
    conn.close()
    
    # 合計計算と損益計算
    jp_market_value = sum(stock['quantity'] * stock['current_price'] for stock in jp_stocks)
    jp_cost = sum(stock['total_cost'] for stock in jp_stocks)
    jp_pnl = jp_market_value - jp_cost
    
    us_market_value_usd = sum(stock['quantity'] * stock['current_price'] for stock in us_stocks)
    us_cost_usd = sum(stock['total_cost'] for stock in us_stocks)
    us_pnl_usd = us_market_value_usd - us_cost_usd
    
    usd_jpy = get_usd_jpy_rate()
    us_market_value_jpy = us_market_value_usd * usd_jpy
    us_cost_jpy = us_cost_usd * usd_jpy
    us_pnl_jpy = us_pnl_usd * usd_jpy
    
    cash_total = sum(item['amount'] for item in cash_items)
    
    gold_market_value = sum(item['quantity'] * item['current_price'] for item in gold_items)
    gold_cost = sum(item['total_cost'] for item in gold_items)
    gold_pnl = gold_market_value - gold_cost
    
    total_market_value = jp_market_value + us_market_value_jpy + cash_total + gold_market_value
    total_cost = jp_cost + us_cost_jpy + gold_cost + cash_total
    total_pnl = total_market_value - total_cost
    
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>資産ダッシュボード</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 1400px; margin: 0 auto; }
            .header { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .nav-links { margin: 20px 0; }
            .nav-links a { display: inline-block; margin-right: 15px; padding: 8px 16px; background: #007bff; color: white; text-decoration: none; border-radius: 4px; }
            .nav-links a:hover { background: #0056b3; }
            .asset-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin: 20px 0; }
            .asset-card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .asset-card h3 { margin: 0 0 15px 0; color: #333; border-bottom: 2px solid #007bff; padding-bottom: 5px; }
            .pnl-section { margin: 15px 0; }
            .pnl-row { display: flex; justify-content: space-between; margin: 8px 0; padding: 5px 0; }
            .pnl-label { font-weight: bold; color: #555; }
            .pnl-value { font-weight: bold; }
            .market-value { color: #007bff; font-size: 18px; }
            .cost-value { color: #666; }
            .pnl-positive { color: #28a745; }
            .pnl-negative { color: #dc3545; }
            .pnl-neutral { color: #6c757d; }
            .total-card { background: linear-gradient(135deg, #28a745, #20c997); color: white; grid-column: 1 / -1; text-align: center; }
            .total-card .pnl-row { border-bottom: 1px solid rgba(255,255,255,0.3); }
            .total-card .pnl-value { color: white; }
            .rate-info { color: #666; font-size: 14px; margin: 10px 0; }
            .user-info { float: right; }
            .logout-btn { background: #dc3545; }
            .logout-btn:hover { background: #c82333; }
            .percentage { font-size: 14px; margin-left: 10px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>資産ダッシュボード
                    <span class="user-info">
                        {{ session.username }} さん
                        <a href="{{ url_for('logout') }}" class="nav-links logout-btn" style="margin: 0 0 0 10px;">ログアウト</a>
                    </span>
                </h1>
                <div class="rate-info">USD/JPY: {{ "{:.2f}".format(usd_jpy) }} 円</div>
            </div>
            
            <div class="nav-links">
                <a href="{{ url_for('manage_assets', asset_type='jp_stock') }}">日本株</a>
                <a href="{{ url_for('manage_assets', asset_type='us_stock') }}">米国株</a>
                <a href="{{ url_for('manage_assets', asset_type='gold') }}">金</a>
                <a href="{{ url_for('manage_assets', asset_type='cash') }}">現金</a>
                <a href="{{ url_for('pnl_analysis') }}">損益詳細</a>
            </div>
            
            <div class="asset-grid">
                <div class="asset-card">
                    <h3>日本株</h3>
                    <div class="pnl-section">
                        <div class="pnl-row">
                            <span class="pnl-label">時価総額:</span>
                            <span class="pnl-value market-value">{{ "{:,.0f}".format(jp_market_value) }} 円</span>
                        </div>
                        <div class="pnl-row">
                            <span class="pnl-label">取得価格:</span>
                            <span class="pnl-value cost-value">{{ "{:,.0f}".format(jp_cost) }} 円</span>
                        </div>
                        <div class="pnl-row">
                            <span class="pnl-label">損益:</span>
                            <span class="pnl-value {{ 'pnl-positive' if jp_pnl > 0 else 'pnl-negative' if jp_pnl < 0 else 'pnl-neutral' }}">
                                {{ "{:+,.0f}".format(jp_pnl) }} 円
                                {% if jp_cost > 0 %}
                                <span class="percentage">({{ "{:+.2f}".format((jp_pnl/jp_cost)*100) }}%)</span>
                                {% endif %}
                            </span>
                        </div>
                        <div style="margin-top: 10px; color: #666; font-size: 14px;">{{ jp_stocks|length }} 銘柄</div>
                    </div>
                </div>
                
                <div class="asset-card">
                    <h3>米国株</h3>
                    <div class="pnl-section">
                        <div class="pnl-row">
                            <span class="pnl-label">時価総額:</span>
                            <span class="pnl-value market-value">${{ "{:,.2f}".format(us_market_value_usd) }} ({{ "{:,.0f}".format(us_market_value_jpy) }} 円)</span>
                        </div>
                        <div class="pnl-row">
                            <span class="pnl-label">取得価格:</span>
                            <span class="pnl-value cost-value">${{ "{:,.2f}".format(us_cost_usd) }} ({{ "{:,.0f}".format(us_cost_jpy) }} 円)</span>
                        </div>
                        <div class="pnl-row">
                            <span class="pnl-label">損益:</span>
                            <span class="pnl-value {{ 'pnl-positive' if us_pnl_usd > 0 else 'pnl-negative' if us_pnl_usd < 0 else 'pnl-neutral' }}">
                                {{ "{:+,.2f}".format(us_pnl_usd) }} USD ({{ "{:+,.0f}".format(us_pnl_jpy) }} 円)
                                {% if us_cost_usd > 0 %}
                                <span class="percentage">({{ "{:+.2f}".format((us_pnl_usd/us_cost_usd)*100) }}%)</span>
                                {% endif %}
                            </span>
                        </div>
                        <div style="margin-top: 10px; color: #666; font-size: 14px;">{{ us_stocks|length }} 銘柄</div>
                    </div>
                </div>
                
                <div class="asset-card">
                    <h3>金 (Gold)</h3>
                    <div class="pnl-section">
                        <div class="pnl-row">
                            <span class="pnl-label">時価総額:</span>
                            <span class="pnl-value market-value">{{ "{:,.0f}".format(gold_market_value) }} 円</span>
                        </div>
                        <div class="pnl-row">
                            <span class="pnl-label">取得価格:</span>
                            <span class="pnl-value cost-value">{{ "{:,.0f}".format(gold_cost) }} 円</span>
                        </div>
                        <div class="pnl-row">
                            <span class="pnl-label">損益:</span>
                            <span class="pnl-value {{ 'pnl-positive' if gold_pnl > 0 else 'pnl-negative' if gold_pnl < 0 else 'pnl-neutral' }}">
                                {{ "{:+,.0f}".format(gold_pnl) }} 円
                                {% if gold_cost > 0 %}
                                <span class="percentage">({{ "{:+.2f}".format((gold_pnl/gold_cost)*100) }}%)</span>
                                {% endif %}
                            </span>
                        </div>
                        <div style="margin-top: 10px; color: #666; font-size: 14px;">{{ gold_items|length }} 項目</div>
                    </div>
                </div>
                
                <div class="asset-card">
                    <h3>現金</h3>
                    <div class="pnl-section">
                        <div class="pnl-row">
                            <span class="pnl-label">残高:</span>
                            <span class="pnl-value market-value">{{ "{:,.0f}".format(cash_total) }} 円</span>
                        </div>
                        <div style="margin-top: 10px; color: #666; font-size: 14px;">{{ cash_items|length }} 項目</div>
                    </div>
                </div>
                
                <div class="asset-card total-card">
                    <h3>総資産 - ポートフォリオサマリー</h3>
                    <div class="pnl-section">
                        <div class="pnl-row">
                            <span class="pnl-label">総時価:</span>
                            <span class="pnl-value" style="font-size: 24px;">{{ "{:,.0f}".format(total_market_value) }} 円</span>
                        </div>
                        <div class="pnl-row">
                            <span class="pnl-label">総投資額:</span>
                            <span class="pnl-value">{{ "{:,.0f}".format(total_cost) }} 円</span>
                        </div>
                        <div class="pnl-row">
                            <span class="pnl-label">総損益:</span>
                            <span class="pnl-value" style="font-size: 20px;">
                                {{ "{:+,.0f}".format(total_pnl) }} 円
                                {% if total_cost > 0 %}
                                <span class="percentage" style="font-size: 16px;">({{ "{:+.2f}".format((total_pnl/(total_cost-cash_total))*100) if (total_cost-cash_total) > 0 else '0.00' }}%)</span>
                                {% endif %}
                            </span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return render_template_string(template, 
                                jp_stocks=jp_stocks, us_stocks=us_stocks, cash_items=cash_items, gold_items=gold_items,
                                jp_market_value=jp_market_value, jp_cost=jp_cost, jp_pnl=jp_pnl,
                                us_market_value_usd=us_market_value_usd, us_cost_usd=us_cost_usd, us_pnl_usd=us_pnl_usd,
                                us_market_value_jpy=us_market_value_jpy, us_cost_jpy=us_cost_jpy, us_pnl_jpy=us_pnl_jpy,
                                cash_total=cash_total, gold_market_value=gold_market_value, gold_cost=gold_cost, gold_pnl=gold_pnl,
                                total_market_value=total_market_value, total_cost=total_cost, total_pnl=total_pnl,
                                usd_jpy=usd_jpy, session=session)

@app.route('/pnl_analysis')
def pnl_analysis():
    """詳細損益分析ページ"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    
    # 全ての投資資産を取得
    all_assets = conn.execute('''
        SELECT *, 
               (quantity * COALESCE(current_price, 0)) as market_value,
               (quantity * COALESCE(current_price, 0) - COALESCE(total_cost, 0)) as unrealized_pnl,
               CASE 
                   WHEN COALESCE(total_cost, 0) > 0 THEN ((quantity * COALESCE(current_price, 0) - COALESCE(total_cost, 0)) / COALESCE(total_cost, 1) * 100)
                   ELSE 0 
               END as pnl_percentage
        FROM assets 
        WHERE user_id = ? AND asset_type != "cash"
        ORDER BY asset_type, symbol
    ''', (user['id'],)).fetchall()
    
    conn.close()
    
    usd_jpy = get_usd_jpy_rate()
    
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>損益詳細分析</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 1400px; margin: 0 auto; }
            .header { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .back-link { color: #007bff; text-decoration: none; margin-bottom: 10px; display: inline-block; }
            .back-link:hover { text-decoration: underline; }
            .pnl-table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
            .pnl-table th, .pnl-table td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; }
            .pnl-table th { background: #f8f9fa; font-weight: bold; color: #333; }
            .text-right { text-align: right; }
            .text-center { text-align: center; }
            .pnl-positive { color: #28a745; font-weight: bold; }
            .pnl-negative { color: #dc3545; font-weight: bold; }
            .pnl-neutral { color: #6c757d; }
            .asset-type-header { background: #007bff; color: white; }
            .jp-stock { border-left: 4px solid #007bff; }
            .us-stock { border-left: 4px solid #28a745; }
            .gold { border-left: 4px solid #ffc107; }
            .summary-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 15px; margin-bottom: 20px; }
            .summary-card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); text-align: center; }
            .summary-card h4 { margin: 0 0 10px 0; color: #333; }
            .summary-value { font-size: 20px; font-weight: bold; }
            .rate-info { color: #666; font-size: 14px; margin: 10px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <a href="{{ url_for('dashboard') }}" class="back-link">← ダッシュボードに戻る</a>
                <h1>損益詳細分析</h1>
                <div class="rate-info">USD/JPY: {{ "{:.2f}".format(usd_jpy) }} 円</div>
            </div>
            
            {% if all_assets %}
            <table class="pnl-table">
                <thead>
                    <tr>
                        <th>資産タイプ</th>
                        <th>銘柄/商品</th>
                        <th>名称</th>
                        <th class="text-right">保有数量</th>
                        <th class="text-right">平均取得単価</th>
                        <th class="text-right">現在価格</th>
                        <th class="text-right">投資金額</th>
                        <th class="text-right">時価評価額</th>
                        <th class="text-right">未実現損益</th>
                        <th class="text-right">損益率</th>
                    </tr>
                </thead>
                <tbody>
                    {% for asset in all_assets %}
                    <tr class="{{ asset.asset_type.replace('_', '-') }}">
                        <td>
                            {% if asset.asset_type == 'jp_stock' %}日本株
                            {% elif asset.asset_type == 'us_stock' %}米国株
                            {% elif asset.asset_type == 'gold' %}金
                            {% endif %}
                        </td>
                        <td><strong>{{ asset.symbol }}</strong></td>
                        <td>{{ asset.name or '-' }}</td>
                        <td class="text-right">{{ "{:,.2f}".format(asset.quantity) }}</td>
                        <td class="text-right">
                            {% if asset.asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.avg_price) }}
                            {% else %}
                                {{ "{:,.0f}".format(asset.avg_price) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right">
                            {% if asset.asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.current_price) }}
                            {% else %}
                                {{ "{:,.0f}".format(asset.current_price) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right">
                            {% if asset.asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.total_cost) }}<br>
                                <small>({{ "{:,.0f}".format(asset.total_cost * usd_jpy) }} 円)</small>
                            {% else %}
                                {{ "{:,.0f}".format(asset.total_cost) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right">
                            {% if asset.asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.market_value) }}<br>
                                <small>({{ "{:,.0f}".format(asset.market_value * usd_jpy) }} 円)</small>
                            {% else %}
                                {{ "{:,.0f}".format(asset.market_value) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right {{ 'pnl-positive' if asset.unrealized_pnl > 0 else 'pnl-negative' if asset.unrealized_pnl < 0 else 'pnl-neutral' }}">
                            {% if asset_type == 'us_stock' %}
                                {{ "{:+,.2f}".format(asset.unrealized_pnl) }} USD
                                <br><small>({{ "{:+,.0f}".format(asset.unrealized_pnl * usd_jpy) }} 円)</small>
                            {% else %}
                                {{ "{:+,.0f}".format(asset.unrealized_pnl) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right {{ 'pnl-positive' if asset.pnl_percentage > 0 else 'pnl-negative' if asset.pnl_percentage < 0 else 'pnl-neutral' }}">
                            {{ "{:+.2f}".format(asset.pnl_percentage) }}%
                        </td>
                        {% endif %}
                        <td>
                            <form method="post" action="{{ url_for('delete_asset') }}" style="display: inline;">
                                <input type="hidden" name="asset_id" value="{{ asset.id }}">
                                <button type="submit" class="delete-btn" onclick="return confirm('削除しますか？')">削除</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="form-section">
                <p>まだ{{ info.title }}が登録されていません。上のフォームから追加してください。</p>
            </div>
            {% endif %}
        </div>
        
        <script>
        function updatePrices() {
            const button = event.target;
            button.disabled = true;
            button.innerHTML = '更新中...';
            
            if(confirm('全ての価格を最新情報で更新しますか？時間がかかる場合があります。')) {
                fetch('{{ url_for("update_prices") }}', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                    },
                    body: 'asset_type={{ asset_type if asset_type is defined else "" }}'
                }).then(response => {
                    if(response.ok) {
                        location.reload();
                    } else {
                        alert('価格更新に失敗しました');
                        button.disabled = false;
                        button.innerHTML = '価格更新';
                    }
                }).catch(error => {
                    alert('エラーが発生しました');
                    button.disabled = false;
                    button.innerHTML = '価格更新';
                });
            } else {
                button.disabled = false;
                button.innerHTML = '価格更新';
            }
        }
        </script>
    </body>
    </html>
    """

    return render_template_string(template, all_assets=all_assets, usd_jpy=usd_jpy)

@app.route('/add_asset', methods=['POST'])
def add_asset():
    """資産を追加/更新（拡張版）"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_type = request.form['asset_type']
    symbol = request.form['symbol'].strip().upper()
    name = request.form.get('name', '').strip()
    quantity = float(request.form['quantity'])
    purchase_price = float(request.form.get('purchase_price', 0)) if asset_type != 'cash' else 0
    
    # 現在価格取得（現金以外）
    current_price = 0
    if asset_type == 'gold':
        # 金の場合、田中貴金属から価格取得
        current_price = get_gold_price()
        if not purchase_price:
            purchase_price = current_price
        if not name:
            name = "金 (Gold)"
    elif asset_type != 'cash':
        is_jp = (asset_type == 'jp_stock')
        try:
            current_price = get_stock_price(symbol, is_jp)
            if not name:
                name = get_stock_name(symbol, is_jp)
            if not purchase_price:
                purchase_price = current_price
        except Exception as e:
            flash(f'価格取得に失敗しました: {symbol}', 'error')
            current_price = 0
            name = name or symbol
    
    conn = get_db()
    
    # 既存の資産をチェック
    existing = conn.execute('''
        SELECT id, quantity, 
               COALESCE(avg_price, 0) as avg_price, 
               COALESCE(total_cost, 0) as total_cost 
        FROM assets 
        WHERE user_id = ? AND asset_type = ? AND symbol = ?
    ''', (user['id'], asset_type, symbol)).fetchone()
    
    if existing and asset_type != 'cash':
        # 既存資産に追加購入として処理
        old_quantity = existing['quantity']
        old_total_cost = existing['total_cost']
        
        new_total_quantity = old_quantity + quantity
        new_total_cost = old_total_cost + (quantity * purchase_price)
        new_avg_price = new_total_cost / new_total_quantity if new_total_quantity > 0 else 0
        
        conn.execute('''
            UPDATE assets SET quantity = ?, avg_price = ?, total_cost = ?, current_price = ?, 
                             name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_total_quantity, new_avg_price, new_total_cost, current_price, name, existing['id']))
        flash(f'{symbol} を追加購入しました（数量: {quantity}）', 'success')
    elif existing and asset_type == 'cash':
        # 現金の場合は単純に更新
        conn.execute('''
            UPDATE assets SET quantity = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (quantity, existing['id']))
        flash(f'{symbol} を更新しました', 'success')
    else:
        # 新規追加
        total_cost = quantity * purchase_price if asset_type != 'cash' else quantity
        conn.execute('''
            INSERT INTO assets (user_id, asset_type, symbol, name, quantity, avg_price, current_price, total_cost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user['id'], asset_type, symbol, name, quantity, purchase_price, current_price, total_cost))
        flash(f'{symbol} を追加しました', 'success')
    
    conn.commit()
    conn.close()
    
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/delete_asset', methods=['POST'])
def delete_asset():
    """資産を削除"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_id = request.form['asset_id']
    
    conn = get_db()
    asset = conn.execute('''
        SELECT asset_type, symbol FROM assets WHERE id = ? AND user_id = ?
    ''', (asset_id, user['id'])).fetchone()
    
    if asset:
        conn.execute('DELETE FROM assets WHERE id = ? AND user_id = ?', (asset_id, user['id']))
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
    """価格を一括更新"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_type = request.form['asset_type']
    
    if asset_type == 'cash':
        return 'OK'  # 現金は価格更新不要
    
    conn = get_db()
    assets = conn.execute('''
        SELECT id, symbol FROM assets WHERE user_id = ? AND asset_type = ?
    ''', (user['id'], asset_type)).fetchall()
    
    updated_count = 0
    failed_count = 0
    
    if asset_type == 'gold':
        # 金の価格を取得
        try:
            gold_price = get_gold_price()
            for asset in assets:
                conn.execute('UPDATE assets SET current_price = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', 
                           (gold_price, asset['id']))
                updated_count += 1
                time.sleep(0.5)  # レート制限対策
        except Exception as e:
            failed_count += 1
    else:
        # 株式の価格を取得
        is_jp = (asset_type == 'jp_stock')
        for asset in assets:
            try:
                price = get_stock_price(asset['symbol'], is_jp)
                conn.execute('UPDATE assets SET current_price = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', 
                           (price, asset['id']))
                updated_count += 1
                time.sleep(1)  # レート制限対策（重要）
            except Exception as e:
                print(f"Failed to update {asset['symbol']}: {e}")
                failed_count += 1
    
    conn.commit()
    conn.close()
    
    return 'OK'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
# The following template fragment was accidentally appended to the end of this Python file
# (it contains Jinja2/HTML and would raise an IndentationError if left as raw text).
# Preserve it as a triple-quoted string so the original text remains in the repository
# but is not executed by the Python interpreter.
_trailing_template = """
                            {% if asset.asset_type == 'us_stock' %}
                                {{ "{:+,.2f}".format(asset.unrealized_pnl) }} USD<br>
                                <small>({{ "{:+,.0f}".format(asset.unrealized_pnl * usd_jpy) }} 円)</small>
                            {% else %}
                                {{ "{:+,.0f}".format(asset.unrealized_pnl) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right {{ 'pnl-positive' if asset.pnl_percentage > 0 else 'pnl-negative' if asset.pnl_percentage < 0 else 'pnl-neutral' }}">
                            {{ "{:+.2f}".format(asset.pnl_percentage) }}%
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div style="background: white; padding: 40px; border-radius: 8px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                <h3>投資資産がまだ登録されていません</h3>
                <p>ダッシュボードから資産を追加してください。</p>
                <a href="{{ url_for('dashboard') }}" style="display: inline-block; padding: 12px 24px; background: #007bff; color: white; text-decoration: none; border-radius: 4px; margin-top: 15px;">ダッシュボードへ</a>
            </div>
            {% endif %}
        </div>
    </body>
    </html>
    """

    # Note: the actual, correct return for the view `pnl_analysis` was already present
    # earlier in the file; this trailing copy is kept as `_trailing_template` for
    # preservation but is not used by the application.


@app.route('/assets/<asset_type>')
def manage_assets(asset_type):
    """資産管理ページ（拡張版）"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    assets = conn.execute('''
        SELECT *, 
               (quantity * COALESCE(current_price, 0)) as market_value,
               (quantity * COALESCE(current_price, 0) - COALESCE(total_cost, 0)) as unrealized_pnl,
               CASE 
                   WHEN COALESCE(total_cost, 0) > 0 THEN ((quantity * COALESCE(current_price, 0) - COALESCE(total_cost, 0)) / COALESCE(total_cost, 1) * 100)
                   ELSE 0 
               END as pnl_percentage
        FROM assets 
        WHERE user_id = ? AND asset_type = ?
        ORDER BY symbol
    ''', (user['id'], asset_type)).fetchall()
    conn.close()
    
    # 資産タイプに応じたタイトルと項目名
    type_info = {
        'jp_stock': {'title': '日本株', 'symbol_label': '証券コード', 'quantity_label': '株数'},
        'us_stock': {'title': '米国株', 'symbol_label': 'シンボル', 'quantity_label': '株数'},
        'gold': {'title': '金 (Gold)', 'symbol_label': '種類', 'quantity_label': '重量(g)'},
        'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'}
    }
    
    info = type_info.get(asset_type, type_info['jp_stock'])
    usd_jpy = get_usd_jpy_rate()
    
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{{ info.title }}管理</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; }
            .header { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .back-link { color: #007bff; text-decoration: none; }
            .back-link:hover { text-decoration: underline; }
            .form-section { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .form-row { display: flex; gap: 10px; align-items: end; margin-bottom: 15px; flex-wrap: wrap; }
            .form-group { flex: 1; min-width: 120px; }
            .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: #333; }
            input[type="text"], input[type="number"] { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
            button { padding: 8px 16px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; white-space: nowrap; }
            button:hover { background: #0056b3; }
            .delete-btn { background: #dc3545; padding: 4px 8px; font-size: 12px; }
            .delete-btn:hover { background: #c82333; }
            table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
            th { background: #f8f9fa; font-weight: bold; }
            .text-right { text-align: right; }
            .update-btn { background: #28a745; margin-left: 5px; }
            .update-btn:hover { background: #218838; }
            .alert { padding: 10px; margin-bottom: 20px; border-radius: 4px; }
            .alert-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .alert-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .loading { color: #666; font-style: italic; }
            .pnl-positive { color: #28a745; font-weight: bold; }
            .pnl-negative { color: #dc3545; font-weight: bold; }
            .pnl-neutral { color: #6c757d; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <a href="{{ url_for('dashboard') }}" class="back-link">← ダッシュボードに戻る</a>
                <h1>{{ info.title }}管理</h1>
            </div>
            
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            
            <div class="form-section">
                <h3>{{ info.title }}を追加</h3>
                <form method="post" action="{{ url_for('add_asset') }}">
                    <input type="hidden" name="asset_type" value="{{ asset_type }}">
                    <div class="form-row">
                        <div class="form-group">
                            <label>{{ info.symbol_label }}</label>
                            <input type="text" name="symbol" placeholder="{{ info.symbol_label }}" required>
                        </div>
                        {% if asset_type not in ['cash'] %}
                        <div class="form-group">
                            <label>名前（オプション）</label>
                            <input type="text" name="name" placeholder="名前（オプション）">
                        </div>
                        {% endif %}
                        <div class="form-group">
                            <label>{{ info.quantity_label }}</label>
                            <input type="number" name="quantity" step="0.01" placeholder="{{ info.quantity_label }}" required>
                        </div>
                        {% if asset_type not in ['cash'] %}
                        <div class="form-group">
                            <label>取得単価</label>
                            <input type="number" name="purchase_price" step="0.01" placeholder="取得単価" required>
                        </div>
                        {% endif %}
                        <div class="form-group">
                            {% if asset_type not in ['cash'] %}
                            <button type="button" onclick="updatePrices()">価格更新</button>
                            {% endif %}
                            <button type="submit">追加</button>
                        </div>
                    </div>
                </form>
            </div>
            
            {% if assets %}
            <table>
                <thead>
                    <tr>
                        <th>{{ info.symbol_label }}</th>
                        {% if asset_type not in ['cash'] %}<th>名前</th>{% endif %}
                        <th class="text-right">{{ info.quantity_label }}</th>
                        {% if asset_type not in ['cash'] %}
                        <th class="text-right">平均取得単価</th>
                        <th class="text-right">現在価格</th>
                        <th class="text-right">投資金額</th>
                        <th class="text-right">評価額</th>
                        <th class="text-right">損益</th>
                        <th class="text-right">損益率</th>
                        {% endif %}
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody>
                    {% for asset in assets %}
                    <tr>
                        <td><strong>{{ asset.symbol }}</strong></td>
                        {% if asset_type not in ['cash'] %}<td>{{ asset.name or '-' }}</td>{% endif %}
                        <td class="text-right">{{ "{:,.2f}".format(asset.quantity) }}</td>
                        {% if asset_type not in ['cash'] %}
                        <td class="text-right">
                            {% if asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.avg_price) }}
                            {% else %}
                                {{ "{:,.0f}".format(asset.avg_price) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right">
                            {% if asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.current_price) }}
                            {% else %}
                                {{ "{:,.0f}".format(asset.current_price) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right">
                            {% if asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.total_cost) }}
                                <br><small>({{ "{:,.0f}".format(asset.total_cost * usd_jpy) }} 円)</small>
                            {% else %}
                                {{ "{:,.0f}".format(asset.total_cost) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right">
                            {% if asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.market_value) }}
                                <br><small>({{ "{:,.0f}".format(asset.market_value * usd_jpy) }} 円)</small>
                            {% else %}
                                {{ "{:,.0f}".format(asset.market_value) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right {{ 'pnl-positive' if asset.unrealized_pnl > 0 else 'pnl-negative' if asset.unrealized_pnl < 0 else 'pnl-neutral' }}">
                            {% if asset_type == 'us_stock' %}
                                {{ "{:+,.2f}".format(asset.unrealized_pnl) }} USD<br>
                                <small>({{ "{:+,.0f}".format(asset.unrealized_pnl * usd_jpy) }} 円)</small>
                            {% else %}
                                {{ "{:+,.0f}".format(asset.unrealized_pnl) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right {{ 'pnl-positive' if asset.pnl_percentage > 0 else 'pnl-negative' if asset.pnl_percentage < 0 else 'pnl-neutral' }}">
                            {{ "{:+.2f}".format(asset.pnl_percentage) }}%
                        </td>
                        <td>
                            <form method="post" action="{{ url_for('delete_asset') }}" style="display: inline;">
                                <input type="hidden" name="asset_id" value="{{ asset.id }}">
                                <button type="submit" class="delete-btn" onclick="return confirm('削除しますか？')">削除</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="form-section">
                <p>まだ{{ info.title }}が登録されていません。上のフォームから追加してください。</p>
            </div>
            {% endif %}
        </div>
        
        <script>
        function updatePrices() {
            const button = event.target;
            button.disabled = true;
            button.innerHTML = '更新中...';
            
            if(confirm('全ての価格を最新情報で更新しますか？時間がかかる場合があります。')) {
                fetch('{{ url_for("update_prices") }}', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                    },
                    body: 'asset_type={{ asset_type if asset_type is defined else "" }}'
                }).then(response => {
                    if(response.ok) {
                        location.reload();
                    } else {
                        alert('価格更新に失敗しました');
                        button.disabled = false;
                        button.innerHTML = '価格更新';
                    }
                }).catch(error => {
                    alert('エラーが発生しました');
                    button.disabled = false;
                    button.innerHTML = '価格更新';
                });
            } else {
                button.disabled = false;
                button.innerHTML = '価格更新';
            }
        }
        </script>
    </body>
    </html>
    """

    return render_template_string(template, assets=assets, asset_type=asset_type, info=info, usd_jpy=usd_jpy)