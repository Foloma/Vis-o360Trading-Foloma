import os, sqlite3, hashlib, base64, json, secrets, time, threading, logging
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, abort
from werkzeug.security import generate_password_hash, check_password_hash

# ==================== CONFIGURAÇÃO ====================
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY é obrigatória. Defina a variável de ambiente.")

DATA_PATH = os.environ.get('DATA_PATH', '/var/data')
DATABASE_PATH = os.path.join(DATA_PATH, 'foloma.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# ==================== ENCRIPTAÇÃO DE TOKENS ====================
try:
    from cryptography.fernet import Fernet
    _fernet_key = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest())
    _fernet = Fernet(_fernet_key)
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logger.warning("Cryptography não instalada. Tokens NÃO serão encriptados.")

def encrypt_token(token: str) -> str:
    if HAS_CRYPTO and token:
        return _fernet.encrypt(token.encode()).decode()
    return token

def decrypt_token(encrypted: str) -> str:
    if HAS_CRYPTO and encrypted:
        try:
            return _fernet.decrypt(encrypted.encode()).decode()
        except Exception:
            logger.error("Falha ao desencriptar token.")
            return encrypted
    return encrypted

# ==================== INICIALIZAÇÃO DA BASE DE DADOS ====================
def init_db():
    os.makedirs(DATA_PATH, exist_ok=True)
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
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        contract_id TEXT UNIQUE,
        symbol TEXT,
        action TEXT,
        amount REAL,
        buy_price REAL,
        sell_price REAL,
        profit REAL,
        result TEXT,
        timestamp REAL DEFAULT (strftime('%s','now'))
    )''')
    conn.commit()
    conn.close()

init_db()

# ==================== MIGRAÇÃO DE users.json (se existir) ====================
def migrate_from_json():
    json_path = os.path.join(DATA_PATH, 'users.json')
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
                                 (email, 'demo', encrypt_token(demo)))
                if real:
                    conn.execute('INSERT OR IGNORE INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                                 (email, 'real', encrypt_token(real)))
            else:
                for acc, tok in tokens.items():
                    if tok:
                        conn.execute('INSERT OR IGNORE INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                                     (email, acc, encrypt_token(tok)))
        conn.commit()
    except Exception as e:
        logger.error(f"Migração falhou: {e}")
    finally:
        conn.close()
    os.rename(json_path, json_path + '.backup')
    logger.info("Migração de JSON concluída.")

migrate_from_json()

# ==================== ARMAZENAMENTO DE UTILIZADORES ====================
class UserStore:
    @staticmethod
    def get(email):
        conn = sqlite3.connect(DATABASE_PATH)
        try:
            row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            if not row:
                return None
            keys = ['email','id','name','password_hash','active_account','created_at','last_login',
                    'referral_code','active','role','affiliate_earnings','referral_link_code']
            user = dict(zip(keys, row))
            tokens = conn.execute('SELECT account_type, token FROM user_tokens WHERE email = ?', (email,)).fetchall()
            user['tokens'] = {acc: decrypt_token(tok) for acc, tok in tokens}
            return user
        finally:
            conn.close()

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
                                 (user['email'], acc, encrypt_token(tok)))
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
        try:
            conn.execute('UPDATE users SET active_account = ? WHERE email = ?', (account_type, email))
            conn.commit()
        finally:
            conn.close()
        return UserStore.get(email)

    @staticmethod
    def add_token(email, account_type, token):
        conn = sqlite3.connect(DATABASE_PATH)
        try:
            conn.execute('INSERT OR REPLACE INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                         (email, account_type, encrypt_token(token)))
            conn.commit()
        finally:
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
            try:
                row = conn.execute('SELECT email FROM users WHERE referral_link_code = ?', (ref,)).fetchone()
                if row:
                    conn.execute('INSERT OR IGNORE INTO referrals (referrer_email, referred_email, timestamp) VALUES (?,?,?)',
                                 (row[0], email, time.time()))
                    conn.execute('UPDATE users SET affiliate_earnings = affiliate_earnings + 1.0 WHERE email = ?', (row[0],))
                    conn.commit()
            except Exception as e:
                logger.error(f"Erro ao processar referral: {e}")
            finally:
                conn.close()
        return user

# ==================== GESTOR DE SESSÃO WEBSOCKET ====================
sessions = {}
sessions_lock = threading.RLock()
session_requests = {}

def reset_bot_state(bot):
    bot.reset_stats()
    bot.reset_martingale()
    if hasattr(bot, 'reset_daily_stats'):
        bot.reset_daily_stats()
    else:
        bot.daily_stats = {'start_balance': 0, 'trades': 0, 'wins': 0, 'losses': 0, 'profit': 0}

def validate_account_type(loginid, expected):
    return loginid.startswith('VR') if expected == 'demo' else not loginid.startswith('VR')

def persist_trade(user_id, trade_data):
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        conn.execute('''INSERT OR REPLACE INTO trades
            (user_id, contract_id, symbol, action, amount, buy_price, sell_price, profit, result, timestamp)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (user_id, trade_data.get('contract_id'), trade_data.get('symbol'),
             trade_data.get('action'), trade_data.get('amount'),
             trade_data.get('buy_price', 0), trade_data.get('sell_price', 0),
             trade_data.get('profit', 0), trade_data.get('result', 'unknown'),
             time.time()))
        conn.commit()
    except Exception as e:
        logger.error(f"Erro ao persistir trade: {e}")
    finally:
        conn.close()

