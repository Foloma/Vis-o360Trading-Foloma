import logging
from forex_indicators import ForexIndicators

logger = logging.getLogger(__name__)


class ForexSignals:
    """
    Gera sinais de trading (compra/venda) para pares de Forex
    com base nos indicadores técnicos fornecidos pelo ForexIndicators.
    Estratégia inicial: RSI + SMA 200 + MACD de confirmação.
    """

    def __init__(self, data_manager):
        self._indicators = ForexIndicators(data_manager)
        self._data = data_manager

    def get_signal(self, symbol):
        """
        Analisa os indicadores para um símbolo e retorna um sinal,
        se existir. O sinal inclui direção, confiança e razão.
        """
        ind = self._indicators.get_all_indicators(symbol, use_candles=True)

        latest = ind.get('latest_price')
        sma200 = ind.get('sma_200')
        rsi = ind.get('rsi_14')
        macd_line, signal_line, histogram = ind.get('macd') or (None, None, None)

        if latest is None or sma200 is None or rsi is None:
            return None

        # Estratégia de compra
        if rsi < 30 and latest > sma200:
            confidence = 70  # confiança base
            reason = f"RSI sobrevendido ({rsi}) + preço acima da SMA 200"

            if macd_line is not None and signal_line is not None:
                if macd_line > signal_line:
                    confidence += 10
                    reason += " + MACD cruzou para cima"
                else:
                    confidence -= 10  # reduz confiança se MACD não confirma

            return {
                'direction': 'BUY',
                'confidence': min(confidence, 100),
                'reason': reason,
                'indicators': {
                    'rsi': rsi,
                    'sma200': sma200,
                    'macd': macd_line,
                    'signal': signal_line
                }
            }

        # Estratégia de venda
        if rsi > 70 and latest < sma200:
            confidence = 70
            reason = f"RSI sobrecomprado ({rsi}) + preço abaixo da SMA 200"

            if macd_line is not None and signal_line is not None:
                if macd_line < signal_line:
                    confidence += 10
                    reason += " + MACD cruzou para baixo"
                else:
                    confidence -= 10

            return {
                'direction': 'SELL',
                'confidence': min(confidence, 100),
                'reason': reason,
                'indicators': {
                    'rsi': rsi,
                    'sma200': sma200,
                    'macd': macd_line,
                    'signal': signal_line
                }
            }

        return None

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
                    'indicators': signal['indicators']
                })
        return signals
