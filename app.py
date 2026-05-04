import os, sqlite3, hashlib, base64, json, secrets, time, threading, logging
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

# ==================== CONFIGURAÇÃO ====================
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY é obrigatória. Defina a variável de ambiente.")

DATABASE_PATH = os.environ.get('DATABASE_PATH', 'foloma.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# ==================== INICIALIZAÇÃO DA BASE DE DADOS ====================
def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        id TEXT NOT NULL,
        name TEXT,
        password_hash TEXT NOT NULL,
        active_account TEXT DEFAULT 'demo',
        created_at REAL,
        last_login REAL,
        referral_code TEXT,
        active INTEGER DEFAULT 1,
        role TEXT DEFAULT 'user',
        affiliate_earnings REAL DEFAULT 0.0,
        referral_link_code TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_tokens (
        email TEXT,
        account_type TEXT,
        token TEXT NOT NULL,
        PRIMARY KEY (email, account_type),
        FOREIGN KEY (email) REFERENCES users(email)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS password_resets (
        email TEXT,
        token_hash TEXT NOT NULL,
        expires_at REAL NOT NULL,
        used INTEGER DEFAULT 0,
        PRIMARY KEY (email, token_hash)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        referrer_email TEXT,
        referred_email TEXT,
        timestamp REAL,
        PRIMARY KEY (referrer_email, referred_email)
    )''')
    conn.commit()
    conn.close()

init_db()

# ==================== MIGRAÇÃO DE users.json PARA SQLite ====================
def migrate_from_json():
    json_path = os.environ.get('DATA_PATH', '.') + '/users.json'
    if not os.path.exists(json_path):
        return
    with open(json_path, 'r') as f:
        old = json.load(f)
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        for email, u in old.items():
            conn.execute('''INSERT OR IGNORE INTO users (email, id, name, password_hash, active_account,
                            created_at, last_login, referral_code, active, role, affiliate_earnings, referral_link_code)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (email, u.get('id'), u.get('name'), u.get('password'),
                          u.get('active_account', u.get('deriv_account_type', 'demo')),
                          u.get('created_at'), u.get('last_login'),
                          u.get('referral_code'), u.get('active', 1), u.get('role', 'user'),
                          u.get('affiliate_earnings', 0.0), u.get('referral_link_code')))
            tokens = u.get('tokens', {})
            if not tokens:
                demo = u.get('deriv_token_demo') or (u.get('deriv_token') if u.get('deriv_account_type') == 'demo' else None)
                real = u.get('deriv_token_real') or (u.get('deriv_token') if u.get('deriv_account_type') == 'real' else None)
                if demo:
                    conn.execute('INSERT OR IGNORE INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                                 (email, 'demo', demo))
                if real:
                    conn.execute('INSERT OR IGNORE INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                                 (email, 'real', real))
            else:
                for acc, tok in tokens.items():
                    if tok:
                        conn.execute('INSERT OR IGNORE INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                                     (email, acc, tok))
        conn.commit()
    except Exception as e:
        logger.error(f"Migração falhou: {e}")
    finally:
        conn.close()
    os.rename(json_path, json_path + '.backup')
    logger.info("Migração de JSON concluída.")

migrate_from_json()

# ==================== ARMAZENAMENTO DE UTILIZADORES (UserStore) ====================
class UserStore:
    @staticmethod
    def get(email):
        conn = sqlite3.connect(DATABASE_PATH)
        row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        conn.close()
        if not row:
            return None
        keys = ['email','id','name','password_hash','active_account','created_at','last_login',
                'referral_code','active','role','affiliate_earnings','referral_link_code']
        user = dict(zip(keys, row))
        conn = sqlite3.connect(DATABASE_PATH)
        tokens = conn.execute('SELECT account_type, token FROM user_tokens WHERE email = ?', (email,)).fetchall()
        conn.close()
        user['tokens'] = {acc: tok for acc, tok in tokens}
        return user

    @staticmethod
    def save(user):
        conn = sqlite3.connect(DATABASE_PATH)
        try:
            conn.execute('''INSERT OR REPLACE INTO users (email, id, name, password_hash, active_account,
                            created_at, last_login, referral_code, active, role, affiliate_earnings, referral_link_code)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (user['email'], user['id'], user['name'], user['password_hash'],
                          user.get('active_account','demo'), user.get('created_at'), user.get('last_login'),
                          user.get('referral_code'), user.get('active',1), user.get('role','user'),
                          user.get('affiliate_earnings',0.0), user.get('referral_link_code')))
            conn.execute('DELETE FROM user_tokens WHERE email = ?', (user['email'],))
            for acc, tok in user.get('tokens', {}).items():
                if tok:
                    conn.execute('INSERT INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                                 (user['email'], acc, tok))
            conn.commit()
        except Exception as e:
            logger.error(f"Erro ao guardar utilizador: {e}")
            raise
        finally:
            conn.close()

    @staticmethod
    def get_active_token(user):
        return user.get('tokens', {}).get(user.get('active_account', 'demo'))

    @staticmethod
    def create_user(email, name, password_hash, referral_code=''):
        uid = str(int(time.time() * 1000))
        ref_link = base64.b64encode(hashlib.md5(uid.encode()).digest()).hex()[:8]
        user = {
            'email': email,
            'id': uid,
            'name': name,
            'password_hash': password_hash,
            'active_account': 'demo',
            'created_at': time.time(),
            'last_login': None,
            'referral_code': referral_code,
            'active': 1,
            'role': 'user',
            'affiliate_earnings': 0.0,
            'referral_link_code': ref_link,
            'tokens': {}
        }
        UserStore.save(user)
        return user

    @staticmethod
    def set_active_account(email, account_type):
        conn = sqlite3.connect(DATABASE_PATH)
        conn.execute('UPDATE users SET active_account = ? WHERE email = ?', (account_type, email))
        conn.commit()
        conn.close()
        return UserStore.get(email)

    @staticmethod
    def add_token(email, account_type, token):
        conn = sqlite3.connect(DATABASE_PATH)
        conn.execute('INSERT OR REPLACE INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                     (email, account_type, token))
        conn.commit()
        conn.close()

# ==================== SERVIÇO DE AUTENTICAÇÃO ====================
class AuthService:
    @staticmethod
    def login(email, password):
        user = UserStore.get(email)
        if not user or not user.get('active'):
            return None
        if not check_password_hash(user['password_hash'], password):
            return None
        user['last_login'] = time.time()
        UserStore.save(user)
        return user

    @staticmethod
    def register(name, email, password, ref):
        if UserStore.get(email):
            return None
        h = generate_password_hash(password)
        user = UserStore.create_user(email, name, h, ref)
        if ref:
            conn = sqlite3.connect(DATABASE_PATH)
            row = conn.execute('SELECT email FROM users WHERE referral_link_code = ?', (ref,)).fetchone()
            if row:
                conn.execute('INSERT OR IGNORE INTO referrals (referrer_email, referred_email, timestamp) VALUES (?,?,?)',
                             (row[0], email, time.time()))
                conn.execute('UPDATE users SET affiliate_earnings = affiliate_earnings + 1.0 WHERE email = ?', (row[0],))
                conn.commit()
            conn.close()
        return user

# ==================== GESTOR DE SESSÃO WEBSOCKET ====================
sessions = {}
sessions_lock = threading.RLock()

def reset_bot_state(bot):
    bot.reset_stats()
    bot.reset_martingale()
    bot.daily_stats = {'start_balance': 0, 'trades': 0, 'wins': 0, 'losses': 0, 'profit': 0}

class InvalidTokenError(Exception):
    pass

def validate_account_type(loginid, expected):
    if expected == 'demo':
        return loginid.startswith('VR')
    else:
        return not loginid.startswith('VR')

def create_session(user_id, user):
    with sessions_lock:
        if user_id in sessions:
            old_client = sessions[user_id]['client']
            old_client._stop_event.set()
            if old_client._ws_thread and old_client._ws_thread.is_alive():
                old_client._ws_thread.join(timeout=5)
            del sessions[user_id]
    bot = TradingBot()
    analyzer = DigitAnalyzer(max_digits=500)
    def tick_callback(tick):
        bot.on_tick(tick)
    client = DerivWebSocketClient(config, on_tick_callback=tick_callback)
    client.set_trading_bot(bot)
    client.set_digit_analyzer(analyzer)
    payment = PaymentSystem(client)
    client.set_payment_system(payment)
    bot.client = client
    bot.digit_analyzer = analyzer
    new_sess = {
        'client': client,
        'trading_bot': bot,
        'digit_analyzer': analyzer,
        'payment_system': payment
    }
    with sessions_lock:
        sessions[user_id] = new_sess
    token = UserStore.get_active_token(user)
    if token:
        client.set_user_token(token)
        def connect_and_validate():
            if getattr(client, '_connecting', False):
                return
            client._connecting = True
            client.connect()
            deadline = time.time() + 10
            while not client.authorized and time.time() < deadline:
                time.sleep(0.2)
            client._connecting = False
            if client.authorized:
                if not validate_account_type(client.loginid, user.get('active_account', 'demo')):
                    logger.warning(f"Token inválido para {user['email']}.")
                    client._stop_event.set()
                    with sessions_lock:
                        sessions.pop(user_id, None)
                    raise InvalidTokenError("Token não corresponde ao tipo de conta.")
                bot.start(client)
                bot.daily_stats['start_balance'] = bot.balance
        threading.Thread(target=connect_and_validate, daemon=True).start()
    return new_sess

def get_session(user_id):
    with sessions_lock:
        return sessions.get(user_id)

# ==================== INICIALIZAÇÃO DO FLASK ====================
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = 86400

from config import config
from deriv_client import DerivWebSocketClient
from trading_bot import TradingBot
from synthetics import DigitAnalyzer
from payment_system import PaymentSystem

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ==================== MIDDLEWARE ====================
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
        user = UserStore.get(session.get('user_email'))
        if not user or user.get('role') != 'admin':
            return jsonify({'error': 'Acesso restrito'}), 403
        return f(*args, **kwargs)
    return decorated

# ==================== ROTAS DE AUTENTICAÇÃO ====================
@app.route('/api/auth/status')
def auth_status():
    if 'user_id' in session:
        user = UserStore.get(session.get('user_email'))
        if user:
            return jsonify({
                'authenticated': True,
                'user': {
                    'id': user['id'], 'name': user['name'], 'email': user['email'],
                    'role': user.get('role'),
                    'has_deriv_token': bool(UserStore.get_active_token(user))
                }
            })
    return jsonify({'authenticated': False})

@app.route('/api/auth/register', methods=['POST'])
@limiter.limit("10 per hour")
def register():
    try:
        d = request.json
        email = d.get('email','').strip().lower()
        name = d.get('name','').strip()
        password = d.get('password','')
        ref = d.get('referral_code','')
        if not (name and email and len(password)>=6):
            return jsonify({'error': 'Campos obrigatórios inválidos'}), 400
        user = AuthService.register(name, email, password, ref)
        if not user:
            return jsonify({'error': 'Email já registado'}), 400
        return jsonify({'status': 'ok', 'message': 'Conta criada!', 'referral_code': user['referral_link_code']})
    except Exception:
        logger.exception("Erro no registo")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/auth/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    try:
        d = request.json
        email = d.get('email','').strip().lower()
        password = d.get('password','')
        user = AuthService.login(email, password)
        if not user:
            return jsonify({'error': 'Credenciais inválidas ou conta desativada'}), 400
        session.permanent = True
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['user_email'] = user['email']
        session['user_role'] = user.get('role', 'user')
        logger.info(f"Login: {email}")
        return jsonify({'status': 'ok', 'user': {
            'id': user['id'], 'name': user['name'], 'email': user['email'],
            'role': session['user_role'], 'has_deriv_token': bool(UserStore.get_active_token(user))
        }})
    except Exception:
        logger.exception("Erro no login")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    user_id = session.get('user_id')
    if user_id:
        sess = get_session(user_id)
        if sess:
            sess['client']._stop_event.set()
        with sessions_lock:
            sessions.pop(user_id, None)
    session.clear()
    return jsonify({'status': 'ok'})

@app.route('/api/auth/save_token', methods=['POST'])
@require_auth
def save_token():
    d = request.json
    token = d.get('token')
    at = d.get('account_type', 'demo')
    if not token:
        return jsonify({'error': 'Token obrigatório'}), 400
    email = session['user_email']
    UserStore.add_token(email, at, token)
    user = UserStore.get(email)
    user['active_account'] = at
    UserStore.save(user)
    return jsonify({'status': 'ok'})

@app.route('/api/auth/reset-password', methods=['POST'])
@limiter.limit("3 per hour")
def reset_password():
    email = request.json.get('email','').strip().lower()
    user = UserStore.get(email)
    if not user:
        return jsonify({'error': 'Email não encontrado'}), 404
    token = secrets.token_urlsafe(64)
    hashed = hashlib.sha256(token.encode()).hexdigest()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute('INSERT OR REPLACE INTO password_resets (email, token_hash, expires_at, used) VALUES (?,?,?,0)',
                 (email, hashed, time.time() + 3600))
    conn.commit()
    conn.close()
    # Enviar email aqui (omitido)
    return jsonify({'status': 'ok', 'message': 'Se o email existir, receberá um link.'})

@app.route('/api/auth/reset-password-confirm', methods=['POST'])
def reset_password_confirm():
    token = request.json.get('token')
    new_pw = request.json.get('new_password')
    if not token or len(new_pw) < 6:
        return jsonify({'error': 'Token ou senha inválidos'}), 400
    hashed = hashlib.sha256(token.encode()).hexdigest()
    conn = sqlite3.connect(DATABASE_PATH)
    row = conn.execute('SELECT email FROM password_resets WHERE token_hash = ? AND used = 0 AND expires_at > ?',
                       (hashed, time.time())).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Token inválido ou expirado'}), 400
    email = row[0]
    new_hash = generate_password_hash(new_pw)
    conn.execute('UPDATE users SET password_hash = ? WHERE email = ?', (new_hash, email))
    conn.execute('UPDATE password_resets SET used = 1 WHERE token_hash = ?', (hashed,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'message': 'Senha alterada com sucesso.'})

# ==================== ROTAS DE CONEXÃO / TRADING ====================
@app.route('/api/connect', methods=['POST'])
@require_auth
@limiter.limit("10 per minute")
def api_connect():
    email = session['user_email']
    user = UserStore.get(email)
    token = UserStore.get_active_token(user)
    if not token:
        return jsonify({'error': 'Token não configurado'}), 400
    sess = create_session(session['user_id'], user)
    client = sess['client']
    if not client.authorized and not getattr(client, '_connecting', False):
        client.set_user_token(token)
        client._connecting = True
        threading.Thread(target=client.connect, daemon=True).start()
    return jsonify({'status': 'connecting', 'account_type': user.get('active_account')})

@app.route('/api/auth/auto-connect')
@require_auth
def auto_connect():
    email = session['user_email']
    user = UserStore.get(email)
    if not UserStore.get_active_token(user):
        return jsonify({'status': 'no_token'})
    sess = create_session(session['user_id'], user)
    return jsonify({'status': 'connecting', 'account_type': user.get('active_account')})

@app.route('/api/auth/switch-account', methods=['POST'])
@require_auth
def switch_account():
    d = request.json
    acc_type = d.get('account_type','').strip().lower()
    if acc_type not in ('demo','real'):
        return jsonify({'error': 'Tipo inválido'}), 400
    email = session['user_email']
    user = UserStore.get(email)
    if not user.get('tokens', {}).get(acc_type):
        return jsonify({'error': 'Sem token para essa conta'}), 400
    UserStore.set_active_account(email, acc_type)
    user = UserStore.get(email)
    sess = create_session(session['user_id'], user)
    reset_bot_state(sess['trading_bot'])
    return jsonify({'status': 'ok', 'message': f'Conta {acc_type} ativada. A aguardar conexão...',
                    'account_type': acc_type})

@app.route('/api/status')
@require_auth
def status():
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'error': 'Sessão não encontrada'}), 500
    client = sess['client']
    bot = sess['trading_bot']
    analyzer = sess['digit_analyzer']
    if client:
        bot.balance = client.balance
        bot.currency = client.currency
        bot._client_connected = client.connected
        bot._client_authorized = client.authorized
    bot_status = bot.get_status()
    bot_status['streaming'] = client.streaming if client else False
    analysis = analyzer.get_analysis()
    return jsonify({
        'bot': bot_status,
        'digits': {
            'last': analyzer.get_current_digit(),
            'parity': analyzer.get_current_parity(),
            'stats': analyzer.get_stats(),
            'analysis': analysis,
            'recent': analyzer.get_recent_digits(),
            'total': len(analyzer.get_recent_digits()),
            'ticks_remaining': analyzer.get_ticks_remaining(),
            'digit_counter': analyzer.get_digit_counter(),
            'ticks_per_digit': analyzer.TICKS_PER_DIGIT
        },
        'symbols': config.AVAILABLE_SYMBOLS
    })

@app.route('/api/debug')
def debug():
    if 'user_id' not in session:
        abort(401)
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'error': 'Sessão não encontrada'}), 500
    c = sess['client']
    return jsonify({
        'connected': c.connected,
        'authorized': c.authorized,
        'streaming': c.streaming,
        'balance': c.balance,
        'symbol': c.current_symbol,
        'loginid': c.loginid if hasattr(c,'loginid') else None,
        'ws_thread_alive': c._ws_thread.is_alive() if c._ws_thread else False,
        'pending_trade': c.pending_trade is not None,
        'last_tick_seconds_ago': round(time.time() - c._last_tick_time, 1) if c._last_tick_time else None
    })

@app.route('/oauth/callback')
def oauth_callback():
    at = session.pop('pending_account_type', 'demo')
    if 'user_id' not in session:
        return redirect('/?error=not_logged_in')
    email = session.get('user_email')
    accounts = []
    i = 1
    while request.args.get(f'token{i}'):
        accounts.append({
            'token': request.args.get(f'token{i}'),
            'acct': request.args.get(f'acct{i}')
        })
        i += 1
    if not accounts:
        return redirect('/?error=oauth_failed')
    for acc in accounts:
        acct = acc['acct']
        tok = acc['token']
        if not tok:
            continue
        if acct.startswith('VR') or acct.startswith('VRTC'):
            UserStore.add_token(email, 'demo', tok)
        else:
            UserStore.add_token(email, 'real', tok)
        if (at == 'demo' and (acct.startswith('VR') or acct.startswith('VRTC'))) or \
           (at == 'real' and not acct.startswith('VR') and not acct.startswith('VRTC')):
            UserStore.set_active_account(email, at)
    user = UserStore.get(email)
    create_session(session['user_id'], user)
    return redirect('/?connected=true')

@app.route('/api/auth/deriv_oauth_url')
@require_auth
def deriv_oauth_url():
    redirect_uri = request.host_url.rstrip('/') + '/oauth/callback'
    url = f"https://oauth.deriv.com/oauth2/authorize?app_id={config.DERIV_APP_ID}&redirect_uri={redirect_uri}&l=PT"
    return jsonify({'url': url})

# ==================== OPERAÇÕES DE TRADING ====================
@app.route('/api/trade', methods=['POST'])
@require_auth
@limiter.limit("20 per minute")
def trade():
    try:
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        d = request.json
        action = d.get('action')
        amt = float(d.get('amount', 0.35))
        if amt < 0.35 or amt > 100:
            return jsonify({'error': 'Valor inválido'}), 400
        bot = sess['trading_bot']
        sig, conf = bot.calculate_signal()
        if conf < config.RISK_LIMITS.get('min_confidence', 50):
            return jsonify({'error': f'Confiança insuficiente: {conf:.1f}%'}), 400
        ok = sess['client'].place_trade('CALL' if action == 'BUY' else 'PUT', amt, False)
        if ok:
            credit_affiliate_commission(session['user_email'], amt)
            return jsonify({'status': 'ok', 'message': f'Trade {action} enviado', 'confidence': conf})
        return jsonify({'error': 'Falha no trade'}), 500
    except Exception:
        logger.exception("Erro no trade")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/trade/digit', methods=['POST'])
@require_auth
@limiter.limit("20 per minute")
def trade_digit():
    try:
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        d = request.json
        pred = d.get('prediction')
        amt = float(d.get('amount', 0.35))
        if pred not in ('odd', 'even'):
            return jsonify({'error': 'Use "odd" ou "even"'}), 400
        if amt < 0.35 or amt > 100:
            return jsonify({'error': 'Valor inválido'}), 400
        analyzer = sess['digit_analyzer']
        tr = analyzer.get_ticks_remaining()
        if tr < 2:
            return jsonify({'error': f'Dígito a sair em {tr} tick(s). Aguarde.'}), 400
        ok = sess['client'].place_trade('CALL' if pred == 'odd' else 'PUT', amt, True)
        if ok:
            credit_affiliate_commission(session['user_email'], amt)
            label = 'ÍMPAR' if pred == 'odd' else 'PAR'
            return jsonify({'status': 'ok', 'message': f'✅ {label} por ${amt:.2f}', 'ticks_remaining': tr})
        return jsonify({'error': 'Falha no trade'}), 500
    except Exception:
        logger.exception("Erro trade dígito")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/trade/hybrid', methods=['POST'])
@require_auth
def trade_hybrid():
    try:
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        d = request.json
        amt = float(d.get('amount', 0.35))
        bot = sess['trading_bot']
        analyzer = sess['digit_analyzer']
        if 'R_' not in bot.current_symbol:
            return jsonify({'error': 'Híbrido apenas para índices R_'}), 400
        sig, conf_a = bot.calculate_signal()
        da = analyzer.get_analysis()
        dr = da.get('recommended_action')
        dc = da.get('confidence', 0)
        if sig == 'BUY' and dr == 'BUY':
            comb = (conf_a + dc) / 2
            action = 'BUY'
        elif sig == 'SELL' and dr == 'SELL':
            comb = (conf_a + dc) / 2
            action = 'SELL'
        else:
            return jsonify({'error': 'Sinais divergentes'}), 400
        if comb < config.ADVANCED_STRATEGY.get('hybrid_min_confidence', 60):
            return jsonify({'error': f'Confiança baixa ({comb:.1f}%)'}), 400
        ok = sess['client'].place_trade('CALL' if action == 'BUY' else 'PUT', amt, False)
        if ok:
            credit_affiliate_commission(session['user_email'], amt)
            return jsonify({'status': 'ok', 'message': 'Híbrido confirmado', 'confidence': comb})
        return jsonify({'error': 'Falha'}), 500
    except Exception:
        logger.exception("Erro trade híbrido")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/trade/manual', methods=['POST'])
@require_auth
def trade_manual():
    try:
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        d = request.json
        action = d.get('action')
        amt = float(d.get('amount', 0.35))
        if amt < 0.35 or amt > 100:
            return jsonify({'error': 'Valor inválido'}), 400
        ok = sess['client'].place_trade('CALL' if action == 'BUY' else 'PUT', amt, False)
        if ok:
            return jsonify({'status': 'ok', 'message': f'Trade manual {action}!'})
        return jsonify({'error': 'Falha'}), 500
    except Exception:
        logger.exception("Erro trade manual")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/symbol/change', methods=['POST'])
@require_auth
def symbol_change():
    try:
        d = request.json
        sym = d.get('symbol')
        if sym not in config.AVAILABLE_SYMBOLS:
            return jsonify({'error': 'Símbolo inválido'}), 400
        sess = get_session(session['user_id'])
        if not sess:
            return jsonify({'error': 'Sessão não encontrada'}), 500
        sess['client'].change_symbol(sym)
        sess['trading_bot'].current_symbol = sym
        return jsonify({'status': 'ok', 'symbol': sym})
    except Exception:
        logger.exception("Erro mudar símbolo")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/pause', methods=['POST'])
@require_auth
def pause():
    try:
        d = request.json
        p = d.get('paused', True)
        sess = get_session(session['user_id'])
        if sess:
            bot = sess['trading_bot']
            if p:
                bot.pause()
            else:
                bot.resume()
        return jsonify({'paused': p})
    except Exception:
        logger.exception("Erro pause")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/martingale/status')
@require_auth
def martingale_status():
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'error': 'Sessão não encontrada'}), 500
    return jsonify(sess['trading_bot'].get_martingale_status())

@app.route('/api/martingale/apply', methods=['POST'])
@require_auth
def martingale_apply():
    d = request.json
    la = float(d.get('last_amount', 0))
    if la <= 0:
        return jsonify({'error': 'Valor inválido'}), 400
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'error': 'Sessão não encontrada'}), 500
    ok, res = sess['trading_bot'].apply_martingale_after_loss(la)
    if ok:
        return jsonify({'status': 'ok', 'martingale': res})
    return jsonify({'error': res}), 400

@app.route('/api/martingale/reset', methods=['POST'])
@require_auth
def martingale_reset():
    sess = get_session(session['user_id'])
    if sess:
        sess['trading_bot'].reset_martingale()
    return jsonify({'status': 'ok'})

@app.route('/api/clear_history', methods=['POST'])
@require_auth
def clear_history():
    sess = get_session(session['user_id'])
    if sess:
        sess['trading_bot'].reset_stats()
    return jsonify({'status': 'ok'})

@app.route('/api/report')
@require_auth
def report():
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'error': 'Sessão não encontrada'}), 500
    return jsonify(sess['trading_bot'].get_trade_report())

# ==================== AFILIADOS E PAGAMENTOS ====================
def credit_affiliate_commission(user_email, amount):
    user = UserStore.get(user_email)
    if not user or not user.get('referral_code'):
        return
    ref_code = user['referral_code']
    conn = sqlite3.connect(DATABASE_PATH)
    ref_user = conn.execute('SELECT email FROM users WHERE referral_link_code = ?', (ref_code,)).fetchone()
    if ref_user:
        commission = amount * (config.MARKUP_PERCENTAGE / 100)
        conn.execute('UPDATE users SET affiliate_earnings = affiliate_earnings + ? WHERE email = ?',
                     (commission, ref_user[0]))
        conn.commit()
    conn.close()

@app.route('/api/affiliate/stats')
@require_auth
def affiliate_stats():
    # retornar estatísticas globais do sistema de afiliados (pode ser simplificado)
    return jsonify({'total_referrals': 0, 'total_commission': 0.0, 'pending_commission': 0.0, 'paid_commission': 0.0})

@app.route('/api/affiliate/link')
@require_auth
def affiliate_link():
    user = UserStore.get(session['user_email'])
    if user and user.get('referral_link_code'):
        return jsonify({'link': f"https://foloma.com/?ref={user['referral_link_code']}",
                        'code': user['referral_link_code']})
    return jsonify({'error': 'Utilizador não encontrado'}), 404

@app.route('/api/affiliate/earnings')
@require_auth
def affiliate_earnings():
    user = UserStore.get(session['user_email'])
    if not user:
        return jsonify({'error': 'Utilizador não encontrado'}), 404
    return jsonify({
        'earnings': user.get('affiliate_earnings', 0.0),
        'referral_link': user.get('referral_link_code', ''),
        'referred_count': 0,
        'referred_list': []
    })

@app.route('/api/payment/deposit', methods=['POST'])
@require_auth
def deposit():
    try:
        d = request.json
        amt = float(d.get('amount', 0))
        if amt <= 0:
            return jsonify({'error': 'Valor inválido'}), 400
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        return jsonify(sess['client'].request_deposit(amt, d.get('currency', 'USD'), d.get('method', 'cryptocurrency')))
    except Exception:
        logger.exception("Erro depósito")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/payment/withdraw', methods=['POST'])
@require_auth
def withdraw():
    try:
        d = request.json
        amt = float(d.get('amount', 0))
        if amt <= 0:
            return jsonify({'error': 'Valor inválido'}), 400
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        if amt > sess['client'].balance:
            return jsonify({'error': 'Saldo insuficiente'}), 400
        return jsonify(sess['client'].request_withdrawal(amt, d.get('currency', 'USD'), d.get('method', 'cryptocurrency')))
    except Exception:
        logger.exception("Erro levantamento")
        return jsonify({'error': 'Erro interno'}), 500

# ==================== ADMIN ====================
@app.route('/api/admin/users')
@require_admin
def admin_users():
    conn = sqlite3.connect(DATABASE_PATH)
    rows = conn.execute('SELECT email, name, active FROM users').fetchall()
    conn.close()
    return jsonify([{'email': r[0], 'name': r[1], 'active': bool(r[2])} for r in rows])

@app.route('/api/admin/toggle-user', methods=['POST'])
@require_admin
def toggle_user():
    d = request.json
    email = d.get('email')
    en = d.get('enable', True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute('UPDATE users SET active = ? WHERE email = ?', (1 if en else 0, email))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ==================== INICIAR ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
