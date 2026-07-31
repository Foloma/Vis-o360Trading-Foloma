import os, sqlite3, hashlib, base64, json, secrets, time, threading, logging, uuid, urllib.parse, urllib.request
from datetime import datetime, date
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, abort, make_response
from werkzeug.security import generate_password_hash, check_password_hash

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
    base_url = os.environ.get('BASE_URL', 'http://localhost:5000')
    reset_url = f"{base_url}/reset-password?token={reset_token}"
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
        email TEXT PRIMARY KEY, id TEXT NOT NULL, name TEXT, password_hash TEXT NOT NULL,
        active_account TEXT DEFAULT 'demo', created_at REAL, last_login REAL,
        referral_code TEXT, active INTEGER DEFAULT 1, role TEXT DEFAULT 'user',
        affiliate_earnings REAL DEFAULT 0.0, referral_link_code TEXT,
        daily_stats_json TEXT, plan TEXT DEFAULT 'free')''')
    try:
        c.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'")
    except sqlite3.OperationalError:
        pass
    c.execute('''CREATE TABLE IF NOT EXISTS user_tokens (
        email TEXT, account_type TEXT, token TEXT NOT NULL,
        PRIMARY KEY (email, account_type), FOREIGN KEY (email) REFERENCES users(email))''')
    c.execute('''CREATE TABLE IF NOT EXISTS password_resets (
        email TEXT, token_hash TEXT NOT NULL, expires_at REAL NOT NULL, used INTEGER DEFAULT 0,
        PRIMARY KEY (email, token_hash))''')
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        referrer_email TEXT, referred_email TEXT, timestamp REAL,
        PRIMARY KEY (referrer_email, referred_email))''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, contract_id TEXT UNIQUE,
        symbol TEXT, action TEXT, amount REAL, buy_price REAL, sell_price REAL, profit REAL,
        result TEXT, timestamp REAL DEFAULT (strftime('%s','now')))''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS martingale_state (
        user_id TEXT PRIMARY KEY, active INTEGER DEFAULT 0, step INTEGER DEFAULT 0,
        original_amount REAL DEFAULT 0, last_updated REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, timestamp REAL NOT NULL,
        symbol TEXT, signal TEXT, confidence REAL, tech_confidence REAL, digit_confidence REAL,
        digit_action TEXT, regime TEXT DEFAULT 'UNKNOWN', executed INTEGER DEFAULT 0,
        result TEXT, profit REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS oauth_states (
        state_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, account_type TEXT DEFAULT 'demo',
        created_at REAL NOT NULL, used INTEGER DEFAULT 0, code_verifier TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS forex_signal_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        direction TEXT,
        signal_type TEXT,
        strategy_used TEXT,
        confidence INTEGER,
        breakdown_json TEXT,
        suggested_duration_minutes INTEGER,
        price_at_signal REAL,
        timestamp REAL,
        evaluated INTEGER DEFAULT 0,
        outcome TEXT,
        price_after REAL,
        evaluated_at REAL,
        was_executed INTEGER DEFAULT 0,
        actual_profit REAL
    )''')

    try:
        c.execute("ALTER TABLE users ADD COLUMN daily_stats_json TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE oauth_states ADD COLUMN code_verifier TEXT")
    except sqlite3.OperationalError:
        pass
    for col in ['entry_digit', 'exit_digit', 'entry_spot', 'exit_spot',
                'entry_tick_time', 'exit_tick_time', 'click_tick', 'latency_ms']:
        try:
            c.execute(f"ALTER TABLE trades ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    c.execute("DELETE FROM password_resets WHERE expires_at < ?", (time.time(),))
    conn.commit()
    conn.close()

init_db()

OAUTH_STATE_TTL = 900

# ==================== NOVA FUNÇÃO: avaliação automática de sinais Forex ====================
def evaluate_pending_forex_signals():
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        cutoff = time.time() - 900
        rows = conn.execute(
            "SELECT id, symbol, direction, price_at_signal FROM forex_signal_log "
            "WHERE evaluated=0 AND timestamp < ?", (cutoff,)
        ).fetchall()
        if not rows:
            conn.close()
            return

        forex_mgr = None
        with sessions_lock:
            for uid, sess in sessions.items():
                fm = sess.get('forex_data')
                if fm:
                    forex_mgr = fm
                    break

        for sid, symbol, direction, price_then in rows:
            price_now = None
            if forex_mgr:
                price_now = forex_mgr.get_latest_price(symbol)
            if price_now is None:
                continue
            moved_up = price_now > price_then
            outcome = 'win' if (direction == 'BUY') == moved_up else 'loss'
            conn.execute(
                "UPDATE forex_signal_log SET evaluated=1, outcome=?, price_after=?, evaluated_at=? WHERE id=?",
                (outcome, price_now, time.time(), sid)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Erro ao avaliar sinais Forex pendentes: {e}")

def _cleanup_loop():
    while True:
        time.sleep(3600)
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            conn.execute("DELETE FROM password_resets WHERE expires_at < ?", (time.time(),))
            conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (time.time() - OAUTH_STATE_TTL,))
            conn.commit()
            conn.close()
            now = time.time()
            with sessions_lock:
                to_remove = []
                for uid, sess in sessions.items():
                    client = sess.get('client')
                    last_tick = getattr(client, '_last_tick_time', 0)
                    if last_tick and (now - last_tick) > 1800:
                        sess['trading_bot'].on_disconnect()
                        client._stop_event.set()
                        to_remove.append(uid)
                for uid in to_remove:
                    sessions.pop(uid, None)
            evaluate_pending_forex_signals()
        except Exception as e:
            logger.error(f"Erro na limpeza periódica: {e}")

threading.Thread(target=_cleanup_loop, daemon=True).start()

def _refresh_forex_candles_loop():
    while True:
        time.sleep(600)
        try:
            with sessions_lock:
                sessions_snapshot = list(sessions.items())
            for uid, sess in sessions_snapshot:
                forex_mgr = sess.get('forex_data')
                client = sess.get('client')
                if forex_mgr and client and client.authorized:
                    for symbol in FOREX_SYMBOLS:
                        forex_mgr.request_candles(symbol, granularity=900, count=250)
                        forex_mgr.request_candles(symbol, granularity=1800, count=100)
                        forex_mgr.request_candles(symbol, granularity=3600, count=30)
        except Exception as e:
            logger.error(f"Erro no refresco periódico de candles Forex: {e}")

threading.Thread(target=_refresh_forex_candles_loop, daemon=True).start()

def load_markup_from_db():
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        row = conn.execute("SELECT value FROM settings WHERE key='referral_commission_percentage'").fetchone()
        if row:
            from config import config
            config.REFERRAL_COMMISSION_PERCENTAGE = float(row[0])
        conn.close()
    except Exception as e:
        logger.error(f"Erro ao carregar comissão: {e}")

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
                    conn.execute('INSERT OR IGNORE INTO user_tokens (email, account_type, token) VALUES (?,?,?)', (email, 'demo', encrypt_token(demo)))
                if real:
                    conn.execute('INSERT OR IGNORE INTO user_tokens (email, account_type, token) VALUES (?,?,?)', (email, 'real', encrypt_token(real)))
            else:
                for acc, tok in tokens.items():
                    if tok:
                        conn.execute('INSERT OR IGNORE INTO user_tokens (email, account_type, token) VALUES (?,?,?)', (email, acc, encrypt_token(tok)))
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
                    'referral_code','active','role','affiliate_earnings','referral_link_code','daily_stats_json','plan']
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
                            created_at, last_login, referral_code, active, role, affiliate_earnings, referral_link_code, daily_stats_json, plan)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (user['email'], user['id'], user['name'], user['password_hash'],
                          user.get('active_account','demo'), user.get('created_at'), user.get('last_login'),
                          user.get('referral_code'), user.get('active',1), user.get('role','user'),
                          user.get('affiliate_earnings',0.0), user.get('referral_link_code'), daily_json,
                          user.get('plan','free')))
            conn.execute('DELETE FROM user_tokens WHERE email = ?', (user['email'],))
            for acc, tok in user.get('tokens', {}).items():
                if tok:
                    conn.execute('INSERT INTO user_tokens (email, account_type, token) VALUES (?,?,?)', (user['email'], acc, encrypt_token(tok)))
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
            'email': email, 'id': uid, 'name': name, 'password_hash': password_hash,
            'active_account': 'demo', 'created_at': time.time(), 'last_login': None,
            'referral_code': referral_code, 'active': 1, 'role': 'user',
            'affiliate_earnings': 0.0, 'referral_link_code': ref_link,
            'tokens': {}, 'daily_stats': None, 'plan': 'free'
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
            conn.execute('INSERT OR REPLACE INTO user_tokens (email, account_type, token) VALUES (?,?,?)', (email, account_type, encrypt_token(token)))
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
                    conn.execute('INSERT OR IGNORE INTO referrals (referrer_email, referred_email, timestamp) VALUES (?,?,?)', (row[0], email, time.time()))
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
    return is_demo if expected == 'demo' else not is_demo

