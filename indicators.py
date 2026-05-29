import math
import threading
from collections import deque


class TechnicalIndicators:
    def __init__(self, max_length=200):
        self.prices_by_symbol = {}
        self.highs_by_symbol = {}
        self.lows_by_symbol = {}
        self.max_length = max_length
        self._lock = threading.RLock()

        # Caches incrementais para MACD (por símbolo)
        self._ema_fast_cache = {}
        self._ema_slow_cache = {}
        self._macd_line_history = {}
        self._macd_initialized = set()

        self.fast = 12
        self.slow = 26
        self.signal_period = 9

    def add_price(self, price, symbol='R_100', high=None, low=None):
        with self._lock:
            if symbol not in self.prices_by_symbol:
                self.prices_by_symbol[symbol] = deque(maxlen=self.max_length)
                self.highs_by_symbol[symbol] = deque(maxlen=self.max_length)
                self.lows_by_symbol[symbol] = deque(maxlen=self.max_length)
                self._macd_line_history[symbol] = deque(maxlen=self.signal_period + 5)

            self.prices_by_symbol[symbol].append(price)

            # Construir high/low sintéticos a partir dos últimos 5 preços
            if high is None or low is None:
                recent = list(self.prices_by_symbol[symbol])
                if len(recent) >= 5:
                    high = max(recent[-5:])
                    low = min(recent[-5:])
                else:
                    high = price
                    low = price

            self.highs_by_symbol[symbol].append(high)
            self.lows_by_symbol[symbol].append(low)
            self._update_macd_cache(symbol)

    def _update_macd_cache(self, symbol):
        prices = self.prices_by_symbol[symbol]
        if len(prices) < self.slow:
            return

        k_fast = 2.0 / (self.fast + 1)
        k_slow = 2.0 / (self.slow + 1)

        if symbol not in self._macd_initialized and len(prices) >= self.slow:
            ema_fast = sum(list(prices)[:self.fast]) / self.fast
            for p in list(prices)[self.fast:]:
                ema_fast = p * k_fast + ema_fast * (1 - k_fast)

            ema_slow = sum(list(prices)[:self.slow]) / self.slow
            for p in list(prices)[self.slow:]:
                ema_slow = p * k_slow + ema_slow * (1 - k_slow)

            self._ema_fast_cache[symbol] = ema_fast
            self._ema_slow_cache[symbol] = ema_slow
            self._macd_line_history[symbol].clear()
            self._macd_initialized.add(symbol)
            return

        if symbol in self._macd_initialized:
            last_price = prices[-1]
            ema_fast = last_price * k_fast + self._ema_fast_cache[symbol] * (1 - k_fast)
            ema_slow = last_price * k_slow + self._ema_slow_cache[symbol] * (1 - k_slow)
            self._ema_fast_cache[symbol] = ema_fast
            self._ema_slow_cache[symbol] = ema_slow
            macd_line = ema_fast - ema_slow
            self._macd_line_history[symbol].append(macd_line)

    def get_prices(self, symbol='R_100'):
        with self._lock:
            return list(self.prices_by_symbol.get(symbol, deque()))

    def _sma(self, data, period):
        if len(data) < period:
            return None
        return sum(data[-period:]) / period

    def _ema(self, data, period):
        if len(data) < period:
            return None
        k = 2.0 / (period + 1)
        ema = sum(data[:period]) / period
        for price in data[period:]:
            ema = price * k + ema * (1 - k)
        return ema

    def _rsi(self, data, period=14):
        if len(data) < period + 1:
            return None
        recent = data[-(period + 1):]
        gains, losses = [], []
        for i in range(1, len(recent)):
            diff = recent[i] - recent[i - 1]
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
        return 100 - (100 / (1 + rs))

    def _macd(self, data, fast=12, slow=26, signal=9, symbol=None):
        if symbol is None or symbol not in self._macd_initialized:
            return None, None, None

        macd_history = self._macd_line_history.get(symbol, deque())
        if len(macd_history) == 0:
            return None, None, None

        macd_line = macd_history[-1]

        if len(macd_history) >= signal:
            values = list(macd_history)[-signal:]
            k_signal = 2.0 / (signal + 1)
            ema_signal = values[0]
            for v in values[1:]:
                ema_signal = v * k_signal + ema_signal * (1 - k_signal)
            signal_line = ema_signal
            histogram = macd_line - signal_line
            return macd_line, signal_line, histogram
        else:
            return macd_line, None, None

    def _bollinger_bands(self, data, period=20, std_dev=None):
        if len(data) < period:
            return None, None, None
        prices = list(data)[-period:]
        sma = sum(prices) / period
        variance = sum((p - sma) ** 2 for p in prices) / period
        std = math.sqrt(variance)

        if std_dev is None:
            avg_price = sma
            volatility = std / avg_price if avg_price > 0 else 0
            if volatility > 0.001:
                std_dev = 2.5
            else:
                std_dev = 2.0
        upper = sma + std_dev * std
        lower = sma - std_dev * std
        return upper, sma, lower

    def _stochastic(self, data, k_period=14, d_period=3):
        if len(data) < k_period + d_period:
            return None, None
        k_values = []
        for j in range(d_period):
            end_idx = len(data) - (d_period - 1 - j)
            window = data[end_idx - k_period:end_idx]
            if len(window) < k_period:
                continue
            low = min(window)
            high = max(window)
            current = window[-1]
            if high == low:
                k_values.append(50.0)
            else:
                k_values.append((current - low) / (high - low) * 100)

        if not k_values:
            return None, None
        k = k_values[-1]
        d = sum(k_values) / len(k_values)
        return k, d

    def _sma_long(self, data, period=200):
        if len(data) < period:
            return None
        return sum(data[-period:]) / period

    # ---------- ATR, ADX, REGIME ----------

    def _atr(self, symbol='R_100', period=14):
        highs = list(self.highs_by_symbol.get(symbol, deque()))
        lows = list(self.lows_by_symbol.get(symbol, deque()))
        prices = self.get_prices(symbol)

        if len(highs) < period + 1 or len(lows) < period + 1 or len(prices) < period + 1:
            return None

        tr_values = []
        for i in range(-period, 0):
            high = highs[i]
            low = lows[i]
            prev_close = prices[i - 1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)

        avg_tr = sum(tr_values[:period]) / period
        k = 2.0 / (period + 1)
        for tr in tr_values[period:]:
            avg_tr = tr * k + avg_tr * (1 - k)

        return avg_tr

    def _adx(self, symbol='R_100', period=14):
        highs = list(self.highs_by_symbol.get(symbol, deque()))
        lows = list(self.lows_by_symbol.get(symbol, deque()))
        prices = self.get_prices(symbol)

        if len(highs) < period * 2 or len(lows) < period * 2 or len(prices) < period * 2:
            return None

        dm_plus = []
        dm_minus = []
        tr_vals = []

        for i in range(-period * 2, 0):
            high = highs[i]
            low = lows[i]
            prev_high = highs[i - 1]
            prev_low = lows[i - 1]
            prev_close = prices[i - 1]

            up_move = high - prev_high
            down_move = prev_low - low

            dm_plus.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
            dm_minus.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_vals.append(tr)

        avg_tr = sum(tr_vals[:period]) / period
        k = 2.0 / (period + 1)
        for tr in tr_vals[period:]:
            avg_tr = tr * k + avg_tr * (1 - k)

        sum_dm_plus = sum(dm_plus[:period])
        sum_dm_minus = sum(dm_minus[:period])

        for j in range(period, len(dm_plus)):
            sum_dm_plus = dm_plus[j] * k + sum_dm_plus * (1 - k)
            sum_dm_minus = dm_minus[j] * k + sum_dm_minus * (1 - k)

        di_plus = (sum_dm_plus / avg_tr * 100) if avg_tr > 0 else 0
        di_minus = (sum_dm_minus / avg_tr * 100) if avg_tr > 0 else 0

        dx = abs(di_plus - di_minus) / (di_plus + di_minus) * 100 if (di_plus + di_minus) > 0 else 0

        return dx

    def get_market_regime(self, symbol='R_100'):
        adx = self._adx(symbol)
        atr = self._atr(symbol)
        prices = self.get_prices(symbol)

        if adx is None or atr is None or len(prices) < 20:
            return 'UNKNOWN', 0.0

        current_price = prices[-1]
        atr_pct = (atr / current_price * 100) if current_price > 0 else 0

        if adx >= 20:
            return 'TRENDING', adx
        elif atr_pct > 0.05:
            return 'VOLATILE', adx
        else:
            return 'RANGING', adx

    # ---------- FIM DOS NOVOS MÉTODOS ----------

    def get_all_indicators(self, symbol='R_100'):
        prices = self.get_prices(symbol)
        n = len(prices)

        if n < 15:
            return {
                'trend':      {'score': 0,  'desc': '---'},
                'rsi':        {'score': 50, 'desc': '---'},
                'macd':       {'score': 0,  'desc': '---'},
                'bollinger':  {'score': 0,  'desc': '---'},
                'stochastic': {'score': 50, 'desc': '---'},
                'adx':        {'score': 0,  'desc': '---', 'regime': 'UNKNOWN'},
                'atr':        {'value': 0},
                'sma200': None, 'sma9': None, 'sma21': None,
                'sma50': None,  'ema12': None, 'ema26': None
            }

        data = list(prices)

        sma9   = self._sma(data, 9)   if n >= 9   else None
        sma21  = self._sma(data, 21)  if n >= 21  else None
        sma50  = self._sma(data, 50)  if n >= 50  else None
        sma200 = self._sma_long(data) if n >= 200 else None
        ema12  = self._ema(data, 12)  if n >= 12  else None
        ema26  = self._ema(data, 26)  if n >= 26  else None

        # TENDÊNCIA
        if sma9 is not None and sma21 is not None:
            if sma9 > sma21:
                trend_desc, trend_score = 'ALTA', 80
            elif sma9 < sma21:
                trend_desc, trend_score = 'BAIXA', 80
            else:
                trend_desc, trend_score = 'LATERAL', 50
        else:
            trend_desc, trend_score = '---', 0

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
            rsi_score, rsi_desc = 50, '---'

        # MACD
        macd_line, signal_line, histogram = self._macd(data, symbol=symbol)
        if macd_line is not None and histogram is not None:
            if histogram > 0:
                macd_desc, macd_score = 'COMPRA', 80
            elif histogram < 0:
                macd_desc, macd_score = 'VENDA', 80
            else:
                macd_desc, macd_score = 'NEUTRO', 50
        elif macd_line is not None:
            if macd_line > 0:
                macd_desc, macd_score = 'COMPRA', 65
            elif macd_line < 0:
                macd_desc, macd_score = 'VENDA', 65
            else:
                macd_desc, macd_score = 'NEUTRO', 50
        else:
            macd_desc, macd_score = '---', 0

        # BOLLINGER
        upper, middle, lower = self._bollinger_bands(data)
        if upper is not None:
            last_price = data[-1]
            band_width = upper - lower
            if band_width > 0 and last_price > upper:
                bb_desc, bb_score = 'VENDA (sobrecomprado)', 80
            elif band_width > 0 and last_price < lower:
                bb_desc, bb_score = 'COMPRA (sobrevendido)', 80
            elif middle is not None and last_price > middle:
                bb_desc, bb_score = 'NEUTRO (acima média)', 55
            elif middle is not None and last_price < middle:
                bb_desc, bb_score = 'NEUTRO (abaixo média)', 45
            else:
                bb_desc, bb_score = 'NEUTRO', 50
        else:
            bb_desc, bb_score = '---', 0

        # ESTOCÁSTICO
        stoch_k, stoch_d = self._stochastic(data)
        if stoch_k is not None:
            stoch_score = stoch_k
            if stoch_k < 20:
                stoch_desc = 'SOBREVENDIDO'
            elif stoch_k > 80:
                stoch_desc = 'SOBRECOMPRADO'
            elif stoch_d is not None and stoch_k > stoch_d and stoch_k < 50:
                stoch_desc = 'POSSÍVEL COMPRA'
            elif stoch_d is not None and stoch_k < stoch_d and stoch_k > 50:
                stoch_desc = 'POSSÍVEL VENDA'
            else:
                stoch_desc = 'NEUTRO'
        else:
            stoch_score, stoch_desc = 50, '---'

        # ADX + REGIME
        adx = self._adx(symbol)
        regime, adx_value = self.get_market_regime(symbol)
        atr = self._atr(symbol)

        if adx is not None:
            if adx >= 25:
                adx_desc = 'TENDÊNCIA FORTE'
            elif adx >= 20:
                adx_desc = 'TENDÊNCIA'
            elif adx >= 15:
                adx_desc = 'FRACO'
            else:
                adx_desc = 'SEM TENDÊNCIA'
            adx_score = adx
        else:
            adx_desc, adx_score = '---', 0

        return {
            'trend':      {'score': trend_score, 'desc': trend_desc},
            'rsi':        {'score': rsi_score,   'desc': rsi_desc},
            'macd':       {'score': macd_score,  'desc': macd_desc},
            'bollinger':  {'score': bb_score,    'desc': bb_desc},
            'stochastic': {'score': stoch_score, 'desc': stoch_desc},
            'adx':        {'score': adx_score,   'desc': adx_desc, 'regime': regime},
            'atr':        {'value': atr if atr else 0},
            'sma200': sma200,
            'sma9':   sma9,
            'sma21':  sma21,
            'sma50':  sma50,
            'ema12':  ema12,
            'ema26':  ema26
        }

    def reset_macd_cache(self):
        self._ema_fast_cache.clear()
        self._ema_slow_cache.clear()
        self._macd_line_history.clear()
        self._macd_initialized.clear()

    def reset_all(self):
        with self._lock:
            self.prices_by_symbol.clear()
            self.highs_by_symbol.clear()
            self.lows_by_symbol.clear()
            self._ema_fast_cache.clear()
            self._ema_slow_cache.clear()
            self._macd_line_history.clear()
            self._macd_initialized.clear()