def create_session(user_id, user, force=False):
    with sessions_lock:
        if user_id in sessions:
            existing = sessions[user_id]
            client = existing['client']
            if not force and client.authorized and client.connected:
                return existing
            client._stop_event.set()
            if client._ws_thread and client._ws_thread.is_alive():
                client._ws_thread.join(timeout=5)
            del sessions[user_id]

    from deriv_client import DerivWebSocketClient
    from trading_bot import TradingBot
    from synthetics import DigitAnalyzer

    bot = TradingBot()
    analyzer = DigitAnalyzer(max_digits=500)

    def on_trade_result(trade):
        try:
            result = 'win' if trade.get('is_win') else 'loss'
            persist_trade(user_id, {
                'contract_id': trade.get('contract_id'),
                'symbol': trade.get('symbol', 'R_100'),
                'action': trade.get('action', ''),
                'amount': trade.get('amount', 0),
                'buy_price': trade.get('buy_price', 0),
                'sell_price': trade.get('sell_price', 0),
                'profit': trade.get('profit', 0),
                'result': result
            })
        except Exception as e:
            logger.error(f"Callback de trade falhou: {e}")

    def tick_callback(tick): bot.on_tick(tick)

    client = DerivWebSocketClient(config, on_tick_callback=tick_callback, on_result_callback=on_trade_result)
    client.set_trading_bot(bot)
    client.set_digit_analyzer(analyzer)
    bot.client = client
    bot.digit_analyzer = analyzer

    new_sess = {
        'client': client,
        'trading_bot': bot,
        'digit_analyzer': analyzer
    }
    with sessions_lock:
        sessions[user_id] = new_sess

    token = UserStore.get_active_token(user)
    if token:
        client.set_user_token(token)
        client._connect_lock = threading.Lock()

        def connect_and_validate():
            with client._connect_lock:
                if client._connecting:
                    return
                client._connecting = True
            try:
                client.connect()
                deadline = time.time() + 10
                while not client.authorized and time.time() < deadline:
                    time.sleep(0.2)
            finally:
                with client._connect_lock:
                    client._connecting = False
            if client.authorized:
                if not validate_account_type(client.loginid, user.get('active_account', 'demo')):
                    logger.warning(f"Token inválido para {user['email']} – a remover sessão.")
                    client._stop_event.set()
                    with sessions_lock:
                        sessions.pop(user_id, None)
                    return
                bot.start(client)
                bot.daily_stats['start_balance'] = bot.balance
            else:
                # Limpeza automática de token inválido
                if getattr(client, 'auth_error', {}).get('code') == 'InvalidToken':
                    UserStore.add_token(user['email'], user.get('active_account', 'demo'), '')
                    logger.warning(f"Token expirado para {user['email']} – removido.")

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

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, app=app,
                      default_limits=["200 per day", "50 per hour"],
                      storage_uri="memory://")
