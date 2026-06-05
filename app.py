import os, sqlite3, hashlib, base64, json, secrets, time, threading, logging, uuid
from datetime import datetime, date
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, abort, make_response
from werkzeug.security import generate_password_hash, check_password_hash
import urllib.parse

# ==================== CONFIGURAÇÃO ====================
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY é obrigatória. Defina a variável de ambiente.")

DATA_PATH = os.environ.get('DATA_PATH', '/var/data')
DATABASE_PATH = os.path.join(DATA_PATH, 'foloma.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# ==================== ENVIO DE EMAIL (opcional) ====================
mail = None
try:
    from flask_mail import Mail, Message
    mail = Mail()
    EMAIL_ENABLED = True
except ImportError:
    logger.warning("Flask-Mail não instalado. Emails não serão enviados (apenas log).")
    EMAIL_ENABLED = False

def send_reset_email(email, reset_token):
    reset_url = f"{os.environ.get('BASE_URL', 'http://localhost:5000')}/reset-password?token={reset_token}"
    if EMAIL_ENABLED and mail:
        try:
            msg = Message(
                subject="Foloma Visão 360 - Recuperação de senha",
                recipients=[email],
                body=f"Use este link para redefinir a sua senha: {reset_url}\nValidade: 1 hora.",
                sender=os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@foloma.com')
            )
            mail.send(msg)
            logger.info(f"Email de recuperação enviado para {email}")
            return True
        except Exception as e:
            logger.error(f"Falha ao enviar email para {email}: {e}")
            return False
    else:
        logger.info(f"EMAIL NÃO CONFIGURADO – link de recuperação para {email}: {reset_url}")
        return False

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
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')

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
        referral_link_code TEXT,
        daily_stats_json TEXT
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
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS martingale_state (
        user_id TEXT PRIMARY KEY,
        active INTEGER DEFAULT 0,
        step INTEGER DEFAULT 0,
        original_amount REAL DEFAULT 0,
        last_updated REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        timestamp REAL NOT NULL,
        symbol TEXT,
        signal TEXT,
        confidence REAL,
        tech_confidence REAL,
        digit_confidence REAL,
        digit_action TEXT,
        regime TEXT DEFAULT 'UNKNOWN',
        executed INTEGER DEFAULT 0,
        result TEXT,
        profit REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS oauth_states (
        state_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        account_type TEXT DEFAULT 'demo',
        created_at REAL NOT NULL,
        used INTEGER DEFAULT 0
    )''')
    try:
        c.execute("ALTER TABLE users ADD COLUMN daily_stats_json TEXT")
    except sqlite3.OperationalError:
        pass
    c.execute("DELETE FROM password_resets WHERE expires_at < ?", (time.time(),))
    conn.commit()
    conn.close()

init_db()

OAUTH_STATE_TTL = 900
OAUTH_STATE_REUSE_WINDOW = 30

def _cleanup_loop():
    while True:
        time.sleep(3600)
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            conn.execute("DELETE FROM password_resets WHERE expires_at < ?", (time.time(),))
            conn.execute("DELETE FROM oauth_states WHERE created_at < ?",
                         (time.time() - OAUTH_STATE_TTL,))
            conn.commit()
            conn.close()
            logger.debug("Limpeza periódica executada.")
        except Exception as e:
            logger.error(f"Erro na limpeza periódica: {e}")

threading.Thread(target=_cleanup_loop, daemon=True).start()

def load_markup_from_db():
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        row = conn.execute("SELECT value FROM settings WHERE key='markup_percentage'").fetchone()
        if row:
            from config import config
            config.MARKUP_PERCENTAGE = float(row[0])
            logger.info(f"Markup carregado da BD: {config.MARKUP_PERCENTAGE}%")
        conn.close()
    except Exception as e:
        logger.error(f"Erro ao carregar markup: {e}")

load_markup_from_db()

def migrate_from_json():
    json_path = os.path.join(DATA_PATH, 'users.json')
    if not os.path.exists(json_path):
        return
    with open(json_path, 'r') as f:
        old = json.load(f)
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
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
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        try:
            row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            if not row:
                return None
            keys = ['email','id','name','password_hash','active_account','created_at','last_login',
                    'referral_code','active','role','affiliate_earnings','referral_link_code','daily_stats_json']
            user = dict(zip(keys, row))
            tokens = conn.execute('SELECT account_type, token FROM user_tokens WHERE email = ?', (email,)).fetchall()
            user['tokens'] = {acc: decrypt_token(tok) for acc, tok in tokens}
            if user.get('daily_stats_json'):
                try:
                    user['daily_stats'] = json.loads(user['daily_stats_json'])
                except:
                    user['daily_stats'] = None
            else:
                user['daily_stats'] = None
            return user
        finally:
            conn.close()

    @staticmethod
    def save(user):
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        try:
            daily_json = json.dumps(user.get('daily_stats')) if user.get('daily_stats') else None
            conn.execute('''INSERT OR REPLACE INTO users (email, id, name, password_hash, active_account,
                            created_at, last_login, referral_code, active, role, affiliate_earnings, referral_link_code, daily_stats_json)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (user['email'], user['id'], user['name'], user['password_hash'],
                          user.get('active_account','demo'), user.get('created_at'), user.get('last_login'),
                          user.get('referral_code'), user.get('active',1), user.get('role','user'),
                          user.get('affiliate_earnings',0.0), user.get('referral_link_code'), daily_json))
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
            'tokens': {},
            'daily_stats': None
        }
        UserStore.save(user)
        return user

    @staticmethod
    def set_active_account(email, account_type):
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        try:
            conn.execute('UPDATE users SET active_account = ? WHERE email = ?', (account_type, email))
            conn.commit()
        finally:
            conn.close()
        return UserStore.get(email)

    @staticmethod
    def add_token(email, account_type, token):
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        try:
            conn.execute('INSERT OR REPLACE INTO user_tokens (email, account_type, token) VALUES (?,?,?)',
                         (email, account_type, encrypt_token(token)))
            conn.commit()
        finally:
            conn.close()

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
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
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
connecting_lock = threading.Lock()
connecting_users = set()

