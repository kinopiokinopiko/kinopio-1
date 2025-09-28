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
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # 資産テーブル
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
    
    # 既存のテーブルにavg_costカラムを追加（存在しない場合）
    try:
        c.execute("ALTER TABLE assets ADD COLUMN avg_cost REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # カラムが既に存在する場合
    
    # 既存のテーブルにcreated_atカラムを追加（存在しない場合）
    try:
        c.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except sqlite3.OperationalError:
        pass  # カラムが既に存在する場合

    # デフォルトユーザー作成（開発・デモ用）
    # 本番環境では以下をコメントアウトすることを推奨
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

# アプリケーション開始時にDB初期化
init_db()

@app.route('/')
def index():
    """ホームページ"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    """ユーザー登録ページ"""
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        
        # バリデーション
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
            # 既存ユーザーチェック
            existing_user = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
            
            if existing_user:
                flash('このユーザー名は既に使用されています', 'error')
                conn.close()
            else:
                # 新規ユーザー作成
                password_hash = generate_password_hash(password)
                conn.execute(
                    'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                    (username, password_hash)
                )
                conn.commit()
                conn.close()
                
                flash('アカウントを作成しました。ログインしてください。', 'success')
                return redirect(url_for('login'))
    
    return render_template('register.html')

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
    
    return render_template('login.html')

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
        SELECT symbol, name, quantity, price, avg_cost FROM assets 
        WHERE user_id = ? AND asset_type = "jp_stock"
    ''', (user['id'],)).fetchall()
    
    us_stocks = conn.execute('''
        SELECT symbol, name, quantity, price, avg_cost FROM assets 
        WHERE user_id = ? AND asset_type = "us_stock"
    ''', (user['id'],)).fetchall()
    
    cash_items = conn.execute('''
        SELECT symbol as label, quantity as amount FROM assets 
        WHERE user_id = ? AND asset_type = "cash"
    ''', (user['id'],)).fetchall()
    
    gold_items = conn.execute('''
        SELECT symbol, name, quantity, price, avg_cost FROM assets 
        WHERE user_id = ? AND asset_type = "gold"
    ''', (user['id'],)).fetchall()
    
    conn.close()
    
    # 合計計算と損益計算
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
                         jp_stocks=jp_stocks, us_stocks=us_stocks, cash_items=cash_items, gold_items=gold_items,
                         jp_total=jp_total, jp_profit=jp_profit,
                         us_total_usd=us_total_usd, us_total_jpy=us_total_jpy, us_profit_usd=us_profit_usd, us_profit_jpy=us_profit_jpy,
                         cash_total=cash_total, gold_total=gold_total, gold_profit=gold_profit,
                         total_assets=total_assets, total_profit=total_profit, usd_jpy=usd_jpy,
                         username=session.get('username', ''))

@app.route('/assets/<asset_type>')
def manage_assets(asset_type):
    """資産管理ページ"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    assets = conn.execute('''
        SELECT * FROM assets WHERE user_id = ? AND asset_type = ?
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
    
    return render_template('manage_assets.html', assets=assets, asset_type=asset_type, info=info)

@app.route('/add_asset', methods=['POST'])
def add_asset():
    """資産を追加/更新"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_type = request.form['asset_type']
    symbol = request.form['symbol'].strip().upper()
    name = request.form.get('name', '').strip()
    quantity = float(request.form['quantity'])
    avg_cost = float(request.form.get('avg_cost', 0)) if request.form.get('avg_cost') else 0
    
    # 価格取得（現金以外）
    price = 0
    if asset_type == 'gold':
        # 金の場合、田中貴金属から価格取得
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
    
    # 既存の資産をチェック
    existing = conn.execute('''
        SELECT id, quantity, avg_cost FROM assets WHERE user_id = ? AND asset_type = ? AND symbol = ?
    ''', (user['id'], asset_type, symbol)).fetchone()
    
    if existing and asset_type != 'cash':
        # 既存資産がある場合、加重平均を計算
        old_quantity = existing['quantity']
        old_avg_cost = existing['avg_cost']
        new_total_quantity = old_quantity + quantity
        
        if new_total_quantity > 0 and avg_cost > 0:
            # 加重平均単価を計算
            new_avg_cost = ((old_quantity * old_avg_cost) + (quantity * avg_cost)) / new_total_quantity
        else:
            new_avg_cost = old_avg_cost if old_avg_cost > 0 else avg_cost
        
        # 更新
        conn.execute('''
            UPDATE assets SET quantity = ?, price = ?, name = ?, avg_cost = ?
            WHERE id = ?
        ''', (new_total_quantity, price, name, new_avg_cost, existing['id']))
        flash(f'{symbol} を更新しました（数量: {new_total_quantity}）', 'success')
    elif existing and asset_type == 'cash':
        # 現金の場合は単純に数量を更新
        conn.execute('''
            UPDATE assets SET quantity = ?, price = ?, name = ?
            WHERE id = ?
        ''', (quantity, price, name, existing['id']))
        flash(f'{symbol} を更新しました', 'success')
    else:
        # 新規追加
        conn.execute('''
            INSERT INTO assets (user_id, asset_type, symbol, name, quantity, price, avg_cost)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user['id'], asset_type, symbol, name, quantity, price, avg_cost))
        flash(f'{symbol} を追加しました', 'success')
    
    conn.commit()
    conn.close()
    
    return redirect(url_for('manage_assets', asset_type=asset_type))

@app.route('/edit_asset/<int:asset_id>')
def edit_asset(asset_id):
    """資産編集ページ"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    asset = conn.execute('''
        SELECT * FROM assets WHERE id = ? AND user_id = ?
    ''', (asset_id, user['id'])).fetchone()
    
    if not asset:
        flash('資産が見つかりません', 'error')
        return redirect(url_for('dashboard'))
    
    # 資産タイプに応じたタイトルと項目名
    type_info = {
        'jp_stock': {'title': '日本株', 'symbol_label': '証券コード', 'quantity_label': '株数'},
        'us_stock': {'title': '米国株', 'symbol_label': 'シンボル', 'quantity_label': '株数'},
        'gold': {'title': '金 (Gold)', 'symbol_label': '種類', 'quantity_label': '重量(g)'},
        'cash': {'title': '現金', 'symbol_label': '項目名', 'quantity_label': '金額'}
    }
    
    info = type_info.get(asset['asset_type'], type_info['jp_stock'])
    conn.close()
    
    return render_template('edit_asset.html', asset=asset, info=info)

@app.route('/update_asset', methods=['POST'])
def update_asset():
    """資産を更新"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    asset_id = request.form['asset_id']
    symbol = request.form['symbol'].strip().upper()
    name = request.form.get('name', '').strip()
    quantity = float(request.form['quantity'])
    avg_cost = float(request.form.get('avg_cost', 0)) if request.form.get('avg_cost') else 0
    
    conn = get_db()
    asset = conn.execute('''
        SELECT asset_type FROM assets WHERE id = ? AND user_id = ?
    ''', (asset_id, user['id'])).fetchone()
    
    if not asset:
        flash('資産が見つかりません', 'error')
        return redirect(url_for('dashboard'))
    
    asset_type = asset['asset_type']
    
    # 価格取得（現金以外）
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
    
    # 更新
    conn.execute('''
        UPDATE assets SET symbol = ?, name = ?, quantity = ?, price = ?, avg_cost = ?
        WHERE id = ? AND user_id = ?
    ''', (symbol, name, quantity, price, avg_cost, asset_id, user['id']))
    
    conn.commit()
    conn.close()
    
    flash(f'{symbol} を更新しました', 'success')
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
                conn.execute('UPDATE assets SET price = ? WHERE id = ?', (gold_price, asset['id']))
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
                conn.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
                updated_count += 1
                time.sleep(1)  # レート制限対策（重要）
            except Exception as e:
                print(f"Failed to update {asset['symbol']}: {e}")
                failed_count += 1
    
    conn.commit()
    conn.close()
    
    return 'OK'

@app.route('/update_all_prices', methods=['POST'])
def update_all_prices():
    """全資産の価格を一括更新"""
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))
    
    conn = get_db()
    
    # 日本株の価格更新
    jp_assets = conn.execute('''
        SELECT id, symbol FROM assets WHERE user_id = ? AND asset_type = "jp_stock"
    ''', (user['id'],)).fetchall()
    
    for asset in jp_assets:
        try:
            price = get_stock_price(asset['symbol'], True)
            conn.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
            time.sleep(1)
        except:
            pass
    
    # 米国株の価格更新
    us_assets = conn.execute('''
        SELECT id, symbol FROM assets WHERE user_id = ? AND asset_type = "us_stock"
    ''', (user['id'],)).fetchall()
    
    for asset in us_assets:
        try:
            price = get_stock_price(asset['symbol'], False)
            conn.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
            time.sleep(1)
        except:
            pass
    
    # 金の価格更新
    gold_assets = conn.execute('''
        SELECT id FROM assets WHERE user_id = ? AND asset_type = "gold"
    ''', (user['id'],)).fetchall()
    
    if gold_assets:
        try:
            gold_price = get_gold_price()
            for asset in gold_assets:
                conn.execute('UPDATE assets SET price = ? WHERE id = ?', (gold_price, asset['id']))
        except:
            pass
    
    conn.commit()
    conn.close()
    
    flash('全ての価格を更新しました', 'success')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
