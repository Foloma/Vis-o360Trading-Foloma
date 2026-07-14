import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

class ForexScorer:
    def __init__(self, weights: Optional[Dict[str, int]] = None):
        self.weights = weights or {
            'trend': 20, 'rsi': 15, 'macd': 15, 'adx': 15,
            'atr_volatility': 10, 'bollinger': 10, 'momentum': 10, 'market_quality': 5
        }
        self.threshold = 10          # emergência: qualquer sinal passa
        self.adx_minimum = 5

    def score(self, ind: dict) -> Tuple[int, str, dict]:
        direction = 'HOLD'
        breakdown = {k: 0 for k in self.weights}
        latest = ind.get('latest_price')
        sma200 = ind.get('sma_200')
        ema50 = ind.get('ema_50')
        rsi = ind.get('rsi_14')
        macd_line = ind.get('macd_line')
        signal_line = ind.get('signal_line')
        adx = ind.get('adx_14')
        atr = ind.get('atr_14')
        upper, middle, lower = ind.get('bollinger') or (None, None, None)
        momentum = ind.get('momentum_10')

        if latest is None:
            return 0, 'HOLD', breakdown

        # Tendência com fallback agressivo
        if sma200 is not None and ema50 is not None:
            if ema50 > sma200:
                breakdown['trend'] = self.weights['trend']
                direction = 'BUY'
            elif ema50 < sma200:
                breakdown['trend'] = self.weights['trend']
                direction = 'SELL'
            else:
                breakdown['trend'] = 5
        elif sma200 is not None:
            if latest > sma200:
                breakdown['trend'] = 10; direction = 'BUY'
            else:
                breakdown['trend'] = 10; direction = 'SELL'
        elif momentum is not None:
            if momentum > 0:
                breakdown['trend'] = 10; direction = 'BUY'
            elif momentum < 0:
                breakdown['trend'] = 10; direction = 'SELL'
        else:
            breakdown['trend'] = 5
            direction = 'BUY'

        # RSI – alargado
        if rsi is not None:
            if direction == 'BUY' and rsi < 60:
                breakdown['rsi'] = self.weights['rsi']
            elif direction == 'SELL' and rsi > 40:
                breakdown['rsi'] = self.weights['rsi']
            else:
                breakdown['rsi'] = 5

        # MACD
        if macd_line is not None and signal_line is not None:
            if direction == 'BUY' and macd_line > signal_line:
                breakdown['macd'] = self.weights['macd']
            elif direction == 'SELL' and macd_line < signal_line:
                breakdown['macd'] = self.weights['macd']
            else:
                breakdown['macd'] = 5

        # ADX – muito permissivo
        if adx is not None:
            if adx > self.adx_minimum:
                breakdown['adx'] = self.weights['adx']
            else:
                breakdown['adx'] = 5

        # ATR
        if atr is not None and latest > 0:
            if (atr / latest) * 100 >= 0.01:
                breakdown['atr_volatility'] = self.weights['atr_volatility']
            else:
                breakdown['atr_volatility'] = 5

        # Bollinger
        if upper and lower:
            breakdown['bollinger'] = 5

        # Momentum
        if momentum is not None:
            if direction == 'BUY' and momentum > 0:
                breakdown['momentum'] = self.weights['momentum']
            elif direction == 'SELL' and momentum < 0:
                breakdown['momentum'] = self.weights['momentum']
            else:
                breakdown['momentum'] = 5

        # Market Quality
        breakdown['market_quality'] = 5

        total = sum(breakdown.values())
        total = min(total, 100)

        if total < self.threshold:
            direction = 'HOLD'

        return total, direction, breakdown
