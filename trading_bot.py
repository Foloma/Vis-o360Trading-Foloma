import logging
import time
import threading
from collections import deque
from datetime import datetime, date
from indicators import TechnicalIndicators
from config import config

logger = logging.getLogger(__name__)

_TECH_WEIGHT = config.SYNTHETIC_TECHNICAL_WEIGHT   # 0.3
_DIGIT_WEIGHT = config.SYNTHETIC_DIGIT_WEIGHT      # 0.7


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
        self._daily_stats_dirty = False

        # Cache do sinal
        self._cached_signal = 'NEUTRAL'
        self._cached_confidence = 0
        self._cached_tech_confidence = 0
        self._cached_digit_confidence = 0
        self._cached_digit_action = None
        
        # Controlo de atualização do sinal por dígito lento
        self._last_digit_counter = -1

        # Callbacks de sinal (definidos pelo app.py)
        self.on_signal_callback = None
        self.on_signal_result_callback = None
        self._last_signal_id = None

        # Diagnóstico
        self._last_signal_for_trade = None
        self._last_entry_price = None

    def start(self, client):
        self.client = client
        self.daily_stats['start_balance'] = self.balance
        self._daily_stats_dirty = True
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
        self.check_take_profit()

        if self._should_update_signal():
            self._update_signal()

    def _should_update_signal(self):
        if not self.digit_analyzer:
            return True
        
        current_counter = self.digit_analyzer.get_digit_counter()
        if current_counter != self._last_digit_counter:
            self._last_digit_counter = current_counter
            return True
        
        return False

    def _update_signal(self):
        (self._cached_signal, 
         self._cached_confidence,
         self._cached_tech_confidence,
         self._cached_digit_confidence,
         self._cached_digit_action) = self._calculate_signal_impl()

    def calculate_signal(self):
        return self._cached_signal, self._cached_confidence

    def get_cached_signal_details(self):
        return (self._cached_signal, self._cached_confidence,
                self._cached_tech_confidence, self._cached_digit_confidence,
                self._cached_digit_action)

    def _calculate_signal_impl(self):
        prices = self.indicators.get_prices(self.current_symbol)
        if len(prices) < 20:
            logger.debug(f"⏳ [{self.current_symbol}] Apenas {len(prices)} preços – a aguardar 20")
            return 'NEUTRAL', 0, 0, 0, None
        if not self.last_analysis:
            return 'NEUTRAL', 0, 0, 0, None

        regime = self.last_analysis.get('adx', {}).get('regime', 'UNKNOWN')
        adx_value = self.last_analysis.get('adx', {}).get('score', 0)

        tech_signal, tech_conf = self._calculate_pure_technical(prices)
        logger.info(
            f"🔍 [{self.current_symbol}] TÉCNICO PURO: {tech_signal} ({tech_conf:.1f}%) "
            f"| Preços: {len(prices)} | Regime: {regime} (ADX {adx_value:.1f})"
        )

        if tech_signal != 'NEUTRAL':
            if regime == 'RANGING':
                logger.info(f"⛔ Sinal {tech_signal} bloqueado – mercado lateral (RANGING)")
                return 'NEUTRAL', 0, tech_conf, 0, None
            elif regime == 'VOLATILE':
                tech_conf *= 0.9
                logger.info(f"⚠️ Sinal {tech_signal} penalizado – mercado volátil, confiança reduzida para {tech_conf:.1f}%")

        if tech_signal != 'NEUTRAL' and not self._check_consensus(self.last_analysis, tech_signal):
            logger.info(f"⛔ Sinal {tech_signal} rejeitado por falta de consenso (>=3 indicadores)")
            return 'NEUTRAL', 0, tech_conf, 0, None

        if not (self.current_symbol.startswith('R_') and self.digit_analyzer):
            if tech_signal != 'NEUTRAL' and tech_conf >= config.RISK_LIMITS.get('min_confidence', 55):
                self._notify_signal(tech_signal, tech_conf, tech_conf, 0, None)
            self._last_signal_for_trade = tech_signal if tech_conf >= config.RISK_LIMITS.get('min_confidence', 55) else None
            return tech_signal, tech_conf, tech_conf, 0, None

        dig_action, dig_conf = self._get_digit_signal()
        if not dig_action or dig_conf < config.RISK_LIMITS.get('min_confidence_digits', 55):
            logger.info(f"🎲 [{self.current_symbol}] Dígito sem sinal válido: {dig_action} ({dig_conf:.1f}%)")
            if tech_signal != 'NEUTRAL' and tech_conf >= config.RISK_LIMITS.get('min_confidence', 55):
                self._notify_signal(tech_signal, tech_conf, tech_conf, dig_conf, dig_action)
            self._last_signal_for_trade = tech_signal if tech_conf >= config.RISK_LIMITS.get('min_confidence', 55) else None
            return tech_signal, tech_conf, tech_conf, dig_conf, dig_action

        logger.info(f"🎲 [{self.current_symbol}] DÍGITO: {dig_action} ({dig_conf:.1f}%)")

        if dig_action == tech_signal:
            total_weight = _TECH_WEIGHT + _DIGIT_WEIGHT
            confidence = (_TECH_WEIGHT * tech_conf + _DIGIT_WEIGHT * dig_conf) / total_weight
            signal = tech_signal
            logger.info(f"🤝 [{self.current_symbol}] CONVERGÊNCIA: {signal} ({confidence:.1f}%)")
        else:
            if _DIGIT_WEIGHT > _TECH_WEIGHT:
                signal = dig_action
                confidence = dig_conf * 0.85
                logger.info(f"⚠️ [{self.current_symbol}] DIVERGÊNCIA (Dígito vence): {signal} ({confidence:.1f}%)")
            else:
                signal = tech_signal
                confidence = tech_conf * 0.85
                logger.info(f"⚠️ [{self.current_symbol}] DIVERGÊNCIA (Técnico vence): {signal} ({confidence:.1f}%)")

        if confidence >= config.RISK_LIMITS.get('min_confidence', 55):
            logger.info(f"🚦 [{self.current_symbol}] SINAL FINAL: {signal} ({confidence:.1f}%) – VÁLIDO")
            self._notify_signal(signal, confidence, tech_conf, dig_conf, dig_action)
            self._last_signal_for_trade = signal
        else:
            logger.info(f"🚦 [{self.current_symbol}] SINAL FINAL: {signal} ({confidence:.1f}%) – ABAIXO DO LIMIAR")
            self._last_signal_for_trade = None

        return signal, min(confidence, 100), tech_conf, dig_conf, dig_action

    def _notify_signal(self, signal, confidence, tech_conf, dig_conf, dig_action):
        if self.on_signal_callback:
            try:
                self.on_signal_callback({
                    'signal': signal,
                    'confidence': confidence,
                    'tech_confidence': tech_conf,
                    'digit_confidence': dig_conf,
                    'digit_action': dig_action,
                    'symbol': self.current_symbol
                })
            except Exception as e:
                logger.error(f"Erro no callback de sinal: {e}")

    def reset_daily_stats(self):
        self.daily_stats = {
            'date': datetime.now().date(), 'trades': 0,
            'wins': 0, 'losses': 0, 'profit_loss': 0,
            'start_balance': self.balance
        }
        self.stop_loss_active = False
        self._daily_stats_dirty = True

    def reset_price_history(self):
        self.indicators.reset_all()
        logger.info("🧹 Histórico de preços e todos os caches resetados (gap de reconexão)")

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

    def get_momentum(self):
        prices = self.indicators.get_prices(self.current_symbol)
        if len(prices) < 25:
            return 0
        current = prices[-1]
        past = prices[-21]
        if past == 0:
            return 0
        return (current - past) / past * 100

    def _get_digit_signal(self):
        if not self.digit_analyzer:
            return None, 0
        analysis = self.digit_analyzer.get_analysis()
        action = analysis.get('recommended_action')
        conf = analysis.get('confidence', 0)
        return action, conf

    def _get_rsi_thresholds(self):
        prices = self.indicators.get_prices(self.current_symbol)
        if len(prices) < 15:
            return 30, 70
        recent = prices[-15:]
        ranges = [abs(recent[i] - recent[i-1]) for i in range(1, len(recent))]
        atr = sum(ranges) / len(ranges)
        avg_price = sum(recent) / len(recent)
        volatility = (atr / avg_price) * 100 if avg_price > 0 else 0
        if volatility > 0.15:
            return 20, 80
        elif volatility < 0.05:
            return 35, 65
        else:
            return 30, 70

    def _check_consensus(self, analysis, signal):
        aligned = 0
        rsi = analysis['rsi']['score']
        if signal == 'BUY' and rsi < 55: aligned += 1
        elif signal == 'SELL' and rsi > 45: aligned += 1
        macd_desc = analysis['macd']['desc']
        if 'COMPRA' in macd_desc and signal == 'BUY': aligned += 1
        elif 'VENDA' in macd_desc and signal == 'SELL': aligned += 1
        trend_desc = analysis['trend']['desc']
        if 'ALTA' in trend_desc and signal == 'BUY': aligned += 1
        elif 'BAIXA' in trend_desc and signal == 'SELL': aligned += 1
        bb_desc = analysis['bollinger']['desc']
        if signal == 'BUY' and ('sobrevendido' in bb_desc or 'abaixo' in bb_desc):
            aligned += 1
        elif signal == 'SELL' and ('sobrecomprado' in bb_desc or 'acima' in bb_desc):
            aligned += 1
        return aligned >= 3

    def _calculate_pure_technical(self, prices):
        if len(prices) < 20 or not self.last_analysis:
            return 'NEUTRAL', 0

        analysis = self.last_analysis
        raw_scores = []

        rsi = analysis['rsi']['score']
        oversold, overbought = self._get_rsi_thresholds()
        if rsi is not None and analysis['rsi']['desc'] != '---':
            if rsi < oversold:
                rsi_conf = min(100, (oversold - rsi) / oversold * 100 + 60)
                raw_scores.append(('BUY', 0.25, rsi_conf))
            elif rsi > overbought:
                rsi_conf = min(100, (rsi - overbought) / (100 - overbought) * 100 + 60)
                raw_scores.append(('SELL', 0.25, rsi_conf))
            elif rsi < oversold + 15:
                raw_scores.append(('BUY', 0.25, 40))
            elif rsi > overbought - 15:
                raw_scores.append(('SELL', 0.25, 40))
            else:
                raw_scores.append(('NEUTRAL', 0.25, 0))
        else:
            raw_scores.append(('NEUTRAL', 0.25, 0))

        macd_desc = analysis['macd']['desc']
        macd_score = analysis['macd']['score']
        if macd_desc != '---':
            if macd_score == 80:
                if 'COMPRA' in macd_desc:
                    raw_scores.append(('BUY', 0.30, 80))
                elif 'VENDA' in macd_desc:
                    raw_scores.append(('SELL', 0.30, 80))
                else:
                    raw_scores.append(('NEUTRAL', 0.30, 0))
            elif macd_score == 65:
                if 'COMPRA' in macd_desc:
                    raw_scores.append(('BUY', 0.30, 55))
                elif 'VENDA' in macd_desc:
                    raw_scores.append(('SELL', 0.30, 55))
                else:
                    raw_scores.append(('NEUTRAL', 0.30, 0))
            else:
                raw_scores.append(('NEUTRAL', 0.30, 0))
        else:
            raw_scores.append(('NEUTRAL', 0.30, 0))

        trend_desc = analysis['trend']['desc']
        if trend_desc != '---':
            if 'ALTA' in trend_desc:
                raw_scores.append(('BUY', 0.25, 70))
            elif 'BAIXA' in trend_desc:
                raw_scores.append(('SELL', 0.25, 70))
            else:
                raw_scores.append(('NEUTRAL', 0.25, 0))
        else:
            raw_scores.append(('NEUTRAL', 0.25, 0))

        bb_desc = analysis['bollinger']['desc']
        trend_is_bull = 'ALTA' in trend_desc if trend_desc else False
        trend_is_bear = 'BAIXA' in trend_desc if trend_desc else False
        if bb_desc != '---':
            if 'sobrevendido' in bb_desc:
                if trend_is_bear:
                    raw_scores.append(('BUY', 0.20, 30))
                else:
                    raw_scores.append(('BUY', 0.20, 80))
            elif 'sobrecomprado' in bb_desc:
                if trend_is_bull:
                    raw_scores.append(('SELL', 0.20, 30))
                else:
                    raw_scores.append(('SELL', 0.20, 80))
            elif 'acima' in bb_desc:
                raw_scores.append(('BUY', 0.20, 45))
            elif 'abaixo' in bb_desc:
                raw_scores.append(('SELL', 0.20, 45))
            else:
                raw_scores.append(('NEUTRAL', 0.20, 0))
        else:
            raw_scores.append(('NEUTRAL', 0.20, 0))

        active_scores = [s for s in raw_scores if s[0] != 'NEUTRAL' and s[2] > 0]
        neutral_weight = sum(s[1] for s in raw_scores if s[0] == 'NEUTRAL' or s[2] == 0)
        if not active_scores:
            return 'NEUTRAL', 0
        total_active_weight = sum(s[1] for s in active_scores)
        scores = []
        for direction, weight, conf in active_scores:
            extra = (weight / total_active_weight) * neutral_weight if total_active_weight > 0 else 0
            scores.append((direction, weight + extra, conf))

        buy_score = sum(peso * conf for dir, peso, conf in scores if dir == 'BUY')
        sell_score = sum(peso * conf for dir, peso, conf in scores if dir == 'SELL')

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
                self._daily_stats_dirty = True
            self.update_stats()
            if self.client:
                self.client.get_balance()

            # 🔬 DIAGNÓSTICO: comparar sinal emitido com resultado
            if self._last_signal_for_trade:
                expected = self._last_signal_for_trade
                actual = 'win' if is_win else 'loss'
                entry_price = self._last_entry_price or 'N/A'
                sell_price = result.get('sell_price', 'N/A')
                logger.warning(
                    f"🔬 DIAGNÓSTICO: Sinal={expected}, Resultado={actual}, "
                    f"Profit={profit:.2f}, Entrada={entry_price}, Saída={sell_price}, "
                    f"Contrato={contract_id}"
                )

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
            'tech_confidence': round(self._cached_tech_confidence, 1),
            'digit_confidence': round(self._cached_digit_confidence, 1) if self._cached_digit_confidence else 0,
            'digit_action': self._cached_digit_action,
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
