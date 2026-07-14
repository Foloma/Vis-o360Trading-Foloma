import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class ForexScorer:
    """
    Calcula uma pontuação de 0 a 100 para um sinal de Forex,
    baseada em indicadores reais. Não força direção quando
    os dados são insuficientes.
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
        self.threshold = 60          # valor original, agora atingível com indicadores corrigidos
        self.adx_minimum = 20

    def score(self, ind: dict) -> Tuple[int, str, dict]:
        """
        Retorna (score, direção, breakdown).
        Se não houver dados suficientes para determinar uma direção,
        devolve (0, 'SEM_DADOS', breakdown).
        """
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

        # --- Validação mínima de dados ---
        if latest is None:
            return 0, 'SEM_DADOS', breakdown

        # --- 1. Tendência (SMA200 / EMA50) ---
        # Só atribui direção se houver informação suficiente
        if sma200 is not None and ema50 is not None:
            if ema50 > sma200:
                breakdown['trend'] = self.weights['trend']
                direction = 'BUY'
            elif ema50 < sma200:
                breakdown['trend'] = self.weights['trend']
                direction = 'SELL'
            else:
                breakdown['trend'] = 0  # empate, não define direção
        elif sma200 is not None:
            # Fallback: usar preço vs SMA200 (metade dos pontos)
            if latest > sma200:
                breakdown['trend'] = self.weights['trend'] // 2
                direction = 'BUY'
            elif latest < sma200:
                breakdown['trend'] = self.weights['trend'] // 2
                direction = 'SELL'
            else:
                breakdown['trend'] = 0
        elif momentum is not None:
            # Fallback fraco: momentum sozinho (poucos pontos)
            if momentum > 0:
                breakdown['trend'] = 5
                direction = 'BUY'
            elif momentum < 0:
                breakdown['trend'] = 5
                direction = 'SELL'
            else:
                breakdown['trend'] = 0
        else:
            # Sem dados de tendência, não inventa direção
            return 0, 'SEM_DADOS', breakdown

        # Se após tendência a direção continuar HOLD, não há sinal
        if direction == 'HOLD':
            return 0, 'SEM_DADOS', breakdown

        # --- 2. RSI (clássico: <30 sobrevendido, >70 sobrecomprado) ---
        if rsi is not None:
            if direction == 'BUY' and rsi < 30:
                breakdown['rsi'] = self.weights['rsi']
            elif direction == 'SELL' and rsi > 70:
                breakdown['rsi'] = self.weights['rsi']
            elif 40 <= rsi <= 60:
                breakdown['rsi'] = 0  # zona neutra
            else:
                breakdown['rsi'] = self.weights['rsi'] // 2  # parcial

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
                # Quanto mais forte a tendência, mais pontos
                adx_factor = min(1.0, (adx - self.adx_minimum) / 20)
                breakdown['adx'] = int(self.weights['adx'] * adx_factor)
            else:
                breakdown['adx'] = 0  # mercado lateral

        # --- 5. ATR / Volatilidade ---
        if atr is not None and latest > 0:
            atr_pct = (atr / latest) * 100
            if 0.05 <= atr_pct <= 0.5:
                breakdown['atr_volatility'] = self.weights['atr_volatility']
            elif atr_pct > 0.5:
                breakdown['atr_volatility'] = self.weights['atr_volatility'] // 2  # muito volátil
            else:
                breakdown['atr_volatility'] = 0

        # --- 6. Bollinger (proporcional à proximidade das bandas) ---
        if upper and lower:
            if direction == 'BUY' and latest <= lower * 1.002:
                breakdown['bollinger'] = self.weights['bollinger']
            elif direction == 'SELL' and latest >= upper * 0.998:
                breakdown['bollinger'] = self.weights['bollinger']
            else:
                # parcial se estiver perto mas não no extremo
                band_width = upper - lower
                if band_width > 0:
                    if direction == 'BUY':
                        proximity = max(0, (lower * 1.01 - latest) / (lower * 0.01))
                    else:
                        proximity = max(0, (latest - upper * 0.99) / (upper * 0.01))
                    breakdown['bollinger'] = int(self.weights['bollinger'] * min(1.0, proximity))

        # --- 7. Momentum ---
        if momentum is not None:
            if direction == 'BUY' and momentum > 0:
                breakdown['momentum'] = self.weights['momentum']
            elif direction == 'SELL' and momentum < 0:
                breakdown['momentum'] = self.weights['momentum']

        # --- 8. Market Quality (combinação) ---
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
