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
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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
            'referral_code': referral_code, 'referrals': [],
            'active': True
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
        if not user.get('active', True):
            return jsonify({'error': 'Conta desativada. Contacte o administrador.'}), 400
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

# ========== ADMINISTRAÇÃO ==========
@app.route('/api/admin/users', methods=['GET'])
@require_auth
def api_admin_users():
    email = session.get('user_email')
    if email != 'admin@foloma.com':
        return jsonify({'error': 'Acesso negado'}), 403
    user_list = []
    for u_email, u in users.items():
        user_list.append({
            'email': u_email,
            'name': u.get('name'),
            'active': u.get('active', True)
        })
    return jsonify({'users': user_list})

@app.route('/api/admin/toggle-user', methods=['POST'])
@require_auth
def api_admin_toggle_user():
    email = session.get('user_email')
    if email != 'admin@foloma.com':
        return jsonify({'error': 'Acesso negado'}), 403
    data = request.json
    target_email = data.get('email')
    enable = data.get('enable', True)
    if target_email not in users:
        return jsonify({'error': 'Utilizador não encontrado'}), 404
    users[target_email]['active'] = enable
    save_users(users)
    return jsonify({'status': 'ok', 'message': f'Utilizador {"ativado" if enable else "desativado"} com sucesso.'})

# ========== RECUPERAÇÃO DE SENHA ==========
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
SMTP_USER = 'seuemail@gmail.com'      # ALTERE AQUI
SMTP_PASSWORD = 'sua_senha_app'       # ALTERE AQUI

reset_tokens = {}

def send_reset_email(to_email, token):
    reset_link = f"https://visao360-jf.onrender.com/reset-password?token={token}"
    subject = "Recuperação de senha - Foloma Trading"
    body = f"""Olá,
Solicitou a recuperação da sua senha. Clique no link abaixo para redefinir a sua senha:
{reset_link}
Se não foi você, ignore este email.
"""
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
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
    data = request.json
    email = data.get('email', '').strip().lower()
    if email not in users:
        return jsonify({'error': 'Email não registado'}), 404
    token = secrets.token_urlsafe(32)
    reset_tokens[token] = email
    if send_reset_email(email, token):
        return jsonify({'status': 'ok', 'message': 'Link de recuperação enviado para o email.'})
    else:
        return jsonify({'error': 'Erro ao enviar email. Tente mais tarde.'}), 500

@app.route('/api/auth/reset-password-confirm', methods=['POST'])
def api_reset_password_confirm():
    data = request.json
    token = data.get('token')
    new_password = data.get('new_password')
    if token not in reset_tokens:
        return jsonify({'error': 'Token inválido ou expirado'}), 400
    email = reset_tokens[token]
    if email not in users:
        return jsonify({'error': 'Utilizador não encontrado'}), 404
    users[email]['password'] = hashlib.sha256(new_password.encode()).hexdigest()
    save_users(users)
    del reset_tokens[token]
    return jsonify({'status': 'ok', 'message': 'Senha alterada com sucesso.'})

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

