import logging
import time

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Implementa os três módulos da Foloma Visão 360 Smart Flow.
    Thresholds ajustados para maior fluidez operacional.
    """

    def __init__(self, client, analyzer):
        self.client = client
        self.analyzer = analyzer
        self._last_differ_digit = None
        self._last_parity_action = None
        self._last_matches_digit = None
        self._consecutive_losses = 0
        self._global_stop_until = 0
        self._cooldown_until = 0
        self._differ_sequence_used = set()
        # Paridade — estados separados
        self._parity_odd_used = False
        self._parity_even_used = False
        self._parity_martingale_used = False
        self._last_parity_streak_type = None
        self._matches_sequence_used = False
        self._matches_cooldown_until = 0
        # Histórico de preços para deteção de spikes
        self._price_history = []

    # -----------------------------------------------------------------
    # Propriedades de estado
    # -----------------------------------------------------------------
    @property
    def is_global_stop(self):
        return time.time() < self._global_stop_until

    @property
    def is_cooldown(self):
        return time.time() < self._cooldown_until

    @property
    def is_matches_cooldown(self):
        return time.time() < self._matches_cooldown_until

    @property
    def can_trade(self):
        if self.is_global_stop:
            return False, "STOP GLOBAL ATIVO"
        if self.is_cooldown:
            return False, "Cooldown ativo"
        if not self.client or not self.client.authorized:
            return False, "Não autorizado"
        if not self.client.streaming:
            return False, "Sem streaming"
        if time.time() - getattr(self.client, '_last_reconnect_time', 0) < 10:
            return False, "Reconexão recente"
        if getattr(self.client, '_ping_ms', 0) > 250:
            return False, f"Latência alta ({self.client._ping_ms}ms)"
        # Verificar estabilidade de mercado
        stable, reason = self.is_market_stable()
        if not stable:
            return False, reason
        return True, "OK"

    def is_market_stable(self):
        """
        Verifica se o mercado está estável para operar.
        Bloqueia em caso de spikes de volatilidade.
        """
        # Verificar spikes nos últimos 5 ticks
        if len(self._price_history) >= 5:
            recent = self._price_history[-5:]
            avg_price = sum(recent) / len(recent)
            for price in recent:
                variation = abs(price - avg_price) / avg_price
                if variation > 0.05:  # 5% de variação num tick é um spike
                    return False, f"Spike detetado (variação {variation:.1%})"
        return True, "OK"

    def on_tick(self, tick):
        """Regista preço para análise de estabilidade."""
        price = tick.get('price', 0)
        if price:
            self._price_history.append(price)
            if len(self._price_history) > 20:
                self._price_history.pop(0)

    def _apply_cooldown(self, ticks):
        self._cooldown_until = time.time() + ticks

    def reset_sequence_state(self):
        self._differ_sequence_used.clear()
        self._parity_odd_used = False
        self._parity_even_used = False
        self._parity_martingale_used = False
        self._last_parity_streak_type = None
        self._matches_sequence_used = False

    def notify_result(self, action, is_win):
        """Chamado pelo app.py após cada trade."""
        if not is_win:
            self._consecutive_losses += 1
            if self._consecutive_losses >= 2:
                self._global_stop_until = time.time() + 180
                logger.warning("🛑 STOP GLOBAL: 2 perdas consecutivas")
                self._consecutive_losses = 0
                self.reset_sequence_state()
                return
            if action.startswith('DIFFER'):
                self._apply_cooldown(5)
            elif action in ('CALL', 'PUT', 'BUY', 'SELL'):
                if not self._parity_martingale_used and self._last_parity_streak_type:
                    self._apply_cooldown(1)
                else:
                    self._apply_cooldown(5)
            elif action.startswith('MATCH'):
                self._matches_cooldown_until = time.time() + 150
                self._apply_cooldown(10)
        else:
            self._consecutive_losses = 0
            self.reset_sequence_state()
            if action.startswith('DIFFER'):
                self._apply_cooldown(1)
            elif action in ('CALL', 'PUT', 'BUY', 'SELL'):
                self._apply_cooldown(2)

    # -----------------------------------------------------------------
    # Métodos de leitura pura para o frontend
    # -----------------------------------------------------------------
    def _peek_differ(self):
        ok, _ = self.can_trade
        if not ok:
            return False, None
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 3:
            return False, None
        # Novo threshold: 2 dígitos iguais consecutivos + dominância nos últimos 10
        last_two = recent[-2:]
        if last_two[0] == last_two[1]:
            digit = last_two[0]
            # Verificar dominância: apareceu ≥3 vezes nos últimos 10?
            last_ten = recent[-10:] if len(recent) >= 10 else recent
            if last_ten.count(digit) >= 3:
                available = digit not in self._differ_sequence_used
                return available, digit if available else None
        return False, None

    def _peek_parity(self):
        ok, _ = self.can_trade
        if not ok:
            return False, None, "Condições básicas não satisfeitas"
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 4:
            return False, None, "Aguardando dados"

        # Novo threshold: 3 pares nos últimos 4 ticks OU 3 ímpares nos últimos 4 ticks
        last_four = [d % 2 != 0 for d in recent[-4:]]
        odd_count = sum(last_four)
        even_count = 4 - odd_count

        # 3 ou 4 ímpares → apostar PAR
        if odd_count >= 3:
            can_enter = not self._parity_odd_used or (
                self._parity_odd_used and not self._parity_martingale_used and self._last_parity_streak_type == 'odd'
            )
            return can_enter, 'even', f"Tendência ÍMPAR ({odd_count}/4) → apostar PAR"

        # 3 ou 4 pares → apostar ÍMPAR
        if even_count >= 3:
            can_enter = not self._parity_even_used or (
                self._parity_even_used and not self._parity_martingale_used and self._last_parity_streak_type == 'even'
            )
            return can_enter, 'odd', f"Tendência PAR ({even_count}/4) → apostar ÍMPAR"

        return False, None, "Nenhuma tendência clara"

    def _peek_matches(self):
        ok, _ = self.can_trade
        if not ok:
            return False
        if self.is_matches_cooldown:
            return False
        absence = getattr(self.analyzer, 'get_digit_absence_counts', None)
        if not absence:
            return False
        for digit, count in absence().items():
            if count >= 15 and not self._matches_sequence_used:
                return True
        return False

    # -----------------------------------------------------------------
    # Módulo 1: DIFFER (thresholds flexibilizados)
    # -----------------------------------------------------------------
    def evaluate_differ(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 3:
            return None, "Aguardando dados"

        # Nova regra: 2 dígitos iguais consecutivos + dominância ≥3 nos últimos 10
        last_two = recent[-2:]
        if last_two[0] == last_two[1]:
            digit = last_two[0]
            last_ten = recent[-10:] if len(recent) >= 10 else recent
            if last_ten.count(digit) >= 3:
                if digit in self._differ_sequence_used:
                    return None, f"Dígito {digit} já utilizado nesta sequência"
                self._differ_sequence_used.add(digit)
                self._last_differ_digit = digit
                logger.info(f"🎯 DIFFER: dígito {digit} — {last_two[0]}{last_two[1]} consecutivos + {last_ten.count(digit)}/10 dominância")
                return digit, f"DIFFER {digit}: {last_two[0]}{last_two[1]} consecutivos + dominância {last_ten.count(digit)}/10"
        # Reset se sequência quebrou
        if len(recent) >= 3 and recent[-3] != recent[-2]:
            self._differ_sequence_used.clear()
        return None, "Nenhum padrão DIFFER"

    # -----------------------------------------------------------------
    # Módulo 2: PAR/ÍMPAR (thresholds flexibilizados + logs)
    # -----------------------------------------------------------------
    def evaluate_parity(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 4:
            return None, "Aguardando dados"

        last_four = [d % 2 != 0 for d in recent[-4:]]
        odd_count = sum(last_four)
        even_count = 4 - odd_count

        logger.info(f"🔍 PAR/ÍMPAR: últimos 4 dígitos={recent[-4:]}, ímpares={odd_count}, pares={even_count}")

        # 3 ou 4 ímpares → apostar PAR
        if odd_count >= 3:
            if not self._parity_odd_used:
                self._parity_odd_used = True
                self._last_parity_streak_type = 'odd'
                self._parity_martingale_used = False
                logger.info(f"🎯 PAR/ÍMPAR: {odd_count}/4 ímpares → ENTRAR PAR (primeira entrada)")
                return 'even', f"Tendência ÍMPAR ({odd_count}/4) → apostar PAR"
            if not self._parity_martingale_used and self._last_parity_streak_type == 'odd':
                self._parity_martingale_used = True
                logger.info(f"🎯 PAR/ÍMPAR: Martingale ativado — streak ÍMPAR continua → apostar PAR novamente")
                return 'even', "Martingale: streak ÍMPAR continua → apostar PAR novamente"
            return None, "Streak ÍMPAR já utilizado"

        # 3 ou 4 pares → apostar ÍMPAR
        if even_count >= 3:
            if not self._parity_even_used:
                self._parity_even_used = True
                self._last_parity_streak_type = 'even'
                self._parity_martingale_used = False
                logger.info(f"🎯 PAR/ÍMPAR: {even_count}/4 pares → ENTRAR ÍMPAR (primeira entrada)")
                return 'odd', f"Tendência PAR ({even_count}/4) → apostar ÍMPAR"
            if not self._parity_martingale_used and self._last_parity_streak_type == 'even':
                self._parity_martingale_used = True
                logger.info(f"🎯 PAR/ÍMPAR: Martingale ativado — streak PAR continua → apostar ÍMPAR novamente")
                return 'odd', "Martingale: streak PAR continua → apostar ÍMPAR novamente"
            return None, "Streak PAR já utilizado"

        # Reset se não há tendência
        self._parity_odd_used = False
        self._parity_even_used = False
        self._parity_martingale_used = False
        self._last_parity_streak_type = None
        return None, "Nenhuma tendência clara"

    # -----------------------------------------------------------------
    # Módulo 3: MATCHES (threshold reduzido)
    # -----------------------------------------------------------------
    def evaluate_matches(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        if self.is_matches_cooldown:
            return None, "Cooldown MATCHES ativo"

        absence = getattr(self.analyzer, 'get_digit_absence_counts', None)
        if not absence:
            return None, "Contador de ausência indisponível"

        for digit, count in absence().items():
            if count >= 15 and not self._matches_sequence_used:
                self._matches_sequence_used = True
                self._last_matches_digit = digit
                logger.info(f"🎯 MATCHES: dígito {digit} ausente há {count} ticks")
                return digit, f"Dígito {digit} ausente há {count} ticks"
        self._matches_sequence_used = False
        return None, "Nenhum dígito ausente ≥15 ticks"

    # -----------------------------------------------------------------
    # Status para o frontend
    # -----------------------------------------------------------------
    def get_status(self):
        differ_avail, differ_digit = self._peek_differ()
        parity_avail, parity_dir, parity_reason = self._peek_parity()
        matches_avail = self._peek_matches()
        return {
            'global_stop': self.is_global_stop,
            'cooldown': self.is_cooldown,
            'matches_cooldown': self.is_matches_cooldown,
            'consecutive_losses': self._consecutive_losses,
            'differ_available': differ_avail,
            'differ_digit': differ_digit,
            'parity_available': parity_avail,
            'parity_direction': parity_dir,
            'parity_reason': parity_reason,
            'matches_available': matches_avail,
        }
