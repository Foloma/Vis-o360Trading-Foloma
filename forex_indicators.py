import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class ForexIndicators:
    """
    Calcula indicadores técnicos para pares de Forex.
    Usa os dados do ForexDataManager (velas e ticks).
    """

    def __init__(self, data_manager):
        self._data = data_manager

    # -----------------------------------------------------------------
    # Funções auxiliares de média
    # -----------------------------------------------------------------
    def _sma(self, prices, period):
        """Média Móvel Simples."""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    def _ema(self, prices, period):
        """Média Móvel Exponencial."""
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for price in prices[period:]:
            ema = (price - ema) * k + ema
        return ema

    def _ema_series(self, prices, period):
        """Retorna a série completa de EMA para cada ponto, a partir do índice `period-1`."""
        if len(prices) < period:
            return []
        k = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        series = [ema]
        for price in prices[period:]:
            ema = (price - ema) * k + ema
            series.append(ema)
        return series

    # -----------------------------------------------------------------
    # Indicadores públicos
    # -----------------------------------------------------------------
    def sma(self, symbol, period=100, granularity=900):
        """SMA simples sobre velas fechadas."""
        candles = self._data.get_recent_candles(symbol, count=period + 50, granularity=granularity, only_closed=True)
        if not candles:
            return None
        prices = [c['close'] for c in candles]
        return self._sma(prices, period)

    def ema(self, symbol, period=50, granularity=900):
        """EMA sobre velas fechadas."""
        candles = self._data.get_recent_candles(symbol, count=period + 50, granularity=granularity, only_closed=True)
        if not candles:
            return None
        prices = [c['close'] for c in candles]
        return self._ema(prices, period)

    def rsi(self, symbol, period=14, granularity=900):
        """RSI (Relative Strength Index) usando o método de Wilder."""
        candles = self._data.get_recent_candles(symbol, count=period * 5, granularity=granularity, only_closed=True)
        if not candles or len(candles) < period + 1:
            return None
        closes = [c['close'] for c in candles]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i-1]
            gains.append(diff if diff > 0 else 0)
            losses.append(abs(diff) if diff < 0 else 0)
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        if avg_loss == 0:
            return 100.0
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 0
        return 100 - (100 / (1 + rs))

    def macd(self, symbol, fast=12, slow=26, signal=9, granularity=900):
        """MACD, linha de sinal e histograma."""
        candles = self._data.get_recent_candles(symbol, count=slow + 50, granularity=granularity, only_closed=True)
        if not candles or len(candles) < slow:
            return None, None, None
        prices = [c['close'] for c in candles]
        ema_fast_series = self._ema_series(prices, fast)
        ema_slow_series = self._ema_series(prices, slow)
        offset = len(ema_fast_series) - len(ema_slow_series)
        if offset < 0:
            return None, None, None
        macd_line = [ema_fast_series[i + offset] - ema_slow_series[i] for i in range(len(ema_slow_series))]
        if len(macd_line) < signal:
            return None, None, None
        signal_line = self._ema(macd_line, signal)
        if signal_line is None:
            return None, None, None
        histogram = macd_line[-1] - signal_line
        return macd_line[-1], signal_line, histogram

    def bollinger_bands(self, symbol, period=20, std_dev=2, granularity=900):
        """Bandas de Bollinger."""
        candles = self._data.get_recent_candles(symbol, count=period + 10, granularity=granularity, only_closed=True)
        if not candles or len(candles) < period:
            return None, None, None
        prices = [c['close'] for c in candles[-period:]]
        middle = sum(prices) / period
        variance = sum((p - middle) ** 2 for p in prices) / period
        std = variance ** 0.5
        return middle + std_dev * std, middle, middle - std_dev * std

    def adx(self, symbol, period=14, granularity=900):
        """
        ADX (Average Directional Index) — suavizado com média móvel de Wilder.
        Exige `period * 2` velas para produzir um ADX verdadeiro.
        """
        candles = self._data.get_recent_candles(symbol, count=period * 3, granularity=granularity, only_closed=True)
        if not candles or len(candles) < period * 2:
            return None
        highs = [c['high'] for c in candles]
        lows = [c['low'] for c in candles]
        closes = [c['close'] for c in candles]

        tr_list, plus_dm_list, minus_dm_list = [], [], []
        for i in range(1, len(candles)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_list.append(tr)
            up_move = highs[i] - highs[i-1]
            down_move = lows[i-1] - lows[i]
            plus_dm = up_move if up_move > down_move and up_move > 0 else 0
            minus_dm = down_move if down_move > up_move and down_move > 0 else 0
            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)

        if len(tr_list) < period * 2:
            return None

        # Suavização inicial (Wilder)
        atr = sum(tr_list[:period]) / period
        plus_di = (sum(plus_dm_list[:period]) / period) / atr * 100 if atr else 0
        minus_di = (sum(minus_dm_list[:period]) / period) / atr * 100 if atr else 0
        dx_list = []
        denom = plus_di + minus_di
        dx_list.append(abs(plus_di - minus_di) / denom * 100 if denom else 0)

        # Continuar a suavizar DI e acumular DX para todos os períodos seguintes
        for i in range(period, len(tr_list)):
            atr = (atr * (period - 1) + tr_list[i]) / period
            plus_di = (plus_di * (period - 1) + (plus_dm_list[i] / atr * 100 if atr else 0)) / period
            minus_di = (minus_di * (period - 1) + (minus_dm_list[i] / atr * 100 if atr else 0)) / period
            denom = plus_di + minus_di
            dx_list.append(abs(plus_di - minus_di) / denom * 100 if denom else 0)

        # ADX = média móvel dos últimos `period` valores de DX
        if len(dx_list) < period:
            return round(sum(dx_list) / len(dx_list), 2)
        return round(sum(dx_list[-period:]) / period, 2)

    def atr(self, symbol, period=14, granularity=900):
        """ATR (Average True Range)."""
        candles = self._data.get_recent_candles(symbol, count=period + 10, granularity=granularity, only_closed=True)
        if not candles or len(candles) < period + 1:
            return None
        highs = [c['high'] for c in candles]
        lows = [c['low'] for c in candles]
        closes = [c['close'] for c in candles]
        tr_values = []
        for i in range(1, len(candles)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_values.append(tr)
        return sum(tr_values[-period:]) / period

    def momentum(self, symbol, period=10, granularity=900):
        """Momentum (diferença entre o fecho atual e o fecho de `period` velas atrás)."""
        candles = self._data.get_recent_candles(symbol, count=period + 5, granularity=granularity, only_closed=True)
        if not candles or len(candles) < period:
            return None
        return candles[-1]['close'] - candles[-period-1]['close']

    # -----------------------------------------------------------------
    # Agregador para o Ensemble
    # -----------------------------------------------------------------
    def get_all_indicators(self, symbol, use_candles=True, granularity=900):
        """
        Retorna um dicionário com todos os indicadores necessários para o ensemble.
        """
        if not use_candles:
            return {'latest_price': self._data.get_latest_price(symbol)}

        latest = self._data.get_latest_price(symbol)
        sma_100 = self.sma(symbol, period=100, granularity=granularity)
        ema_50 = self.ema(symbol, period=50, granularity=granularity)
        rsi_14 = self.rsi(symbol, period=14, granularity=granularity)
        adx_14 = self.adx(symbol, period=14, granularity=granularity)
        atr_14 = self.atr(symbol, period=14, granularity=granularity)
        macd_line, signal_line, histogram = self.macd(symbol, fast=12, slow=26, signal=9, granularity=granularity)
        bb_upper, bb_middle, bb_lower = self.bollinger_bands(symbol, period=20, granularity=granularity)
        momentum_10 = self.momentum(symbol, period=10, granularity=granularity)

        return {
            'latest_price': latest,
            'sma_100': sma_100,
            'ema_50': ema_50,
            'rsi_14': rsi_14,
            'adx_14': adx_14,
            'atr_14': atr_14,
            'macd_line': macd_line,
            'signal_line': signal_line,
            'macd_histogram': histogram,
            'bollinger': (bb_upper, bb_middle, bb_lower),
            'momentum_10': momentum_10
        }