def reset_bot_state(bot):
    bot.reset_stats()
    bot.reset_martingale()
    if hasattr(bot, 'reset_daily_stats'):
        bot.reset_daily_stats()
    else:
        bot.daily_stats = {'start_balance': 0, 'trades': 0, 'wins': 0, 'losses': 0, 'profit': 0}

def validate_account_type(loginid, expected):
    is_demo = loginid.startswith('VR')
    logger.info(f"🔍 Validando conta: loginid={loginid}, esperado={expected}, is_demo={is_demo}")
    return is_demo if expected == 'demo' else not is_demo

def persist_trade(user_id, trade_data):
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
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

def _save_daily_stats_to_db(email, bot):
    if not email or not bot:
        return
    if not getattr(bot, '_daily_stats_dirty', False):
        return
    try:
        stats_json = json.dumps(bot.get_daily_stats_for_db())
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        try:
            conn.execute('UPDATE users SET daily_stats_json = ? WHERE email = ?',
                         (stats_json, email))
            conn.commit()
            bot._daily_stats_dirty = False
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Erro ao guardar daily_stats: {e}")

def _load_martingale_state(user_id, bot):
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        row = conn.execute('SELECT active, step, original_amount FROM martingale_state WHERE user_id = ?', 
                          (user_id,)).fetchone()
        conn.close()
        if row and row[0]:
            bot.martingale['active'] = bool(row[0])
            bot.martingale['step'] = row[1]
            bot.martingale['original_amount'] = row[2]
            logger.info(f"📈 Martingale carregado: passo {row[1]}")
    except Exception as e:
        logger.error(f"Erro ao carregar martingale: {e}")

