import logging
import os
import sqlite3
import time
import threading
from collections import deque
from datetime import datetime, date
from config import config

logger = logging.getLogger(__name__)


class TradingBot:
    """
    Versão focada exclusivamente em DÍGITOS.
    O sinal agora é orquestrado pelo StrategyManager (módulo strategy.py),
    mas o bot mantém a gestão de trades, estatísticas e risco.

    FIX stats: contadores acumulados (nunca recalculados a partir do deque).

    COLETA PARALELA: grava cada slow_digit novo em digit_research_log.
    Esta é uma coleta passiva — não altera nenhuma lógica de trading,
    score, risk, martingale, Forex ou Parity/DIFFER.
    """

    def __init__(self):
        self.client = None
        self.current_price = 0
        self.current_symbol = 'R_100'
        self.balance = 0
        self.currency = 'USD'
        self.paused = False
        self.stop_loss_active = False
        self.digit_analyzer = None
        self.strategy = None

        self.stats = {
            'total': 0, 'wins': 0, 'losses': 0,
            'win_rate': 0, 'profit_loss': 0,
            'total_invested': 0, 'total_return': 0,
            'expired_trades': 0
        }

        self.daily_stats = {
            'date': datetime.now().date(), 'trades': 0,
            'wins': 0, 'losses': 0, 'profit_loss': 0,
            'start_balance': 0, 'expired_trades': 0
        }

        self.trades = deque(maxlen=100)
        self.consecutive_losses = 0
        self.consecutive_wins = 0

        self.martingale = {
            'active': False, 'step': 0,
            'original_amount': 0, 'last_result': None
        }

        self._client_connected = False
        self._client_authorized = False
        self._state_lock = threading.RLock()
        self._daily_stats_dirty = False

        self.on_signal_callback = None
        self.on_signal_result_callback = None
        self._last_signal_id = None

        self.last_trade_result = None

        self._last_click_time = None
        self._last_click_tick = None

        # ============ COLETA DE PESQUISA ============
        self._db_path = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
        self._last_research_slow_number = 0

    def start(self, client):
        self.client = client
        self.daily_stats['start_balance'] = self.balance
        self._daily_stats_dirty = True
        logger.info("🚀 Bot iniciado (modo Dígitos)")

    def pause(self):
        self.paused = True
        logger.info("⏸️ Pausado")

    def resume(self):
        self.paused = False
        logger.info("▶️ Resumido")

    def on_disconnect(self):
        self._client_connected = False
        self._client_authorized = False
        self.client = None
        logger.info("🔌 Bot: sessão desconectada, flags resetadas")

    def check_risk_limits(self):
        max_loss_pct = config.RISK_LIMITS.get('max_daily_loss_percent', 5)
        if self.daily_stats['start_balance'] > 0:
            daily_loss_pct = (
                abs(min(0, self.daily_stats['profit_loss'])) /
                self.daily_stats['start_balance'] * 100
            )
            if daily_loss_pct >= max_loss_pct:
                if not self.paused:
                    self.pause()
                    logger.warning(
                        f"🛑 Stop-loss ativado: {daily_loss_pct:.1f}% perda diária"
                    )
                self.stop_loss_active = True
                return False
        self.stop_loss_active = False
        return True

    def check_take_profit(self):
        if not config.RISK_LIMITS.get('take_profit_enabled', True):
            return False
        target_pct = config.RISK_LIMITS.get('daily_target_percent', 10)
        if self.daily_stats['start_balance'] > 0:
            profit_pct = (self.balance - self.daily_stats['start_balance']) / self.daily_stats['start_balance'] * 100
            if profit_pct >= target_pct:
                self.pause()
                logger.info(f"🏆 Take-profit atingido: {profit_pct:.1f}%")
                return True
        return False

    def on_tick(self, tick):
        symbol = tick.get('symbol', '')

        if symbol.startswith('frx'):
            return

        if symbol != self.current_symbol:
            return

        self.current_price = tick['price']

        if self.digit_analyzer:
            self.digit_analyzer.add_tick(self.current_price)
            self._maybe_record_slow_digit(symbol)

        if self.client:
            self.balance = self.client.balance
            self.currency = self.client.currency
            today = datetime.now().date()
            if self.daily_stats['date'] != today:
                self.reset_daily_stats()

        self.check_risk_limits()
        self.check_take_profit()

    def _maybe_record_slow_digit(self, symbol):
        """
        Coleta paralela — grava cada novo slow_digit em digit_research_log.
        Só grava quando o slow_number avança (evita duplicação por callback).
        Não interfere com trading, score, risk, martingale nem Forex.
        Falha silenciosamente (log) para não interromper ticks.
        """
        try:
            getter = getattr(self.digit_analyzer, 'get_last_slow_digit_info', None)
            if not getter:
                return
            slow_digit, slow_number = getter()
            if slow_digit is None or slow_number is None:
                return
            if slow_number <= self._last_research_slow_number:
                return

            conn = sqlite3.connect(self._db_path, timeout=5)
            try:
                conn.execute(
                    "INSERT INTO digit_research_log (symbol, digit, slow_number, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (symbol, int(slow_digit), int(slow_number), time.time())
                )
                conn.commit()
                self._last_research_slow_number = slow_number
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Erro ao registrar digit_research_log: {e}")

    def get_status(self):
        self.check_pending_trades()
        digit_action = None
        digit_conf = 0
        if self.client:
            conn = self.client.connected
            auth = self.client.authorized
        else:
            conn = self._client_connected
            auth = self._client_authorized
        return {
            'connected': conn,
            'authorized': auth,
            'price': self.current_price,
            'symbol': self.current_symbol,
            'balance': self.balance,
            'currency': self.currency,
            'signal': 'NEUTRAL',
            'confidence': 0,
            'tech_confidence': 0,
            'digit_confidence': digit_conf,
            'digit_action': digit_action,
            'analysis': {},
            'stats': self.stats,
            'paused': self.paused,
            'stop_loss_active': self.stop_loss_active,
            'martingale': self.get_martingale_status(),
            'daily_stats': self.daily_stats,
            'consecutive_wins': self.consecutive_wins,
            'consecutive_losses': self.consecutive_losses,
            'last_trade_result': self.last_trade_result
        }

    def get_martingale_status(self):
        with self._state_lock:
            return {
                'active': self.martingale['active'],
                'step': self.martingale['step'],
                'original_amount': self.martingale['original_amount'],
                'next_amount': self.get_martingale_amount(config.DEFAULT_STAKE),
                'system_max_steps': config.MARTINGALE_CONFIG.get('max_steps', 4),
                'multiplier': config.MARTINGALE_CONFIG.get('multiplier', 2.0)
            }

    def get_martingale_amount(self, base_amount):
        with self._state_lock:
            if not self.martingale['active'] or self.martingale['step'] == 0:
                return base_amount
            multiplier = config.MARTINGALE_CONFIG.get('multiplier', 2.0)
            return base_amount * (multiplier ** self.martingale['step'])

    def apply_martingale_after_loss(self, last_trade_amount, user_max_steps=None):
        with self._state_lock:
            system_max = config.MARTINGALE_CONFIG.get('max_steps', 4)
            max_steps = min(user_max_steps, system_max) if user_max_steps else system_max

            if self.martingale['step'] >= max_steps:
                return False, f"Máximo de {max_steps} perdas consecutivas atingido"
            self.martingale['step'] += 1
            self.martingale['active'] = True
            self.martingale['original_amount'] = last_trade_amount
            nxt = self.get_martingale_amount(last_trade_amount)
            if self.balance < nxt * 1.2:
                self.reset_martingale()
                return False, f"Saldo insuficiente para martingale (precisa ${nxt*1.2:.2f})"
            return True, {
                'step': self.martingale['step'],
                'next_amount': nxt,
                'multiplier': config.MARTINGALE_CONFIG.get('multiplier', 2.0),
                'max_steps': max_steps,
                'message': f"📈 Martingale ativo - Passo {self.martingale['step']}/{max_steps} | Próximo: ${nxt:.2f}"
            }

    def reset_martingale(self):
        with self._state_lock:
            self.martingale = {
                'active': False,
                'step': 0,
                'original_amount': 0,
                'last_result': None
            }

    def reset_daily_stats(self):
        self.daily_stats = {
            'date': datetime.now().date(), 'trades': 0,
            'wins': 0, 'losses': 0, 'profit_loss': 0,
            'start_balance': self.balance, 'expired_trades': 0
        }
        self.stop_loss_active = False
        self._daily_stats_dirty = True

    def set_daily_stats_from_db(self, saved):
        if saved and isinstance(saved, dict):
            try:
                saved_date = datetime.strptime(saved.get('date', ''), '%Y-%m-%d').date()
                if saved_date == datetime.now().date():
                    with self._state_lock:
                        self.daily_stats = {
                            'date': saved_date,
                            'trades': saved.get('trades', 0),
                            'wins': saved.get('wins', 0),
                            'losses': saved.get('losses', 0),
                            'profit_loss': saved.get('profit_loss', 0),
                            'start_balance': saved.get('start_balance', self.balance),
                            'expired_trades': saved.get('expired_trades', 0)
                        }
                        self.stop_loss_active = saved.get('stop_loss_active', False)
                    logger.info(f"📂 Estatísticas diárias carregadas da BD: {self.daily_stats}")
                    self._daily_stats_dirty = False
                    return
            except Exception as e:
                logger.error(f"Erro ao carregar daily_stats da BD: {e}")
        self.reset_daily_stats()

    def get_daily_stats_for_db(self):
        with self._state_lock:
            s = dict(self.daily_stats)
            if isinstance(s['date'], date):
                s['date'] = s['date'].strftime('%Y-%m-%d')
            else:
                s['date'] = str(s['date'])
            s['stop_loss_active'] = self.stop_loss_active
            return s

    def register_trade(self, trade_data):
        trade_data['timestamp'] = datetime.now()
        with self._state_lock:
            self.trades.append(trade_data)
            self.stats['total'] += 1
            self.stats['total_invested'] += trade_data['amount']
            self.daily_stats['trades'] += 1
            self._daily_stats_dirty = True
        self.update_stats()

    def update_stats(self):
        """
        FIX: usa contadores acumulados — nunca recalcula a partir do deque.
        """
        with self._state_lock:
            total = self.stats['total']
            wins = self.stats['wins']
            losses = self.stats['losses']
            invested = self.stats['total_invested']
            pl = self.stats['profit_loss']

            self.stats['win_rate'] = (wins / total) * 100 if total > 0 else 0
            self.stats['total_return'] = (pl / invested) * 100 if invested > 0 else 0

    def check_pending_trades(self):
        now = datetime.now()
        updated = False
        for trade in list(self.trades):
            if trade.get('result') == 'pending':
                elapsed = (now - trade['timestamp']).total_seconds()
                is_digit = trade.get('is_digit', False)
                timeout = 15 if is_digit else 60
                if elapsed > timeout:
                    with self._state_lock:
                        if trade.get('result') != 'pending':
                            logger.info(f"Trade {trade.get('contract_id')} já resolvido como '{trade.get('result')}' — a ignorar expiração")
                            continue
                        trade['result'] = 'expired'
                        trade['profit'] = 0
                        self.daily_stats['losses'] += 1
                        self.daily_stats['profit_loss'] -= trade.get('amount', 0)
                        self.stats['expired_trades'] += 1
                        self.daily_stats['expired_trades'] += 1
                        self._daily_stats_dirty = True
                        updated = True
                    logger.warning(f"⚠️ Trade pendente expirado: {trade.get('action')} ${trade.get('amount')} (ID: {trade.get('contract_id')})")
        if updated:
            self.update_stats()

    def on_trade_result(self, result):
        try:
            contract_id = result.get('contract_id')
            profit = result.get('profit', 0)
            is_win = profit > 0
            exit_digit = result.get('exit_digit')

            target_trade = None
            with self._state_lock:
                if contract_id:
                    for trade in self.trades:
                        if trade.get('contract_id') == contract_id:
                            target_trade = trade
                            break

                if not target_trade:
                    pending = [t for t in self.trades if t.get('result') == 'pending']
                    if pending:
                        target_trade = pending[-1]
                        pending_ids = [t.get('contract_id') for t in pending]
                        logger.warning(
                            f"⚠️ FALLBACK: contract_id {contract_id} não encontrado. "
                            f"Usando último trade pendente (ID: {target_trade.get('contract_id')}). "
                            f"Pendentes: {pending_ids}. "
                            f"Resultado original: action={result.get('action')}, profit={profit}, is_win={is_win}"
                        )
                    else:
                        logger.warning(f"⚠️ Nenhum trade pendente para contract_id {contract_id}. Ignorando.")
                        return

            with self._state_lock:
                was_expired = target_trade.get('result') == 'expired'

                if target_trade.get('result') not in ('pending', 'expired'):
                    logger.warning(f"Trade {contract_id} já tem resultado '{target_trade.get('result')}'. Ignorando.")
                    return

                if was_expired:
                    logger.info(f"🔄 Trade {contract_id} foi expirado mas o POC chegou — a aplicar resultado real")
                    self.daily_stats['losses'] -= 1
                    self.daily_stats['profit_loss'] += target_trade.get('amount', 0)
                    self.stats['expired_trades'] -= 1
                    self.daily_stats['expired_trades'] -= 1

                if is_win:
                    target_trade['result'] = 'win'
                    target_trade['profit'] = profit
                    self.daily_stats['wins'] += 1
                    self.daily_stats['profit_loss'] += profit
                    self.stats['wins'] += 1
                    self.stats['profit_loss'] += profit
                    self.consecutive_wins += 1
                    self.consecutive_losses = 0
                    logger.info(f"✅ GANHO! +${profit:.2f} | Vitórias consecutivas: {self.consecutive_wins}")
                    self.reset_martingale()
                else:
                    loss = target_trade.get('amount', 0)
                    target_trade['result'] = 'loss'
                    target_trade['profit'] = 0
                    self.daily_stats['losses'] += 1
                    self.daily_stats['profit_loss'] -= loss
                    self.stats['losses'] += 1
                    self.stats['profit_loss'] -= loss
                    self.consecutive_losses += 1
                    self.consecutive_wins = 0
                    logger.info(f"❌ PERDA! -${loss:.2f} | Contrato: {contract_id} | Ação: {target_trade.get('action')} | Perdas consecutivas: {self.consecutive_losses}")

                self._daily_stats_dirty = True

                click_time = self._last_click_time
                entry_time = result.get('entry_tick_time')
                latency_total_ms = None
                if click_time and entry_time:
                    latency_total_ms = round((entry_time - click_time) * 1000)

                action = target_trade.get('action', '')
                barrier = target_trade.get('digit_barrier', None)

                if exit_digit is not None:
                    won_digit = str(exit_digit)
                else:
                    if barrier is not None:
                        if 'DIFFER' in action:
                            won_digit = barrier if not is_win else '≠' + str(barrier)
                        elif 'MATCH' in action:
                            won_digit = barrier if is_win else '≠' + str(barrier)
                        else:
                            won_digit = '?'
                    else:
                        if 'DIGITODD' in action or action == 'CALL':
                            won_digit = 'Ímpar' if is_win else 'Par'
                        elif 'DIGITEVEN' in action or action == 'PUT':
                            won_digit = 'Par' if is_win else 'Ímpar'
                        else:
                            won_digit = '?'

                self.last_trade_result = {
                    'contract_id': contract_id,
                    'action': action,
                    'barrier': barrier,
                    'is_win': is_win,
                    'profit': profit,
                    'digit': won_digit,
                    'entry_digit': result.get('entry_digit'),
                    'exit_digit': exit_digit,
                    'entry_spot': result.get('entry_spot'),
                    'exit_spot': result.get('exit_spot'),
                    'entry_tick_time': entry_time,
                    'exit_tick_time': result.get('exit_tick_time'),
                    'latency_total_ms': latency_total_ms,
                    'click_tick': self._last_click_tick,
                }

            self.update_stats()
            if self.client:
                self.client.get_balance()

            if self.on_signal_result_callback and self._last_signal_id:
                try:
                    self.on_signal_result_callback(
                        self._last_signal_id,
                        'win' if is_win else 'loss',
                        profit
                    )
                    self._last_signal_id = None
                except Exception as e:
                    logger.error(f"Erro no callback de resultado: {e}")
        except Exception as e:
            logger.error(f"Erro ao processar resultado: {e}")

    def get_trade_report(self):
        self.check_pending_trades()
        hoje = datetime.now().date()
        with self._state_lock:
            trades_snapshot = list(self.trades)
            trades_hoje = [t for t in trades_snapshot if t['timestamp'].date() == hoje]
            return {
                'resumo': {
                    'total_trades': self.stats['total'],
                    'trades_hoje': len(trades_hoje),
                    'wins': self.stats['wins'],
                    'losses': self.stats['losses'],
                    'win_rate': round(self.stats['win_rate'], 2),
                    'profit_loss': round(self.stats['profit_loss'], 2),
                    'total_invested': round(self.stats['total_invested'], 2),
                    'total_return': round(self.stats['total_return'], 2),
                    'expired_trades': self.stats['expired_trades']
                },
                'historico': [{
                    'time': t['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                    'symbol': t.get('symbol', ''),
                    'action': t.get('action', ''),
                    'amount': t.get('amount', 0),
                    'result': t.get('result', 'pending'),
                    'profit': t.get('profit', 0),
                    'is_digit': t.get('is_digit', False)
                } for t in trades_snapshot[-50:]]
            }

    def reset_stats(self):
        with self._state_lock:
            self.stats = {
                'total': 0, 'wins': 0, 'losses': 0,
                'win_rate': 0, 'profit_loss': 0,
                'total_invested': 0, 'total_return': 0,
                'expired_trades': 0
            }
            self.trades.clear()
            self.consecutive_losses = 0
            self.consecutive_wins = 0
        logger.info("📊 Estatísticas e histórico resetados")
