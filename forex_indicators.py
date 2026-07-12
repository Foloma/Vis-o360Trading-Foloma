import logging
import threading
from statistics import mean, pstdev

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
        """
        Retorna a SMA (Simple Moving Average) para o período pedido.
        Se use_candles=True, usa velas de fecho; caso contrário, usa ticks.
        """
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
        """
        Retorna a EMA (Exponential Moving Average) para o período pedido.
        """
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
        """Calcula EMA a partir de uma lista de preços."""
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
        """
        Retorna o valor do RSI (0-100) para o período pedido.
        """
        if use_candles:
            data = self._data.get_recent_candles(symbol, count=period + 1)
            prices = [c['close'] for c in data]
        else:
            data = self._data.get_recent_ticks(symbol, count=period + 1)
            prices = [t['price'] for t in data]

        if len(prices) < period + 1:
            return None

        # Calcular mudanças de preço
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
        """
        Retorna (MACD_line, signal_line, histogram).
        Corrigido: calcula séries contínuas de EMA sem reinicializar a semente.
        """
        if use_candles:
            data = self._data.get_recent_candles(symbol, count=slow + signal + 50)
            prices = [c['close'] for c in data]
        else:
            data = self._data.get_recent_ticks(symbol, count=slow + signal + 200)
            prices = [t['price'] for t in data]

        if len(prices) < slow + signal:
            return None, None, None

        # Calcular séries contínuas de EMA
        ema_fast_series = self._ema_series(prices, fast)
        ema_slow_series = self._ema_series(prices, slow)

        if ema_fast_series is None or ema_slow_series is None:
            return None, None, None

        # Calcular a série de MACD (diferença entre EMAs)
        macd_series = []
        min_len = min(len(ema_fast_series), len(ema_slow_series))
        # Alinhar as séries (a EMA rápida pode começar antes da lenta)
        offset = len(ema_fast_series) - len(ema_slow_series)
        for i in range(min_len):
            macd_series.append(ema_fast_series[offset + i] - ema_slow_series[i])

        if len(macd_series) < signal:
            return round(macd_series[-1], 5), None, None

        # Calcular a linha de sinal (EMA dos últimos 'signal' valores do MACD)
        signal_line = self._ema_from_prices(macd_series[-signal:], signal)
        macd_line = macd_series[-1]
        histogram = macd_line - signal_line

        return round(macd_line, 5), round(signal_line, 5), round(histogram, 5)

    def _ema_series(self, prices, period):
        """
        Retorna a série completa de EMA para cada ponto após o período inicial.
        """
        if len(prices) < period:
            return None
        sma = sum(prices[:period]) / period
        multiplier = 2 / (period + 1)
        ema = sma
        series = [sma]  # o primeiro valor é a SMA inicial
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
            series.append(ema)
        return series

    # -----------------------------------------------------------------
    # Bandas de Bollinger
    # -----------------------------------------------------------------
    def bollinger_bands(self, symbol, period=20, std_dev=2, use_candles=True):
        """
        Retorna (upper_band, middle_band, lower_band).
        Usa pstdev (populacional) por convenção de mercado.
        """
        if use_candles:
            data = self._data.get_recent_candles(symbol, count=period)
            prices = [c['close'] for c in data]
        else:
            data = self._data.get_recent_ticks(symbol, count=period)
            prices = [t['price'] for t in data]

        if len(prices) < period:
            return None, None, None

        sma = sum(prices[-period:]) / period
        std = pstdev(prices[-period:])  # populacional (N), convenção de mercado

        upper = sma + (std_dev * std)
        lower = sma - (std_dev * std)

        return round(upper, 5), round(sma, 5), round(lower, 5)

    # -----------------------------------------------------------------
    # Indicadores compostos (para sinais)
    # -----------------------------------------------------------------
    def get_all_indicators(self, symbol, use_candles=True):
        """
        Retorna um dicionário com todos os indicadores calculados.
        """
        with self._lock:
            indicators = {
                'sma_200': self.sma(symbol, 200, use_candles),
                'ema_50': self.ema(symbol, 50, use_candles),
                'rsi_14': self.rsi(symbol, 14, use_candles),
                'macd': self.macd(symbol, 12, 26, 9, use_candles),
                'bollinger': self.bollinger_bands(symbol, 20, 2, use_candles),
                'latest_price': self._data.get_latest_price(symbol),
            }
            return indicators
