"""
Motor de risco para Forex.
Independente da lógica de sinal — avalia condições de mercado
e decide se o trade deve avançar, mesmo com consenso alto.
"""

import logging

logger = logging.getLogger(__name__)


class ForexRiskEngine:
    """
    Avalia se as condições de mercado permitem a execução de um trade.
    Não substitui o Ensemble — é chamado depois dele, como filtro adicional.
    """

    def __init__(self, min_consensus_pct=60, min_adx=20, max_atr_pct=0.5):
        self.min_consensus_pct = min_consensus_pct
        self.min_adx = min_adx
        self.max_atr_pct = max_atr_pct          # volatilidade máxima aceitável (ATR) – mantida em 0.5%
        self.min_bandwidth_pct = 0.5            # REVERTIDO: largura mínima de Bollinger voltou a 0.5% (era 0.15% experimental)

    def can_execute(self, ind, consensus_pct):
        """
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

        # 4. Mercado lateral (Bollinger muito estreito) – limiar original de 0.5%
        bollinger = ind.get('bollinger')
        if bollinger:
            upper, middle, lower = bollinger
            if upper and middle and lower:
                bandwidth = (upper - lower) / middle * 100
                if bandwidth < self.min_bandwidth_pct:
                    return False, f"Mercado lateral (Bollinger bandwidth={bandwidth:.2f}% < {self.min_bandwidth_pct}%)"

        return True, "OK"
