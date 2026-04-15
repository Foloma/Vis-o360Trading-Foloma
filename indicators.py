import math
from collections import deque

class TechnicalIndicators:
    def __init__(self, max_length=100):
        self.prices = deque(maxlen=max_length)
        self.trend_score = 0
        self.rsi_score = 0
        self.macd_score = 0
        self.bollinger_score = 0
        self.trend_desc = '---'
        self.rsi_desc = '---'
        self.macd_desc = '---'
        self.bollinger_desc = '---'

    def add_price(self, price):
        self.prices.append(price)

    def _sma(self, data, period):
        if len(data) < period:
            return None
        return sum(data[-period:]) / period

    def _ema(self, data, period):
        if len(data) < period:
            return None
        k = 2 / (period + 1)
        ema = data[-period]  # primeiro valor é a média simples
        for i in range(-period+1, 0):
            ema = (data[i] - ema) * k + ema
        return ema

    def _rsi(self, period=14):
        if len(self.prices) < period + 1:
            return None
        gains = []
        losses = []
        prices = list(self.prices)
        for i in range(1, period+1):
            diff = prices[-i] - prices[-i-1]
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

    def _macd(self, fast=12, slow=26, signal=9):
        if len(self.prices) < slow + signal:
            return None, None
        prices = list(self.prices)
        ema_fast = self._ema(prices, fast)
        ema_slow = self._ema(prices, slow)
        if ema_fast is None or ema_slow is None:
            return None, None
        macd_line = ema_fast - ema_slow
        # Signal line = EMA de 9 períodos da macd_line
        macd_history = []
        for i in range(len(prices)-slow, len(prices)):
            ema_f = self._ema(prices[:i+1], fast)
            ema_s = self._ema(prices[:i+1], slow)
            if ema_f is None or ema_s is None:
                macd_history.append(None)
            else:
                macd_history.append(ema_f - ema_s)
        # Últimos 'signal' valores não nulos
        valid = [x for x in macd_history if x is not None]
        if len(valid) < signal:
            return macd_line, None
        signal_line = self._ema(valid, signal)
        return macd_line, signal_line

    def _bollinger_bands(self, period=20, std_dev=2):
        if len(self.prices) < period:
            return None, None, None
        prices = list(self.prices)[-period:]
        sma = sum(prices) / period
        variance = sum((p - sma) ** 2 for p in prices) / period
        std = math.sqrt(variance)
        upper = sma + std_dev * std
        lower = sma - std_dev * std
        return upper, sma, lower

    def get_all_indicators(self):
        # 1. Tendência (usando inclinação da média móvel de 9 períodos)
        sma9 = self._sma(list(self.prices), 9)
        sma21 = self._sma(list(self.prices), 21)
        if sma9 is not None and sma21 is not None:
            if sma9 > sma21:
                self.trend_desc = 'ALTA'
                self.trend_score = 80
            elif sma9 < sma21:
                self.trend_desc = 'BAIXA'
                self.trend_score = 80
            else:
                self.trend_desc = 'LATERAL'
                self.trend_score = 50
        else:
            self.trend_desc = '---'
            self.trend_score = 0

        # 2. RSI
        rsi = self._rsi()
        if rsi is not None:
            self.rsi_score = rsi
            if rsi < 30:
                self.rsi_desc = 'SOBREVENDIDO'
            elif rsi > 70:
                self.rsi_desc = 'SOBRECOMPRADO'
            elif rsi < 45:
                self.rsi_desc = 'NEUTRO (baixo)'
            elif rsi > 55:
                self.rsi_desc = 'NEUTRO (alto)'
            else:
                self.rsi_desc = 'NEUTRO'
        else:
            self.rsi_score = 0
            self.rsi_desc = '---'

        # 3. MACD
        macd_line, signal_line = self._macd()
        if macd_line is not None and signal_line is not None:
            if macd_line > signal_line:
                self.macd_desc = 'COMPRA'
                self.macd_score = 80
            elif macd_line < signal_line:
                self.macd_desc = 'VENDA'
                self.macd_score = 80
            else:
                self.macd_desc = 'NEUTRO'
                self.macd_score = 50
        else:
            self.macd_desc = '---'
            self.macd_score = 0

        # 4. Bollinger Bands
        upper, middle, lower = self._bollinger_bands()
        if upper is not None and len(self.prices) > 0:
            last_price = self.prices[-1]
            if last_price > upper:
                self.bollinger_desc = 'VENDA (sobrecomprado)'
                self.bollinger_score = 80
            elif last_price < lower:
                self.bollinger_desc = 'COMPRA (sobrevendido)'
                self.bollinger_score = 80
            else:
                self.bollinger_desc = 'NEUTRO'
                self.bollinger_score = 50
        else:
            self.bollinger_desc = '---'
            self.bollinger_score = 0

        return {
            'trend': {'score': self.trend_score, 'desc': self.trend_desc},
            'rsi': {'score': self.rsi_score, 'desc': self.rsi_desc},
            'macd': {'score': self.macd_score, 'desc': self.macd_desc},
            'bollinger': {'score': self.bollinger_score, 'desc': self.bollinger_desc},
            'sma9': self._sma(list(self.prices), 9),
            'sma21': self._sma(list(self.prices), 21),
            'sma50': self._sma(list(self.prices), 50),
            'ema12': self._ema(list(self.prices), 12),
            'ema26': self._ema(list(self.prices), 26)
        }
