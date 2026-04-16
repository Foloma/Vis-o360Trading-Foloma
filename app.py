from flask import Flask, render_template, jsonify, request, session
import threading
import time
import logging
import hashlib
import base64
import json
import os
from collections import deque
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from config import config
from deriv_client import DerivWebSocketClient
from trading_bot import trading_bot
from synthetics import digit_analyzer
from payment_system import PaymentSystem

app = Flask(__name__)
app.secret_key = 'foloma_trading_secret_key_2024'

# ========== SISTEMA DE UTILIZADORES COM JSON ==========
DATA_DIR = os.environ.get('DATA_PATH', '.')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')

def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_users(users):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

users = load_users()

# ========== SISTEMA DE AFILIADO ==========
class AffiliateSystem:
    def __init__(self):
        self.referrals = deque(maxlen=1000)
        self.commissions = {'total': 0, 'pending': 0, 'paid': 0, 'history': deque(maxlen=100)}

    def generate_referral_link(self, user_id):
        code = base64.b64encode(hashlib.md5(str(user_id).encode()).digest())[:8].decode()
        return f"https://foloma.com/ref/{code}"

    def track_referral(self, referrer_id, new_user_id):
        referral = {'referrer_id': referrer_id, 'new_user_id': new_user_id,
                    'timestamp': time.time(), 'status': 'pending', 'commission': 0}
        self.referrals.append(referral)
        return referral

    def calculate_commission(self, trade_amount, markup_percentage):
        commission = trade_amount * (markup_percentage / 100)
        self.commissions['total'] += commission
        self.commissions['pending'] += commission
        return commission

    def get_affiliate_stats(self):
        return {
            'total_referrals': len(self.referrals),
            'total_commission': round(self.commissions['total'], 2),
            'pending_commission': round(self.commissions['pending'], 2),
            'paid_commission': round(self.commissions['paid'], 2)
        }

affiliate = AffiliateSystem()

deriv_client = None
payment_system = None

def on_tick_callback(tick):
    trading_bot.on_tick(tick)

def require_auth(f):
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Não autenticado'}), 401
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated

# ========== ROTAS DE AUTENTICAÇÃO ==========
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/auth/status')
def api_auth_status():
    if 'user_id' in session:
        email = session.get('user_email')
        user = users.get(email)
        if user:
            return jsonify({'authenticated': True, 'user': {
                'id': user['id'],
                'name': user['name'],
                'email': user['email'],
                'has_deriv_token': bool(user.get('deriv_token'))
            }})
    return jsonify({'authenticated': False})

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    try:
        data = request.json
        name = data.get('name', '').strip()
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')
        referral_code = data.get('referral_code', '')
        
        if not name or not email or not password:
            return jsonify({'error': 'Todos os campos são obrigatórios'}), 400
        if len(password) < 6:
            return jsonify({'error': 'Senha deve ter pelo menos 6 caracteres'}), 400
        
        if email in users:
            return jsonify({'error': 'Email já registado'}), 400
        
        user_id = str(int(time.time() * 1000))
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        users[email] = {
            'id': user_id, 'name': name, 'email': email, 'password': password_hash,
            'deriv_token': None, 'deriv_account_type': None,
            'created_at': time.time(), 'last_login': None,
            'referral_code': referral_code, 'referrals': []
        }
        save_users(users)
        
        if referral_code:
            for u_email, u_data in users.items():
                if u_data.get('referral_link_code') == referral_code:
                    affiliate.track_referral(u_data['id'], user_id)
                    break
        
        return jsonify({'status': 'ok', 'message': 'Conta criada com sucesso!'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    try:
        data = request.json
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')
        user = users.get(email)
        if not user:
            return jsonify({'error': 'Utilizador não encontrado'}), 400
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        if user['password'] != password_hash:
            return jsonify({'error': 'Senha incorreta'}), 400
        
        user['last_login'] = time.time()
        save_users(users)
        
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['user_email'] = user['email']
        
        return jsonify({'status': 'ok', 'user': {
            'id': user['id'],
            'name': user['name'],
            'email': user['email'],
            'has_deriv_token': bool(user.get('deriv_token'))
        }})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'status': 'ok'})

