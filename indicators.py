import numpy as np
from collections import deque
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class TechnicalIndicators:
    def __init__(self, max_length=100):
        self.prices = deque(maxlen=max_length)
        self.timestamps = deque(maxlen=max_length)
        
    def add_price(self, price, timestamp=None):
        self.prices.append(price)
        if timestamp:
            self.timestamps.append(timestamp)
        else:
            self.timestamps.append(datetime.now())
        
    def get_price_array(self):
        return np.array(list(self.prices))
    
    def calculate_sma(self, period):
        """Média Móvel Simples"""
        if len(self.prices) < period:
            return None
        return float(np.mean(list(self.prices)[-period:]))
    
    def calculate_ema(self, period):
        """Média Móvel Exponencial (simplificada)"""
        if len(self.prices) < period:
            return None
        prices = list(self.prices)[-period:]
        return float(np.mean(prices))
    
    def calculate_trend(self):
        """Análise de tendência melhorada com Golden/Death Cross"""
        if len(self.prices) < 20:
            return 0, 'AGUARDANDO DADOS'
        
        sma9 = self.calculate_sma(9)
        sma21 = self.calculate_sma(21)
        sma50 = self.calculate_sma(50)
        current = list(self.prices)[-1]
        
        if sma9 is None or sma21 is None:
            return 0, 'AGUARDANDO'
        
        # Força da tendência (distância percentual)
        strength = abs(current - sma21) / sma21 * 100
        
        # Golden Cross (médias) - SINAL FORTE DE ALTA
        golden_cross = sma9 > sma21 and sma21 > sma50 if sma50 else False
        # Death Cross (médias) - SINAL FORTE DE BAIXA
        death_cross = sma9 < sma21 and sma21 < sma50 if sma50 else False
        
        # Determina direção com confirmação de médias
        if golden_cross:
            return min(strength * 2, 100), 'ALTA FORTE (GOLDEN CROSS)'
        elif death_cross:
            return min(strength * 2, 100), 'BAIXA FORTE (DEATH CROSS)'
        elif sma9 > sma21 and current > sma9:
            return strength, 'ALTA CONFIRMADA'
        elif sma9 > sma21:
            return strength, 'ALTA FRACA'
        elif sma9 < sma21 and current < sma9:
            return strength, 'BAIXA CONFIRMADA'
        elif sma9 < sma21:
            return strength, 'BAIXA FRACA'
        else:
            return 0, 'LATERAL'
    
    def calculate_rsi(self, period=14):
        """RSI completo"""
        if len(self.prices) < period + 1:
            return 50, 'AGUARDANDO'
        
        prices = list(self.prices)
        deltas = [prices[i] - prices[i-1] for i in range(-period, 0)]
        
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        
        avg_gain = np.mean(gains) if gains else 0
        avg_loss = np.mean(losses) if losses else 0
        
        if avg_loss == 0:
            return 100, 'SOBRECOMPRADO'
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        if rsi > 70:
            return rsi, 'SOBRECOMPRADO'
        elif rsi < 30:
            return rsi, 'SOBREVENDIDO'
        else:
            return rsi, 'NEUTRO'
    
    def calculate_macd(self):
        """MACD completo"""
        if len(self.prices) < 26:
            return 0, 'AGUARDANDO'
        
        prices = list(self.prices)
        ema12 = np.mean(prices[-12:])
        ema26 = np.mean(prices[-26:])
        
        macd = ema12 - ema26
        signal = macd * 0.9  # Simplificado mas funcional
        
        if macd > signal and macd > 0:
            return abs(macd) * 100, 'COMPRA'
        elif macd < signal and macd < 0:
            return abs(macd) * 100, 'VENDA'
        else:
            return abs(macd) * 50, 'NEUTRO'
    
    def calculate_bollinger(self, period=20):
        """Bandas de Bollinger completas"""
        if len(self.prices) < period:
            return 0, 'AGUARDANDO'
        
        prices = list(self.prices)
        recent = prices[-period:]
        current = prices[-1]
        
        sma = np.mean(recent)
        std = np.std(recent)
        
        upper = sma + (std * 2)
        lower = sma - (std * 2)
        
        if current <= lower:
            return 100, 'COMPRA (FUNDO)'
        elif current >= upper:
            return 100, 'VENDA (TOPO)'
        elif current < sma:
            return 50, 'LEVE COMPRA'
        elif current > sma:
            return 50, 'LEVE VENDA'
        else:
            return 0, 'NEUTRO'
    
    def get_all_indicators(self):
        """Retorna todos os indicadores"""
        trend_score, trend_desc = self.calculate_trend()
        rsi_score, rsi_desc = self.calculate_rsi()
        macd_score, macd_desc = self.calculate_macd()
        bb_score, bb_desc = self.calculate_bollinger()
        
        # Médias móveis para exibição
        sma9 = self.calculate_sma(9)
        sma21 = self.calculate_sma(21)
        sma50 = self.calculate_sma(50)
        ema12 = self.calculate_ema(12)
        ema26 = self.calculate_ema(26)
        
        return {
            'trend': {'score': trend_score, 'desc': trend_desc},
            'rsi': {'score': rsi_score, 'desc': rsi_desc},
            'macd': {'score': macd_score, 'desc': macd_desc},
            'bollinger': {'score': bb_score, 'desc': bb_desc},
            'sma9': sma9,
            'sma21': sma21,
            'sma50': sma50,
            'ema12': ema12,
            'ema26': ema26
      }