def persist_trade(user_id, trade_data):
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        conn.execute('''INSERT OR REPLACE INTO trades
            (user_id, contract_id, symbol, action, amount, buy_price, sell_price, profit, result, timestamp,
             entry_digit, exit_digit, entry_spot, exit_spot, entry_tick_time, exit_tick_time, click_tick, latency_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (user_id, trade_data.get('contract_id'), trade_data.get('symbol'),
             trade_data.get('action'), trade_data.get('amount'),
             trade_data.get('buy_price', 0), trade_data.get('sell_price', 0),
             trade_data.get('profit', 0), trade_data.get('result', 'unknown'),
             time.time(),
             str(trade_data.get('entry_digit')) if trade_data.get('entry_digit') is not None else None,
             str(trade_data.get('exit_digit')) if trade_data.get('exit_digit') is not None else None,
             str(trade_data.get('entry_spot')) if trade_data.get('entry_spot') is not None else None,
             str(trade_data.get('exit_spot')) if trade_data.get('exit_spot') is not None else None,
             str(trade_data.get('entry_tick_time')) if trade_data.get('entry_tick_time') is not None else None,
             str(trade_data.get('exit_tick_time')) if trade_data.get('exit_tick_time') is not None else None,
             str(trade_data.get('click_tick')) if trade_data.get('click_tick') is not None else None,
             str(trade_data.get('latency_ms')) if trade_data.get('latency_ms') is not None else None))
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
            conn.execute('UPDATE users SET daily_stats_json = ? WHERE email = ?', (stats_json, email))
            conn.commit()
            bot._daily_stats_dirty = False
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Erro ao guardar daily_stats: {e}")

def _load_martingale_state(user_id, bot):
    try:
        conn = sqlite3.connect(DATABASE_PATH, timeout=10)
        row = conn.execute('SELECT active, step, original_amount FROM martingale_state WHERE user_id = ?', (user_id,)).fetchone()
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

def get_otp_ws_url(email, account_type):
    try:
        user = UserStore.get(email)
        if not user:
            return None, 0, 'USD'
        access_token = user.get('tokens', {}).get(account_type)
        if not access_token:
            logger.error("OTP: sem access_token")
            return None, 0, 'USD'
        headers = {
            'Deriv-App-ID': config.DERIV_APP_ID,
            'Authorization': f'Bearer {access_token}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9'
        }
        req = urllib.request.Request(
            f"{config.DERIV_REST_URL}/trading/v1/options/accounts",
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            accounts = json.loads(resp.read())
        selected_acc = None
        for a in accounts.get('data', []):
            if not a.get('is_disabled') and a.get('account_type') == account_type:
                selected_acc = a
                break
        if not selected_acc:
            for a in accounts.get('data', []):
                if not a.get('is_disabled'):
                    selected_acc = a
                    break
        if not selected_acc:
            logger.error("OTP: nenhuma conta encontrada")
            return None, 0, 'USD'
        account_id = selected_acc['account_id']
        balance = float(selected_acc.get('balance', 0))
        currency = selected_acc.get('currency', 'USD')
        req = urllib.request.Request(
            f"{config.DERIV_REST_URL}/trading/v1/options/accounts/{account_id}/otp",
            data=b'{}',
            headers={**headers, 'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            otp_resp = json.loads(resp.read())
        ws_url = otp_resp.get('data', {}).get('url')
        logger.info(f"🔑 OTP obtido: {ws_url[:60] if ws_url else 'None'} | Saldo: {balance} {currency}")
        return ws_url, balance, currency
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        logger.error(f"OTP HTTP {e.code}: {body}")
        return None, 0, 'USD'
    except Exception as e:
        logger.error(f"Erro OTP: {e}")
        return None, 0, 'USD'

from forex_data import ForexDataManager, FOREX_SYMBOLS
from forex_indicators import ForexIndicators
from forex_signals import ForexSignals

def create_session(user_id, user, force=False, ws_url_override=None):
    with sessions_lock:
        if user_id in sessions:
            existing = sessions[user_id]
            client = existing['client']
            if not force and client.authorized and client.connected:
                return existing
            if 'trading_bot' in existing:
                existing['trading_bot'].on_disconnect()
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
            max_digits=1000, diff_min_window=50, diff_max_pct=5, diff_absent_ticks=20, volatile_unique=8
        )
        client = DerivWebSocketClient(config, on_tick_callback=None, on_result_callback=None)
        client.set_trading_bot(bot)
        client.set_digit_analyzer(analyzer)
        bot.client = client
        bot.digit_analyzer = analyzer

        forex_mgr = ForexDataManager()
        forex_mgr.set_client(client)
        forex_indicators = ForexIndicators(forex_mgr)
        forex_signals = ForexSignals(forex_mgr)

        if ws_url_override:
            if not ws_url_override.startswith(('wss://', 'ws://')):
                logger.error(f"URL inválido: {ws_url_override}")
                return None
            client.set_ws_url(ws_url_override)

        user_email = user.get('email', '')
        user_acct = user.get('active_account', 'demo')
        client._otp_refresh_callback = lambda: get_otp_ws_url(user_email, user_acct)[0]

        def refresh_balance():
            _, bal, cur = get_otp_ws_url(user_email, user_acct)
            return bal, cur
        client._balance_refresh_callback = refresh_balance

        strategy = StrategyManager(client, analyzer)
        bot.strategy = strategy

        def on_trade_result(trade):
            try:
                result = 'win' if trade.get('is_win') else 'loss'
                action = trade.get('action', '')
                is_win = trade.get('is_win', False)
                contract_id = trade.get('contract_id', 'N/A')
                profit = trade.get('profit', 0)
                persist_trade(user_id, {
                    'contract_id': contract_id, 'symbol': trade.get('symbol', 'R_100'),
                    'action': action, 'amount': trade.get('amount', 0),
                    'buy_price': trade.get('buy_price', 0), 'sell_price': trade.get('sell_price', 0),
                    'profit': profit, 'result': result,
                    'entry_digit': trade.get('entry_digit'), 'exit_digit': trade.get('exit_digit'),
                    'entry_spot': trade.get('entry_spot'), 'exit_spot': trade.get('exit_spot'),
                    'entry_tick_time': trade.get('entry_tick_time'), 'exit_tick_time': trade.get('exit_tick_time'),
                    'click_tick': getattr(bot, '_last_click_tick', None),
                    'latency_ms': getattr(bot, 'last_trade_result', {}).get('latency_total_ms')
                })
                _save_martingale_state(user_id, bot)
                strategy.notify_result(action, is_win)
                if strategy.is_global_stop:
                    bot.reset_martingale()
                    _save_martingale_state(user_id, bot)
            except Exception as e:
                logger.error(f"Callback de trade falhou: {e}")

        def tick_callback(tick):
            bot.on_tick(tick)
            if strategy:
                strategy.on_tick(tick)
                pending_parity, pending_differ = strategy.get_pending_bets()
                if pending_parity:
                    if strategy._check_pending_bets():
                        direction = pending_parity['direction']
                        amt = pending_parity['amount']
                        contract = 'CALL' if direction == 'odd' else 'PUT'
                        ok = client.place_trade(contract, amt, is_digit=True)
                        if not ok:
                            strategy.set_execution_error(f"Falha ao executar paridade: {contract}")
                        else:
                            strategy.clear_execution_error()
                if pending_differ:
                    if strategy._check_pending_bets():
                        digit = pending_differ['digit']
                        amt = pending_differ['amount']
                        ok = client.place_differ_trade(digit, amt)
                        if not ok:
                            strategy.set_execution_error(f"Falha ao executar DIFFER: {digit}")
                        else:
                            strategy.clear_execution_error()
            forex_mgr.on_tick(tick)

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
            except Exception as e:
                logger.error(f"Erro ao registar sinal: {e}")

        def on_signal_result(signal_id, result, profit):
            if not signal_id:
                return
            try:
                conn = sqlite3.connect(DATABASE_PATH, timeout=10)
                conn.execute('UPDATE signals SET executed=1, result=?, profit=? WHERE id=?', (result, profit, signal_id))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Erro ao atualizar sinal: {e}")

        bot.on_signal_callback = on_signal
        bot.on_signal_result_callback = on_signal_result
        bot._last_signal_id = None

        new_sess = {
            'client': client, 'trading_bot': bot, 'digit_analyzer': analyzer,
            'strategy': strategy, 'candles': [],
            'forex_data': forex_mgr, 'forex_signals': forex_signals, 'forex_indicators': forex_indicators
        }

        def on_candles(candles, req_id=None):
            new_sess['candles'] = candles
            forex_mgr.on_candles({'candles': candles}, req_id=req_id)

        client.on_candles_callback = on_candles
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
                if client.loginid != 'OTP_AUTH':
                    if not validate_account_type(client.loginid, user.get('active_account', 'demo')):
                        logger.warning(f"Token inválido para {user['email']} – a remover sessão.")
                        bot.on_disconnect()
                        client._stop_event.set()
                        with sessions_lock:
                            sessions.pop(user_id, None)
                        return
                bot.start(client)
                bot.daily_stats['start_balance'] = bot.balance
                forex_mgr.subscribe_all()
                for symbol in FOREX_SYMBOLS:
                    forex_mgr.request_candles(symbol, granularity=60, count=250)
                    forex_mgr.request_candles(symbol, granularity=300, count=200)
                    forex_mgr.request_candles(symbol, granularity=900, count=250)
                    forex_mgr.request_candles(symbol, granularity=1800, count=100)
                    forex_mgr.request_candles(symbol, granularity=3600, count=30)
            else:
                auth_err = getattr(client, 'auth_error', None)
                if auth_err and isinstance(auth_err, dict) and auth_err.get('code') == 'InvalidToken':
                    UserStore.add_token(user['email'], user.get('active_account', 'demo'), '')
                bot.on_disconnect()
        threading.Thread(target=connect_and_validate, daemon=True).start()
    return new_sess

def get_session(user_id):
    with sessions_lock:
        return sessions.get(user_id)

# ==================== INICIALIZAÇÃO DO FLASK ====================
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = 86400

is_production = os.environ.get('RENDER', 'false').lower() == 'true' or os.environ.get('FLASK_DEBUG', '0') == '0'
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

def get_user_or_ip():
    if 'user_id' in session:
        return f"user:{session['user_id']}"
    return f"ip:{request.remote_addr}"

try:
    from flask_limiter import Limiter
    limiter = Limiter(get_user_or_ip, app=app, default_limits=["120 per minute", "10000 per day"], storage_uri="memory://")
except ImportError:
    logger.warning("Flask-Limiter não instalado. Rate limiting desativado.")
    limiter = None

def limit_if_available(limit_string):
    def decorator(f):
        if limiter:
            return limiter.limit(limit_string)(f)
        return f
    return decorator

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
                    'has_real_token': bool(tokens.get('real')),
                    'plan': user.get('plan', 'free')
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
        name = ' '.join(name.split())
        user = AuthService.register(name, email, password, ref)
        if not user:
            return jsonify({'error': 'Email já registado'}), 400
        session.permanent = True
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['user_email'] = user['email']
        session['user_role'] = user.get('role', 'user')
        return jsonify({
            'status': 'ok', 'message': 'Conta criada! Conecte à Deriv para começar.',
            'user': {'id': user['id'], 'name': user['name'], 'email': user['email'],
                     'role': user.get('role'), 'has_deriv_token': False, 'active_account': 'demo', 'plan': 'free'},
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
        return jsonify({'status': 'ok', 'user': {
            'id': user['id'], 'name': user['name'], 'email': user['email'],
            'role': session['user_role'], 'has_deriv_token': bool(UserStore.get_active_token(user)),
            'active_account': user.get('active_account'), 'plan': user.get('plan', 'free')
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
            sess['trading_bot'].on_disconnect()
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
        sess['trading_bot'].on_disconnect()
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
        conn.execute('INSERT OR REPLACE INTO password_resets (email, token_hash, expires_at, used) VALUES (?,?,?,0)', (email, hashed, time.time() + 3600))
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
        row = conn.execute('SELECT email FROM password_resets WHERE token_hash = ? AND used = 0 AND expires_at > ?', (hashed, time.time())).fetchone()
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

def _get_otp_with_retry(email, account_type, max_attempts=2):
    for attempt in range(max_attempts):
        ws_url, balance, currency = get_otp_ws_url(email, account_type)
        if ws_url:
            return ws_url, balance, currency
        logger.warning(f"OTP falhou na tentativa {attempt + 1} para {email}")
        if attempt < max_attempts - 1:
            time.sleep(1)
    return None, 0, 'USD'

@app.route('/api/connect', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def api_connect():
    user_id = session['user_id']
    with connecting_lock:
        if user_id in connecting_users:
            return jsonify({'status': 'connecting_already', 'message': 'Conexão já em andamento'})
        connecting_users.add(user_id)
    try:
        email = session['user_email']
        user = UserStore.get(email)
        token = UserStore.get_active_token(user)
        if not token:
            return jsonify({'error': 'Token não configurado'}), 400
        ws_url, balance, currency = _get_otp_with_retry(email, user.get('active_account', 'demo'))
        if not ws_url:
            return jsonify({'error': 'Sessão expirada. Clique em Reconectar.'}), 400
        sess = create_session(user_id, user, ws_url_override=ws_url)
        if sess and balance > 0:
            sess['client'].balance = balance
            sess['client'].currency = currency
            sess['trading_bot'].balance = balance
            sess['trading_bot'].currency = currency
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
        return jsonify({'status': 'no_token'})
    sess = get_session(session['user_id'])
    if sess and sess['client'].connected and sess['client'].authorized:
        return jsonify({'status': 'already_connected', 'balance': sess['client'].balance})
    ws_url, balance, currency = _get_otp_with_retry(email, user.get('active_account', 'demo'))
    if not ws_url:
        return jsonify({'status': 'token_expired', 'message': 'Sessão expirada. Clique em Reconectar.'})
    sess = create_session(session['user_id'], user, ws_url_override=ws_url)
    if sess and balance > 0:
        sess['client'].balance = balance
        sess['client'].currency = currency
        sess['trading_bot'].balance = balance
        sess['trading_bot'].currency = currency
    return jsonify({'status': 'connecting'})

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
            old_sess = sessions[user_id]
            old_sess['trading_bot'].on_disconnect()
            old_client = old_sess['client']
            old_client._stop_event.set()
            if old_client._ws_thread and old_client._ws_thread.is_alive():
                old_client._ws_thread.join(timeout=5)
            del sessions[user_id]
    ws_url, balance, currency = get_otp_ws_url(email, acc_type)
    sess = create_session(user_id, user, force=True, ws_url_override=ws_url)
    reset_bot_state(sess['trading_bot'])
    if sess and balance > 0:
        sess['client'].balance = balance
        sess['client'].currency = currency
        sess['trading_bot'].balance = balance
        sess['trading_bot'].currency = currency
    return jsonify({'status': 'connecting', 'message': f'Conta {acc_type} ativada. A aguardar conexão...', 'account_type': acc_type})

@app.route('/api/status')
@require_auth
def status():
    user_id = session['user_id']
    sess = get_session(user_id)
    if not sess:
        return jsonify({'bot': {'connected': False, 'authorized': False}, 'digits': {}, 'symbols': config.AVAILABLE_SYMBOLS})
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
    bot_status['has_pending_trade'] = client.pending_trade is not None if client else False
    bot_status['last_trade_latency_ms'] = getattr(client, 'last_trade_latency_ms', 0) if client else 0
    bot_status['last_valid_ping_ms'] = getattr(client, '_last_valid_ping_ms', 0) if client else 0
    bot_status['last_tick_seconds_ago'] = client.get_last_tick_seconds_ago() if client else 999
    bot_status['last_tick_epoch'] = getattr(client, '_last_tick_epoch', None) if client else None
    bot_status['auth_error'] = getattr(client, 'auth_error', None) if client else None
    bot_status['token_expired'] = (
        isinstance(getattr(client, 'auth_error', None), dict) and
        getattr(client, 'auth_error', {}).get('code') in ('InvalidToken', 'TokenExpired')
    ) if client else False
    analysis = analyzer.get_analysis()
    digit_frequencies = analyzer.get_digit_frequencies()
    least_frequent = analyzer.get_least_frequent_digit()
    most_frequent = analyzer.get_most_frequent_digit()
    _save_daily_stats_to_db(session.get('user_email'), bot)
    forex_status = sess.get('forex_data').get_status() if sess.get('forex_data') else {}
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
        'loginid': client.loginid if client else None,
        'forex': forex_status
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
@require_admin
def debug():
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'error': 'Sessão não encontrada'}), 500
    c = sess['client']
    raw_ping = getattr(c, '_ping_ms', 0)
    if raw_ping >= 9999 and c.streaming and c._last_tick_time:
        effective_ping = 0 if time.time() - c._last_tick_time < 10 else 250
    else:
        effective_ping = raw_ping
    return jsonify({
        'connected': c.connected, 'authorized': c.authorized, 'streaming': c.streaming,
        'balance': c.balance, 'symbol': c.current_symbol, 'loginid': c.loginid if hasattr(c,'loginid') else None,
        'ws_thread_alive': c._ws_thread.is_alive() if c._ws_thread else False,
        'pending_trade': c.pending_trade is not None,
        'last_tick_seconds_ago': round(time.time() - c._last_tick_time, 1) if c._last_tick_time else None,
        'ping_ms': effective_ping,
        'reconnect_count': getattr(c, '_reconnect_count', 0),
        'last_reconnect_ago': round(time.time() - getattr(c, '_last_reconnect_time', time.time()), 1)
    })

# ==================== OAUTH PKCE + OTP ====================
@app.route('/api/auth/deriv_oauth_url')
@require_auth
def deriv_oauth_url():
    app_id = config.DERIV_APP_ID
    if not app_id:
        return jsonify({'error': 'Configuração OAuth em falta'}), 500
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    redirect_uri = base_url + '/oauth/callback'
    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('ascii')).digest()
    ).rstrip(b'=').decode('ascii')
    state_id = uuid.uuid4().hex
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        conn.execute(
            "INSERT INTO oauth_states (state_id, user_id, account_type, created_at, code_verifier) VALUES (?, ?, ?, ?, ?)",
            (state_id, session['user_id'], request.args.get('account_type', 'demo'), time.time(), code_verifier)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Erro ao criar state OAuth: {e}")
        return jsonify({'error': 'Erro interno ao iniciar OAuth'}), 500
    finally:
        conn.close()
    auth_url = (
        "https://auth.deriv.com/oauth2/auth"
        f"?response_type=code&client_id={app_id}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        f"&scope=trade&state={state_id}"
        f"&code_challenge={code_challenge}&code_challenge_method=S256"
    )
    return jsonify({'url': auth_url})

@app.route('/oauth/callback')
def oauth_callback():
    state_id = request.args.get('state')
    code = request.args.get('code')
    error = request.args.get('error')
    if error:
        return redirect('/?error=oauth_denied')
    if not state_id or not code:
        return redirect('/?error=invalid_params')
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT user_id, account_type, code_verifier FROM oauth_states WHERE state_id = ? AND used = 0 AND created_at > ?",
            (state_id, time.time() - OAUTH_STATE_TTL)
        ).fetchone()
        if not row:
            return redirect('/?error=state_expired')
        user_id, account_type, code_verifier = row
        token_url = "https://auth.deriv.com/oauth2/token"
        data = {
            'grant_type': 'authorization_code', 'client_id': config.DERIV_APP_ID,
            'code': code, 'code_verifier': code_verifier,
            'redirect_uri': f"{os.environ.get('BASE_URL', request.host_url.rstrip('/'))}/oauth/callback"
        }
        post_data = urllib.parse.urlencode(data).encode('ascii')
        req = urllib.request.Request(
            token_url, data=post_data,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
                'Origin': os.environ.get('BASE_URL', request.host_url.rstrip('/')),
                'Referer': os.environ.get('BASE_URL', request.host_url.rstrip('/'))
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                token_resp = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8')
            logger.error(f"Token exchange {e.code}: {body}")
            return redirect('/?error=token_exchange_failed')
        if 'error' in token_resp:
            return redirect('/?error=token_exchange_failed')
        access_token = token_resp.get('access_token')
        if not access_token:
            return redirect('/?error=no_access_token')
        conn.execute("UPDATE oauth_states SET used = 1 WHERE state_id = ?", (state_id,))
        conn.commit()
    finally:
        conn.close()
    email_row = sqlite3.connect(DATABASE_PATH, timeout=10).execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not email_row:
        return redirect('/?error=user_not_found')
    email = email_row[0]
    UserStore.add_token(email, account_type, access_token)
    UserStore.set_active_account(email, account_type)
    ws_url, balance, currency = get_otp_ws_url(email, account_type)
    if not ws_url:
        return redirect('/?error=otp_failed')
    user = UserStore.get(email)
    session['user_id'] = user_id
    session['user_email'] = email
    session['user_name'] = user['name']
    session['user_role'] = user.get('role', 'user')
    session.permanent = True
    sess = create_session(user_id, user, force=True, ws_url_override=ws_url)
    if sess and balance > 0:
        sess['client'].balance = balance
        sess['client'].currency = currency
        sess['trading_bot'].balance = balance
        sess['trading_bot'].currency = currency
    return make_response("""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OAuth</title></head>
<body><script>localStorage.setItem('oauth_result','connected');localStorage.setItem('oauth_ts',Date.now().toString());window.close();</script></body></html>""")

# ==================== VALIDAÇÃO DE AMOUNT ====================
def _validate_amount(amount):
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return None, 'Valor inválido'
    if amt < 0.35 or amt > 100:
        return None, 'Valor entre 0.35 e 100'
    return amt, None

# ==================== TRADING SINTÉTICOS (agendamento corrigido) ====================
@app.route('/api/trade/digit', methods=['POST'])
@require_auth
@limit_if_available("20 per minute")
def trade_digit():
    sess = get_session(session['user_id'])
    if not sess or not sess['client'].authorized:
        return jsonify({'error': 'Não conectado'}), 400
    bot = sess['trading_bot']
    if bot.stop_loss_active:
        return jsonify({'error': '🛑 Stop-loss activo. Limite diário atingido.'}), 400
    strategy = sess.get('strategy')
    if not strategy:
        return jsonify({'error': 'Estratégia indisponível'}), 400
    client = sess['client']
    if client.pending_trade is not None:
        return jsonify({'error': 'Trade pendente, aguarde'}), 400
    if client.active_trades:
        return jsonify({'error': 'Contrato ativo, aguarde resultado'}), 400

    analyzer = sess['digit_analyzer']
    last_digit = analyzer.get_current_digit()
    if last_digit is None:
        return jsonify({'error': 'Dígito indisponível'}), 400

    amt, err = _validate_amount(request.json.get('amount', 0.35))
    if err:
        return jsonify({'error': err}), 400

    is_odd = last_digit % 2 != 0
    direction = 'odd' if not is_odd else 'even'

    bot._last_click_tick = last_digit

    ok, msg = strategy.schedule_parity_bet(direction, amt)
    if not ok:
        return jsonify({'error': msg}), 400

    return jsonify({'status': 'ok', 'message': f'📅 {msg}'})

@app.route('/api/trade/differ', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def trade_differ():
    sess = get_session(session['user_id'])
    if not sess or not sess['client'].authorized:
        return jsonify({'error': 'Não conectado'}), 400
    bot = sess['trading_bot']
    if bot.stop_loss_active:
        return jsonify({'error': '🛑 Stop-loss activo. Limite diário atingido.'}), 400
    strategy = sess.get('strategy')
    if not strategy:
        return jsonify({'error': 'Estratégia indisponível'}), 400
    client = sess['client']
    if client.pending_trade is not None:
        return jsonify({'error': 'Trade pendente, aguarde'}), 400
    if client.active_trades:
        return jsonify({'error': 'Contrato ativo, aguarde resultado'}), 400

    analyzer = sess['digit_analyzer']
    digit = analyzer.get_current_digit()
    if digit is None:
        return jsonify({'error': 'Dígito indisponível'}), 400

    amt, err = _validate_amount(request.json.get('amount', 0.35))
    if err:
        return jsonify({'error': err}), 400

    bot._last_click_tick = digit

    ok, msg = strategy.schedule_differ_bet(digit, amt)
    if not ok:
        return jsonify({'error': msg}), 400

    return jsonify({'status': 'ok', 'message': f'📅 {msg}'})

@app.route('/api/trade/matches', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def trade_matches():
    sess = get_session(session['user_id'])
    if not sess or not sess['client'].authorized:
        return jsonify({'error': 'Não conectado'}), 400
    bot = sess['trading_bot']
    if bot.stop_loss_active:
        return jsonify({'error': '🛑 Stop-loss activo. Limite diário atingido.'}), 400
    strategy = sess.get('strategy')
    if not strategy or strategy._trade_locked:
        return jsonify({'error': 'Trade em curso — aguarde'}), 400
    client = sess['client']
    if client.pending_trade is not None:
        return jsonify({'error': 'Trade pendente, aguarde'}), 400
    if client.active_trades:
        return jsonify({'error': 'Contrato ativo, aguarde resultado'}), 400
    digit, reason = strategy.evaluate_matches()
    if digit is None:
        return jsonify({'error': f'⛔ {reason}'}), 400
    amt, err = _validate_amount(request.json.get('amount', 0.35))
    if err:
        return jsonify({'error': err}), 400
    analyzer = sess['digit_analyzer']
    tr = analyzer.get_ticks_remaining()
    if tr < 3:
        return jsonify({'error': f'⏳ Fim do ciclo ({tr} ticks). Aguarde o próximo.'}), 400
    sess['trading_bot']._last_click_tick = analyzer.get_current_digit()
    ok = sess['client'].place_matches_trade(digit, amt)
    if ok:
        strategy.lock_trade()
        credit_referral_commission(session['user_email'], amt)
        return jsonify({'status': 'ok', 'message': f'🎯 MATCHES no dígito {digit} por ${amt:.2f}'})
    return jsonify({'error': 'Falha no trade MATCHES'}), 500

@app.route('/api/trade/zscore', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def trade_zscore():
    sess = get_session(session['user_id'])
    if not sess or not sess['client'].authorized:
        return jsonify({'error': 'Não conectado'}), 400
    bot = sess['trading_bot']
    if bot.stop_loss_active:
        return jsonify({'error': '🛑 Stop-loss activo. Limite diário atingido.'}), 400
    strategy = sess.get('strategy')
    if not strategy or strategy._trade_locked:
        return jsonify({'error': 'Trade em curso — aguarde'}), 400
    client = sess['client']
    if client.pending_trade is not None:
        return jsonify({'error': 'Trade pendente, aguarde'}), 400
    if client.active_trades:
        return jsonify({'error': 'Contrato ativo, aguarde resultado'}), 400
    action, digit, reason = strategy.evaluate_zscore()
    if action is None:
        return jsonify({'error': f'⛔ {reason}'}), 400
    amt, err = _validate_amount(request.json.get('amount', 0.35))
    if err:
        return jsonify({'error': err}), 400
    analyzer = sess['digit_analyzer']
    tr = analyzer.get_ticks_remaining()
    if tr < 3:
        return jsonify({'error': f'⏳ Fim do ciclo ({tr} ticks). Aguarde o próximo.'}), 400
    sess['trading_bot']._last_click_tick = analyzer.get_current_digit()
    if action == 'DIFFER':
        ok = sess['client'].place_differ_trade(digit, amt)
    elif action == 'MATCHES':
        ok = sess['client'].place_matches_trade(digit, amt)
    else:
        return jsonify({'error': 'Ação Z‑Score inválida'}), 400
    if ok:
        strategy._zscore_sequence_used = True
        strategy.lock_trade()
        credit_referral_commission(session['user_email'], amt)
        return jsonify({'status': 'ok', 'message': f'🎯 Z‑Score {action} no dígito {digit} por ${amt:.2f}'})
    return jsonify({'error': 'Falha no trade Z‑Score'}), 500

@app.route('/api/zscore/ignore', methods=['POST'])
@require_auth
def ignore_zscore():
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'error': 'Sem sessão'}), 400
    strategy = sess.get('strategy')
    if strategy:
        strategy._zscore_sequence_used = True
        return jsonify({'status': 'ok', 'message': 'Sinal Z‑Score ignorado.'})
    return jsonify({'error': 'Estratégia não disponível'}), 400

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
    user_max = d.get('max_steps')
    if user_max is not None:
        try:
            user_max = int(user_max)
            if user_max < 1 or user_max > 4:
                return jsonify({'error': 'max_steps deve estar entre 1 e 4'}), 400
        except (ValueError, TypeError):
            return jsonify({'error': 'max_steps inválido'}), 400
    if la <= 0:
        return jsonify({'error': 'Valor inválido'}), 400
    sess = get_session(session['user_id'])
    if not sess:
        return jsonify({'error': 'Sessão não encontrada'}), 500
    bot = sess['trading_bot']
    strategy = sess.get('strategy') or getattr(bot, 'strategy', None)
    if strategy and strategy.is_global_stop:
        return jsonify({'error': '🛑 STOP GLOBAL ativo. Aguarde 3 minutos antes de aplicar Martingale.'}), 400
    ok, res = bot.apply_martingale_after_loss(la, user_max_steps=user_max)
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

@app.route('/api/signals/scoreboard')
@require_auth
def signals_scoreboard():
    user_id = session['user_id']
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        total = conn.execute('SELECT COUNT(*) FROM trades WHERE user_id=? AND timestamp>=?', (user_id, today_start)).fetchone()[0]
        wins = conn.execute('SELECT COUNT(*) FROM trades WHERE user_id=? AND timestamp>=? AND result="win"', (user_id, today_start)).fetchone()[0]
        losses = conn.execute('SELECT COUNT(*) FROM trades WHERE user_id=? AND timestamp>=? AND result="loss"', (user_id, today_start)).fetchone()[0]
    finally:
        conn.close()
    resolved = wins + losses
    win_rate = (wins / resolved * 100) if resolved > 0 else 0
    return jsonify({'today_trades': total, 'wins': wins, 'losses': losses, 'win_rate': round(win_rate, 1)})

def credit_referral_commission(user_email, amount):
    user = UserStore.get(user_email)
    if not user or not user.get('referral_code'):
        return
    ref_code = user['referral_code']
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        ref_user = conn.execute('SELECT email FROM users WHERE referral_link_code = ?', (ref_code,)).fetchone()
        if ref_user:
            commission = amount * (config.REFERRAL_COMMISSION_PERCENTAGE / 100)
            conn.execute('UPDATE users SET affiliate_earnings = affiliate_earnings + ? WHERE email = ?', (commission, ref_user[0]))
            conn.commit()
    except Exception as e:
        logger.error(f"Erro ao creditar comissão: {e}")
    finally:
        conn.close()

# ==================== ROTAS FOREX ====================
@app.route('/api/forex/signals')
@require_auth
def forex_signals():
    sess = get_session(session['user_id'])
    if not sess or not sess.get('forex_signals'):
        return jsonify({'error': 'Módulo Forex indisponível'}), 503
    signals = sess['forex_signals'].get_all_signals()
    return jsonify({'signals': signals})

@app.route('/api/forex/status')
@require_auth
def forex_status():
    sess = get_session(session['user_id'])
    if not sess or not sess.get('forex_data'):
        return jsonify({'error': 'Módulo Forex indisponível'}), 503
    return jsonify(sess['forex_data'].get_status())

@app.route('/api/forex/indicators/<symbol>')
@require_auth
def forex_indicators(symbol):
    sess = get_session(session['user_id'])
    if not sess or not sess.get('forex_indicators'):
        return jsonify({'error': 'Módulo Forex indisponível'}), 503
    ind = sess['forex_indicators'].get_all_indicators(symbol)
    return jsonify({'symbol': symbol, 'indicators': ind})

@app.route('/api/forex/candles/<symbol>')
@require_auth
def forex_candles(symbol):
    sess = get_session(session['user_id'])
    if not sess or not sess.get('forex_data'):
        return jsonify({'error': 'Módulo Forex indisponível'}), 503
    granularity = request.args.get('granularity', 60, type=int)
    count = request.args.get('count', 50, type=int)
    sess['forex_data'].request_candles(symbol, granularity=granularity, count=count)
    candles = sess['forex_data'].get_recent_candles(symbol, count=count, granularity=granularity)
    return jsonify({'candles': candles, 'symbol': symbol})

UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}

def _duration_str_to_seconds(dur_str):
    if not dur_str:
        return None
    unit = dur_str[-1]
    if unit == 't':
        return None
    try:
        val = int(dur_str[:-1])
    except ValueError:
        return None
    mult = UNIT_SECONDS.get(unit)
    return val * mult if mult else None

@app.route('/api/forex/contracts_for/<symbol>')
@require_auth
def forex_contracts_for(symbol):
    sess = get_session(session['user_id'])
    if not sess or not sess['client'].authorized:
        return jsonify({'error': 'Não conectado à Deriv'}), 400

    client = sess['client']
    durations = client.request_contracts_for(symbol)
    if durations is None:
        return jsonify({'error': 'Não foi possível obter as durações para este símbolo'}), 503

    result = {}
    for ctype, limits in durations.items():
        min_s = _duration_str_to_seconds(limits.get('min'))
        max_s = _duration_str_to_seconds(limits.get('max'))
        if min_s is None or max_s is None:
            continue

        min_m = max(1, -(-min_s // 60))
        max_m = max_s // 60
        if max_m < min_m:
            continue

        values = list(range(min_m, min(max_m, 60) + 1))
        for extra in (90, 120, 240, 480, 1440):
            if min_m <= extra <= max_m and extra not in values:
                values.append(extra)

        result[ctype] = {
            'unit': 'm',
            'min': limits['min'],
            'max': limits['max'],
            'allowed_values': values
        }

    return jsonify({'symbol': symbol, 'durations': result})

@app.route('/api/forex/trade', methods=['POST'])
@require_auth
@limit_if_available("10 per minute")
def forex_trade():
    d = request.json
    symbol = d.get('symbol', '').strip()
    direction = d.get('direction', '').strip().upper()

    amount_raw = d.get('amount')
    if amount_raw is None:
        return jsonify({'error': 'Valor da aposta em falta'}), 400
    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        return jsonify({'error': 'Valor da aposta inválido'}), 400

    duration_raw = d.get('duration', 1)
    try:
        duration = int(duration_raw)
    except (TypeError, ValueError):
        duration = 1

    if direction not in ('BUY', 'SELL'):
        return jsonify({'error': 'Direção inválida. Use BUY ou SELL.'}), 400
    if amount < 0.35 or amount > 100:
        return jsonify({'error': 'Valor entre 0.35 e 100'}), 400

    sess = get_session(session['user_id'])
    if not sess or not sess['client'].authorized:
        return jsonify({'error': 'Não conectado à Deriv'}), 400

    client = sess['client']
    if client.pending_trade is not None:
        status = client.get_pending_trade_status()
        if status and status.get('error'):
            client.pending_trade = None
        else:
            return jsonify({'error': 'Já existe um trade pendente'}), 400

    from forex_data import FOREX_SYMBOLS
    if symbol not in FOREX_SYMBOLS:
        return jsonify({'error': f'Símbolo inválido. Use: {list(FOREX_SYMBOLS.keys())}'}), 400

    if client.balance and client.balance < amount:
        return jsonify({'error': 'Saldo insuficiente'}), 400

    ok, msg = client.place_forex_trade(symbol, direction, amount, duration)
    if not ok:
        return jsonify({'error': msg or 'Falha ao enviar ordem'}), 400

    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get_pending_trade_status()
        if status is None:
            return jsonify({'status': 'ok', 'message': f'{direction} {symbol} ${amount:.2f} executado!'})
        if status.get('error'):
            err_msg = status['error'].get('message', 'Erro desconhecido')
            client.pending_trade = None
            return jsonify({'error': f'Proposta rejeitada: {err_msg}'}), 400
        time.sleep(0.2)

    return jsonify({'error': 'Proposta demorou muito. Verifique o estado na Deriv.'}), 500

@app.route('/api/forex/assertividade')
@require_auth
def forex_assertividade():
    symbol = request.args.get('symbol')
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    q = "SELECT outcome FROM forex_signal_log WHERE evaluated=1"
    params = []
    if symbol:
        q += " AND symbol=?"
        params.append(symbol)
    q += " ORDER BY id DESC LIMIT 50"
    rows = conn.execute(q, params).fetchall()
    conn.close()

    if len(rows) < 15:
        return jsonify({'assertividade': None, 'amostra': len(rows), 'message': 'Amostra insuficiente (mínimo 15)'})

    wins = sum(1 for r in rows if r[0] == 'win')
    return jsonify({'assertividade': round(wins / len(rows) * 100, 1), 'amostra': len(rows)})

# ==================== ROTAS ADMIN ====================
@app.route('/api/admin/users')
@require_admin
def admin_users():
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        rows = conn.execute('SELECT email, name, active FROM users').fetchall()
        users = [{'email': r[0], 'name': ' '.join(r[1].split()) if r[1] else '', 'active': bool(r[2])} for r in rows]
        return jsonify({'users': users})
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
            conn.execute('UPDATE users SET active_account = ? WHERE email = ?', ('demo', email))
            conn.commit()
        else:
            conn.execute('DELETE FROM user_tokens')
            conn.execute('UPDATE users SET active_account = ?', ('demo',))
            conn.commit()
    finally:
        conn.close()
    with sessions_lock:
        for uid, sess in list(sessions.items()):
            if (target_uid and uid == target_uid) or not target_uid:
                sess['trading_bot'].on_disconnect()
                sess['client']._stop_event.set()
                del sessions[uid]
    return jsonify({'status': 'ok', 'message': 'Tokens removidos. Utilizador terá que refazer OAuth.'})

@app.route('/api/admin/settings')
@require_admin
def admin_settings():
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='referral_commission_percentage'").fetchone()
        referral = float(row[0]) if row else config.REFERRAL_COMMISSION_PERCENTAGE
    finally:
        conn.close()
    return jsonify({'referral_commission_percentage': referral})

@app.route('/api/admin/set-markup', methods=['POST'])
@require_admin
def set_markup():
    d = request.json
    pct = float(d.get('percentage', 0.5))
    if not (0.0 <= pct <= 3.0):
        return jsonify({'error': 'Percentagem deve estar entre 0% e 3%'}), 400
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('referral_commission_percentage', ?)", (str(pct),))
        conn.commit()
    finally:
        conn.close()
    config.REFERRAL_COMMISSION_PERCENTAGE = pct
    return jsonify({'status': 'ok', 'referral_commission_percentage': pct})

@app.route('/api/admin/set-plan', methods=['POST'])
@require_admin
def set_plan():
    d = request.json
    email = d.get('email', '').strip().lower()
    plan = d.get('plan', 'free')
    if plan not in ('free', 'pro'):
        return jsonify({'error': 'Plano inválido. Use "free" ou "pro".'}), 400
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        conn.execute('UPDATE users SET plan = ? WHERE email = ?', (plan, email))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok', 'message': f'Plano de {email} alterado para {plan}.'})

# ==================== ROTAS AFILIADO / PAGAMENTO ====================
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
        referred_count = conn.execute('SELECT COUNT(*) FROM referrals WHERE referrer_email = ?', (email,)).fetchone()[0]
        total_commission = user.get('affiliate_earnings', 0.0)
    finally:
        conn.close()
    return jsonify({'total_referrals': referred_count, 'total_commission': total_commission, 'pending_commission': 0.0, 'paid_commission': total_commission})

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
            conn.execute('UPDATE users SET referral_link_code = ? WHERE email = ?', (ref_link, user['email']))
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
        referred_count = conn.execute('SELECT COUNT(*) FROM referrals WHERE referrer_email = ?', (user['email'],)).fetchone()[0]
    finally:
        conn.close()
    return jsonify({'earnings': user.get('affiliate_earnings', 0.0), 'referral_link': user.get('referral_link_code', ''), 'referred_count': referred_count, 'referred_list': []})

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

# ==================== INICIALIZAÇÃO ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