@app.route('/api/auth/save_token', methods=['POST'])
@require_auth
def api_save_token():
    try:
        data = request.json
        token = data.get('token')
        account_type = data.get('account_type', 'demo')
        if not token:
            return jsonify({'error': 'Token necessário'}), 400
        email = session.get('user_email')
        if email not in users:
            return jsonify({'error': 'Utilizador não encontrado'}), 404
        users[email]['deriv_token'] = token
        users[email]['deriv_account_type'] = account_type
        save_users(users)
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/generate_referral_link', methods=['GET'])
@require_auth
def api_generate_referral_link():
    try:
        email = session.get('user_email')
        user = users.get(email)
        if not user:
            return jsonify({'error': 'Utilizador não encontrado'}), 404
        code = base64.b64encode(hashlib.md5(str(user['id']).encode()).digest())[:8].decode()
        user['referral_link_code'] = code
        save_users(users)
        link = f"https://foloma.com/?ref={code}"
        return jsonify({'link': link, 'code': code})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== ROTAS DA PLATAFORMA ==========
@app.route('/api/connect', methods=['POST'])
@require_auth
def api_connect():
    global deriv_client, payment_system
    try:
        data = request.json
        account_type = data.get('account_type', 'demo')
        symbol = data.get('symbol', 'R_100')
        email = session.get('user_email')
        user = users.get(email)
        if not user:
            return jsonify({'error': 'Utilizador não encontrado'}), 404
        token = user.get('deriv_token')
        if not token:
            return jsonify({'error': 'Token não configurado. Configure o token nas definições.'}), 400
        
        if account_type == 'real':
            config.REAL_API_TOKEN = token
            config.MARKUP_PERCENTAGE = 0.5
        else:
            config.DEMO_API_TOKEN = token
        
        deriv_client = DerivWebSocketClient(config, on_tick_callback)
        deriv_client.set_user_token(token)
        deriv_client.set_trading_bot(trading_bot)
        deriv_client.connect()
        time.sleep(2)
        deriv_client.subscribe_ticks(symbol)
        trading_bot.start(deriv_client)
        payment_system = PaymentSystem(deriv_client)
        deriv_client.set_payment_system(payment_system)
        return jsonify({'status': 'conectando', 'account_type': account_type, 'is_demo': account_type == 'demo'})
    except Exception as e:
        logger.error(f"❌ Erro: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/status')
@require_auth
def api_status():
    try:
        if deriv_client:
            trading_bot.balance = deriv_client.balance
            trading_bot.currency = deriv_client.currency
        status = trading_bot.get_status()
        digits = {
            'last': digit_analyzer.get_current_digit(),
            'parity': digit_analyzer.get_current_parity(),
            'stats': digit_analyzer.get_stats(),
            'analysis': digit_analyzer.get_analysis(),
            'recent': digit_analyzer.get_recent_digits(20),
            'countdown': digit_analyzer.get_countdown()
        }
        return jsonify({'bot': status, 'digits': digits, 'symbols': config.AVAILABLE_SYMBOLS})
    except Exception as e:
        logger.error(f"Erro: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/display_digit')
@require_auth
def api_display_digit():
    try:
        digit, parity, countdown = digit_analyzer.get_next_display_digit()
        return jsonify({
            'digit': digit,
            'parity': parity,
            'countdown': countdown,
            'timestamp': time.time()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/symbol/change', methods=['POST'])
@require_auth
def api_symbol_change():
    try:
        data = request.json
        symbol = data.get('symbol')
        if symbol not in config.AVAILABLE_SYMBOLS:
            return jsonify({'error': 'Símbolo inválido'}), 400
        if deriv_client:
            deriv_client.change_symbol(symbol)
            trading_bot.current_symbol = symbol
        return jsonify({'status': 'ok', 'symbol': symbol})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== TRADES (APENAS AS ROTAS PRINCIPAIS – MANTENHA O RESTO IGUAL) ==========
# (As rotas /api/trade, /api/trade/digit, /api/trade/hybrid, /api/report, etc.
#  devem permanecer como estavam, sem alterações.)
# Devido ao tamanho, não vou repetir todo o código aqui, mas você pode manter as mesmas
# rotas que já tinha (desde que não usem o SQLAlchemy).

# ========== FIM ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
