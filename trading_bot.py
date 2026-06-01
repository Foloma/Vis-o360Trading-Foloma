import logging
import time
import threading
from collections import deque
from datetime import datetime, date
from config import config

logger = logging.getLogger(__name__)


class TradingBot:
    """
    Versão focada exclusivamente em DÍGITOS.
    Os modos Ativos e Híbrido foram desativados por falta de eficácia comprovada.
    O sinal agora depende apenas do DigitAnalyzer, com dois filtros:
    1. Entropia favorável (BEM DEFINIDO ou PREVISÍVEL)
    2. Confiança mínima (>= 60%)
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

        self.stats = {
            'total': 0, 'wins': 0, 'losses': 0,
            'win_rate': 0, 'profit_loss': 0,
            'total_invested': 0, 'total_return': 0
        }

        self.daily_stats = {
            'date': datetime.now().date(), 'trades': 0,
            'wins': 0, 'losses': 0, 'profit_loss': 0,
            'start_balance': 0
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

        # Cache do último sinal emitido (evita spam na BD)
        self._last_emitted_signal = None

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
        self.current_price = tick['price']
        self.current_symbol = tick['symbol']

        if self.digit_analyzer:
            self.digit_analyzer.add_tick(self.current_price)

        if self.client:
            self.balance = self.client.balance
            self.currency = self.client.currency
            today = datetime.now().date()
            if self.daily_stats['date'] != today:
                self.reset_daily_stats()

        self.check_risk_limits()
        self.check_take_profit()

    def feed_candle_data(self, high, low, close):
        """
        🔥 NOVO: Recebe dados de high/low das velas para alimentar
        os indicadores (ATR/ADX). Chamado pelo deriv_client quando as velas chegam.
        """
        pass

    def calculate_signal(self):
        """
        Devolve sempre NEUTRAL para manter compatibilidade com rotas de Ativos/Híbrido.
        Estas rotas bloqueiam o trade porque a confiança é 0.
        """
        return 'NEUTRAL', 0

    def get_digit_signal(self):
        """
        Sinal de dígito com apenas dois filtros:
        1. Entropia favorável (BEM DEFINIDO ou PREVISÍVEL)
        2. Confiança >= 60%
        
        Só emite callback quando o sinal muda (evita spam na BD).
        """
        if not self.digit_analyzer:
            return None, 0

        analysis = self.digit_analyzer.get_analysis()
        action = analysis.get('recommended_action')
        conf = analysis.get('confidence', 0)
        entropy_verdict = analysis.get('entropy_verdict', '---')

        # Reset se não há sinal válido
        if entropy_verdict not in ('BEM DEFINIDO', 'PREVISÍVEL'):
            self._last_emitted_signal = None
            return None, 0

        if not action or conf < 60:
            self._last_emitted_signal = None
            return None, 0

        # Só notifica se o sinal for diferente do último emitido
        if self.on_signal_callback and action != self._last_emitted_signal:
            self._last_emitted_signal = action
            try:
                self.on_signal_callback({
                    'signal': action,
                    'confidence': conf,
                    'tech_confidence': 0,
                    'digit_confidence': conf,
                    'digit_action': action,
                    'symbol': self.current_symbol
                })
            except Exception as e:
                logger.error(f"Erro no callback de sinal: {e}")

        return action, conf

    def get_status(self):
        self.check_pending_trades()
        digit_action, digit_conf = self.get_digit_signal()
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
            'consecutive_losses': self.consecutive_losses
        }

    def get_martingale_status(self):
        with self._state_lock:
            return {
                'active': self.martingale['active'],
                'step': self.martingale['step'],
                'original_amount': self.martingale['original_amount'],
                'next_amount': self.get_martingale_amount(config.DEFAULT_STAKE),
                'max_steps': config.MARTINGALE_CONFIG.get('max_steps', 2),
                'multiplier': config.MARTINGALE_CONFIG.get('multiplier', 2.0)
            }

    def get_martingale_amount(self, base_amount):
        with self._state_lock:
            if not self.martingale['active'] or self.martingale['step'] == 0:
                return base_amount
            multiplier = config.MARTINGALE_CONFIG.get('multiplier', 2.0)
            return base_amount * (multiplier ** self.martingale['step'])

    def apply_martingale_after_loss(self, last_trade_amount):
        with self._state_lock:
            max_steps = config.MARTINGALE_CONFIG.get('max_steps', 2)
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
            'start_balance': self.balance
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
                            'start_balance': saved.get('start_balance', self.balance)
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
        with self._state_lock:
            wins = losses = profit_loss = 0
            for trade in self.trades:
                if trade.get('result') == 'win':
                    wins += 1
                    profit_loss += trade.get('profit', 0)
                elif trade.get('result') == 'loss':
                    losses += 1
                    profit_loss -= trade.get('amount', 0)
            self.stats['wins'] = wins
            self.stats['losses'] = losses
            self.stats['win_rate'] = (wins / self.stats['total']) * 100 if self.stats['total'] > 0 else 0
            self.stats['profit_loss'] = profit_loss
            self.stats['total_return'] = (profit_loss / self.stats['total_invested']) * 100 if self.stats['total_invested'] > 0 else 0

    def check_pending_trades(self):
        now = datetime.now()
        updated = False
        for trade in list(self.trades):
            if trade.get('result') == 'pending':
                elapsed = (now - trade['timestamp']).total_seconds()
                is_digit = trade.get('is_digit', False)
                timeout = 120 if is_digit else 90
                if elapsed > timeout:
                    with self._state_lock:
                        trade['result'] = 'loss'
                        trade['profit'] = 0
                        self.daily_stats['losses'] += 1
                        self.daily_stats['profit_loss'] -= trade.get('amount', 0)
                        self._daily_stats_dirty = True
                        updated = True
                    logger.warning(f"⚠️ Trade pendente expirado: {trade.get('action')} ${trade.get('amount')}")
        if updated:
            self.update_stats()

    def on_trade_result(self, result):
        try:
            contract_id = result.get('contract_id')
            profit = result.get('profit', 0)
            is_win = profit > 0
            target_trade = None
            if contract_id:
                for trade in self.trades:
                    if trade.get('contract_id') == contract_id:
                        target_trade = trade
                        break

            # 🔥 CORREÇÃO: Fallback para o último trade pendente se o contract_id não bater
            if not target_trade:
                with self._state_lock:
                    pending = [t for t in self.trades if t.get('result') == 'pending']
                    if pending:
                        target_trade = pending[-1]
                        logger.info(f"⚡ Fallback: usando último trade pendente (contract_id original: {contract_id})")
                    else:
                        logger.warning(f"⚠️ Nenhum trade pendente para contract_id {contract_id}. Ignorando.")
                        return

            if target_trade.get('result') != 'pending':
                logger.warning(f"Trade {contract_id} já tem resultado '{target_trade.get('result')}'. Ignorando.")
                return

            with self._state_lock:
                if is_win:
                    target_trade['result'] = 'win'
                    target_trade['profit'] = profit
                    self.daily_stats['wins'] += 1
                    self.daily_stats['profit_loss'] += profit
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
                    self.consecutive_losses += 1
                    self.consecutive_wins = 0
                    logger.info(f"❌ PERDA! -${loss:.2f} | Perdas consecutivas: {self.consecutive_losses}")
                self._daily_stats_dirty = True
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
        trades_snapshot = list(self.trades)
        trades_hoje = [t for t in trades_snapshot if t['timestamp'].date() == hoje]
        with self._state_lock:
            return {
                'resumo': {
                    'total_trades': self.stats['total'],
                    'trades_hoje': len(trades_hoje),
                    'wins': self.stats['wins'],
                    'losses': self.stats['losses'],
                    'win_rate': round(self.stats['win_rate'], 2),
                    'profit_loss': round(self.stats['profit_loss'], 2),
                    'total_invested': round(self.stats['total_invested'], 2),
                    'total_return': round(self.stats['total_return'], 2)
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
                'total_invested': 0, 'total_return': 0
            }
            self.trades.clear()
            self.consecutive_losses = 0
            self.consecutive_wins = 0
        logger.info("📊 Estatísticas e histórico resetados")
