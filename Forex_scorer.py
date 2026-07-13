# forex_scorer.py
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

class ForexScorer:
    """
    Calcula uma pontuação de 0 a 100 para um sinal de Forex,
    baseada em vários indicadores e filtros.
    """
    def __init__(self, weights: Optional[Dict[str, int]] = None):
        self.weights = weights or {
            'trend': 20,
            'rsi': 15,
            'macd': 15,
            'adx': 15,
            'atr_volatility': 10,
            'bollinger': 10,
            'momentum': 10,
            'market_quality': 5
        }
        self.threshold = 75
        self.adx_minimum = 20

    def score(self, ind: dict) -> Tuple[int, str, dict]:
        direction = 'HOLD'
        breakdown = {k: 0 for k in self.weights}
        total = 0

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

        if latest is None or sma200 is None:
            return 0, 'HOLD', breakdown

        # --- 1. Tendência ---
        if ema50 is not None:
            if ema50 > sma200:
                breakdown['trend'] = self.weights['trend']
                direction = 'BUY'
            elif ema50 < sma200:
                breakdown['trend'] = self.weights['trend']
                direction = 'SELL'
        else:
            if latest > sma200:
                breakdown['trend'] = self.weights['trend'] // 2
                direction = 'BUY'
            elif latest < sma200:
                breakdown['trend'] = self.weights['trend'] // 2
                direction = 'SELL'

        # --- 2. RSI ---
        if rsi is not None:
            if direction == 'BUY' and rsi < 30:
                breakdown['rsi'] = self.weights['rsi']
            elif direction == 'SELL' and rsi > 70:
                breakdown['rsi'] = self.weights['rsi']
            elif 40 <= rsi <= 60:
                breakdown['rsi'] = 0
            else:
                breakdown['rsi'] = self.weights['rsi'] // 2

        # --- 3. MACD ---
        if macd_line is not None and signal_line is not None:
            if direction == 'BUY' and macd_line > signal_line:
                breakdown['macd'] = self.weights['macd']
            elif direction == 'SELL' and macd_line < signal_line:
                breakdown['macd'] = self.weights['macd']
            else:
                breakdown['macd'] = 0

        # --- 4. ADX ---
        if adx is not None:
            if adx > self.adx_minimum:
                adx_factor = min(1.0, (adx - self.adx_minimum) / 20)
                breakdown['adx'] = int(self.weights['adx'] * adx_factor)
            else:
                breakdown['adx'] = 0

        # --- 5. ATR / Volatilidade ---
        if atr is not None and latest > 0:
            atr_pct = (atr / latest) * 100
            if 0.05 <= atr_pct <= 0.5:
                breakdown['atr_volatility'] = self.weights['atr_volatility']
            elif atr_pct > 0.5:
                breakdown['atr_volatility'] = self.weights['atr_volatility'] // 2
            else:
                breakdown['atr_volatility'] = 0

        # --- 6. Bollinger ---
        if upper and lower:
            if direction == 'BUY' and latest <= lower * 1.002:
                breakdown['bollinger'] = self.weights['bollinger']
            elif direction == 'SELL' and latest >= upper * 0.998:
                breakdown['bollinger'] = self.weights['bollinger']

        # --- 7. Momentum ---
        if momentum is not None:
            if direction == 'BUY' and momentum > 0:
                breakdown['momentum'] = self.weights['momentum']
            elif direction == 'SELL' and momentum < 0:
                breakdown['momentum'] = self.weights['momentum']

        # --- 8. Market Quality (simplificado) ---
        mqi = 0
        if adx and adx > self.adx_minimum:
            mqi += 40
        if rsi and ((direction == 'BUY' and rsi < 50) or (direction == 'SELL' and rsi > 50)):
            mqi += 30
        if breakdown['atr_volatility'] > 0:
            mqi += 30
        breakdown['market_quality'] = int(self.weights['market_quality'] * (mqi / 100))

        total = sum(breakdown.values())
        total = min(total, 100)

        if total < self.threshold:
            direction = 'HOLD'

        return total, direction, breakdown
