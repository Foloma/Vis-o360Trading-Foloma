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
from functools import wraps
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from config import config
from deriv_client import DerivWebSocketClient
from trading_bot import TradingBot
from synthetics import DigitAnalyzer
from payment_system import PaymentSystem

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'foloma_trading_secret_key_2024')

DATA_DIR = os.environ.get('DATA_PATH', '.')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')


def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            logger.error("Erro ao carregar users.json")
            return {}
    logger.warning("users.json não encontrado, a criar novo.")
    return {}


def save_users(u):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(USERS_FILE, 'w') as f:
            json.dump(u, f, indent=2)
    except Exception as e:
        logger.error(f"Erro ao guardar users.json: {e}")


users = load_users()


def ensure_admin_exists():
    admin_email = os.environ.get('ADMIN_EMAIL', 'admin@foloma.com')
    admin_password = os.environ.get('ADMIN_PASSWORD', 'Admin123!')

    logger.info(f"Verificando administrador: {admin_email}")

    if admin_email in users:
        if users[admin_email].get('role') != 'admin':
            users[admin_email]['role'] = 'admin'
            save_users(users)
            logger.info(f"✅ Utilizador {admin_email} promovido a admin.")
        else:
            logger.info(f"ℹ️ Utilizador {admin_email} já é admin.")
    else:
        uid = str(int(time.time() * 1000))
        users[admin_email] = {
            'id': uid,
            'name': 'Administrador',
            'email': admin_email,
            'password': generate_password_hash(admin_password),
            'deriv_token': None,
            'deriv_account_type': None,
            'created_at': time.time(),
            'last_login': None,
            'referral_code': '',
            'referrals': [],
            'active': True,
            'role': 'admin',
            'affiliate_earnings': 0.0,
            'referred_users': []
        }
        save_users(users)
        logger.info(f"🔑 Admin criado: {admin_email}")


ensure_admin_exists()


class AffiliateSystem:
    def __init__(self):
        self.referrals = deque(maxlen=1000)
        self.commissions = {'total': 0, 'pending': 0, 'paid': 0}

    def generate_referral_link(self, uid):
        code = base64.b64encode(hashlib.md5(str(uid).encode()).digest())[:8].decode()
        return f"https://foloma.com/?ref={code}"

    def track_referral(self, rid, nid):
        self.referrals.append({
            'referrer_id': rid,
            'new_user_id': nid,
            'timestamp': time.time()
        })

    def calculate_commission(self, amt, pct):
        c = amt * (pct / 100)
        self.commissions['total'] += c
        self.commissions['pending'] += c
        return c

    def get_affiliate_stats(self):
        return {
            'total_referrals': len(self.referrals),
            'total_commission': round(self.commissions['total'], 2),
            'pending_commission': round(self.commissions['pending'], 2),
            'paid_commission': round(self.commissions['paid'], 2)
        }


affiliate = AffiliateSystem()

user_sessions = {}
sessions_lock = threading.Lock()


def get_user_session(user_id):
    with sessions_lock:
        if user_id not in user_sessions:
            bot = TradingBot()
            analyzer = DigitAnalyzer(max_digits=500)
            def tick_callback(tick):
                bot.on_tick(tick)
            client = DerivWebSocketClient(config, on_tick_callback=tick_callback)
            client.set_trading_bot(bot)
            client.set_digit_analyzer(analyzer)
            payment = PaymentSystem(client)
            client.set_payment_system(payment)
            bot.client = client          # <-- ADICIONADO AQUI
            user_sessions[user_id] = {
                'client': client,
                'trading_bot': bot,
                'digit_analyzer': analyzer,
                'payment_system': payment
            }
        return user_sessions[user_id]


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Não autenticado'}), 401
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Não autenticado'}), 401
        email = session.get('user_email')
        user = users.get(email)
        if not user or user.get('role') != 'admin':
            return jsonify({'error': 'Acesso restrito ao administrador'}), 403
        return f(*args, **kwargs)
    return decorated


