import logging
import threading
from statistics import pstdev

logger = logging.getLogger(__name__)


class ForexIndicators:
    """
    Calcula indicadores técnicos para pares de Forex.
    Usa os dados do ForexDataManager (ticks e velas).
    """

    def __init__(self, data_manager):
        self._data = data_manager
        self._lock = threading.RLock()

    # -----------------------------------------------------------------
    # Média Móvel Simples (SMA)
    # -----------------------------------------------------------------
    def sma(self, symbol, period=200, use_candles=True):
        if use_candles:
            data = self._data.get_recent_candles(symbol, count=period)
            prices = [c['close'] for c in data]
        else:
            data = self._data.get_recent_ticks(symbol, count=period)
            prices = [t['price'] for t in data]

        if len(prices) < period:
            return None

        return sum(prices[-period:]) / period

    # -----------------------------------------------------------------
    # Média Móvel Exponencial (EMA)
    # -----------------------------------------------------------------
    def ema(self, symbol, period=200, use_candles=True):
        if use_candles:
            data = self._data.get_recent_candles(symbol, count=period * 2)
            prices = [c['close'] for c in data]
        else:
            data = self._data.get_recent_ticks(symbol, count=period * 2)
            prices = [t['price'] for t in data]

        if len(prices) < period:
            return None

        return self._ema_from_prices(prices, period)

    def _ema_from_prices(self, prices, period):
        if len(prices) < period:
            return None
        sma = sum(prices[:period]) / period
        multiplier = 2 / (period + 1)
        ema = sma
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    # -----------------------------------------------------------------
    # RSI (Relative Strength Index)
    # -----------------------------------------------------------------
    def rsi(self, symbol, period=14, use_candles=True):
        if use_candles:
            data = self._data.get_recent_candles(symbol, count=period + 1)
            prices = [c['close'] for c in data]
        else:
            data = self._data.get_recent_ticks(symbol, count=period + 1)
            prices = [t['price'] for t in data]

        if len(prices) < period + 1:
            return None

        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)

    # -----------------------------------------------------------------
    # MACD (Moving Average Convergence Divergence)
    # -----------------------------------------------------------------
    def macd(self, symbol, fast=12, slow=26, signal=9, use_candles=True):
        if use_candles:
            data = self._data.get_recent_candles(symbol, count=slow + signal + 50)
            prices = [c['close'] for c in data]
        else:
            data = self._data.get_recent_ticks(symbol, count=slow + signal + 200)
            prices = [t['price'] for t in data]

        if len(prices) < slow + signal:
            return None, None, None

        ema_fast_series = self._ema_series(prices, fast)
        ema_slow_series = self._ema_series(prices, slow)

        if ema_fast_series is None or ema_slow_series is None:
            return None, None, None

        macd_series = []
        min_len = min(len(ema_fast_series), len(ema_slow_series))
        offset = len(ema_fast_series) - len(ema_slow_series)
        for i in range(min_len):
            macd_series.append(ema_fast_series[offset + i] - ema_slow_series[i])

        if len(macd_series) < signal:
            return round(macd_series[-1], 5), None, None

        signal_line = self._ema_from_prices(macd_series[-signal:], signal)
        macd_line = macd_series[-1]
        histogram = macd_line - signal_line

        return round(macd_line, 5), round(signal_line, 5), round(histogram, 5)

    def _ema_series(self, prices, period):
        if len(prices) < period:
            return None
        sma = sum(prices[:period]) / period
        multiplier = 2 / (period + 1)
        ema = sma
        series = [sma]
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
            series.append(ema)
        return series

    # -----------------------------------------------------------------
    # Bandas de Bollinger
    # -----------------------------------------------------------------
    def bollinger_bands(self, symbol, period=20, std_dev=2, use_candles=True):
        if use_candles:
            data = self._data.get_recent_candles(symbol, count=period)
            prices = [c['close'] for c in data]
        else:
            data = self._data.get_recent_ticks(symbol, count=period)
            prices = [t['price'] for t in data]

        if len(prices) < period:
            return None, None, None

        sma = sum(prices[-period:]) / period
        std = pstdev(prices[-period:])  # desvio padrão populacional

        upper = sma + (std_dev * std)
        lower = sma - (std_dev * std)

        return round(upper, 5), round(sma, 5), round(lower, 5)

    # -----------------------------------------------------------------
    # ADX (Average Directional Index)
    # -----------------------------------------------------------------
    def adx(self, symbol, period=14, use_candles=True):
        if use_candles:
            candles = self._data.get_recent_candles(symbol, count=period + 1)
            if len(candles) < period + 1:
                return None
            high = [c['high'] for c in candles]
            low = [c['low'] for c in candles]
            close = [c['close'] for c in candles]
        else:
            return None

        tr = [max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
              for i in range(1, len(high))]
        atr = sum(tr) / period

        plus_dm = [high[i]-high[i-1] if high[i]-high[i-1] > low[i-1]-low[i] and high[i]-high[i-1] > 0 else 0
                   for i in range(1, len(high))]
        minus_dm = [low[i-1]-low[i] if low[i-1]-low[i] > high[i]-high[i-1] and low[i-1]-low[i] > 0 else 0
                    for i in range(1, len(high))]

        atr = sum(tr) / period
        plus_di = sum(plus_dm) / atr * 100
        minus_di = sum(minus_dm) / atr * 100
        dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
        return round(dx, 2)

    # -----------------------------------------------------------------
    # ATR (Average True Range)
    # -----------------------------------------------------------------
    def atr(self, symbol, period=14, use_candles=True):
        if use_candles:
            candles = self._data.get_recent_candles(symbol, count=period + 1)
            if len(candles) < period + 1:
                return None
            high = [c['high'] for c in candles]
            low = [c['low'] for c in candles]
            close = [c['close'] for c in candles]
        else:
            return None

        tr = [max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
              for i in range(1, len(high))]
        return round(sum(tr) / len(tr), 5)

    # -----------------------------------------------------------------
    # Momentum
    # -----------------------------------------------------------------
    def momentum(self, symbol, period=10, use_candles=True):
        if use_candles:
            candles = self._data.get_recent_candles(symbol, count=period + 1)
            if len(candles) < period + 1:
                return None
            return candles[-1]['close'] - candles[-period-1]['close']
        else:
            ticks = self._data.get_recent_ticks(symbol, count=period + 1)
            if len(ticks) < period + 1:
                return None
            return ticks[-1]['price'] - ticks[-period-1]['price']

    # -----------------------------------------------------------------
    # Indicadores compostos (para o scorer)
    # -----------------------------------------------------------------
    def get_all_indicators(self, symbol, use_candles=True):
        with self._lock:
            macd_line, signal_line, _ = self.macd(symbol, 12, 26, 9, use_candles)
            return {
                'latest_price': self._data.get_latest_price(symbol),
                'sma_200': self.sma(symbol, 200, use_candles),
                'ema_50': self.ema(symbol, 50, use_candles),
                'rsi_14': self.rsi(symbol, 14, use_candles),
                'macd_line': macd_line,
                'signal_line': signal_line,
                'bollinger': self.bollinger_bands(symbol, 20, 2, use_candles),
                'adx_14': self.adx(symbol, 14, use_candles),
                'atr_14': self.atr(symbol, 14, use_candles),
                'momentum_10': self.momentum(symbol, 10, use_candles),
            }