# ========== TRADES ==========
@app.route('/api/trade', methods=['POST'])
@require_auth
def api_trade():
    try:
        data = request.json
        action = data.get('action')
        amount = float(data.get('amount', 0.35))
        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amount < 0.35 or amount > 100:
            return jsonify({'error': 'Valor inválido'}), 400
        signal, confidence = trading_bot.calculate_signal()
        min_confidence = config.RISK_LIMITS.get('min_confidence', 50)
        if confidence < min_confidence:
            return jsonify({'error': f'Confiança baixa: {confidence:.1f}%'}), 400
        contract_type = 'CALL' if action == 'BUY' else 'PUT'
        success = deriv_client.place_trade(contract_type=contract_type, amount=amount, is_digit=False)
        if success:
            if hasattr(deriv_client, 'markup_percentage') and deriv_client.markup_percentage > 0:
                affiliate.calculate_commission(amount, deriv_client.markup_percentage)
            return jsonify({'status': 'ok', 'message': f'Trade {action} enviado', 'confidence': confidence})
        else:
            return jsonify({'error': 'Falha no trade'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/trade/digit', methods=['POST'])
@require_auth
def api_trade_digit():
    try:
        data = request.json
        prediction = data.get('prediction')
        amount = float(data.get('amount', 0.35))
        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amount < 0.35 or amount > 100:
            return jsonify({'error': 'Valor inválido'}), 400
        # Sem verificação de confiança (operação manual)
        contract_type = 'CALL' if prediction == 'odd' else 'PUT'
        success = deriv_client.place_trade(contract_type=contract_type, amount=amount, is_digit=True)
        if success:
            response = {'status': 'ok', 'message': f'Aposta em {prediction.upper()} enviada!'}
            if hasattr(deriv_client, 'markup_percentage') and deriv_client.markup_percentage > 0:
                affiliate.calculate_commission(amount, deriv_client.markup_percentage)
            return jsonify(response)
        else:
            return jsonify({'error': 'Falha no trade'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== MODO HÍBRIDO ==========
@app.route('/api/trade/hybrid', methods=['POST'])
@require_auth
def api_trade_hybrid():
    try:
        data = request.json
        amount = float(data.get('amount', 0.35))
        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amount < 0.35 or amount > 100:
            return jsonify({'error': 'Valor inválido'}), 400

        signal, conf_ativo = trading_bot.calculate_signal()
        digit_analysis = digit_analyzer.get_analysis()
        digit_recommend = digit_analysis.get('recommended_action')
        digit_conf = digit_analysis.get('confidence', 0)

        logger.info(f"🔍 Híbrido: Sinal ativo={signal}, conf_ativo={conf_ativo}")
        logger.info(f"🔍 Híbrido: Dígitos recomendação={digit_recommend}, conf_digit={digit_conf}")

        if signal == 'BUY' and digit_recommend == 'BUY':
            combined_conf = (conf_ativo + digit_conf) / 2
            action = 'BUY'
            message = '✅ Sinal CONFIRMADO: Ativo e Dígitos apontam para COMPRA'
        elif signal == 'SELL' and digit_recommend == 'SELL':
            combined_conf = (conf_ativo + digit_conf) / 2
            action = 'SELL'
            message = '✅ Sinal CONFIRMADO: Ativo e Dígitos apontam para VENDA'
        else:
            logger.info("⚠️ Híbrido: Sinais divergentes")
            return jsonify({'error': '⚠️ Sinais divergentes. Aguarde convergência.'}), 400

        min_hybrid = config.ADVANCED_STRATEGY.get('hybrid_min_confidence', 60)
        if combined_conf < min_hybrid:
            logger.info(f"❌ Híbrido: Confiança combinada baixa: {combined_conf:.1f}% (mínimo {min_hybrid}%)")
            return jsonify({'error': f'Confiança combinada baixa ({combined_conf:.1f}%)'}), 400

        contract_type = 'CALL' if action == 'BUY' else 'PUT'
        success = deriv_client.place_trade(contract_type=contract_type, amount=amount, is_digit=False)
        if success:
            if hasattr(deriv_client, 'markup_percentage') and deriv_client.markup_percentage > 0:
                affiliate.calculate_commission(amount, deriv_client.markup_percentage)
            logger.info(f"✅ Híbrido: Trade {action} executado com confiança {combined_conf:.1f}%")
            return jsonify({'status': 'ok', 'message': message, 'confidence': combined_conf})
        else:
            return jsonify({'error': 'Falha no trade'}), 500
    except Exception as e:
        logger.error(f"❌ Erro no modo híbrido: {e}")
        return jsonify({'error': str(e)}), 500

# ========== MODO MANUAL ==========
@app.route('/api/trade/manual', methods=['POST'])
@require_auth
def api_trade_manual():
    try:
        data = request.json
        action = data.get('action')
        amount = float(data.get('amount', 0.35))
        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amount < 0.35 or amount > 100:
            return jsonify({'error': 'Valor inválido'}), 400

        contract_type = 'CALL' if action == 'BUY' else 'PUT'
        success = deriv_client.place_trade(contract_type=contract_type, amount=amount, is_digit=False)
        if success:
            if hasattr(deriv_client, 'markup_percentage') and deriv_client.markup_percentage > 0:
                affiliate.calculate_commission(amount, deriv_client.markup_percentage)
            return jsonify({'status': 'ok', 'message': f'Trade manual {action} executado!'})
        else:
            return jsonify({'error': 'Falha no trade'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== LIMPAR HISTÓRICO ==========
@app.route('/api/clear_history', methods=['POST'])
@require_auth
def api_clear_history():
    try:
        trading_bot.reset_stats()
        return jsonify({'status': 'ok', 'message': 'Histórico apagado com sucesso!'})
    except Exception as e:
        logger.error(f"Erro ao limpar histórico: {e}")
        return jsonify({'error': str(e)}), 500

# ========== OUTRAS ROTAS ==========
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
    data = request.json
    paused = data.get('paused', True)
    if paused:
        trading_bot.pause()
    else:
        trading_bot.resume()
    return jsonify({'paused': paused})

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
        data = request.json
        last_amount = float(data.get('last_amount', 0))
        if last_amount <= 0:
            return jsonify({'error': 'Valor inválido'}), 400
        success, result = trading_bot.apply_martingale_after_loss(last_amount)
        if success:
            return jsonify({'status': 'ok', 'martingale': result})
        else:
            return jsonify({'error': result}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/martingale/reset', methods=['POST'])
@require_auth
def api_martingale_reset():
    try:
        trading_bot.reset_martingale()
        return jsonify({'status': 'ok', 'message': 'Martingale resetado'})
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
        user_id = session.get('user_id')
        link = affiliate.generate_referral_link(user_id)
        return jsonify({'link': link, 'code': link.split('/')[-1]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/payment/deposit', methods=['POST'])
@require_auth
def api_deposit():
    try:
        data = request.json
        amount = float(data.get('amount', 0))
        currency = data.get('currency', 'USD')
        method = data.get('method', 'cryptocurrency')
        if amount <= 0:
            return jsonify({'error': 'Valor inválido'}), 400
        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        result = deriv_client.request_deposit(amount, currency, method)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/payment/withdraw', methods=['POST'])
@require_auth
def api_withdraw():
    try:
        data = request.json
        amount = float(data.get('amount', 0))
        currency = data.get('currency', 'USD')
        method = data.get('method', 'cryptocurrency')
        if amount <= 0:
            return jsonify({'error': 'Valor inválido'}), 400
        if not deriv_client or not deriv_client.authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amount > deriv_client.balance:
            return jsonify({'error': 'Saldo insuficiente'}), 400
        result = deriv_client.request_withdrawal(amount, currency, method)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