# ───── Rotas da aplicação ─────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/auth/status')
def api_auth_status():
    if 'user_id' in session:
        email = session.get('user_email')
        user = users.get(email)
        if user:
            return jsonify({
                'authenticated': True,
                'user': {
                    'id': user['id'],
                    'name': user['name'],
                    'email': user['email'],
                    'role': user.get('role', 'user'),
                    'has_deriv_token': bool(user.get('deriv_token'))
                }
            })
    return jsonify({'authenticated': False})


@app.route('/api/auth/register', methods=['POST'])
def api_register():
    try:
        d = request.json
        name = d.get('name', '').strip()
        email = d.get('email', '').strip().lower()
        password = d.get('password', '')
        ref = d.get('referral_code', '')

        if not name or not email or not password:
            return jsonify({'error': 'Todos os campos obrigatórios'}), 400
        if len(password) < 6:
            return jsonify({'error': 'Senha min 6 caracteres'}), 400
        if email in users:
            return jsonify({'error': 'Email já registado'}), 400

        uid = str(int(time.time() * 1000))
        users[email] = {
            'id': uid,
            'name': name,
            'email': email,
            'password': generate_password_hash(password),
            'deriv_token': None,
            'deriv_account_type': None,
            'created_at': time.time(),
            'last_login': None,
            'referral_code': ref,
            'referrals': [],
            'active': True,
            'role': 'user',
            'affiliate_earnings': 0.0,
            'referred_users': []
        }
        save_users(users)

        if ref:
            for ue, ud in users.items():
                if ud.get('referral_link_code') == ref:
                    ud['affiliate_earnings'] = ud.get('affiliate_earnings', 0) + 1.0
                    ud.setdefault('referred_users', []).append(email)
                    save_users(users)
                    break
        return jsonify({'status': 'ok', 'message': 'Conta criada!'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/login', methods=['POST'])
def api_login():
    try:
        d = request.json
        email = d.get('email', '').strip().lower()
        password = d.get('password', '')
        user = users.get(email)

        if not user:
            return jsonify({'error': 'Utilizador não encontrado'}), 400
        if not user.get('active', True):
            return jsonify({'error': 'Conta desativada'}), 400
        if not check_password_hash(user['password'], password):
            return jsonify({'error': 'Senha incorreta'}), 400

        user['last_login'] = time.time()
        save_users(users)

        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['user_email'] = user['email']
        session['user_role'] = user.get('role', 'user')

        logger.info(f"Login bem-sucedido: {email}, role={session['user_role']}")

        return jsonify({
            'status': 'ok',
            'user': {
                'id': user['id'],
                'name': user['name'],
                'email': user['email'],
                'role': session['user_role'],
                'has_deriv_token': bool(user.get('deriv_token'))
            }
        })
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
        d = request.json
        token = d.get('token')
        at = d.get('account_type', 'demo')

        if not token:
            return jsonify({'error': 'Token necessário'}), 400

        email = session.get('user_email')
        if email not in users:
            return jsonify({'error': 'Utilizador não encontrado'}), 404

        users[email]['deriv_token'] = token
        users[email]['deriv_account_type'] = at
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
        return jsonify({'link': f"https://foloma.com/?ref={code}", 'code': code})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/users', methods=['GET'])
@require_admin
def api_admin_users():
    return jsonify({
        'users': [
            {
                'email': e,
                'name': u.get('name'),
                'active': u.get('active', True)
            }
            for e, u in users.items()
        ]
    })


@app.route('/api/admin/toggle-user', methods=['POST'])
@require_admin
def api_admin_toggle_user():
    d = request.json
    tgt = d.get('email')
    en = d.get('enable', True)

    if tgt not in users:
        return jsonify({'error': 'Utilizador não encontrado'}), 404

    users[tgt]['active'] = en
    save_users(users)
    return jsonify({'status': 'ok', 'message': f'Utilizador {"ativado" if en else "desativado"}.'})


SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')


def send_reset_email(to_email, token):
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.error("SMTP não configurado")
        return False
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    link = f"{base_url}/reset-password?token={token}"
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = 'Recuperação - Foloma'
    msg.attach(MIMEText(f"Redefinir senha:\n{link}", 'plain'))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        logger.error(f"Erro ao enviar email: {e}")
        return False


@app.route('/api/auth/reset-password', methods=['POST'])
def api_reset_password():
    if not SMTP_USER or not SMTP_PASSWORD:
        return jsonify({'error': 'Recuperação não configurada'}), 500

    d = request.json
    email = d.get('email', '').strip().lower()
    if email not in users:
        return jsonify({'error': 'Email não registado'}), 404

    token = secrets.token_urlsafe(32)
    users[email]['reset_token'] = token
    users[email]['reset_token_expiry'] = time.time() + 3600
    save_users(users)

    if send_reset_email(email, token):
        return jsonify({'status': 'ok', 'message': 'Link enviado.'})
    return jsonify({'error': 'Erro ao enviar email.'}), 500


@app.route('/api/auth/reset-password-confirm', methods=['POST'])
def api_reset_password_confirm():
    d = request.json
    token = d.get('token')
    new_password = d.get('new_password')

    for email, user in users.items():
        if user.get('reset_token') == token and user.get('reset_token_expiry', 0) > time.time():
            user['password'] = generate_password_hash(new_password)
            user.pop('reset_token', None)
            user.pop('reset_token_expiry', None)
            save_users(users)
            return jsonify({'status': 'ok', 'message': 'Senha alterada.'})

    return jsonify({'error': 'Token inválido ou expirado'}), 400


# ───── Rotas de Trading (acessíveis a todos os utilizadores autenticados) ─────
@app.route('/api/connect', methods=['POST'])
@require_auth
def api_connect():
    try:
        d = request.json
        at = d.get('account_type', 'demo')
        symbol = d.get('symbol', 'R_100')
        email = session.get('user_email')
        user = users.get(email)
        if not user:
            return jsonify({'error': 'Utilizador não encontrado'}), 404

        token = user.get('deriv_token')
        if not token:
            return jsonify({'error': 'Token não configurado. Vá a Configurações e insira o token da Deriv.'}), 400

        if at == 'real':
            config.REAL_API_TOKEN = token
            config.MARKUP_PERCENTAGE = 0.5
        else:
            config.DEMO_API_TOKEN = token

        sess = get_user_session(user['id'])
        client = sess['client']
        client.set_user_token(token)
        client.connect()

        # Aguardar até que o WebSocket esteja autorizado (máximo 15 segundos)
        timeout = time.time() + 15
        while not client.authorized and time.time() < timeout:
            time.sleep(0.3)

        if not client.authorized:
            return jsonify({'error': 'Não foi possível autorizar a ligação. Verifique o token.'}), 500

        client.subscribe_ticks(symbol)
        sess['trading_bot'].start(client)

        # Pequena pausa para o primeiro tick chegar e o estado ficar 100% sincronizado
        time.sleep(0.5)

        return jsonify({'status': 'conectando', 'account_type': at, 'is_demo': at == 'demo'})
    except Exception as e:
        logger.error(f"❌ Erro na conexão: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/status')
@require_auth
def api_status():
    try:
        user_id = session['user_id']
        sess = get_user_session(user_id)
        client = sess['client']
        bot = sess['trading_bot']
        analyzer = sess['digit_analyzer']

        if client:
            bot.balance = client.balance
            bot.currency = client.currency
            bot.client = client
            # Sincronizar estado real
            bot._client_connected = client.connected
            bot._client_authorized = client.authorized

        bot_status = bot.get_status()
        analysis = analyzer.get_analysis()
        all_digits = analyzer.get_recent_digits()

        digits = {
            'last': analyzer.get_current_digit(),
            'parity': analyzer.get_current_parity(),
            'stats': analyzer.get_stats(),
            'analysis': analysis,
            'recent': all_digits,
            'total': len(all_digits),
            'ticks_remaining': analyzer.get_ticks_remaining(),
            'digit_counter': analyzer.get_digit_counter(),
            'ticks_per_digit': analyzer.TICKS_PER_DIGIT,
        }
        return jsonify({'bot': bot_status, 'digits': digits, 'symbols': config.AVAILABLE_SYMBOLS})
    except Exception as e:
        logger.error(f"Status: {e}")
        return jsonify({'error': str(e)}), 500


# ... (as restantes rotas permanecem exatamente iguais ao último app.py fornecido,
#  incluindo api_trade, api_trade_digit, api_trade_hybrid, etc.)
# Para manter a mensagem focada, não as repeti aqui, mas elas devem continuar no ficheiro.
