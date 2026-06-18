import websocket
import json
import threading
import time
import logging
import random
from collections import deque
from config import config

logger = logging.getLogger(__name__)

class DerivWebSocketClient:
    ST_DISCONNECTED = 'DISCONNECTED'
    ST_CONNECTING   = 'CONNECTING'
    ST_CONNECTED    = 'CONNECTED'
    ST_STREAMING    = 'STREAMING'

    def __init__(self, config_obj, on_tick_callback=None, on_result_callback=None):
        self.config = config_obj
        self.ws = None
        self.connected = False
        self.authorized = False
        self.streaming = False
        self.balance = 0
        self.currency = 'USD'
        self.current_symbol = 'R_100'
        self.on_tick_callback = on_tick_callback
        self.on_result_callback = on_result_callback
        self.on_candles_callback = None
        self.trading_bot = None
        self.subscribed_symbols = set()
        self.user_token = None
        self.active_trades = {}
        self.pending_trade = None
        self.pending_trade_time = 0
        self._trade_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._digit_analyzer = None
        self._balance_subscribed = False
        self._stop_event = threading.Event()
        self._keep_alive_stop = threading.Event()
        self._watchdog_stop = threading.Event()
        self._poller_stop = threading.Event()
        self._last_tick_time = None
        self._last_trade_time = 0
        self._processed_contracts = deque(maxlen=1000)
        self._processed_lock = threading.Lock()
        self._req_counter = 1000
        self.state = self.ST_DISCONNECTED
        self._auth_time = 0
        self._keep_alive_thread = None
        self._watchdog_thread = None
        self._ws_thread = None
        self._poller_thread = None

        self.loginid = None
        self._connecting = False
        self._connect_lock = threading.Lock()
        self.auth_error = None
        self._had_gap = False
        self._first_connect = True

        self._candles_cache = {}
        self._candles_cache_lock = threading.Lock()

        self._last_reconnect_time = 0
        self._ping_ms = 0
        self._last_valid_ping_ms = 0
        self._reconnect_count = 0
        self._ping_sent_at = 0
        self._ping_pending = False
        self._ping_timer = None
        self._ping_failures = 0

        self._consecutive_failures = 0
        self._max_failures = 5
        self._cooldown_until = 0
        self._token_permanently_invalid = False

        self.last_trade_latency_ms = 0

        self._ws_url = None
        self._otp_refresh_callback = None

    def set_digit_analyzer(self, a): 
        self._digit_analyzer = a

    def set_trading_bot(self, b):
        self.trading_bot = b
        if b: 
            b.balance, b.currency, b.client = self.balance, self.currency, self

    def set_user_token(self, t):
        if not t:
            logger.error("❌ Token vazio recebido!")
            return
        self.user_token = t
        self._token_permanently_invalid = False
        logger.info(f"🔑 Token configurado: {t[:8]}...")

    def set_ws_url(self, url):
        self._ws_url = url
        logger.info(f"🔗 WebSocket URL personalizado: {url}")

    def _get_ws_url(self):
        return self._ws_url or self.config.WS_URL

    def _is_otp_ws(self):
        return self._ws_url and 'otp=' in self._ws_url

    def connect(self):
        self._stop_event.set()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2)
        self._close_connection()
        self._stop_event.clear()
        self._ws_thread = threading.Thread(target=self._run_forever, daemon=True)
        self._ws_thread.start()
        logger.info("🔌 Thread de ligação iniciada")

    def _run_forever(self):
        backoff = 1
        while not self._stop_event.is_set():
            if self._consecutive_failures >= self._max_failures:
                cooldown = 300
                logger.warning(f"🛑 {self._max_failures} falhas consecutivas. Pausa de {cooldown}s.")
                if self._stop_event.wait(timeout=cooldown):
                    break
                self._consecutive_failures = 0
                backoff = 1

            if self._token_permanently_invalid:
                logger.error("🔒 Token inválido. Aguardando novo token...")
                if self._stop_event.wait(timeout=60):
                    break
                continue

            if self._otp_refresh_callback and self._reconnect_count > 0:
                try:
                    new_url = self._otp_refresh_callback()
                    if new_url:
                        self._ws_url = new_url
                        logger.info(f"🔄 OTP renovado: {new_url[:60]}...")
                except Exception as e:
                    logger.error(f"Erro ao renovar OTP: {e}")

            self._reset_state()
            try:
                ws_url = self._get_ws_url()
                logger.info(f"🔌 A ligar à Deriv em {ws_url}...")
                self.ws = websocket.create_connection(ws_url, timeout=5)
                self.ws.settimeout(1.0)
                self.connected = True
                if not self._authorize_and_wait():
                    logger.error("Falha na autorização")
                    self._consecutive_failures += 1
                    continue

                self._consecutive_failures = 0
                self._last_reconnect_time = time.time()
                self._reconnect_count += 1

                if not self._is_otp_ws():
                    self._subscribe_balance()

                if self.current_symbol:
                    self._subscribe_ticks(self.current_symbol)
                    self.request_candles(self.current_symbol)

                self._resubscribe_active_trades()
                self._start_poller()

                if self._had_gap and not self._first_connect and self.trading_bot:
                    logger.warning("🕳️ Gap detetado – a limpar dados históricos do bot")
                    if hasattr(self.trading_bot, 'reset_price_history'):
                        self.trading_bot.reset_price_history()
                    else:
                        self.trading_bot.reset_stats()
                self._had_gap = False
                self._first_connect = False

                logger.info("🟢 Conectado e autorizado")
                self._start_keep_alive()
                self._start_watchdog()
                self._read_loop()
            except Exception as e:
                logger.error(f"Erro no loop principal: {e}")
                self._consecutive_failures += 1
            finally:
                self._stop_poller()
                self._teardown_connection()
                if not self._first_connect:
                    self._had_gap = True

            if self._stop_event.is_set():
                break
            backoff = min(backoff * 2, 60)
            jitter = random.uniform(0, 2)
            total_wait = backoff + jitter
            logger.info(f"🔄 Nova tentativa em {total_wait:.1f}s")
            if self._stop_event.wait(timeout=total_wait):
                break

    def _start_poller(self):
        self._stop_poller()
        self._poller_stop.clear()
        self._poller_thread = threading.Thread(target=self._poller_loop, daemon=True)
        self._poller_thread.start()
        logger.info("🔄 Poller de contratos iniciado")

    def _stop_poller(self):
        self._poller_stop.set()
        if self._poller_thread and self._poller_thread.is_alive():
            self._poller_thread.join(timeout=2)
        self._poller_thread = None

    def _poller_loop(self):
        while not self._poller_stop.wait(timeout=8):
            if self._stop_event.is_set() or not self.authorized:
                break
            if not self.active_trades:
                continue
            now = time.time()
            for cid, trade in list(self.active_trades.items()):
                if now - trade.get('timestamp', now) > 30:
                    logger.info(f"🔍 Poller: a forçar verificação do contrato {cid}")
                    try:
                        self.ws.send(json.dumps({
                            "proposal_open_contract": 1,
                            "contract_id": cid,
                            "subscribe": 1,
                            "req_id": self._next_req()
                        }))
                    except Exception as e:
                        logger.error(f"Erro no poller para {cid}: {e}")

    def _reset_state(self):
        self.subscribed_symbols.clear()
        with self._pending_lock:
            self.pending_trade = None
        self.pending_trade_time = 0
        self._balance_subscribed = False
        self.connected = False
        self.authorized = False
        self.streaming = False
        self._last_tick_time = None
        self.state = self.ST_DISCONNECTED
        self.loginid = None
        self.auth_error = None

    def _authorize_and_wait(self, timeout=10):
        if self._is_otp_ws():
            self.authorized = True
            self.loginid = 'OTP_AUTH'
            logger.info("✅ Autenticado via OTP URL (sem authorize)")
            return True

        if not self.user_token:
            logger.error("🚫 Tentativa de autorizar sem token!")
            return False
        if len(self.user_token) < 10:
            logger.error(f"🚫 Token suspeito: '{self.user_token}'")
            return False
        self.ws.send(json.dumps({"authorize": self.user_token, "req_id": self._next_req()}))
        self._auth_time = time.time()
        logger.info("🔐 Pedido de autorização enviado")
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop_event.is_set():
            try:
                msg = self.ws.recv()
                data = json.loads(msg)
                if data.get('msg_type') == 'authorize':
                    if data.get('error'):
                        err = data['error']
                        logger.error(f"❌ Auth erro: {err}")
                        self.auth_error = err
                        if err.get('code') in ('InvalidToken', 'TokenExpired'):
                            self._token_permanently_invalid = True
                            logger.error("🔒 Token inválido/expirado.")
                        return False
                    logger.info("✅ Autorizado com sucesso!")
                    self.authorized = True
                    self.loginid = data.get('authorize', {}).get('loginid', '')
                    logger.info(f"LoginID: {self.loginid}")
                    return True
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                logger.error(f"Erro ao aguardar autorização: {e}")
                return False
        return False

    def _teardown_connection(self):
        self._stop_keep_alive()
        self._stop_watchdog()
        self._close_connection()

    def _close_connection(self):
        if self.ws:
            try:
                self.ws.send(json.dumps({"forget_all": "ticks", "req_id": self._next_req()}))
            except Exception:
                pass
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.connected = False
        self.authorized = False
        self.streaming = False
        self.state = self.ST_DISCONNECTED

    def _read_loop(self):
        while not self._stop_event.is_set() and self.ws:
            try:
                msg = self.ws.recv()
                if not msg:
                    break
                self._on_message(msg)
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break

    def _on_message(self, message):
        try:
            data = json.loads(message)
            msg_type = data.get('msg_type', '')
            if msg_type not in ['tick', 'balance', 'time', 'ping', 'pong']:
                logger.debug(f"📨 [{msg_type}]")
            handlers = {
                'tick':                   self._on_tick,
                'balance':                self._on_balance,
                'proposal':               self._on_proposal,
                'buy':                    self._on_buy_response,
                'proposal_open_contract': self._on_poc,
                'error':                  self._on_api_error,
                'candles':                self._on_candles,
                'pong':                   self._on_pong,
            }
            handler = handlers.get(msg_type)
            if handler:
                handler(data)
        except json.JSONDecodeError:
            logger.error("Mensagem JSON inválida: %s", message[:200])
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {e}", exc_info=True)

    def _start_keep_alive(self):
        self._stop_keep_alive()
        self._keep_alive_stop.clear()
        self._ping_failures = 0
        self._keep_alive_thread = threading.Thread(target=self._keep_alive_loop, daemon=True)
        self._keep_alive_thread.start()

    def _stop_keep_alive(self):
        self._keep_alive_stop.set()
        if self._keep_alive_thread and self._keep_alive_thread.is_alive():
            self._keep_alive_thread.join(timeout=2)
        self._cancel_ping_timer()

    def _keep_alive_loop(self):
        consecutive_failures = 0
        while not self._stop_event.is_set() and not self._keep_alive_stop.is_set():
            if self._keep_alive_stop.wait(timeout=30):
                break
            if not self.ws or not self.connected:
                break
            try:
                self._ping_sent_at = time.time()
                self._ping_pending = True
                self.ws.send(json.dumps({"ping": 1, "req_id": self._next_req()}))
                self._start_ping_timer()
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"Erro ao enviar ping ({consecutive_failures} falha(s)): {e}")
                if consecutive_failures >= 3:
                    logger.warning("🛑 Keep‑alive falhou 3x — a forçar reconexão")
                    self._close_connection()
                    break

    def _start_ping_timer(self):
        self._cancel_ping_timer()
        self._ping_timer = threading.Timer(5.0, self._ping_timeout)
        self._ping_timer.daemon = True
        self._ping_timer.start()

    def _cancel_ping_timer(self):
        if self._ping_timer:
            self._ping_timer.cancel()
            self._ping_timer = None

    def _ping_timeout(self):
        if self._ping_pending:
            self._ping_ms = 9999
            self._ping_pending = False
            logger.warning("🏓 Pong não recebido — latência >2000ms")

    def _on_pong(self, data):
        self._cancel_ping_timer()
        if self._ping_sent_at and self._ping_pending:
            self._ping_ms = round((time.time() - self._ping_sent_at) * 1000)
            self._ping_pending = False
            self._last_valid_ping_ms = self._ping_ms
            logger.debug(f"🏓 Ping: {self._ping_ms}ms")

    def _start_watchdog(self):
        self._stop_watchdog()
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _stop_watchdog(self):
        self._watchdog_stop.set()
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2)

    def _watchdog_loop(self):
        while not self._stop_event.is_set() and not self._watchdog_stop.is_set():
            if self._watchdog_stop.wait(timeout=10):
                break
            if not self.ws or not self.connected:
                break
            if self.streaming and self._last_tick_time is not None:
                if time.time() - self._last_tick_time > 180:
                    logger.warning("🛑 Watchdog: >180s sem ticks.")
                    self._close_connection()
                    break
            elif not self.streaming:
                if self._auth_time and time.time() - self._auth_time > 180:
                    logger.warning("🛑 Watchdog: 180s ligado sem stream.")
                    self._close_connection()
                    break

    def change_symbol(self, symbol):
        if symbol in self.subscribed_symbols:
            self.subscribed_symbols.discard(symbol)
        self.current_symbol = symbol
        if self.authorized:
            self._subscribe_ticks(symbol)
            self.request_candles(symbol)

    def _subscribe_balance(self):
        try:
            self.ws.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": self._next_req()}))
            self._balance_subscribed = True
        except Exception as e:
            logger.error(f"Erro subs. saldo: {e}")

    def _on_balance(self, data):
        bd = data.get('balance', {})
        if bd:
            self.balance = float(bd.get('balance', 0))
            self.currency = bd.get('currency', 'USD')
            if self.trading_bot:
                self.trading_bot.balance = self.balance
                self.trading_bot.currency = self.currency

    def get_balance(self, force=False):
        if not self._balance_subscribed:
            self._subscribe_balance()
        elif force:
            try:
                self.ws.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": self._next_req()}))
            except Exception as e:
                logger.error(f"Erro ao pedir saldo: {e}")

    def _subscribe_ticks(self, symbol):
        if not self.authorized:
            return
        if symbol in self.subscribed_symbols:
            return
        try:
            self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": self._next_req()}))
            self.subscribed_symbols.add(symbol)
            self.current_symbol = symbol
            logger.info(f"📊 Subscrição de ticks para {symbol} enviada")
        except Exception as e:
            logger.error(f"Erro subs. ticks: {e}")

    def _on_tick(self, data):
        tick = data.get('tick', {})
        if not tick:
            return
        if not self.streaming:
            self.streaming = True
            self.state = self.ST_STREAMING
            logger.info("📡 Estado STREAMING ativado!")
        self._last_tick_time = time.time()
        if self.on_tick_callback:
            self.on_tick_callback({
                'symbol':    tick.get('symbol', self.current_symbol),
                'price':     float(tick.get('quote', 0)),
                'timestamp': tick.get('epoch', time.time())
            })

    def _next_req(self):
        with self._req_lock:
            self._req_counter += 1
            return self._req_counter

    def _pre_trade_check(self):
        if time.time() - self._last_reconnect_time < 10:
            return False, "Reconexão recente"
        if not self.streaming:
            return False, "Sem streaming"
        if self.balance <= 0:
            return False, "Saldo não carregado"
        if self.balance < 0.35:
            return False, "Saldo insuficiente"
        if time.time() - self._last_trade_time < 2:
            return False, "Intervalo mínimo 2s"
        if not self.authorized:
            return False, "Não autorizado"
        with self._pending_lock:
            if self.pending_trade is not None:
                if time.time() - self.pending_trade_time > 60:
                    self.pending_trade = None
                else:
                    return False, "Trade pendente"
        return True, None

    def place_trade(self, contract_type, amount, is_digit=False):
        if self.trading_bot and not self.trading_bot.check_risk_limits():
            logger.warning("🚫 Trade bloqueado pelo stop‑loss diário")
            return False

        with self._trade_lock:
            ok, err = self._pre_trade_check()
            if not ok:
                logger.warning(f"🚫 Trade bloqueado: {err}")
                return False

            self._last_trade_time = time.time()

            if is_digit:
                duration = self.config.DIGIT_CONTRACT_DURATION
                duration_unit = 't'
                contract_type_full = 'DIGITODD' if contract_type == 'CALL' else 'DIGITEVEN'
            else:
                duration = self.config.CONTRACT_DURATION
                duration_unit = self.config.CONTRACT_DURATION_UNIT
                contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

            req_id = self._next_req()
            with self._pending_lock:
                self.pending_trade = {
                    'amount': amount,
                    'contract_type': contract_type,
                    'is_digit': is_digit,
                    'timestamp': time.time(),
                    'status': 'waiting_proposal',
                    'req_id': req_id
                }
            self.pending_trade_time = time.time()

            logger.info(f"📤 Enviando proposta: {contract_type_full}, amount={amount}, symbol={self.current_symbol}")
            try:
                payload = {
                    "proposal": 1, "amount": amount, "basis": "stake",
                    "contract_type": contract_type_full, "currency": self.currency,
                    "duration": duration, "duration_unit": duration_unit,
                }
                if not self._is_otp_ws():
                    payload["symbol"] = self.current_symbol
                self.ws.send(json.dumps(payload))
                return True
            except Exception as e:
                logger.error(f"❌ Erro trade: {e}")
                with self._pending_lock:
                    self.pending_trade = None
                return False

    def place_differ_trade(self, digit, amount):
        if self.trading_bot and not self.trading_bot.check_risk_limits():
            logger.warning("🚫 Trade bloqueado pelo stop‑loss diário")
            return False

        with self._trade_lock:
            ok, err = self._pre_trade_check()
            if not ok:
                logger.warning(f"🚫 Trade bloqueado: {err}")
                return False

            self._last_trade_time = time.time()
            duration = self.config.DIGIT_CONTRACT_DURATION
            duration_unit = 't'
            req_id = self._next_req()
            with self._pending_lock:
                self.pending_trade = {
                    'amount': amount, 'contract_type': f'DIFFER_{digit}',
                    'is_digit': True, 'is_differ': True, 'digit_barrier': digit,
                    'timestamp': time.time(), 'status': 'waiting_proposal', 'req_id': req_id
                }
            self.pending_trade_time = time.time()
            logger.info(f"📤 Enviando DIGITDIFF: barreira={digit}, amount={amount}")
            try:
                payload = {
                    "proposal": 1, "amount": amount, "basis": "stake",
                    "contract_type": "DIGITDIFF", "currency": self.currency,
                    "duration": duration, "duration_unit": duration_unit,
                    "barrier": digit,
                }
                if not self._is_otp_ws():
                    payload["symbol"] = self.current_symbol
                self.ws.send(json.dumps(payload))
                return True
            except Exception as e:
                logger.error(f"❌ Erro DIGITDIFF: {e}")
                with self._pending_lock:
                    self.pending_trade = None
                return False

    def place_matches_trade(self, digit, amount):
        if self.trading_bot and not self.trading_bot.check_risk_limits():
            logger.warning("🚫 Trade bloqueado pelo stop‑loss diário")
            return False

        with self._trade_lock:
            ok, err = self._pre_trade_check()
            if not ok:
                logger.warning(f"🚫 Trade bloqueado: {err}")
                return False

            self._last_trade_time = time.time()
            duration = self.config.DIGIT_CONTRACT_DURATION
            duration_unit = 't'
            req_id = self._next_req()
            with self._pending_lock:
                self.pending_trade = {
                    'amount': amount, 'contract_type': f'MATCH_{digit}',
                    'is_digit': True, 'is_matches': True, 'digit_barrier': digit,
                    'timestamp': time.time(), 'status': 'waiting_proposal', 'req_id': req_id
                }
            self.pending_trade_time = time.time()
            logger.info(f"📤 Enviando DIGITMATCH: dígito={digit}, amount={amount}")
            try:
                payload = {
                    "proposal": 1, "amount": amount, "basis": "stake",
                    "contract_type": "DIGITMATCH", "currency": self.currency,
                    "duration": duration, "duration_unit": duration_unit,
                    "barrier": digit,
                }
                if not self._is_otp_ws():
                    payload["symbol"] = self.current_symbol
                self.ws.send(json.dumps(payload))
                return True
            except Exception as e:
                logger.error(f"❌ Erro DIGITMATCH: {e}")
                with self._pending_lock:
                    self.pending_trade = None
                return False

    def _on_proposal(self, data):
        with self._pending_lock:
            if self.pending_trade is None:
                logger.debug("📨 Proposta recebida mas sem pending_trade")
                return
            if data.get('req_id') != self.pending_trade.get('req_id'):
                logger.debug(f"📨 req_id diferente: {data.get('req_id')} != {self.pending_trade.get('req_id')}")
                return
            if data.get('error'):
                logger.error(f"❌ Erro na proposta: {data['error']}")
                self.pending_trade = None
                return
            p = data.get('proposal', {})
            pid, ask = p.get('id'), p.get('ask_price')
            if not pid or ask is None:
                logger.warning(f"⚠️ Proposta sem id/ask_price")
                self.pending_trade = None
                return
            if 'proposal_id' in self.pending_trade:
                logger.warning("BUY já enviado")
                return
            self.pending_trade['proposal_id'] = pid
            logger.info(f"📥 Proposta recebida: id={pid}, ask_price={ask}")
        try:
            self.ws.send(json.dumps({"buy": pid, "price": ask, "req_id": self._next_req()}))
            logger.info(f"🛒 Buy enviado para proposta {pid}")
        except Exception as e:
            logger.error(f"❌ Erro ao enviar buy: {e}")
            with self._pending_lock:
                self.pending_trade = None

    def _on_buy_response(self, data):
        with self._pending_lock:
            if data.get('error'):
                logger.error(f"❌ Erro na resposta de buy: {data['error']}")
                self.pending_trade = None
                return
            bd = data.get('buy', {})
            cid, bp = bd.get('contract_id'), bd.get('buy_price', 0)
            if not cid:
                logger.warning(f"⚠️ Buy sem contract_id")
                self.pending_trade = None
                return
            if self.pending_trade:
                trade_timestamp = self.pending_trade.get('timestamp', time.time())
                amt = self.pending_trade.get('amount', 0)
                action = self.pending_trade.get('contract_type', '')
                is_digit = self.pending_trade.get('is_digit', False)
                is_differ = self.pending_trade.get('is_differ', False)
                is_matches = self.pending_trade.get('is_matches', False)
                digit_barrier = self.pending_trade.get('digit_barrier')

                latency_ms = round((time.time() - trade_timestamp) * 1000)
                logger.info(f"✅ Contrato comprado: cid={cid}, bp={bp}, action={action}, latency={latency_ms}ms")
                if latency_ms > 300:
                    logger.warning(f"⚠️ Latência alta ({latency_ms}ms)")

                if self._digit_analyzer:
                    entry_tick = self._digit_analyzer.get_current_digit()
                    entry_tick_count = self._digit_analyzer._tick_count
                else:
                    entry_tick = 'N/A'
                    entry_tick_count = 'N/A'
                logger.info(
                    f"🔍 AUDITORIA ENTRADA | contract_id={cid} "
                    f"| tick_entrada={entry_tick} "
                    f"| tick_count_entrada={entry_tick_count}"
                )

                self.last_trade_latency_ms = latency_ms

                if self.trading_bot:
                    self.trading_bot.register_trade({
                        'contract_id': cid, 'symbol': self.current_symbol,
                        'action': action, 'amount': amt, 'price': bp,
                        'result': 'pending', 'is_digit': is_digit,
                        'is_differ': is_differ, 'is_matches': is_matches,
                        'digit_barrier': digit_barrier
                    })
                self.active_trades[cid] = {
                    'contract_id': cid, 'amount': amt, 'buy_price': bp,
                    'timestamp': time.time(), 'action': action,
                    'is_digit': is_digit, 'is_differ': is_differ,
                    'is_matches': is_matches, 'digit_barrier': digit_barrier,
                    'symbol': self.current_symbol
                }
                try:
                    self._subscribe_contract(cid)
                except Exception as e:
                    logger.error(f"Erro ao subscrever contrato {cid}: {e}")
                finally:
                    self.pending_trade = None

    def _subscribe_contract(self, cid):
        try:
            self.ws.send(json.dumps({
                "proposal_open_contract": 1, "contract_id": cid,
                "subscribe": 1, "req_id": self._next_req()
            }))
            logger.info(f"📎 Subscrição de contrato enviada: {cid}")
        except Exception as e:
            logger.error(f"Erro subs. contrato {cid}: {e}")
            raise

    def _resubscribe_active_trades(self):
        if not self.active_trades:
            return
        now = time.time()
        expired = [cid for cid, t in self.active_trades.items()
                   if now - t.get('timestamp', now) > 120]
        for cid in expired:
            logger.warning(f"⚠️ Trade {cid} expirado (120s+) — removido")
            del self.active_trades[cid]
        if not self.active_trades:
            return
        logger.info(f"🔄 Reassinar {len(self.active_trades)} contrato(s)...")
        for cid in list(self.active_trades.keys()):
            try:
                self.ws.send(json.dumps({
                    "proposal_open_contract": 1, "contract_id": cid,
                    "subscribe": 1, "req_id": self._next_req()
                }))
            except Exception as e:
                logger.error(f"Falha ao reassinar {cid}: {e}")

    def _on_poc(self, data):
        c = data.get('proposal_open_contract', {})
        cid = c.get('contract_id')
        if not cid or not c.get('is_sold'):
            return
        logger.info(f"📦 POC recebido: contract_id={cid}, is_sold=True")

        bp = c.get('buy_price', 0)
        sp = c.get('sell_price', 0)

        if sp is None:
            with self._processed_lock:
                if cid in self._processed_contracts:
                    self._processed_contracts.remove(cid)
            logger.warning(f"⚠️ POC ignorado: sell_price ausente para {cid} — permitindo reprocessamento")
            return

        with self._processed_lock:
            if cid in self._processed_contracts:
                return
            self._processed_contracts.append(cid)

        profit = sp - bp
        is_win = profit > 0
        logger.info(f"💰 POC: cid={cid}, bp={bp}, sp={sp}, profit={profit:.4f}, is_win={is_win}")

        if self._digit_analyzer:
            current_tick = self._digit_analyzer.get_current_digit()
            current_tick_count = self._digit_analyzer._tick_count
        else:
            current_tick = 'N/A'
            current_tick_count = 'N/A'
        logger.info(
            f"🔍 AUDITORIA POC | contract_id={cid} "
            f"| tick_no_resultado={current_tick} "
            f"| tick_count_resultado={current_tick_count} "
            f"| profit={profit:.4f} | is_win={is_win}"
        )

        trade_info = self.active_trades.get(cid, {})
        if not trade_info:
            logger.warning(f"⚠️ POC para contrato desconhecido: {cid}")

        if self.trading_bot:
            self.trading_bot.on_trade_result({
                'contract_id': cid, 'buy_price': bp, 'sell_price': sp,
                'profit': profit, 'amount': trade_info.get('amount', bp),
                'is_win': is_win
            })
        if self.on_result_callback:
            self.on_result_callback({
                'contract_id': cid, 'symbol': trade_info.get('symbol', self.current_symbol),
                'action': trade_info.get('action', ''), 'amount': trade_info.get('amount', bp),
                'buy_price': bp, 'sell_price': sp, 'profit': profit, 'is_win': is_win,
                'is_digit': trade_info.get('is_digit', False),
                'is_differ': trade_info.get('is_differ', False),
                'is_matches': trade_info.get('is_matches', False),
                'digit_barrier': trade_info.get('digit_barrier')
            })
        if cid in self.active_trades:
            del self.active_trades[cid]

    def _on_api_error(self, data):
        err = data.get('error', {})
        code = err.get('code', 'N/A')
        msg = err.get('message', 'desconhecido')
        self.auth_error = err
        logger.error(f"❌ API Error: {msg} (código: {code})")
        if code == 'InvalidToken':
            logger.error("🔑 Token inválido")
        elif code == 'AuthorizationRequired':
            logger.error("🔒 Autorização necessária")
        elif code == 'RateLimit':
            logger.warning("⏱️ Rate limit")

    def request_candles(self, symbol=None, granularity=60, count=50):
        symbol = symbol or self.current_symbol
        try:
            self.ws.send(json.dumps({
                "ticks_history": symbol, "style": "candles",
                "granularity": granularity, "count": count,
                "end": "latest", "req_id": self._next_req()
            }))
        except Exception as e:
            logger.error(f"Erro ao pedir velas: {e}")

    def _on_candles(self, data):
        candles = data.get('candles', [])
        if candles:
            with self._candles_cache_lock:
                self._candles_cache = {'data': candles, 'timestamp': time.time()}
            if self.on_candles_callback:
                self.on_candles_callback(candles)

    def get_cached_candles(self, max_age=None):
        if max_age is None:
            max_age = config.CANDLE_CACHE_TTL
        with self._candles_cache_lock:
            if not self._candles_cache:
                return None
            age = time.time() - self._candles_cache.get('timestamp', 0)
            if age > max_age:
                return None
            return self._candles_cache['data']

    def request_deposit(self, amount, currency, method):
        return {'status': 'pending', 'message': f'Depósito ${amount} solicitado.'}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance:
            return {'error': 'Saldo insuficiente'}
        return {'status': 'pending', 'message': f'Saque ${amount} solicitado.'}
