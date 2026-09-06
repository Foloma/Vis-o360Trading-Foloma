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
    """

    def __init__(self, min_consensus_pct=60, min_adx=20, max_atr_pct=0.5):
        self.min_consensus_pct = min_consensus_pct
        self.min_adx = min_adx
        self.max_atr_pct = max_atr_pct          # volatilidade máxima aceitável (ATR)
        # Removido: min_bandwidth_pct e can_execute (código morto)

    def evaluate(self, ind, consensus_pct):
        """
        Penalização calibrada com dados reais (1467 sinais, ago/2026).
        - ADX fraco: penalização contínua (distância ao limiar) limitada a 25.
        - ATR: mantido por precaução (agora com suavização de Wilder, verificar se dispara).
        """
        penalty = 0
        reasons = []

        # ADX fraco — preditor forte confirmado. Peso máximo 25.
        adx = ind.get('adx_14')
        if adx is None:
            penalty += 25   # sem ADX, assume pior caso
            reasons.append("ADX indisponível")
        elif adx < self.min_adx:
            # Penalização contínua: 0 no limiar, 25 quando ADX=0
            distance = self.min_adx - adx
            adx_penalty = min(25, int(distance * 1.5))  # 1.5 por ponto abaixo do limiar
            penalty += adx_penalty
            reasons.append(f"Tendência fraca (ADX={adx})")

        # ATR — volatilidade excessiva
        atr = ind.get('atr_14')
        price = ind.get('latest_price')
        if atr and price:
            atr_pct = (atr / price) * 100
            if atr_pct > self.max_atr_pct:
                penalty += 10
                reasons.append(f"Volatilidade excessiva ({atr_pct:.2f}%)")

        adjusted_confidence = max(0, consensus_pct - penalty)
        return adjusted_confidence, reasons
