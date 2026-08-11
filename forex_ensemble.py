import logging

logger = logging.getLogger(__name__)


class ForexEnsemble:
    """
    Sistema de votação para sinais Forex.
    Agrega os votos de vários indicadores e retorna uma direção consensual.
    """

    def __init__(self, consensus_threshold=0.6):
        """
        consensus_threshold: proporção mínima de votos não neutros para aprovar um sinal (0.0 a 1.0)
        """
        self.consensus_threshold = consensus_threshold

    # -----------------------------------------------------------------
    # Votos individuais
    # -----------------------------------------------------------------
    def _vote_trend(self, ind):
        """Voto baseado no cruzamento EMA50 vs SMA100."""
        sma100 = ind.get('sma_100')
        ema50 = ind.get('ema_50')
        if sma100 is None or ema50 is None:
            return 'NEUTRO'
        if ema50 > sma100:
            return 'BUY'
        elif ema50 < sma100:
            return 'SELL'
        else:
            return 'NEUTRO'

    def _vote_rsi(self, ind):
        """Voto baseado no RSI (14 períodos). Limiares: 35/65."""
        rsi = ind.get('rsi_14')
        if rsi is None:
            return 'NEUTRO'
        if rsi < 35:                # revertido de 30 para 35
            return 'BUY'
        elif rsi > 65:              # revertido de 70 para 65
            return 'SELL'
        else:
            return 'NEUTRO'

    def _vote_macd(self, ind):
        """Voto baseado no MACD (cruzamento da linha de sinal)."""
        macd = ind.get('macd_line')
        signal = ind.get('signal_line')
        if macd is None or signal is None:
            return 'NEUTRO'
        if macd > signal:
            return 'BUY'
        elif macd < signal:
            return 'SELL'
        else:
            return 'NEUTRO'

    def _vote_bollinger(self, ind):
        """Voto baseado nas Bandas de Bollinger (reversão à média)."""
        bollinger = ind.get('bollinger')
        price = ind.get('latest_price')
        if bollinger is None or price is None:
            return 'NEUTRO'
        upper, middle, lower = bollinger
        if upper is None or lower is None:
            return 'NEUTRO'
        # Preço próximo da banda inferior → compra (reversão)
        if price <= lower * 1.001:
            return 'BUY'
        # Preço próximo da banda superior → venda (reversão)
        elif price >= upper * 0.999:
            return 'SELL'
        else:
            return 'NEUTRO'

    def _vote_momentum(self, ind):
        """Voto baseado no Momentum (10 períodos)."""
        momentum = ind.get('momentum_10')
        if momentum is None:
            return 'NEUTRO'
        if momentum > 0:
            return 'BUY'
        elif momentum < 0:
            return 'SELL'
        else:
            return 'NEUTRO'

    # -----------------------------------------------------------------
    # Decisão do ensemble
    # -----------------------------------------------------------------
    def decide(self, indicators):
        """
        Recebe um dicionário de indicadores e retorna (direction, consensus, votes).
        direction: 'BUY', 'SELL' ou 'HOLD'
        consensus: percentagem de votos alinhados (0-100)
        votes: dicionário com o voto de cada indicador
        """
        votes = {
            'trend':     self._vote_trend(indicators),
            'rsi':       self._vote_rsi(indicators),
            'macd':      self._vote_macd(indicators),
            'bollinger': self._vote_bollinger(indicators),
            'momentum':  self._vote_momentum(indicators),
        }

        # Contar votos não neutros
        buy_votes = sum(1 for v in votes.values() if v == 'BUY')
        sell_votes = sum(1 for v in votes.values() if v == 'SELL')
        total_votes = len(votes)
        neutral = total_votes - (buy_votes + sell_votes)

        # Calcular consenso (proporção de votos na direção vencedora)
        if buy_votes > sell_votes:
            direction = 'BUY'
            consensus = buy_votes / total_votes
        elif sell_votes > buy_votes:
            direction = 'SELL'
            consensus = sell_votes / total_votes
        else:
            direction = 'HOLD'
            consensus = 0

        consensus_pct = round(consensus * 100)

        logger.info(f"🔍 DEBUG ENSEMBLE: votes={votes}, buy={buy_votes}, sell={sell_votes}, "
                    f"neutral={neutral}, direction={direction}, consensus={consensus_pct}%")

        # Só aprova se o consenso atingir o limiar
        if consensus < self.consensus_threshold:
            direction = 'HOLD'

        return direction, consensus_pct, votes
