import logging
import time
import threading
from collections import deque
from datetime import datetime
from indicators import TechnicalIndicators
from config import config

logger = logging.getLogger(__name__)

_TECH_WEIGHT = getattr(config, 'SYNTHETIC_TECHNICAL_WEIGHT', 0.6)
_DIGIT_WEIGHT = getattr(config, 'SYNTHETIC_DIGIT_WEIGHT', 0.4)


class TradingBot:
    def __init__(self):
        self.client = None
        self.indicators = TechnicalIndicators()
        self.current_price = 0
        self.current_symbol = 'R_100'
        self.balance = 0
        self.currency = 'USD'
        self.paused = False
        self.stop_loss_active = False
        self.last_analysis = {}
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

    def start(self, client):
        self.client = client
        self.daily_stats['start_balance'] = self.balance
        logger.info("🚀 Bot iniciado")

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
                        f"🛑 Stop-loss activado: {daily_loss_pct:.1f}% perda diária"
                    )
                self.stop_loss_active = True
                return False
        self.stop_loss_active = False
        return True

    def on_tick(self, tick):
        self.current_price = tick['price']
        self.current_symbol = tick['symbol']
        self.indicators.add_price(self.current_price, self.current_symbol)

        if 'R_' in self.current_symbol and self.digit_analyzer:
            self.digit_analyzer.add_tick(self.current_price)

        self.last_analysis = self.indicators.get_all_indicators(self.current_symbol)
        if self.client:
            self.balance = self.client.balance
            self.currency = self.client.currency
            today = datetime.now().date()
            if self.daily_stats['date'] != today:
                self.reset_daily_stats()

        self.check_risk_limits()

    def reset_daily_stats(self):
        self.daily_stats = {
            'date': datetime.now().date(), 'trades': 0,
            'wins': 0, 'losses': 0, 'profit_loss': 0,
            'start_balance': self.balance
        }
        self.stop_loss_active = False

    def get_momentum(self):
        prices = self.indicators.get_prices(self.current_symbol)
        if len(prices) < 5:
            return 0
        recent = list(prices)[-5:]
        return (recent[-1] - recent[0]) / recent[0] * 100

    def _get_digit_signal(self):
        if not self.digit_analyzer:
            return None, 0
        analysis = self.digit_analyzer.get_analysis()
        action = analysis.get('recommended_action')
        conf = analysis.get('confidence', 0)
        return action, conf

    # ✅ NOVO MÉTODO DE CONFIANÇA REAL
    def _calculate_pure_technical(self, prices):
        if len(prices) < 20 or not self.last_analysis:
            return 'NEUTRAL', 0

        analysis = self.last_analysis
        scores = []

        # RSI (25%)
        rsi = analysis['rsi']['score']
        if rsi is not None and analysis['rsi']['desc'] != '---':
            if rsi < 30:
                rsi_conf = min(100, (30 - rsi) / 30 * 100 + 60)
                scores.append(('BUY', 0.25, rsi_conf))
            elif rsi > 70:
                rsi_conf = min(100, (rsi - 70) / 30 * 100 + 60)
                scores.append(('SELL', 0.25, rsi_conf))
            elif rsi < 45:
                scores.append(('BUY', 0.25, 40))
            elif rsi > 55:
                scores.append(('SELL', 0.25, 40))
            else:
                scores.append(('NEUTRAL', 0.25, 0))

        # MACD (30%)
        macd_desc = analysis['macd']['desc']
        macd_score = analysis['macd']['score']
        if macd_desc != '---':
            if macd_score == 80:
                if 'COMPRA' in macd_desc:
                    scores.append(('BUY', 0.30, 80))
                elif 'VENDA' in macd_desc:
                    scores.append(('SELL', 0.30, 80))
            elif macd_score == 65:
                if 'COMPRA' in macd_desc:
                    scores.append(('BUY', 0.30, 55))
                elif 'VENDA' in macd_desc:
                    scores.append(('SELL', 0.30, 55))
            else:
                scores.append(('NEUTRAL', 0.30, 0))

        # TREND (25%)
        trend_desc = analysis['trend']['desc']
        if trend_desc != '---':
            if 'ALTA' in trend_desc:
                scores.append(('BUY', 0.25, 70))
            elif 'BAIXA' in trend_desc:
                scores.append(('SELL', 0.25, 70))
            else:
                scores.append(('NEUTRAL', 0.25, 0))

        # Bollinger (20%)
        bb_desc = analysis['bollinger']['desc']
        if bb_desc != '---':
            if 'sobrevendido' in bb_desc:
                scores.append(('BUY', 0.20, 80))
            elif 'sobrecomprado' in bb_desc:
                scores.append(('SELL', 0.20, 80))
            elif 'acima' in bb_desc:
                scores.append(('BUY', 0.20, 45))
            elif 'abaixo' in bb_desc:
                scores.append(('SELL', 0.20, 45))
            else:
                scores.append(('NEUTRAL', 0.20, 0))

        if not scores:
            return 'NEUTRAL', 0

        buy_score = sum(peso * conf for dir, peso, conf in scores if dir == 'BUY')
        sell_score = sum(peso * conf for dir, peso, conf in scores if dir == 'SELL')
        total_weight = sum(peso for dir, peso, conf in scores if dir != 'NEUTRAL')

        if total_weight == 0:
            return 'NEUTRAL', 0

        if buy_score > sell_score:
            signal = 'BUY'
            confidence = buy_score / (buy_score + sell_score) * 100 if (buy_score + sell_score) > 0 else 0
        elif sell_score > buy_score:
            signal = 'SELL'
            confidence = sell_score / (buy_score + sell_score) * 100 if (buy_score + sell_score) > 0 else 0
        else:
            return 'NEUTRAL', 0

        momentum = self.get_momentum()
        if signal == 'BUY' and momentum > config.ADVANCED_STRATEGY.get('momentum_threshold', 0.1):
            confidence = min(confidence + 5, 100)
        elif signal == 'SELL' and momentum < -config.ADVANCED_STRATEGY.get('momentum_threshold', 0.1):
            confidence = min(confidence + 5, 100)

        opposing = sell_score if signal == 'BUY' else buy_score
        if opposing > 15:
            contradiction_penalty = min(15, opposing / 3)
            confidence = max(0, confidence - contradiction_penalty)

        return signal, min(round(confidence, 1), 100)

    def calculate_signal(self):
        prices = self.indicators.get_prices(self.current_symbol)
        if len(prices) < 20:
            logger.debug(f"⏳ [{self.current_symbol}] Apenas {len(prices)} preços – a aguardar 20")
            return 'NEUTRAL', 0

        if not self.last_analysis:
            return 'NEUTRAL', 0

        signal, confidence = self._calculate_pure_technical(prices)
        dig_action = dig_conf = None

        logger.info(
            f"🔍 [{self.current_symbol}] TÉCNICO PURO: {signal} ({confidence:.1f}%) "
            f"| Preços: {len(prices)}"
        )

        if self.current_symbol.startswith('R_') and self.digit_analyzer:
            dig_action, dig_conf = self._get_digit_signal()
            if dig_action and dig_conf >= 55:
                logger.info(
                    f"🎲 [{self.current_symbol}] DÍGITO: {dig_action} ({dig_conf:.1f}%)"
                )
                total_weight = _TECH_WEIGHT + _DIGIT_WEIGHT
                combined = (_TECH_WEIGHT * confidence + _DIGIT_WEIGHT * dig_conf) / total_weight
                if dig_action != signal and _DIGIT_WEIGHT > _TECH_WEIGHT:
                    logger.info(
                        f"🔄 [{self.current_symbol}] Dígito prevalece: {dig_action} (peso {_DIGIT_WEIGHT} > {_TECH_WEIGHT})"
                    )
                    signal = dig_action
                confidence = combined
                logger.info(
                    f"✅ [{self.current_symbol}] COMBINADO: {signal} ({confidence:.1f}%)"
                )
            else:
                logger.debug(f"🎲 [{self.current_symbol}] DÍGITO sem recomendação ou confiança < 55%")
        else:
            logger.debug(f"📊 [{self.current_symbol}] Sem analisador de dígitos ou não R_")

        if confidence >= 60:
            logger.info(
                f"🚦 [{self.current_symbol}] SINAL FINAL: {signal} ({confidence:.1f}%) – VÁLIDO"
            )
        else:
            logger.info(
                f"🚦 [{self.current_symbol}] SINAL FINAL: {signal} ({confidence:.1f}%) – ABAIXO DO LIMIAR"
            )

        return signal, min(confidence, 100)

    def register_trade(self, trade_data):
        trade_data['timestamp'] = datetime.now()
        with self._state_lock:
            self.trades.append(trade_data)
            self.stats['total'] += 1
            self.stats['total_invested'] += trade_data['amount']
            self.daily_stats['trades'] += 1
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
                        updated = True
                    logger.warning(f"⚠️ Trade pendente expirado: {trade.get('action')} ${trade.get('amount')} (digit={is_digit})")
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

            if not target_trade:
                logger.warning(f"⚠️ Nenhum trade pendente com contract_id {contract_id}. Ignorando.")
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

            self.update_stats()
            if self.client:
                self.client.get_balance()

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

    def get_status(self):
        self.check_pending_trades()
        signal, confidence = self.calculate_signal()

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
            'signal': signal,
            'confidence': round(confidence, 1),
            'analysis': self.last_analysis,
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
