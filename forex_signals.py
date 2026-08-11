import logging
import time
from forex_indicators import ForexIndicators
from forex_ensemble import ForexEnsemble
from forex_risk import ForexRiskEngine
from forex_scorer import ForexScorer

logger = logging.getLogger(__name__)


class ForexSignals:
    """
    Gera sinais de trading (compra/venda) para pares de Forex.
    Usa o Ensemble para votação e o RiskEngine para validação final.
    Inclui sugestão de duração baseada em hierarquia top-down.
    """

    def __init__(self, data_manager):
        self._indicators = ForexIndicators(data_manager)
        self._ensemble = ForexEnsemble(consensus_threshold=0.6)
        self._risk = ForexRiskEngine(min_consensus_pct=60, min_adx=20, max_atr_pct=0.5)
        self._scorer = ForexScorer()
        self._data = data_manager

    # -----------------------------------------------------------------
    # Sinal de scoring (scorer legacy — apenas para registo paralelo)
    # -----------------------------------------------------------------
    def get_signal(self, symbol):
        ind = self._indicators.get_all_indicators(symbol, use_candles=True)
        logger.info(f"🔍 DEBUG {symbol}: sma_100={ind.get('sma_100')}, ema_50={ind.get('ema_50')}, "
                    f"rsi_14={ind.get('rsi_14')}, adx_14={ind.get('adx_14')}, latest={ind.get('latest_price')}, "
                    f"macd={ind.get('macd_line')}, signal={ind.get('signal_line')}, "
                    f"bollinger={ind.get('bollinger')}, momentum={ind.get('momentum_10')}")
        if not ind.get('latest_price'):
            logger.debug(f"Sinal {symbol}: sem preço, ignorado")
            return None

        total, direction, breakdown = self._scorer.score(ind)
        logger.info(f"🔍 DEBUG {symbol} SCORE: total={total}, direction={direction}, breakdown={breakdown}")

        if direction in ('HOLD', 'SEM_DADOS'):
            logger.debug(f"Sinal {symbol}: {direction} (score={total})")
            return None

        reason_parts = []
        if breakdown.get('trend', 0) > 0:
            reason_parts.append(f"Tendência forte ({direction})")
        if breakdown.get('rsi', 0) > 0:
            reason_parts.append("RSI alinhado")
        if breakdown.get('macd', 0) > 0:
            reason_parts.append("MACD confirma")
        reason = f"Score {total}/100: " + ", ".join(reason_parts) if reason_parts else f"Score {total}/100"

        self._log_signal(symbol, direction, total, breakdown, ind, source='scorer_legacy')

        return {
            'direction': direction,
            'confidence': total,
            'reason': reason,
            'indicators': ind,
            'breakdown': breakdown,
            'type': 'scoring'
        }

    # -----------------------------------------------------------------
    # Sinal multi-timeframe (top-down: H1 → M15) — PRINCIPAL
    # -----------------------------------------------------------------
    def get_signal_multi_timeframe(self, symbol):
        """
        Hierarquia:
        1. H1 define tendência de fundo (EMA20 vs preço).
        2. M15 procura entrada com o ensemble completo, só na direção do H1.
        Retorna (sinal, motivo_bloqueio) onde sinal é o dicionário do sinal ou None.
        """

        # --- H1: contexto de tendência ---
        ema_h1 = self._indicators.ema(symbol, period=20, granularity=3600)
        price = self._data.get_latest_price(symbol)
        if ema_h1 is None or price is None:
            return None, "H1 sem dados (EMA ou preço None)"

        # Margem de whipsaw REVERTIDA para 0.02% (Fase 1 - base limpa)
        if price > ema_h1 * 1.0002:
            h1_bias = 'BUY'
        elif price < ema_h1 * 0.9998:
            h1_bias = 'SELL'
        else:
            motivo = f"H1 sem tendência clara (price={price:.5f}, ema_h1={ema_h1:.5f})"
            logger.info(f"🔍 DEBUG {symbol} MTF: {motivo}")
            return None, motivo

        # --- M15: ensemble completo ---
        # Pedir velas frescas antes de avaliar (reduz obsolescência a longo prazo)
        self._data.request_candles(symbol, granularity=900, count=50)

        ind_15 = self._indicators.get_all_indicators(symbol, granularity=900)

        # --- DIAGNÓSTICO DE REPAINT (mantido para monitorização) ---
        candles_check = self._data.get_recent_candles(symbol, count=1, granularity=900)
        if candles_check:
            last_epoch = candles_check[-1].get('epoch', 0)
            now = time.time()
            seconds_into_candle = now - last_epoch
            logger.info(f"🔍 REPAINT-CHECK {symbol}: última vela epoch={last_epoch}, "
                        f"agora={now:.0f}, segundos dentro do período={seconds_into_candle:.0f}s "
                        f"(vela M15 fecha aos 900s)")

        if not ind_15.get('latest_price'):
            return None, "M15 sem preço"

        direction, consensus, votes = self._ensemble.decide(ind_15)
        logger.info(f"🔍 DEBUG {symbol} MTF: h1_bias={h1_bias}, m15_direction={direction}, consensus={consensus}%")

        # Regra original da Fase 1: só executar se M15 concordar com H1 (sem reversão)
        if direction != h1_bias:
            motivo = f"M15 ({direction}) discorda de H1 ({h1_bias})"
            logger.info(f"🔍 DEBUG {symbol} MTF: {motivo} — sem sinal")
            return None, motivo

        # --- Risk Engine ---
        can_exec, reason = self._risk.can_execute(ind_15, consensus)
        logger.info(f"🔍 DEBUG {symbol} RISK: can_exec={can_exec}, reason={reason}")
        if not can_exec:
            motivo = f"Risk Engine: {reason}"
            logger.info(f"Sinal {symbol} vetado pelo Risk Engine: {reason}")
            return None, motivo

        # Sinal aprovado
        seconds_into_candle = None
        if candles_check:
            seconds_into_candle = round(time.time() - candles_check[-1].get('epoch', 0))
        logger.info(f"🔍 REPAINT-AT-SIGNAL {symbol}: sinal aprovado com vela a {seconds_into_candle}s de maturidade (de 900s)")

        votes_with_meta = dict(votes)
        votes_with_meta['_candle_maturity_seconds'] = seconds_into_candle

        self._log_signal(symbol, direction, consensus, votes_with_meta, ind_15, source='ensemble')

        sinal = {
            'direction': direction,
            'confidence': consensus,
            'reason': f"H1 define {h1_bias}, M15 confirma com {consensus}% de consenso",
            'indicators': ind_15,
            'breakdown': votes_with_meta,
            'type': 'ensemble',
            'suggested_duration_minutes': 15,
            'timeframe_label': '15 min (H1 + M15)',
            'h1_bias': h1_bias,
            'm30_confidence': None,
            'h1_confidence': None
        }
        return sinal, None

    # -----------------------------------------------------------------
    # Liquidação
    # -----------------------------------------------------------------
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

    # -----------------------------------------------------------------
    # Registo de sinal para tracking de performance
    # -----------------------------------------------------------------
    def _log_signal(self, symbol, direction, confidence, votes, indicators, source='ensemble'):
        try:
            import sqlite3, json, os
            db_path = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute(
                "INSERT INTO forex_signal_log (symbol, direction, signal_type, strategy_used, "
                "confidence, breakdown_json, suggested_duration_minutes, price_at_signal, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    symbol,
                    direction,
                    source,
                    source,
                    confidence,
                    json.dumps(votes),
                    15,
                    indicators.get('latest_price'),
                    time.time()
                )
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Erro ao registar sinal no log: {e}")

    # -----------------------------------------------------------------
    # Registo de sinal bloqueado pelo MTF (comparação futura)
    # -----------------------------------------------------------------
    def _log_blocked_by_mtf(self, symbol, scorer_direction, scorer_total, mtf_reason, ind):
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

    # -----------------------------------------------------------------
    # Todos os sinais (usa multi-timeframe como principal, scorer como paralelo)
    # -----------------------------------------------------------------
    def get_all_signals(self):
        from forex_data import FOREX_SYMBOLS

        signals = []
        for symbol in FOREX_SYMBOLS:
            s, motivo_bloqueio = self.get_signal_multi_timeframe(symbol)
            if s:
                pair_name = FOREX_SYMBOLS[symbol]
                signals.append({
                    'symbol': symbol,
                    'pair': pair_name,
                    'direction': s['direction'],
                    'confidence': s['confidence'],
                    'reason': s['reason'],
                    'indicators': s['indicators'],
                    'breakdown': s.get('breakdown'),
                    'type': s.get('type', 'ensemble'),
                    'suggested_duration_minutes': s.get('suggested_duration_minutes', 15),
                    'timeframe_label': s.get('timeframe_label', '15 min'),
                })

            scorer_result = self.get_signal(symbol)
            if scorer_result and not s:
                ind_for_log = scorer_result['indicators']
                self._log_blocked_by_mtf(
                    symbol,
                    scorer_result['direction'],
                    scorer_result['confidence'],
                    motivo_bloqueio if motivo_bloqueio else 'MTF não confirmou (sem motivo detalhado)',
                    ind_for_log
                )

            liq = self.get_liquidation_signal(symbol)
            if liq:
                pair_name = FOREX_SYMBOLS[symbol]
                signals.append({
                    'symbol': symbol,
                    'pair': pair_name,
                    'direction': liq['direction'],
                    'confidence': liq['confidence'],
                    'reason': liq['reason'],
                    'indicators': None,
                    'type': liq['type'],
                    'suggested_duration_minutes': liq.get('suggested_duration_minutes', 15),
                    'timeframe_label': liq.get('timeframe_label', '15 min (liquidação)'),
                })
        return signals
