import logging
import time
import os
from forex_indicators import ForexIndicators
from forex_ensemble import ForexEnsemble
from forex_risk import ForexRiskEngine
from forex_scorer import ForexScorer

logger = logging.getLogger(__name__)

MIN_UI_CONFIDENCE = int(os.environ.get('MIN_UI_CONFIDENCE', '80'))


def _calc_pct_b(bollinger, price):
    """%B = (price - lower) / (upper - lower). Fora de [0,1] = preço fora das bandas."""
    if not bollinger or price is None:
        return None
    upper, middle, lower = bollinger
    if upper is None or lower is None:
        return None
    width = upper - lower
    if width <= 0:
        return None
    return (price - lower) / width


class ForexSignals:
    """
    Gera sinais de trading (compra/venda) para pares de Forex.

    Deduplicação de log do ensemble (10.1, 11.1):
      _active_since[(symbol, direction)] → timestamp de início do sinal
      _last_logged[(symbol, direction, active_since)] → último active_duration_seconds gravado

    Throttling das fontes secundárias (10.2, 11.2):
      _should_log_secondary(symbol, source, cond_key) bloqueia logs repetidos
      da mesma condição estrutural durante 900s.

    NOTA (12.1): _clear_active_since NÃO limpa _last_secondary_log.
      Os dois mecanismos são independentes.

    Filtro de UI:
      get_all_signals() só devolve sinais com confidence >= MIN_UI_CONFIDENCE.
      Todos os sinais continuam a ser gravados em forex_signal_log.
    """

    def __init__(self, data_manager):
        self._indicators = ForexIndicators(data_manager)
        self._ensemble = ForexEnsemble(consensus_threshold=0.6)
        self._risk = ForexRiskEngine(min_consensus_pct=60, min_adx=20, max_atr_pct=0.5)
        self._scorer = ForexScorer()
        self._data = data_manager

        self._db_path = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')

        self._active_since = {}
        self._last_logged = {}

        self._last_secondary_log = {}
        self._secondary_log_interval = 900

    def _clear_active_since(self, symbol):
        """
        Limpa o estado do ensemble para um símbolo.
        NOTA (12.1): NÃO limpa _last_secondary_log — esse throttle é independente.
        """
        self._active_since = {k: v for k, v in self._active_since.items() if k[0] != symbol}
        self._last_logged = {k: v for k, v in self._last_logged.items() if k[0] != symbol}

    def _should_log_secondary(self, symbol, source, direction):
        """Throttle por (symbol, source, direction). Máximo 1 log/900s."""
        key = (symbol, source, direction)
        now = time.time()
        last = self._last_secondary_log.get(key, 0)
        if now - last >= self._secondary_log_interval:
            self._last_secondary_log[key] = now
            return True
        return False

    def _get_persisted_active_since(self, symbol, direction):
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path, timeout=5)
            row = conn.execute(
                "SELECT timestamp FROM forex_signal_log "
                "WHERE symbol=? AND direction=? AND strategy_used='ensemble' "
                "ORDER BY id DESC LIMIT 1",
                (symbol, direction)
            ).fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            logger.error(f"Erro ao consultar histórico de persistência: {e}")
            return None

    def get_signal(self, symbol):
        ind = self._indicators.get_all_indicators(symbol, use_candles=True)
        if not ind.get('latest_price'):
            return None

        total, direction, breakdown = self._scorer.score(ind)
        if direction in ('HOLD', 'SEM_DADOS'):
            return None

        reason_parts = []
        if breakdown.get('trend', 0) > 0:
            reason_parts.append(f"Tendência forte ({direction})")
        if breakdown.get('rsi', 0) > 0:
            reason_parts.append("RSI alinhado")
        if breakdown.get('macd', 0) > 0:
            reason_parts.append("MACD confirma")
        reason = f"Score {total}/100: " + ", ".join(reason_parts) if reason_parts else f"Score {total}/100"

        if self._should_log_secondary(symbol, 'scorer_legacy', direction):
            self._log_signal(symbol, direction, total, breakdown, ind, source='scorer_legacy')

        return {
            'direction': direction,
            'confidence': total,
            'reason': reason,
            'indicators': ind,
            'breakdown': breakdown,
            'type': 'scoring'
        }

    def get_signal_multi_timeframe(self, symbol):
        ema_h1 = self._indicators.ema(symbol, period=20, granularity=3600)
        price = self._data.get_latest_price(symbol)
        if ema_h1 is None or price is None:
            self._clear_active_since(symbol)
            return None, "H1 sem dados (EMA ou preço None)"

        if price > ema_h1 * 1.0002:
            h1_bias = 'BUY'
        elif price < ema_h1 * 0.9998:
            h1_bias = 'SELL'
        else:
            self._clear_active_since(symbol)
            return None, f"H1 sem tendência clara (price={price:.5f}, ema_h1={ema_h1:.5f})"

        self._data.request_candles(symbol, granularity=900, count=50)
        ind_15 = self._indicators.get_all_indicators(symbol, granularity=900)
        candles_check = self._data.get_recent_candles(symbol, count=1, granularity=900)

        if not ind_15.get('latest_price'):
            self._clear_active_since(symbol)
            return None, "M15 sem preço"

        direction, consensus, votes = self._ensemble.decide(ind_15)

        if direction != h1_bias:
            self._clear_active_since(symbol)
            return None, f"M15 ({direction}) discorda de H1 ({h1_bias})"

        adjusted_confidence, risk_reasons = self._risk.evaluate(ind_15, consensus)

        key = (symbol, direction)
        now = time.time()

        if key not in self._active_since:
            persisted_since = self._get_persisted_active_since(symbol, direction)
            if persisted_since and (now - persisted_since) < 900:
                self._clear_active_since(symbol)
                self._active_since[key] = persisted_since
            else:
                self._clear_active_since(symbol)
                self._active_since[key] = now

        active_since = self._active_since[key]
        active_duration_seconds = round(now - active_since)
        is_new_signal = active_duration_seconds < 120

        log_key = (symbol, direction, active_since)
        last_logged_secs = self._last_logged.get(log_key, -1)

        should_log = False
        if last_logged_secs < 0:
            should_log = True
        elif (active_duration_seconds - last_logged_secs) >= 900:
            should_log = True

        if should_log:
            self._last_logged[log_key] = active_duration_seconds

            seconds_into_candle = None
            if candles_check:
                seconds_into_candle = round(time.time() - candles_check[-1].get('epoch', 0))

            votes_with_meta = dict(votes)
            votes_with_meta['_candle_maturity_seconds'] = seconds_into_candle
            votes_with_meta['_risk_penalties'] = risk_reasons

            self._log_signal(
                symbol, direction, adjusted_confidence, votes_with_meta, ind_15,
                source='ensemble', active_duration_seconds=active_duration_seconds
            )

        reason_text = f"H1 define {h1_bias}, M15 confirma com {adjusted_confidence}% de consenso"
        if risk_reasons:
            reason_text += f" — ajustado: {', '.join(risk_reasons)}"

        return {
            'direction': direction,
            'confidence': adjusted_confidence,
            'raw_consensus': consensus,
            'reason': reason_text,
            'indicators': ind_15,
            'breakdown': votes,
            'type': 'ensemble',
            'suggested_duration_minutes': 15,
            'timeframe_label': '15 min (H1 + M15)',
            'h1_bias': h1_bias,
            'm30_confidence': None,
            'h1_confidence': None,
            'active_since': active_since,
            'active_duration_seconds': active_duration_seconds,
            'is_new_signal': is_new_signal,
        }, None

    def get_liquidation_signal(self, symbol, granularity=900):
        ind = self._indicators.get_all_indicators(symbol, use_candles=True, granularity=granularity)
        if not ind['latest_price'] or not ind['bollinger']:
            return None

        upper, middle, lower = ind['bollinger']
        price = ind['latest_price']
        rsi = ind.get('rsi_14')

        if upper and lower and rsi:
            band_width = upper - lower
            if band_width > 0:
                dist_lower = (price - lower) / band_width
                dist_upper = (price - upper) / band_width

                if dist_lower < -0.05 and rsi < 20:
                    confidence = min(90, 50 + int(abs(dist_lower) * 100))
                    if self._should_log_secondary(symbol, 'liquidation', 'BUY'):
                        self._log_signal(symbol, 'BUY', confidence, {}, ind, source='liquidation')
                    return {
                        'direction': 'BUY',
                        'confidence': confidence,
                        'reason': f'Liquidation Reversal: preço {abs(dist_lower)*100:.1f}% abaixo da banda inferior, RSI={rsi}',
                        'type': 'liquidation',
                        'suggested_duration_minutes': 15,
                        'timeframe_label': '15 min (liquidação)'
                    }

                if dist_upper > 0.05 and rsi > 80:
                    confidence = min(90, 50 + int(dist_upper * 100))
                    if self._should_log_secondary(symbol, 'liquidation', 'SELL'):
                        self._log_signal(symbol, 'SELL', confidence, {}, ind, source='liquidation')
                    return {
                        'direction': 'SELL',
                        'confidence': confidence,
                        'reason': f'Liquidation Reversal: preço {dist_upper*100:.1f}% acima da banda superior, RSI={rsi}',
                        'type': 'liquidation',
                        'suggested_duration_minutes': 15,
                        'timeframe_label': '15 min (liquidação)'
                    }
        return None

    def _log_signal(self, symbol, direction, confidence, votes, indicators, source='ensemble', active_duration_seconds=None):
        try:
            import sqlite3, json, os

            if source == 'ensemble' and indicators:
                bollinger = indicators.get('bollinger')
                price = indicators.get('latest_price')
                ema50 = indicators.get('ema_50')

                ema_dist_pct = None
                if ema50 and price and ema50 != 0:
                    ema_dist_pct = (price - ema50) / ema50 * 100

                votes = dict(votes)
                votes['_raw_indicators'] = {
                    'rsi_14': indicators.get('rsi_14'),
                    'adx_14': indicators.get('adx_14'),
                    'atr_14': indicators.get('atr_14'),
                    'pct_b': _calc_pct_b(bollinger, price),
                    'ema_dist_pct': ema_dist_pct,
                    'momentum_10': indicators.get('momentum_10'),
                    'macd_line': indicators.get('macd_line'),
                    'signal_line': indicators.get('signal_line'),
                }

            db_path = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute(
                "INSERT INTO forex_signal_log (symbol, direction, signal_type, strategy_used, "
                "confidence, breakdown_json, suggested_duration_minutes, price_at_signal, timestamp, active_duration_seconds) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, direction, source, source, confidence, json.dumps(votes),
                 15, indicators.get('latest_price'), time.time(), active_duration_seconds)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Erro ao registar sinal no log: {e}")

    def _log_blocked_by_mtf(self, symbol, scorer_direction, scorer_total, mtf_reason, ind):
        if not self._should_log_secondary(symbol, 'blocked_by_mtf', scorer_direction):
            return
        try:
            import sqlite3, json, os
            db_path = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute(
                "INSERT INTO forex_signal_log (symbol, direction, signal_type, strategy_used, "
                "confidence, breakdown_json, suggested_duration_minutes, price_at_signal, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (symbol, scorer_direction, 'blocked_by_mtf', 'blocked_by_mtf',
                 scorer_total, json.dumps({'motivo_bloqueio': mtf_reason}), 15,
                 ind.get('latest_price'), time.time())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Erro ao registar sinal bloqueado: {e}")

    def get_all_signals(self):
        from forex_data import FOREX_SYMBOLS

        signals = []
        filtrados = 0

        for symbol in FOREX_SYMBOLS:
            s, motivo_bloqueio = self.get_signal_multi_timeframe(symbol)

            if s:
                if s['confidence'] >= MIN_UI_CONFIDENCE:
                    signals.append({
                        'symbol': symbol,
                        'pair': FOREX_SYMBOLS[symbol],
                        'direction': s['direction'],
                        'confidence': s['confidence'],
                        'reason': s['reason'],
                        'indicators': s['indicators'],
                        'breakdown': s.get('breakdown'),
                        'type': s.get('type', 'ensemble'),
                        'suggested_duration_minutes': s.get('suggested_duration_minutes', 15),
                        'timeframe_label': s.get('timeframe_label', '15 min'),
                        'active_duration_seconds': s.get('active_duration_seconds', 0),
                        'is_new_signal': s.get('is_new_signal', False),
                    })
                else:
                    filtrados += 1

            scorer_result = self.get_signal(symbol)
            if scorer_result and not s:
                self._log_blocked_by_mtf(
                    symbol,
                    scorer_result['direction'],
                    scorer_result['confidence'],
                    motivo_bloqueio if motivo_bloqueio else 'MTF não confirmou (sem motivo detalhado)',
                    scorer_result['indicators']
                )

            liq = self.get_liquidation_signal(symbol)
            if liq:
                signals.append({
                    'symbol': symbol,
                    'pair': FOREX_SYMBOLS[symbol],
                    'direction': liq['direction'],
                    'confidence': liq['confidence'],
                    'reason': liq['reason'],
                    'indicators': None,
                    'type': liq['type'],
                    'suggested_duration_minutes': liq.get('suggested_duration_minutes', 15),
                    'timeframe_label': liq.get('timeframe_label', '15 min (liquidação)'),
                })

        if filtrados:
            logger.info(f"🔇 {filtrados} sinal(is) do ensemble filtrado(s) por confiança < {MIN_UI_CONFIDENCE}")
        return signals
