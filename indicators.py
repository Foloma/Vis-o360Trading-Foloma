import math
from collections import deque

class TechnicalIndicators:
    def __init__(self, max_length=100):
        self.prices_by_symbol = {}
        self.max_length = max_length

    def add_price(self, price, symbol='R_100'):
        if symbol not in self.prices_by_symbol:
            self.prices_by_symbol[symbol] = deque(maxlen=self.max_length)
        self.prices_by_symbol[symbol].append(price)

    def get_prices(self, symbol='R_100'):
        return self.prices_by_symbol.get(symbol, deque())

    def _sma(self, data, period):
        if len(data) < period:
            return None
        return sum(data[-period:]) / period

    def _ema(self, data, period):
        if len(data) < period:
            return None
        k = 2 / (period + 1)
        ema = data[-period]
        for i in range(-period+1, 0):
            ema = (data[i] - ema) * k + ema
        return ema

    def _rsi(self, data, period=14):
        if len(data) < period + 1:
            return None
        gains = []
        losses = []
        for i in range(1, period+1):
            diff = data[-i] - data[-i-1]
            if diff >= 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(-diff)
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _macd(self, data, fast=12, slow=26, signal=9):
        if len(data) < slow + signal:
            return None, None
        ema_fast = self._ema(data, fast)
        ema_slow = self._ema(data, slow)
        if ema_fast is None or ema_slow is None:
            return None, None
        macd_line = ema_fast - ema_slow
        # não calculamos signal line por simplicidade
        return macd_line, None

    def _bollinger_bands(self, data, period=20, std_dev=2):
        if len(data) < period:
            return None, None, None
        prices = list(data)[-period:]
        sma = sum(prices) / period
        variance = sum((p - sma) ** 2 for p in prices) / period
        std = math.sqrt(variance)
        upper = sma + std_dev * std
        lower = sma - std_dev * std
        return upper, sma, lower

    def _stochastic(self, data, k_period=14, d_period=3):
        if len(data) < k_period:
            return None, None
        low = min(data[-k_period:])
        high = max(data[-k_period:])
        current = data[-1]
        if high == low:
            return 50, 50
        k = (current - low) / (high - low) * 100
        # simplificado: retorna %K
        return k, None

    def _sma_long(self, data, period=200):
        if len(data) < period:
            return None
        return sum(data[-period:]) / period

    def get_all_indicators(self, symbol='R_100'):
        prices = self.get_prices(symbol)
        if len(prices) < 10:
            return {
                'trend': {'score': 0, 'desc': '---'},
                'rsi': {'score': 0, 'desc': '---'},
                'macd': {'score': 0, 'desc': '---'},
                'bollinger': {'score': 0, 'desc': '---'},
                'stochastic': {'score': 0, 'desc': '---'},
                'sma200': None,
                'sma9': None, 'sma21': None, 'sma50': None,
                'ema12': None, 'ema26': None
            }
        data = list(prices)
        sma9 = self._sma(data, 9)
        sma21 = self._sma(data, 21)
        sma50 = self._sma(data, 50)
        sma200 = self._sma_long(data, 200)
        ema12 = self._ema(data, 12)
        ema26 = self._ema(data, 26)

        # Tendência
        if sma9 is not None and sma21 is not None:
            if sma9 > sma21:
                trend_desc = 'ALTA'
                trend_score = 80
            elif sma9 < sma21:
                trend_desc = 'BAIXA'
                trend_score = 80
            else:
                trend_desc = 'LATERAL'
                trend_score = 50
        else:
            trend_desc = '---'
            trend_score = 0

        # RSI
        rsi = self._rsi(data)
        if rsi is not None:
            rsi_score = rsi
            if rsi < 30:
                rsi_desc = 'SOBREVENDIDO'
            elif rsi > 70:
                rsi_desc = 'SOBRECOMPRADO'
            elif rsi < 45:
                rsi_desc = 'NEUTRO (baixo)'
            elif rsi > 55:
                rsi_desc = 'NEUTRO (alto)'
            else:
                rsi_desc = 'NEUTRO'
        else:
            rsi_score = 0
            rsi_desc = '---'

        # MACD
        macd_line, _ = self._macd(data)
        if macd_line is not None:
            if macd_line > 0:
                macd_desc = 'COMPRA'
                macd_score = 80
            else:
                macd_desc = 'VENDA'
                macd_score = 80
        else:
            macd_desc = '---'
            macd_score = 0

        # Bollinger
        upper, middle, lower = self._bollinger_bands(data)
        if upper is not None and data:
            last_price = data[-1]
            if last_price > upper:
                bb_desc = 'VENDA (sobrecomprado)'
                bb_score = 80
            elif last_price < lower:
                bb_desc = 'COMPRA (sobrevendido)'
                bb_score = 80
            else:
                bb_desc = 'NEUTRO'
                bb_score = 50
        else:
            bb_desc = '---'
            bb_score = 0

        # Estocástico
        stoch, _ = self._stochastic(data)
        if stoch is not None:
            if stoch < 20:
                stoch_desc = 'SOBREVENDIDO'
                stoch_score = 80
            elif stoch > 80:
                stoch_desc = 'SOBRECOMPRADO'
                stoch_score = 80
            else:
                stoch_desc = 'NEUTRO'
                stoch_score = 50
        else:
            stoch_desc = '---'
            stoch_score = 0

        return {
            'trend': {'score': trend_score, 'desc': trend_desc},
            'rsi': {'score': rsi_score, 'desc': rsi_desc},
            'macd': {'score': macd_score, 'desc': macd_desc},
            'bollinger': {'score': bb_score, 'desc': bb_desc},
            'stochastic': {'score': stoch_score, 'desc': stoch_desc},
            'sma200': sma200,
            'sma9': sma9,
            'sma21': sma21,
            'sma50': sma50,
            'ema12': ema12,
            'ema26': ema26
        }
