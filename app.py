from flask import Flask, render_template_string, request, redirect, url_for, session, flash
import requests
import json
import os
from datetime import datetime, timezone, timedelta
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import yfinance as yf
import re
from bs4 import BeautifulSoup

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
    
    # 資産テーブル
    c.execute('''CREATE TABLE IF NOT EXISTS assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        asset_type TEXT NOT NULL,
        symbol TEXT NOT NULL,
        name TEXT,
        quantity REAL NOT NULL,
        price REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id)
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

def get_jp_stock_info(code):
    """日本株の情報を取得（yfinance使用）"""
    try:
        ticker = yf.Ticker(f"{code}.T")
        info = ticker.info
        current_price = info.get('currentPrice', 0)
        if current_price == 0:
            hist = ticker.history(period="1d")
            if not hist.empty:
                current_price = hist['Close'].iloc[-1]
        
        return {
            'name': info.get('longName', f'Stock {code}'),
            'price': round(current_price, 2) if current_price else 0
        }
    except:
        return {'name': f'Stock {code}', 'price': 0}

def get_us_stock_info(symbol):
    """米国株の情報を取得（yfinance使用）"""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        current_price = info.get('currentPrice', 0)
        if current_price == 0:
            hist = ticker.history(period="1d")
            if not hist.empty:
                current_price = hist['Close'].iloc[-1]
        
        return {
            'name': info.get('longName', symbol),
            'price': round(current_price, 2) if current_price else 0
        }
    except:
        return {'name': symbol, 'price': 0}

def get_stock_price(symbol, is_jp=False):
    """株価を取得（yfinance使用）"""
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
        res = requests.get(tanaka_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) > 1 and tds[0].get_text(strip=True).upper() == "GOLD":
                price_text = tds[1].get_text(strip=True)
                price_match = re.search(r"([0-9,]+) yen", price_text)
                if price_match:
                    return int(price_match.group(1).replace(",", ""))
        return 0  # 取得できなかった場合は0を返す
    except:
        return 0

def get_usd_jpy_rate():
    """USD/JPY レートを取得"""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/USDJPY=X"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if 'chart' in data and 'result' in data['chart'] and data['chart']['result']:
            result = data['chart']['result'][0]
            if 'meta' in result and 'regularMarketPrice' in result['meta']:
                return result['meta']['regularMarketPrice']
        return 150.0
    except:
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
        SELECT symbol, name, quantity, price FROM assets 
        WHERE user_id = ? AND asset_type = "jp_stock"
    ''', (user['id'],)).fetchall()
    
    us_stocks = conn.execute('''
        SELECT symbol, name, quantity, price FROM assets 
        WHERE user_id = ? AND asset_type = "us_stock"
    ''', (user['id'],)).fetchall()
    
    cash_items = conn.execute('''
        SELECT symbol as label, quantity as amount FROM assets 
        WHERE user_id = ? AND asset_type = "cash"
    ''', (user['id'],)).fetchall()
    
    gold_items = conn.execute('''
        SELECT symbol, name, quantity, price FROM assets 
        WHERE user_id = ? AND asset_type = "gold"
    ''', (user['id'],)).fetchall()
    
    conn.close()
    
    # 合計計算
    jp_total = sum(stock['quantity'] * stock['price'] for stock in jp_stocks)
    us_total_usd = sum(stock['quantity'] * stock['price'] for stock in us_stocks)
    usd_jpy = get_usd_jpy_rate()
    us_total_jpy = us_total_usd * usd_jpy
    cash_total = sum(item['amount'] for item in cash_items)
    gold_total = sum(item['quantity'] * item['price'] for item in gold_items)
    
    total_assets = jp_total + us_total_jpy + cash_total + gold_total
    
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>資産ダッシュボード</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; }
            .header { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .nav-links { margin: 20px 0; }
            .nav-links a { display: inline-block; margin-right: 15px; padding: 8px 16px; background: #007bff; color: white; text-decoration: none; border-radius: 4px; }
            .nav-links a:hover { background: #0056b3; }
            .asset-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin: 20px 0; }
            .asset-card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .asset-card h3 { margin: 0 0 10px 0; color: #333; }
            .asset-value { font-size: 24px; font-weight: bold; color: #007bff; }
            .total-card { background: #28a745; color: white; grid-column: 1 / -1; text-align: center; }
            .rate-info { color: #666; font-size: 14px; margin: 10px 0; }
            .user-info { float: right; }
            .logout-btn { background: #dc3545; }
            .logout-btn:hover { background: #c82333; }
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
            </div>
            
            <div class="asset-grid">
                <div class="asset-card">
                    <h3>日本株</h3>
                    <div class="asset-value">{{ "{:,.0f}".format(jp_total) }} 円</div>
                    <div>{{ jp_stocks|length }} 銘柄</div>
                </div>
                
                <div class="asset-card">
                    <h3>米国株</h3>
                    <div class="asset-value">{{ "{:,.0f}".format(us_total_jpy) }} 円</div>
                    <div>${{ "{:,.2f}".format(us_total_usd) }} ({{ us_stocks|length }} 銘柄)</div>
                </div>
                
                <div class="asset-card">
                    <h3>現金</h3>
                    <div class="asset-value">{{ "{:,.0f}".format(cash_total) }} 円</div>
                    <div>{{ cash_items|length }} 項目</div>
                </div>
                
                <div class="asset-card">
                    <h3>金 (Gold)</h3>
                    <div class="asset-value">{{ "{:,.0f}".format(gold_total) }} 円</div>
                    <div>{{ gold_items|length }} 項目</div>
                </div>
                
                <div class="asset-card total-card">
                    <h3>総資産</h3>
                    <div class="asset-value">{{ "{:,.0f}".format(total_assets) }} 円</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return render_template_string(template, 
                                jp_stocks=jp_stocks, us_stocks=us_stocks, cash_items=cash_items, gold_items=gold_items,
                                jp_total=jp_total, us_total_usd=us_total_usd, us_total_jpy=us_total_jpy,
                                cash_total=cash_total, gold_total=gold_total, total_assets=total_assets, usd_jpy=usd_jpy,
                                session=session)

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
    
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{{ info.title }}管理</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .container { max-width: 1000px; margin: 0 auto; }
            .header { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .back-link { color: #007bff; text-decoration: none; }
            .back-link:hover { text-decoration: underline; }
            .form-section { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .form-row { display: flex; gap: 10px; align-items: end; margin-bottom: 10px; }
            input[type="text"], input[type="number"] { padding: 8px; border: 1px solid #ddd; border-radius: 4px; flex: 1; }
            button { padding: 8px 16px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
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
                <h3>{{ info.title }}を追加/更新</h3>
                <form method="post" action="{{ url_for('add_asset') }}">
                    <input type="hidden" name="asset_type" value="{{ asset_type }}">
                    <div class="form-row">
                        <input type="text" name="symbol" placeholder="{{ info.symbol_label }}" required>
                        {% if asset_type not in ['cash'] %}
                        <input type="text" name="name" placeholder="名前（オプション）">
                        {% endif %}
                        <input type="number" name="quantity" step="0.01" placeholder="{{ info.quantity_label }}" required>
                        {% if asset_type not in ['cash'] %}
                        <button type="button" onclick="updatePrices()">価格更新</button>
                        {% endif %}
                        <button type="submit">追加/更新</button>
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
                        <th class="text-right">価格</th>
                        <th class="text-right">評価額</th>
                        {% endif %}
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody>
                    {% for asset in assets %}
                    <tr>
                        <td>{{ asset.symbol }}</td>
                        {% if asset_type not in ['cash'] %}<td>{{ asset.name or '-' }}</td>{% endif %}
                        <td class="text-right">{{ "{:,.2f}".format(asset.quantity) }}</td>
                        {% if asset_type not in ['cash'] %}
                        <td class="text-right">
                            {% if asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.price) }}
                            {% else %}
                                {{ "{:,.0f}".format(asset.price) }} 円
                            {% endif %}
                        </td>
                        <td class="text-right">
                            {% if asset_type == 'us_stock' %}
                                ${{ "{:,.2f}".format(asset.quantity * asset.price) }}
                            {% else %}
                                {{ "{:,.0f}".format(asset.quantity * asset.price) }} 円
                            {% endif %}
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
            if(confirm('全ての価格を最新情報で更新しますか？')) {
                fetch('{{ url_for("update_prices") }}', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                    },
                    body: 'asset_type={{ asset_type }}'
                }).then(response => {
                    if(response.ok) {
                        location.reload();
                    }
                });
            }
        }
        </script>
    </body>
    </html>
    """
    
    return render_template_string(template, assets=assets, asset_type=asset_type, info=info)

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
    
    # 価格取得（現金以外）
    price = 0
    if asset_type == 'gold':
        # 金の場合、田中貴金属から価格取得
        price = get_gold_price()
        if not name:
            name = "金 (Gold)"
    elif asset_type != 'cash':
        is_jp = (asset_type == 'jp_stock')
        price = get_stock_price(symbol, is_jp)
        if not name:
            name = get_stock_name(symbol, is_jp)
    
    conn = get_db()
    
    # 既存の資産をチェック
    existing = conn.execute('''
        SELECT id FROM assets WHERE user_id = ? AND asset_type = ? AND symbol = ?
    ''', (user['id'], asset_type, symbol)).fetchone()
    
    if existing:
        # 更新
        conn.execute('''
            UPDATE assets SET quantity = ?, price = ?, name = ?
            WHERE id = ?
        ''', (quantity, price, name, existing['id']))
        flash(f'{symbol} を更新しました', 'success')
    else:
        # 新規追加
        conn.execute('''
            INSERT INTO assets (user_id, asset_type, symbol, name, quantity, price)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user['id'], asset_type, symbol, name, quantity, price))
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
    
    if asset_type == 'gold':
        # 金の価格を取得
        gold_price = get_gold_price()
        for asset in assets:
            conn.execute('UPDATE assets SET price = ? WHERE id = ?', (gold_price, asset['id']))
    else:
        # 株式の価格を取得
        is_jp = (asset_type == 'jp_stock')
        for asset in assets:
            price = get_stock_price(asset['symbol'], is_jp)
            conn.execute('UPDATE assets SET price = ? WHERE id = ?', (price, asset['id']))
    
    conn.commit()
    conn.close()
    
    return 'OK'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)