except ImportError:
    logger.warning("Flask-Limiter não instalado. Rate limiting desativado.")
    limiter = None

def limit_if_available(limit_string):
    def decorator(f):
        if limiter:
            return limiter.limit(limit_string)(f)
        return f
    return decorator

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

# ==================== ROTA PRINCIPAL ====================
@app.route('/')
def index():
    return render_template('index.html')

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
@limit_if_available("10 per hour")
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
@limit_if_available("5 per minute")
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
@limit_if_available("3 per hour")
def reset_password():
    email = request.json.get('email','').strip().lower()
    user = UserStore.get(email)
    if not user:
        return jsonify({'error': 'Email não encontrado'}), 404
    token = secrets.token_urlsafe(64)
    hashed = hashlib.sha256(token.encode()).hexdigest()
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        conn.execute('INSERT OR REPLACE INTO password_resets (email, token_hash, expires_at, used) VALUES (?,?,?,0)',
                     (email, hashed, time.time() + 3600))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok', 'message': 'Se o email existir, receberá um link.'})

@app.route('/api/auth/reset-password-confirm', methods=['POST'])
def reset_password_confirm():
    token = request.json.get('token')
    new_pw = request.json.get('new_password')
    if not token or new_pw is None or len(new_pw) < 6:
        return jsonify({'error': 'Token ou senha inválidos'}), 400
    hashed = hashlib.sha256(token.encode()).hexdigest()
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        row = conn.execute('SELECT email FROM password_resets WHERE token_hash = ? AND used = 0 AND expires_at > ?',
                           (hashed, time.time())).fetchone()
        if not row:
            return jsonify({'error': 'Token inválido ou expirado'}), 400
        email = row[0]
        new_hash = generate_password_hash(new_pw)
        conn.execute('UPDATE users SET password_hash = ? WHERE email = ?', (new_hash, email))
        conn.execute('UPDATE password_resets SET used = 1 WHERE token_hash = ?', (hashed,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok', 'message': 'Senha alterada com sucesso.'})

# ==================== ROTAS DE CONEXÃO / TRADING ====================
@app.route('/api/connect', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def api_connect():
    email = session['user_email']
    user = UserStore.get(email)
    token = UserStore.get_active_token(user)
    if not token:
        return jsonify({'error': 'Token não configurado'}), 400
    create_session(session['user_id'], user)
    return jsonify({'status': 'connecting', 'account_type': user.get('active_account')})

@app.route('/api/auth/auto-connect')
@require_auth
def auto_connect():
    email = session['user_email']
    user = UserStore.get(email)
    if not UserStore.get_active_token(user):
        return jsonify({
            'status': 'no_token',
            'account_type': user.get('active_account', 'demo')
        })
    sess = get_session(session['user_id'])
    if sess and sess['client'].connected and sess['client'].authorized:
        return jsonify({
            'status': 'already_connected',
            'account_type': user.get('active_account', 'demo'),
            'balance': sess['client'].balance
        })
    create_session(session['user_id'], user)
    return jsonify({'status': 'connecting', 'account_type': user.get('active_account', 'demo')})

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

    # Atualiza a conta ativa na BD
    UserStore.set_active_account(email, acc_type)
    user = UserStore.get(email)

    # Remove a sessão antiga imediatamente
    user_id = session['user_id']
    with sessions_lock:
        if user_id in sessions:
            old_client = sessions[user_id]['client']
            old_client._stop_event.set()
            if old_client._ws_thread and old_client._ws_thread.is_alive():
                old_client._ws_thread.join(timeout=5)
            del sessions[user_id]

    # Cria nova sessão
    sess = create_session(user_id, user, force=True)
    reset_bot_state(sess['trading_bot'])

    # Aguarda até 5 segundos pela autorização
    client = sess['client']
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.authorized and client.loginid:
            if validate_account_type(client.loginid, acc_type):
                return jsonify({
                    'status': 'ok',
                    'message': f'Conta {acc_type} ativada.',
                    'account_type': acc_type,
                    'balance': client.balance,
                    'loginid': client.loginid
                })
        time.sleep(0.3)

    return jsonify({
        'status': 'connecting',
        'message': f'A aguardar conexão da conta {acc_type}...',
        'account_type': acc_type
    })

@app.route('/api/status')
@require_auth
def status():
    user_id = session['user_id']
    sess = get_session(user_id)
    if not sess:
        email = session.get('user_email')
        user = UserStore.get(email)
        if user and UserStore.get_active_token(user):
            sess = create_session(user_id, user)
        else:
            return jsonify({'bot': {}, 'digits': {}, 'symbols': config.AVAILABLE_SYMBOLS})
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
        'symbols': config.AVAILABLE_SYMBOLS,
        'loginid': client.loginid if client else None
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
        'request_id': getattr(c, 'request_id', None),
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
    # Garantir que ambos os tokens estão guardados (same for all accounts received)
    user = UserStore.get(email)
    create_session(session['user_id'], user)
    return redirect('/?connected=true')

@app.route('/api/auth/deriv_oauth_url')
@require_auth
def deriv_oauth_url():
    redirect_uri = os.environ.get('BASE_URL', request.host_url.rstrip('/')) + '/oauth/callback'
    at = request.args.get('account_type', 'demo')
    session['pending_account_type'] = at
    url = f"https://oauth.deriv.com/oauth2/authorize?app_id={config.DERIV_APP_ID}&redirect_uri={redirect_uri}&l=PT"
    return jsonify({'url': url})

# ==================== ROTAS DE TRADING ====================
@app.route('/api/trade', methods=['POST'])
@require_auth
@limit_if_available("20 per minute")
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
@limit_if_available("20 per minute")
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
    try:
        ref_user = conn.execute('SELECT email FROM users WHERE referral_link_code = ?', (ref_code,)).fetchone()
        if ref_user:
            commission = amount * (config.MARKUP_PERCENTAGE / 100)
            conn.execute('UPDATE users SET affiliate_earnings = affiliate_earnings + ? WHERE email = ?',
                         (commission, ref_user[0]))
            conn.commit()
    except Exception as e:
        logger.error(f"Erro ao creditar comissão: {e}")
    finally:
        conn.close()

@app.route('/api/affiliate/stats')
@require_auth
def affiliate_stats():
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
        return jsonify({'status': 'pending', 'message': f'Depósito ${amt} solicitado.', 'amount': amt})
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
        return jsonify({'status': 'pending', 'message': f'Saque ${amt} solicitado.', 'amount': amt})
    except Exception:
        logger.exception("Erro levantamento")
        return jsonify({'error': 'Erro interno'}), 500

# ==================== ADMIN ====================
@app.route('/api/admin/users')
@require_admin
def admin_users():
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        rows = conn.execute('SELECT email, name, active FROM users').fetchall()
        return jsonify({'users': [{'email': r[0], 'name': r[1], 'active': bool(r[2])} for r in rows]})
    finally:
        conn.close()

@app.route('/api/admin/toggle-user', methods=['POST'])
@require_admin
def toggle_user():
    d = request.json
    email = d.get('email')
    en = d.get('enable', True)
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        conn.execute('UPDATE users SET active = ? WHERE email = ?', (1 if en else 0, email))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok', 'message': f'Utilizador {"ativado" if en else "desativado"}.'})

@app.route('/api/admin/clear-tokens', methods=['POST'])
@require_admin
def admin_clear_tokens():
    d = request.json
    email = d.get('email', '').strip().lower()
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        if email:
            conn.execute('DELETE FROM user_tokens WHERE email = ?', (email,))
            conn.commit()
            logger.info(f"Tokens removidos para {email}")
            conn.execute('UPDATE users SET active_account = ? WHERE email = ?', ('demo', email))
            conn.commit()
        else:
            conn.execute('DELETE FROM user_tokens')
            conn.execute('UPDATE users SET active_account = ?', ('demo',))
            conn.commit()
            logger.info("Todos os tokens removidos")
    finally:
        conn.close()
    # Remover sessões ativas desses utilizadores
    with sessions_lock:
        for uid, sess in list(sessions.items()):
            if not email or sess.get('email') == email:
                sess['client']._stop_event.set()
                del sessions[uid]
    return jsonify({'status': 'ok', 'message': 'Tokens removidos. Utilizador terá que refazer OAuth.'})

# ==================== INICIAR ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
