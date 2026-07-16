"""
Sistema de votação Ensemble para Forex.
Cada indicador vota BUY, SELL ou NEUTRO.
A direção final é determinada pela percentagem de votos concordantes.
ADX não vota direção — serve apenas como filtro de força.
"""

import logging

logger = logging.getLogger(__name__)


class ForexEnsemble:
    """
    Ensemble de indicadores técnicos.
    Cada indicador emite um voto (BUY/SELL/NEUTRO).
    A direção final requer um consenso mínimo (padrão: 60%).
    """

    def __init__(self, consensus_threshold=0.6):
        self.consensus_threshold = consensus_threshold  # 60% dos votos concordantes

    # -----------------------------------------------------------------
    # Votos individuais
    # -----------------------------------------------------------------
    def _vote_trend(self, ind):
        """Tendência: EMA50 vs SMA200."""
        sma200 = ind.get('sma_200')
        ema50 = ind.get('ema_50')
        if sma200 is None or ema50 is None:
            return 'NEUTRO'
        if ema50 > sma200:
            return 'BUY'
        elif ema50 < sma200:
            return 'SELL'
        return 'NEUTRO'

    def _vote_rsi(self, ind):
        """RSI: <35 = sobrevendido (BUY), >65 = sobrecomprado (SELL)."""
        rsi = ind.get('rsi_14')
        if rsi is None:
            return 'NEUTRO'
        if rsi < 35:
            return 'BUY'
        if rsi > 65:
            return 'SELL'
        return 'NEUTRO'

    def _vote_macd(self, ind):
        """MACD: linha > sinal = BUY, linha < sinal = SELL."""
        macd_line = ind.get('macd_line')
        signal_line = ind.get('signal_line')
        if macd_line is None or signal_line is None:
            return 'NEUTRO'
        if macd_line > signal_line:
            return 'BUY'
        elif macd_line < signal_line:
            return 'SELL'
        return 'NEUTRO'

    def _vote_bollinger(self, ind):
        """Bollinger: preço na banda inferior = BUY, na superior = SELL."""
        bollinger = ind.get('bollinger')
        price = ind.get('latest_price')
        if not bollinger or price is None:
            return 'NEUTRO'
        upper, middle, lower = bollinger
        if price <= lower:
            return 'BUY'
        if price >= upper:
            return 'SELL'
        return 'NEUTRO'

    def _vote_momentum(self, ind):
        """Momentum: positivo = BUY, negativo = SELL."""
        momentum = ind.get('momentum_10')
        if momentum is None:
            return 'NEUTRO'
        if momentum > 0:
            return 'BUY'
        elif momentum < 0:
            return 'SELL'
        return 'NEUTRO'

    # -----------------------------------------------------------------
    # Decisão do ensemble
    # -----------------------------------------------------------------
    def decide(self, ind):
        """
        Recebe um dicionário de indicadores e retorna:
        - direction: 'BUY', 'SELL' ou 'HOLD'
        - consensus_pct: % de votos concordantes (0-100)
        - votes: dicionário com o voto de cada indicador
        """
        votes = {
            'trend': self._vote_trend(ind),
            'rsi': self._vote_rsi(ind),
            'macd': self._vote_macd(ind),
            'bollinger': self._vote_bollinger(ind),
            'momentum': self._vote_momentum(ind),
        }

        buy_votes = sum(1 for v in votes.values() if v == 'BUY')
        sell_votes = sum(1 for v in votes.values() if v == 'SELL')
        total_voters = len(votes)

        # ADX como filtro de força (não vota direção)
        adx = ind.get('adx_14') or 0
        strength_ok = adx > 20

        if buy_votes / total_voters >= self.consensus_threshold and strength_ok:
            direction = 'BUY'
        elif sell_votes / total_voters >= self.consensus_threshold and strength_ok:
            direction = 'SELL'
        else:
            direction = 'HOLD'

        consensus_pct = round(max(buy_votes, sell_votes) / total_voters * 100)

        logger.debug(
            f"Ensemble: BUY={buy_votes}/{total_voters}, SELL={sell_votes}/{total_voters}, "
            f"ADX={adx}, strength_ok={strength_ok}, direction={direction}, consensus={consensus_pct}%"
        )

        return direction, consensus_pct, votes
