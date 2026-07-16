import logging
import time
from forex_indicators import ForexIndicators
from forex_ensemble import ForexEnsemble
from forex_risk import ForexRiskEngine

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
        self._data = data_manager

    # -----------------------------------------------------------------
    # Sinal multi-timeframe (top-down: H1 → M15 → M5 opcional)
    # -----------------------------------------------------------------
    def get_signal_multi_timeframe(self, symbol):
        """
        Hierarquia:
        1. H1 define tendência de fundo (EMA20 vs preço).
        2. M15 procura entrada com o ensemble completo, só na direção do H1.
        3. M5 é opcional (não implementado nesta fase).
        """

        # --- H1: contexto de tendência ---
        ema_h1 = self._indicators.ema(symbol, period=20, granularity=3600)
        price = self._data.get_latest_price(symbol)
        if ema_h1 is None or price is None:
            return None

        # Margem de 0.05% para evitar whipsaw
        if price > ema_h1 * 1.0005:
            h1_bias = 'BUY'
        elif price < ema_h1 * 0.9995:
            h1_bias = 'SELL'
        else:
            return None  # sem tendência clara

        # --- M15: ensemble completo ---
        ind_15 = self._indicators.get_all_indicators(symbol, granularity=900)
        if not ind_15.get('latest_price'):
            return None

        direction, consensus, votes = self._ensemble.decide(ind_15)

        # Só executar se M15 concordar com H1
        if direction != h1_bias:
            return None

        # --- Risk Engine ---
        can_exec, reason = self._risk.can_execute(ind_15, consensus)
        if not can_exec:
            logger.info(f"Sinal {symbol} vetado pelo Risk Engine: {reason}")
            return None

        # Registar no log de performance
        self._log_signal(symbol, direction, consensus, votes, ind_15)

        return {
            'direction': direction,
            'confidence': consensus,
            'reason': f"H1 define {h1_bias}, M15 confirma com {consensus}% de consenso",
            'indicators': ind_15,
            'breakdown': votes,  # agora os votos substituem o breakdown antigo
            'type': 'ensemble',
            'suggested_duration_minutes': 15,
            'timeframe_label': '15 min (H1 + M15)',
            'h1_bias': h1_bias,
            'm30_confidence': None,
            'h1_confidence': None
        }

    # -----------------------------------------------------------------
    # Liquidação (inalterado, mas agora com granularidade padrão M15)
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
                    self._log_signal(symbol, 'BUY', confidence, {}, ind)
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
                    self._log_signal(symbol, 'SELL', confidence, {}, ind)
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
    def _log_signal(self, symbol, direction, confidence, votes, indicators):
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
                    'ensemble',
                    'ensemble',
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
    # Todos os sinais
    # -----------------------------------------------------------------
    def get_all_signals(self):
        from forex_data import FOREX_SYMBOLS

        signals = []
        for symbol in FOREX_SYMBOLS:
            # Sinal principal (multi-timeframe com ensemble)
            s = self.get_signal_multi_timeframe(symbol)
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
                    'm30_confidence': s.get('m30_confidence'),
                    'h1_confidence': s.get('h1_confidence')
                })

            # Sinal de liquidação
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
                    'm30_confidence': None,
                    'h1_confidence': None
                })
        return signals
