"""
Motor de risco para Forex.
Independente da lógica de sinal — avalia condições de mercado
e ajusta a confiança do sinal em vez de simplesmente bloquear.
"""

import logging

logger = logging.getLogger(__name__)


class ForexRiskEngine:
    """
    Avalia as condições de mercado e ajusta a confiança do sinal.
    Mantém o método can_execute() para compatibilidade, mas o novo
    método evaluate() devolve confiança ajustada e motivos de penalização.
    """

    def __init__(self, min_consensus_pct=60, min_adx=20, max_atr_pct=0.5):
        self.min_consensus_pct = min_consensus_pct
        self.min_adx = min_adx
        self.max_atr_pct = max_atr_pct          # volatilidade máxima aceitável (ATR)
        self.min_bandwidth_pct = 0.5            # largura mínima de Bollinger (mantida em can_execute)

    def can_execute(self, ind, consensus_pct):
        """
        Método antigo, mantido para compatibilidade.
        Retorna (True, "OK") ou (False, motivo).
        """
        # 1. Consenso mínimo
        if consensus_pct < self.min_consensus_pct:
            return False, f"Consenso insuficiente ({consensus_pct}%)"

        # 2. Força da tendência (ADX)
        adx = ind.get('adx_14')
        if adx is None or adx < self.min_adx:
            return False, f"Mercado sem direção clara (ADX={adx})"

        # 3. Volatilidade excessiva (ATR)
        atr = ind.get('atr_14')
        price = ind.get('latest_price')
        if atr and price:
            atr_pct = (atr / price) * 100
            if atr_pct > self.max_atr_pct:
                return False, f"Volatilidade excessiva ({atr_pct:.2f}%)"

        # 4. Mercado lateral (Bollinger muito estreito)
        bollinger = ind.get('bollinger')
        if bollinger:
            upper, middle, lower = bollinger
            if upper and middle and lower:
                bandwidth = (upper - lower) / middle * 100
                if bandwidth < self.min_bandwidth_pct:
                    return False, f"Mercado lateral (Bollinger bandwidth={bandwidth:.2f}% < {self.min_bandwidth_pct}%)"

        return True, "OK"

    def evaluate(self, ind, consensus_pct):
        """
        Penalização calibrada com dados reais (1467 sinais, ago/2026).
        - ADX fraco: peso 25 (evidência forte: 31.2% vs 61.6%)
        - ATR: mantido por precaução (nunca disparou na amostra, investigar)
        - Bandwidth lateral: REMOVIDO (efeito invertido: 52.1% vs 47.1%)
        """
        penalty = 0
        reasons = []

        # ADX fraco — preditor forte confirmado. Peso aumentado.
        adx = ind.get('adx_14')
        if adx is None or adx < self.min_adx:
            penalty += 25   # era 15
            reasons.append(f"Tendência fraca (ADX={adx if adx is not None else 'N/A'})")

        # ATR — nunca disparou em 1467 sinais nesta amostra. Mantido por precaução.
        atr = ind.get('atr_14')
        price = ind.get('latest_price')
        if atr and price:
            atr_pct = (atr / price) * 100
            if atr_pct > self.max_atr_pct:
                penalty += 10
                reasons.append(f"Volatilidade excessiva ({atr_pct:.2f}%)")

        # Bandwidth lateral — REMOVIDO (efeito invertido nos dados).
        # bollinger = ind.get('bollinger')
        # if bollinger:
        #     upper, middle, lower = bollinger
        #     if upper and middle and lower:
        #         bandwidth = (upper - lower) / middle * 100
        #         if bandwidth < self.min_bandwidth_pct:
        #             penalty += 15
        #             reasons.append(f"Mercado lateral (bandwidth={bandwidth:.2f}%)")

        adjusted_confidence = max(0, consensus_pct - penalty)
        return adjusted_confidence, reasons
