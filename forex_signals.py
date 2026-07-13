import logging
from forex_indicators import ForexIndicators
from forex_scorer import ForexScorer

logger = logging.getLogger(__name__)


class ForexSignals:
    """
    Gera sinais de trading (compra/venda) para pares de Forex
    com base num sistema de scoring inteligente.
    Só emite sinal se a pontuação total for >= 75/100.
    """

    def __init__(self, data_manager):
        self._indicators = ForexIndicators(data_manager)
        self._scorer = ForexScorer()

    def get_signal(self, symbol):
        """
        Analisa os indicadores para um símbolo e retorna um sinal,
        se existir. O sinal inclui direção, confiança (score) e breakdown.
        """
        ind = self._indicators.get_all_indicators(symbol, use_candles=True)
        if not ind.get('latest_price'):
            return None

        total, direction, breakdown = self._scorer.score(ind)

        if direction == 'HOLD':
            return None

        reason_parts = []
        if breakdown.get('trend', 0) > 0:
            reason_parts.append(f"Tendência forte ({direction})")
        if breakdown.get('rsi', 0) > 0:
            reason_parts.append("RSI alinhado")
        if breakdown.get('macd', 0) > 0:
            reason_parts.append("MACD confirma")
        if breakdown.get('adx', 0) > 0:
            reason_parts.append("Tendência robusta (ADX)")
        reason = f"Score {total}/100: " + ", ".join(reason_parts) if reason_parts else f"Score {total}/100"

        return {
            'direction': direction,
            'confidence': total,
            'reason': reason,
            'indicators': {
                'rsi': ind.get('rsi_14'),
                'sma200': ind.get('sma_200'),
                'macd': ind.get('macd_line'),
                'signal': ind.get('signal_line'),
                'adx': ind.get('adx_14'),
                'atr': ind.get('atr_14'),
                'bollinger': ind.get('bollinger'),
                'momentum': ind.get('momentum_10')
            },
            'breakdown': breakdown
        }

    def get_all_signals(self):
        """
        Retorna uma lista de sinais para todos os pares Forex disponíveis.
        """
        from forex_data import FOREX_SYMBOLS

        signals = []
        for symbol in FOREX_SYMBOLS:
            signal = self.get_signal(symbol)
            if signal:
                pair_name = FOREX_SYMBOLS[symbol]
                signals.append({
                    'symbol': symbol,
                    'pair': pair_name,
                    'direction': signal['direction'],
                    'confidence': signal['confidence'],
                    'reason': signal['reason'],
                    'indicators': signal['indicators'],
                    'breakdown': signal['breakdown']
                })
        return signals
