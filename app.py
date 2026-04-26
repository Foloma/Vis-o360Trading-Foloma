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
from trading_bot import trading_bot
from synthetics import digit_analyzer
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
            return {}
    return {}


def save_users(u):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USERS_FILE, 'w') as f:
        json.dump(u, f, indent=2)


users = load_users()


def ensure_admin_exists():
    """Garante que existe um administrador definido pelas variáveis de ambiente."""
    admin_email = os.environ.get('ADMIN_EMAIL', 'admin@foloma.com')
    admin_password = os.environ.get('ADMIN_PASSWORD', 'Admin123!')

    if admin_email not in users:
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
            'role': 'admin'
        }
        save_users(users)
        logger.info(f"🔑 Admin criado automaticamente: {admin_email}")


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
deriv_client = None
payment_system = None


def on_tick_callback(tick):
    """Distribui o tick apenas para o trading_bot (que já alimenta o digit_analyzer)."""
    trading_bot.on_tick(tick)


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
            'role': 'user'
        }
        save_users(users)

        if ref:
            for ue, ud in users.items():
                if ud.get('referral_link_code') == ref:
                    affiliate.track_referral(ud['id'], uid)
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

        return jsonify({
            'status': 'ok',
            'user': {
                'id': user['id'],
                'name': user['name'],
                'email': user['email'],
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


# Configurações SMTP (entre variáveis de ambiente)
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
    users[email]['reset_token_expiry'] = time.time() + 3600  # 1 hora
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


# ───── Rotas de Trading (admin apenas) ─────
@app.route('/api/connect', methods=['POST'])
@require_admin
def api_connect():
    global deriv_client, payment_system
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
            return jsonify({'error': 'Token não configurado.'}), 400

        if at == 'real':
            config.REAL_API_TOKEN = token
            config.MARKUP_PERCENTAGE = 0.5
        else:
            config.DEMO_API_TOKEN = token

        deriv_client = DerivWebSocketClient(config, on_tick_callback)
        deriv_client.set_user_token(token)
        deriv_client.set_trading_bot(trading_bot)
        deriv_client.set_digit_analyzer(digit_analyzer)
        deriv_client.connect()
        time.sleep(2)
        deriv_client.subscribe_ticks(symbol)
        trading_bot.start(deriv_client)

        payment_system = PaymentSystem(deriv_client)
        deriv_client.set_payment_system(payment_system)

        return jsonify({'status': 'conectando', 'account_type': at, 'is_demo': at == 'demo'})
    except Exception as e:
        logger.error(f"❌ Erro na conexão: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/status')
@require_auth
def api_status():
    try:
        if deriv_client:
            trading_bot.balance = deriv_client.balance
            trading_bot.currency = deriv_client.currency

        bot_status = trading_bot.get_status()
        analysis = digit_analyzer.get_analysis()
        all_digits = digit_analyzer.get_recent_digits()

        digits = {
            'last': digit_analyzer.get_current_digit(),
            'parity': digit_analyzer.get_current_parity(),
            'stats': digit_analyzer.get_stats(),
            'analysis': analysis,
            'recent': all_digits,
            'total': len(all_digits),
            'ticks_remaining': digit_analyzer.get_ticks_remaining(),
            'digit_counter': digit_analyzer.get_digit_counter(),
            'ticks_per_digit': digit_analyzer.TICKS_PER_DIGIT,
        }
        return jsonify({'bot': bot_status, 'digits': digits, 'symbols': config.AVAILABLE_SYMBOLS})
    except Exception as e:
        logger.error(f"Status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/display_digit')
@require_auth
def api_display_digit():
    try:
        d, p, tr = digit_analyzer.get_next_display_digit()
        return jsonify({'digit': d, 'parity': p, 'ticks_remaining': tr, 'timestamp': time.time()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/symbol/change', methods=['POST'])
@require_auth
def api_symbol_change():
    try:
        d = request.json
        sym = d.get('symbol')
        if sym not in config.AVAILABLE_SYMBOLS:
            return jsonify({'error': 'Símbolo inválido'}), 400
        if deriv_client:
            deriv_client.change_symbol(sym)
            trading_bot.current_symbol = sym
        return jsonify({'status': 'ok', 'symbol': sym})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trade', methods=['POST'])
@require_admin
def api_trade():
    try:
        d = request.json
        action = d.get('action')
        amt = float(d.get('amount', 0.35))

        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amt < 0.35 or amt > 100:
            return jsonify({'error': 'Valor inválido'}), 400

        sig, conf = trading_bot.calculate_signal()
        if conf < config.RISK_LIMITS.get('min_confidence', 50):
            return jsonify({'error': f'Confiança baixa: {conf:.1f}%'}), 400

        ok = deriv_client.place_trade('CALL' if action == 'BUY' else 'PUT', amt, is_digit=False)
        if ok:
            if deriv_client.markup_percentage > 0:
                affiliate.calculate_commission(amt, deriv_client.markup_percentage)
            return jsonify({'status': 'ok', 'message': f'Trade {action} enviado', 'confidence': conf})
        return jsonify({'error': 'Falha no trade'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trade/digit', methods=['POST'])
@require_admin
def api_trade_digit():
    try:
        d = request.json
        pred = d.get('prediction')
        amt = float(d.get('amount', 0.35))

        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amt < 0.35 or amt > 100:
            return jsonify({'error': 'Valor inválido'}), 400
        if pred not in ('odd', 'even'):
            return jsonify({'error': 'Use "odd" ou "even"'}), 400

        tr = digit_analyzer.get_ticks_remaining()
        if tr < 2:
            return jsonify({'error': f'Dígito a sair em {tr} tick(s)! Aguarde.'}), 400

        ok = deriv_client.place_trade('CALL' if pred == 'odd' else 'PUT', amt, is_digit=True)
        if ok:
            if deriv_client.markup_percentage > 0:
                affiliate.calculate_commission(amt, deriv_client.markup_percentage)
            label = 'ÍMPAR' if pred == 'odd' else 'PAR'
            return jsonify({
                'status': 'ok',
                'message': f'✅ ${amt:.2f} em {label}! Resultado em ~{tr} ticks.',
                'ticks_remaining': tr
            })
        return jsonify({'error': 'Falha no trade'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trade/hybrid', methods=['POST'])
@require_admin
def api_trade_hybrid():
    try:
        d = request.json
        amt = float(d.get('amount', 0.35))

        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if 'R_' not in trading_bot.current_symbol:
            return jsonify({'error': 'Modo híbrido disponível apenas para índices de volatilidade (R_)'}), 400

        sig, conf_a = trading_bot.calculate_signal()
        da = digit_analyzer.get_analysis()
        dr = da.get('recommended_action')
        dc = da.get('confidence', 0)

        if sig == 'BUY' and dr == 'BUY':
            comb = (conf_a + dc) / 2
            action = 'BUY'
            msg = '✅ COMPRA CONFIRMADA'
        elif sig == 'SELL' and dr == 'SELL':
            comb = (conf_a + dc) / 2
            action = 'SELL'
            msg = '✅ VENDA CONFIRMADA'
        else:
            return jsonify({'error': '⚠️ Sinais divergentes.'}), 400

        if comb < config.ADVANCED_STRATEGY.get('hybrid_min_confidence', 60):
            return jsonify({'error': f'Confiança baixa ({comb:.1f}%)'}), 400

        ok = deriv_client.place_trade('CALL' if action == 'BUY' else 'PUT', amt, is_digit=False)
        if ok:
            return jsonify({'status': 'ok', 'message': msg, 'confidence': comb})
        return jsonify({'error': 'Falha'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trade/manual', methods=['POST'])
@require_admin
def api_trade_manual():
    try:
        d = request.json
        action = d.get('action')
        amt = float(d.get('amount', 0.35))

        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400

        ok = deriv_client.place_trade('CALL' if action == 'BUY' else 'PUT', amt, is_digit=False)
        if ok:
            return jsonify({'status': 'ok', 'message': f'Trade manual {action}!'})
        return jsonify({'error': 'Falha'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/clear_history', methods=['POST'])
@require_auth
def api_clear_history():
    try:
        trading_bot.reset_stats()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/report')
@require_auth
def api_report():
    try:
        return jsonify(trading_bot.get_trade_report())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/pause', methods=['POST'])
@require_auth
def api_pause():
    d = request.json
    p = d.get('paused', True)
    if p:
        trading_bot.pause()
    else:
        trading_bot.resume()
    return jsonify({'paused': p})


@app.route('/api/martingale/status', methods=['GET'])
@require_auth
def api_martingale_status():
    try:
        return jsonify(trading_bot.get_martingale_status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/martingale/apply', methods=['POST'])
@require_auth
def api_martingale_apply():
    try:
        d = request.json
        la = float(d.get('last_amount', 0))
        if la <= 0:
            return jsonify({'error': 'Valor inválido'}), 400
        ok, res = trading_bot.apply_martingale_after_loss(la)
        if ok:
            return jsonify({'status': 'ok', 'martingale': res})
        return jsonify({'error': res}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/martingale/reset', methods=['POST'])
@require_auth
def api_martingale_reset():
    try:
        trading_bot.reset_martingale()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/affiliate/stats')
@require_auth
def api_affiliate_stats():
    try:
        return jsonify(affiliate.get_affiliate_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/affiliate/link')
@require_auth
def api_affiliate_link():
    try:
        uid = session.get('user_id')
        link = affiliate.generate_referral_link(uid)
        return jsonify({'link': link, 'code': link.split('=')[-1]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/payment/deposit', methods=['POST'])
@require_auth
def api_deposit():
    try:
        d = request.json
        amt = float(d.get('amount', 0))
        if amt <= 0:
            return jsonify({'error': 'Valor inválido'}), 400
        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        return jsonify(deriv_client.request_deposit(amt, d.get('currency', 'USD'), d.get('method', 'cryptocurrency')))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/payment/withdraw', methods=['POST'])
@require_auth
def api_withdraw():
    try:
        d = request.json
        amt = float(d.get('amount', 0))
        if amt <= 0:
            return jsonify({'error': 'Valor inválido'}), 400
        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amt > deriv_client.balance:
            return jsonify({'error': 'Saldo insuficiente'}), 400
        return jsonify(deriv_client.request_withdrawal(amt, d.get('currency', 'USD'), d.get('method', 'cryptocurrency')))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
