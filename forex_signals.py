import logging
from forex_indicators import ForexIndicators
from forex_scorer import ForexScorer

logger = logging.getLogger(__name__)


class ForexSignals:
    """
    Gera sinais de trading (compra/venda) para pares de Forex
    com base no scoring do ForexScorer e também deteta
    Liquidation Reversal Signals (Bollinger + RSI).
    """

    def __init__(self, data_manager):
        self._indicators = ForexIndicators(data_manager)
        self._scorer = ForexScorer()

    def get_signal(self, symbol):
        """
        Analisa os indicadores para um símbolo e retorna um sinal
        de scoring, se a confiança for >= threshold.
        """
        ind = self._indicators.get_all_indicators(symbol, use_candles=True)
        if not ind.get('latest_price'):
            return None

        total, direction, breakdown = self._scorer.score(ind)

        if direction == 'HOLD':
            return None

        # Construir mensagem de razão
        reason_parts = []
        if breakdown.get('trend', 0) > 0:
            reason_parts.append(f"Tendência forte ({direction})")
        if breakdown.get('rsi', 0) > 0:
            reason_parts.append("RSI alinhado")
        if breakdown.get('macd', 0) > 0:
            reason_parts.append("MACD confirma")
        reason = f"Score {total}/100: " + ", ".join(reason_parts) if reason_parts else f"Score {total}/100"

        return {
            'direction': direction,
            'confidence': total,
            'reason': reason,
            'indicators': ind,
            'breakdown': breakdown,
            'type': 'scoring'
        }

    def get_liquidation_signal(self, symbol):
        """
        Detecta possíveis reversões de liquidação:
        - Preço fechou fora das Bandas de Bollinger (2 desvios)
        - RSI está em extremo (<20 ou >80)
        Retorna direção esperada da reversão e confiança.
        """
        ind = self._indicators.get_all_indicators(symbol, use_candles=True)
        if not ind['latest_price'] or not ind['bollinger']:
            return None

        upper, middle, lower = ind['bollinger']
        price = ind['latest_price']
        rsi = ind.get('rsi_14')

        if upper and lower and rsi:
            band_width = upper - lower
            if band_width > 0:
                dist_lower = (price - lower) / band_width  # negativo se abaixo
                dist_upper = (price - upper) / band_width  # positivo se acima

                # Preço abaixo da banda inferior e RSI < 20 → reversão para cima
                if dist_lower < -0.05 and rsi < 20:
                    confidence = min(90, 50 + int(abs(dist_lower) * 100))
                    return {
                        'direction': 'BUY',
                        'confidence': confidence,
                        'reason': f'Liquidation Reversal: preço {abs(dist_lower)*100:.1f}% abaixo da banda inferior, RSI={rsi}',
                        'type': 'liquidation'
                    }

                # Preço acima da banda superior e RSI > 80 → reversão para baixo
                if dist_upper > 0.05 and rsi > 80:
                    confidence = min(90, 50 + int(dist_upper * 100))
                    return {
                        'direction': 'SELL',
                        'confidence': confidence,
                        'reason': f'Liquidation Reversal: preço {dist_upper*100:.1f}% acima da banda superior, RSI={rsi}',
                        'type': 'liquidation'
                    }
        return None

    def get_all_signals(self):
        """
        Retorna uma lista de todos os sinais (scoring + liquidação)
        para todos os pares Forex disponíveis.
        """
        from forex_data import FOREX_SYMBOLS

        signals = []
        for symbol in FOREX_SYMBOLS:
            # Sinal normal (scoring)
            s = self.get_signal(symbol)
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
                    'type': s.get('type', 'scoring')
                })

            # Sinal de liquidação (se existir)
            liq = self.get_liquidation_signal(symbol)
            if liq:
                pair_name = FOREX_SYMBOLS[symbol]
                signals.append({
                    'symbol': symbol,
                    'pair': pair_name,
                    'direction': liq['direction'],
                    'confidence': liq['confidence'],
                    'reason': liq['reason'],
                    'indicators': None,  # não inclui indicadores completos para liquidação
                    'type': liq['type']
                })
        return signals