def _save_martingale_state(user_id, bot):
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        conn.execute('''INSERT OR REPLACE INTO martingale_state 
            (user_id, active, step, original_amount, last_updated)
            VALUES (?,?,?,?,?)''',
            (user_id, int(bot.martingale['active']), bot.martingale['step'],
             bot.martingale['original_amount'], time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Erro ao guardar martingale: {e}")

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
    from strategy import StrategyManager

    bot = TradingBot()
    analyzer = DigitAnalyzer(
        max_digits=1000,
        diff_min_window=50,
        diff_max_pct=5,
        diff_absent_ticks=20,
        volatile_unique=8
    )

    client = DerivWebSocketClient(config, on_tick_callback=None, on_result_callback=None)
    client.set_trading_bot(bot)
    client.set_digit_analyzer(analyzer)
    bot.client = client
    bot.digit_analyzer = analyzer

    # Instanciar o strategy e injetar no bot (Bug #5)
    strategy = StrategyManager(client, analyzer)
    bot.strategy = strategy

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
            _save_martingale_state(user_id, bot)
            # Notificar o strategy (única notificação, bug #1 corrigido)
            strategy.notify_result(trade.get('action', ''), trade.get('is_win', False))
        except Exception as e:
            logger.error(f"Callback de trade falhou: {e}")

    # Callbacks de tick e resultado para o cliente
    def tick_callback(tick):
        bot.on_tick(tick)

    client.on_tick_callback = tick_callback
    client.on_result_callback = on_trade_result

    saved_daily = user.get('daily_stats')
    if saved_daily:
        bot.set_daily_stats_from_db(saved_daily)
    else:
        bot.reset_daily_stats()
    bot._daily_stats_dirty = False

    _load_martingale_state(user_id, bot)

    def on_signal(signal_data):
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            conn.execute('''INSERT INTO signals 
                (user_id, timestamp, symbol, signal, confidence, tech_confidence, digit_confidence, digit_action, regime)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (user_id, time.time(), signal_data.get('symbol', bot.current_symbol),
                 signal_data['signal'], signal_data['confidence'],
                 signal_data.get('tech_confidence', 0), signal_data.get('digit_confidence', 0),
                 signal_data.get('digit_action'), 'UNKNOWN'))
            conn.commit()
            signal_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.close()
            bot._last_signal_id = signal_id
            logger.info(f"📡 Sinal registado na BD: ID={signal_id}, {signal_data['signal']} ({signal_data['confidence']:.1f}%)")
        except Exception as e:
            logger.error(f"Erro ao registar sinal: {e}")

    def on_signal_result(signal_id, result, profit):
        if not signal_id:
            return
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            conn.execute('UPDATE signals SET executed=1, result=?, profit=? WHERE id=?',
                         (result, profit, signal_id))
            conn.commit()
            conn.close()
            logger.info(f"📡 Sinal ID={signal_id} atualizado: {result}, profit={profit}")
        except Exception as e:
            logger.error(f"Erro ao atualizar sinal: {e}")

    bot.on_signal_callback = on_signal
    bot.on_signal_result_callback = on_signal_result
    bot._last_signal_id = None

    new_sess = {
        'client': client,
        'trading_bot': bot,
        'digit_analyzer': analyzer,
        'strategy': strategy,
        'candles': []
        # Bug #4: matches_cooldown_until removido — gestão exclusiva do StrategyManager
    }

    def on_candles(candles):
        new_sess['candles'] = candles

    client.on_candles_callback = on_candles

    with sessions_lock:
        sessions[user_id] = new_sess

    token = UserStore.get_active_token(user)
    if token:
        client.set_user_token(token)

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
                auth_err = getattr(client, 'auth_error', None)
                if auth_err and isinstance(auth_err, dict) and auth_err.get('code') == 'InvalidToken':
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

is_production = os.environ.get('FLASK_ENV', 'production') == 'production'
app.config['SESSION_COOKIE_SECURE'] = is_production
app.config['SESSION_COOKIE_SAMESITE'] = 'None' if is_production else 'Lax'

from config import config

app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.example.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@foloma.com')

if EMAIL_ENABLED:
    mail.init_app(app)

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

# ==================== ROTAS ====================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/go/airtm')
def go_airtm():
    target = os.environ.get('AIRTM_AFFILIATE_URL', 'https://app.airtm.com/ivt/jacob2wa5qcpp')
    return redirect(target)

@app.route('/api/auth/status')
def auth_status():
    if 'user_id' in session:
        user = UserStore.get(session.get('user_email'))
        if user:
            tokens = user.get('tokens', {})
            return jsonify({
                'authenticated': True,
                'user': {
                    'id': user['id'], 'name': user['name'], 'email': user['email'],
                    'role': user.get('role'),
                    'has_deriv_token': bool(UserStore.get_active_token(user)),
                    'active_account': user.get('active_account'),
                    'has_demo_token': bool(tokens.get('demo')),
                    'has_real_token': bool(tokens.get('real'))
                }
            })
    return jsonify({'authenticated': False})

@app.route('/api/auth/register', methods=['POST'])
@limit_if_available("10 per hour")
def register():
    session.clear()
    try:
        d = request.json
        email = d.get('email', '').strip().lower()
        name = d.get('name', '').strip()
        password = d.get('password', '')
        ref = d.get('referral_code', '')
        if not (name and email and len(password) >= 6):
            return jsonify({'error': 'Campos obrigatórios inválidos'}), 400
        user = AuthService.register(name, email, password, ref)
        if not user:
            return jsonify({'error': 'Email já registado'}), 400

        session.permanent = True
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['user_email'] = user['email']
        session['user_role'] = user.get('role', 'user')
        logger.info(f"Registo e auto-login: {email}")

        return jsonify({
            'status': 'ok',
            'message': 'Conta criada! Conecte à Deriv para começar.',
            'user': {
                'id': user['id'],
                'name': user['name'],
                'email': user['email'],
                'role': user.get('role'),
                'has_deriv_token': False,
                'active_account': 'demo'
            },
            'referral_code': user['referral_link_code']
        })
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
            'role': session['user_role'], 'has_deriv_token': bool(UserStore.get_active_token(user)),
            'active_account': user.get('active_account')
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

@app.route('/api/disconnect', methods=['POST'])
@require_auth
def disconnect():
    user_id = session['user_id']
    sess = get_session(user_id)
    if sess:
        sess['client']._stop_event.set()
        with sessions_lock:
            sessions.pop(user_id, None)
        logger.info(f"Sessão de {user_id} desconectada manualmente")
    return jsonify({'status': 'ok', 'message': 'Desconectado'})

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
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        conn.execute('INSERT OR REPLACE INTO password_resets (email, token_hash, expires_at, used) VALUES (?,?,?,0)',
                     (email, hashed, time.time() + 3600))
        conn.commit()
    finally:
        conn.close()
    send_reset_email(email, token)
    return jsonify({'status': 'ok', 'message': 'Se o email existir, receberá um link.'})

@app.route('/api/auth/reset-password-confirm', methods=['POST'])
def reset_password_confirm():
    token = request.json.get('token')
    new_pw = request.json.get('new_password')
    if not token or new_pw is None or len(new_pw) < 6:
        return jsonify({'error': 'Token ou senha inválidos'}), 400
    hashed = hashlib.sha256(token.encode()).hexdigest()
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
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

# ==================== CONEXÃO / TRADING ====================
@app.route('/api/connect', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def api_connect():
    user_id = session['user_id']
    with connecting_lock:
        if user_id in connecting_users:
            logger.info(f"Conectando já em curso para {user_id}, ignorando pedido duplicado")
            return jsonify({'status': 'connecting_already', 'message': 'Conexão já em andamento'})
        connecting_users.add(user_id)
    try:
        email = session['user_email']
        user = UserStore.get(email)
        token = UserStore.get_active_token(user)
        if not token:
            return jsonify({'error': 'Token não configurado'}), 400
        create_session(user_id, user)
        return jsonify({'status': 'connecting', 'account_type': user.get('active_account')})
    finally:
        with connecting_lock:
            connecting_users.discard(user_id)

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
    UserStore.set_active_account(email, acc_type)
    user = UserStore.get(email)
    user_id = session['user_id']
    with sessions_lock:
        if user_id in sessions:
            old_client = sessions[user_id]['client']
            old_client._stop_event.set()
            if old_client._ws_thread and old_client._ws_thread.is_alive():
                old_client._ws_thread.join(timeout=5)
            del sessions[user_id]
    sess = create_session(user_id, user, force=True)
    reset_bot_state(sess['trading_bot'])
    return jsonify({
        'status': 'connecting',
        'message': f'Conta {acc_type} ativada. A aguardar conexão...',
        'account_type': acc_type
    })

@app.route('/api/status')
@require_auth
def status():
    user_id = session['user_id']
    sess = get_session(user_id)
    if not sess:
        return jsonify({
            'bot': {'connected': False, 'authorized': False},
            'digits': {},
            'symbols': config.AVAILABLE_SYMBOLS
        })
    client = sess['client']
    bot = sess['trading_bot']
    analyzer = sess['digit_analyzer']
    strategy = sess.get('strategy')
    if client:
        bot.balance = client.balance
        bot.currency = client.currency
        bot._client_connected = client.connected
        bot._client_authorized = client.authorized
    bot_status = bot.get_status()
    bot_status['streaming'] = client.streaming if client else False
    analysis = analyzer.get_analysis()
    digit_frequencies = analyzer.get_digit_frequencies()
    least_frequent = analyzer.get_least_frequent_digit()
    most_frequent = analyzer.get_most_frequent_digit()

    _save_daily_stats_to_db(session.get('user_email'), bot)

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
            'ticks_per_digit': analyzer.TICKS_PER_DIGIT,
            'digit_frequencies': digit_frequencies,
            'least_frequent_digit': least_frequent,
            'most_frequent_digit': most_frequent
        },
        'strategy': strategy.get_status() if strategy else {},
        'symbols': config.AVAILABLE_SYMBOLS,
        'loginid': client.loginid if client else None
    })

@app.route('/api/daily-stats/sync', methods=['POST'])
@require_auth
def sync_daily_stats():
    sess = get_session(session['user_id'])
    if sess:
        sess['trading_bot']._daily_stats_dirty = True
        _save_daily_stats_to_db(session.get('user_email'), sess['trading_bot'])
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Sem sessão'}), 400

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
        'last_tick_seconds_ago': round(time.time() - c._last_tick_time, 1) if c._last_tick_time else None,
        'ping_ms': getattr(c, '_ping_ms', 0),
        'reconnect_count': getattr(c, '_reconnect_count', 0),
        'last_reconnect_ago': round(time.time() - getattr(c, '_last_reconnect_time', time.time()), 1)
    })

# ==================== OAUTH (PERSISTIDO NA BD) ====================
@app.route('/oauth/callback')
def oauth_callback():
    state_id = request.args.get('state')
    logger.info(f"📥 OAuth Callback recebido. State: {state_id}, Session: {dict(session)}, Args: {request.args.to_dict()}")
    if not state_id:
        logger.error("🚫 Callback sem state!")
        return redirect('/?error=invalid_state')

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>OAuth Callback</title></head>
<body>
    <p>A processar autenticação...</p>
    <script>
        (function() {{
            const hash = window.location.hash.substring(1);
            const params = new URLSearchParams(hash);
            const accessToken = params.get('access_token');
            if (!accessToken) {{
                window.location.href = '/?error=no_token';
                return;
            }}
            const tokens = [];
            for (let i = 1; params.get('token' + i); i++) {{
                tokens.push({{ token: params.get('token' + i), acct: params.get('acct' + i) || '' }});
            }}
            if (tokens.length === 0 && accessToken) {{
                tokens.push({{ token: accessToken, acct: '' }});
            }}
            fetch('/api/auth/process-oauth', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ state: '{state_id}', tokens: tokens }})
            }})
            .then(response => response.json())
            .then(data => {{
                if (data.status === 'ok') {{
                    if (window.opener && !window.opener.closed) {{
                        window.opener.location.href = '/?connected=true';
                    }} else {{
                        window.location.href = '/?connected=true';
                    }}
                }} else {{
                    if (window.opener && !window.opener.closed) {{
                        window.opener.location.href = '/?error=' + (data.error || 'oauth_failed');
                    }} else {{
                        window.location.href = '/?error=' + (data.error || 'oauth_failed');
                    }}
                }}
                window.close();
            }})
            .catch(() => {{
                if (window.opener && !window.opener.closed) {{
                    window.opener.location.href = '/?error=request_failed';
                }} else {{
                    window.location.href = '/?error=request_failed';
                }}
                window.close();
            }});
        }})();
    </script>
</body>
</html>"""
    return make_response(html)

@app.route('/api/auth/process-oauth', methods=['POST'])
def process_oauth():
    data = request.json
    state_id = data.get('state')
    tokens = data.get('tokens', [])
    logger.info(f"📥 Process OAuth. State: {state_id}, Session: {dict(session)}, Tokens recebidos: {tokens}")
    if not state_id or not tokens:
        return jsonify({'error': 'Dados incompletos'}), 400

    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT user_id, account_type FROM oauth_states WHERE state_id = ? AND used = 0 AND created_at > ?",
            (state_id, time.time() - OAUTH_STATE_TTL)
        ).fetchone()
        if not row:
            logger.error(f"🚫 State '{state_id}' não encontrado ou já usado/expirado.")
            return jsonify({'error': 'OAuth expirado. Por favor, inicie novamente.'}), 401

        user_id = row[0]
        account_type_request = row[1]

        conn.execute("UPDATE oauth_states SET used = 1 WHERE state_id = ?", (state_id,))
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        row = conn.execute('SELECT email FROM users WHERE id = ?', (user_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Utilizador não encontrado'}), 404
        email = row[0]
    finally:
        conn.close()

    for acc in tokens:
        tok = acc.get('token')
        acct = acc.get('acct', '')
        if not tok:
            continue
        if acct.startswith('VR'):
            UserStore.add_token(email, 'demo', tok)
        else:
            UserStore.add_token(email, 'real', tok)
        if (account_type_request == 'demo' and acct.startswith('VR')) or \
           (account_type_request == 'real' and not acct.startswith('VR')):
            UserStore.set_active_account(email, account_type_request)

    user = UserStore.get(email)
    session['user_id'] = user_id
    session['user_email'] = email
    session['user_name'] = user['name']
    session['user_role'] = user.get('role', 'user')
    session.permanent = True

    create_session(user_id, user, force=True)
    logger.info(f"✅ OAuth concluído para {email}")
    return jsonify({'status': 'ok'})

@app.route('/api/auth/deriv_oauth_url')
@require_auth
def deriv_oauth_url():
    app_id = config.DERIV_APP_ID
    if not app_id:
        logger.error("DERIV_APP_ID não definido")
        return jsonify({'error': 'Configuração OAuth em falta'}), 500
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    redirect_uri = base_url + '/oauth/callback'
    encoded_redirect = urllib.parse.quote(redirect_uri, safe='')
    state_id = uuid.uuid4().hex
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        conn.execute(
            "INSERT INTO oauth_states (state_id, user_id, account_type, created_at) VALUES (?, ?, ?, ?)",
            (state_id, session['user_id'], request.args.get('account_type', 'demo'), time.time())
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Erro ao criar state OAuth: {e}")
        return jsonify({'error': 'Erro interno ao iniciar OAuth'}), 500
    finally:
        conn.close()

    url = (
        f"https://oauth.deriv.com/oauth2/authorize"
        f"?app_id={app_id}"
        f"&redirect_uri={encoded_redirect}"
        f"&response_type=token"
        f"&state={state_id}"
        f"&l=PT"
    )
    logger.info(f"URL OAuth gerado: {url}")
    return jsonify({'url': url})

# ==================== TRADING ====================

@app.route('/api/trade/digit', methods=['POST'])
@require_auth
@limit_if_available("20 per minute")
def trade_digit():
    try:
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        bot = sess['trading_bot']
        if bot.stop_loss_active:
            return jsonify({'error': '🛑 Stop-loss activo. Limite diário atingido.'}), 400

        strategy = sess.get('strategy')
        action = None
        reason = None
        if strategy:
            action, reason = strategy.evaluate_parity()
            if not action:
                return jsonify({'error': f'⛔ {reason}'}), 400
        else:
            # fallback antigo
            d = request.json
            pred = d.get('prediction')
            if pred not in ('odd', 'even'):
                return jsonify({'error': 'Use "odd" ou "even"'}), 400
            action = pred

        d = request.json
        amt = float(d.get('amount', 0.35))
        if amt < 0.35 or amt > 100:
            return jsonify({'error': 'Valor inválido'}), 400

        analyzer = sess['digit_analyzer']
        tr = analyzer.get_ticks_remaining()
        if tr < 2:
            return jsonify({'error': f'Dígito a sair em {tr} tick(s). Aguarde.'}), 400

        ok = sess['client'].place_trade('CALL' if action == 'odd' else 'PUT', amt, True)
        if ok:
            credit_affiliate_commission(session['user_email'], amt)
            label = 'ÍMPAR' if action == 'odd' else 'PAR'
            return jsonify({
                'status': 'ok',
                'message': f'✅ {label} por ${amt:.2f}',
                'ticks_remaining': tr,
                'executed_action': action   # Bug #13: devolve a direção real executada
            })
        return jsonify({'error': 'Falha no trade'}), 500
    except Exception:
        logger.exception("Erro trade dígito")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/trade/differ', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def trade_differ():
    try:
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        bot = sess['trading_bot']
        if bot.stop_loss_active:
            return jsonify({'error': '🛑 Stop-loss activo. Limite diário atingido.'}), 400

        strategy = sess.get('strategy')
        if strategy:
            digit, reason = strategy.evaluate_differ()
            if digit is None:
                return jsonify({'error': f'⛔ {reason}'}), 400
        else:
            analyzer = sess['digit_analyzer']
            least = analyzer.get_least_frequent_digit()
            if least is None:
                return jsonify({'error': 'Nenhum dígito sub‑representado. Aguarde.'}), 400
            digit = least

        d = request.json
        amt = float(d.get('amount', 0.35))
        if amt < 0.35 or amt > 100:
            return jsonify({'error': 'Valor inválido'}), 400

        analyzer = sess['digit_analyzer']
        tr = analyzer.get_ticks_remaining()
        if tr < 2:
            return jsonify({'error': f'Dígito a sair em {tr} tick(s). Aguarde.'}), 400

        ok = sess['client'].place_differ_trade(digit, amt)
        if ok:
            credit_affiliate_commission(session['user_email'], amt)
            return jsonify({
                'status': 'ok',
                'message': f'🎯 DIFFER no dígito {digit} por ${amt:.2f}',
                'digit': digit
            })
        return jsonify({'error': 'Falha no trade DIFFER'}), 500
    except Exception:
        logger.exception("Erro trade differ")
        return jsonify({'error': 'Erro interno'}), 500

@app.route('/api/trade/matches', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def trade_matches():
    try:
        sess = get_session(session['user_id'])
        if not sess or not sess['client'].authorized:
            return jsonify({'error': 'Não conectado'}), 400
        bot = sess['trading_bot']
        if bot.stop_loss_active:
            return jsonify({'error': '🛑 Stop-loss activo. Limite diário atingido.'}), 400

        strategy = sess.get('strategy')
        if strategy:
            # Bug #4: cooldown MATCHES agora gerido exclusivamente pelo strategy
            digit, reason = strategy.evaluate_matches()
            if digit is None:
                return jsonify({'error': f'⛔ {reason}'}), 400
        else:
            analyzer = sess['digit_analyzer']
            most = analyzer.get_most_frequent_digit()
            if most is None:
                return jsonify({'error': 'Condições para MATCHES não atingidas. Aguarde mais ticks.'}), 400
            digit = most

        d = request.json
        amt = float(d.get('amount', 0.35))
        if amt < 0.35 or amt > 100:
            return jsonify({'error': 'Valor inválido'}), 400

        analyzer = sess['digit_analyzer']
        tr = analyzer.get_ticks_remaining()
        if tr < 2:
            return jsonify({'error': f'Dígito a sair em {tr} tick(s). Aguarde.'}), 400

        ok = sess['client'].place_matches_trade(digit, amt)
        if ok:
            credit_affiliate_commission(session['user_email'], amt)
            return jsonify({
                'status': 'ok',
                'message': f'🎯 MATCHES no dígito {digit} por ${amt:.2f}',
                'digit': digit
            })
        return jsonify({'error': 'Falha no trade MATCHES'}), 500
    except Exception:
        logger.exception("Erro trade matches")
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

    bot = sess['trading_bot']
    ok, res = bot.apply_martingale_after_loss(la)
    if ok:
        _save_martingale_state(session['user_id'], bot)
        return jsonify({'status': 'ok', 'martingale': res})
    return jsonify({'error': res}), 400

@app.route('/api/martingale/reset', methods=['POST'])
@require_auth
def martingale_reset():
    sess = get_session(session['user_id'])
    if sess:
        sess['trading_bot'].reset_martingale()
        _save_martingale_state(session['user_id'], sess['trading_bot'])
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

@app.route('/api/candles/data')
@require_auth
def candles_data():
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'candles': []})
    granularity = request.args.get('granularity', 60, type=int)
    symbol = sess['client'].current_symbol
    sess['client'].request_candles(symbol, granularity=granularity, count=50)
    return jsonify({'candles': sess.get('candles', [])})

# ==================== PLACAR DE SINAIS ====================
@app.route('/api/signals/scoreboard')
@require_auth
def signals_scoreboard():
    user_id = session['user_id']
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        total = conn.execute('SELECT COUNT(*) FROM signals WHERE user_id=? AND timestamp>=?',
                             (user_id, today_start)).fetchone()[0]
        wins = conn.execute('SELECT COUNT(*) FROM signals WHERE user_id=? AND timestamp>=? AND result="win"',
                            (user_id, today_start)).fetchone()[0]
        losses = conn.execute('SELECT COUNT(*) FROM signals WHERE user_id=? AND timestamp>=? AND result="loss"',
                              (user_id, today_start)).fetchone()[0]
        pending = total - wins - losses
    finally:
        conn.close()
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    return jsonify({
        'today_signals': total,
        'would_have_won': wins,
        'would_have_lost': losses,
        'pending': pending,
        'simulated_win_rate': round(win_rate, 1)
    })

# ==================== AFILIADOS / PAGAMENTOS ====================
def credit_affiliate_commission(user_email, amount):
    user = UserStore.get(user_email)
    if not user or not user.get('referral_code'):
        return
    ref_code = user['referral_code']
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
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
    email = session.get('user_email')
    if not email:
        return jsonify({'error': 'Utilizador não encontrado'}), 404
    user = UserStore.get(email)
    if not user:
        return jsonify({'error': 'Utilizador não encontrado'}), 404
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        referred_count = conn.execute(
            'SELECT COUNT(*) FROM referrals WHERE referrer_email = ?', (email,)
        ).fetchone()[0]
        total_commission = user.get('affiliate_earnings', 0.0)
    finally:
        conn.close()
    return jsonify({
        'total_referrals': referred_count,
        'total_commission': total_commission,
        'pending_commission': 0.0,
        'paid_commission': total_commission
    })

@app.route('/api/affiliate/link')
@require_auth
def affiliate_link():
    email = session.get('user_email')
    user = UserStore.get(email) if email else None
    if not user:
        user_id = session.get('user_id')
        if user_id:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            try:
                row = conn.execute('SELECT email FROM users WHERE id = ?', (user_id,)).fetchone()
                if row:
                    email = row[0]
                    user = UserStore.get(email)
                    session['user_email'] = email
            finally:
                conn.close()
    if not user:
        return jsonify({'error': 'Utilizador não encontrado'}), 404
    if not user.get('referral_link_code'):
        ref_link = base64.b64encode(hashlib.md5(user['id'].encode()).digest()).hex()[:8]
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        try:
            conn.execute('UPDATE users SET referral_link_code = ? WHERE email = ?',
                         (ref_link, user['email']))
            conn.commit()
        finally:
            conn.close()
        user['referral_link_code'] = ref_link
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    link = f"{base_url}/?ref={user['referral_link_code']}"
    return jsonify({'link': link, 'code': user['referral_link_code']})

@app.route('/api/affiliate/earnings')
@require_auth
def affiliate_earnings():
    email = session.get('user_email')
    user = UserStore.get(email) if email else None
    if not user:
        user_id = session.get('user_id')
        if user_id:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            try:
                row = conn.execute('SELECT email FROM users WHERE id = ?', (user_id,)).fetchone()
                if row:
                    email = row[0]
                    user = UserStore.get(email)
                    session['user_email'] = email
            finally:
                conn.close()
    if not user:
        return jsonify({'error': 'Utilizador não encontrado'}), 404
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        referred_count = conn.execute('SELECT COUNT(*) FROM referrals WHERE referrer_email = ?',
                                       (user['email'],)).fetchone()[0]
    finally:
        conn.close()
    return jsonify({
        'earnings': user.get('affiliate_earnings', 0.0),
        'referral_link': user.get('referral_link_code', ''),
        'referred_count': referred_count,
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
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
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
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
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
    user = UserStore.get(email) if email else None
    target_uid = user['id'] if user else None
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
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
    with sessions_lock:
        for uid, sess in list(sessions.items()):
            if (target_uid and uid == target_uid) or not target_uid:
                sess['client']._stop_event.set()
                del sessions[uid]
                logger.info(f"Sessão de {uid} encerrada")
    return jsonify({'status': 'ok', 'message': 'Tokens removidos. Utilizador terá que refazer OAuth.'})

@app.route('/api/admin/settings')
@require_admin
def admin_settings():
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='markup_percentage'").fetchone()
        markup = float(row[0]) if row else config.MARKUP_PERCENTAGE
    finally:
        conn.close()
    return jsonify({'markup': markup})

@app.route('/api/admin/set-markup', methods=['POST'])
@require_admin
def set_markup():
    d = request.json
    pct = float(d.get('percentage', 0.5))
    if not (0.0 <= pct <= 3.0):
        return jsonify({'error': 'Percentagem deve estar entre 0% e 3%'}), 400
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('markup_percentage', ?)",
                     (str(pct),))
        conn.commit()
    finally:
        conn.close()
    config.MARKUP_PERCENTAGE = pct
    logger.info(f"Markup alterado para {pct}%")
    return jsonify({'status': 'ok', 'percentage': pct})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
