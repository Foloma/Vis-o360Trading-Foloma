import logging
import time

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Implementa os três módulos da Foloma Visão 360 Safe Flow.
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
        # Separar estado de paridade
        self._parity_odd_used = False   # streak de ímpares usado
        self._parity_even_used = False  # streak de pares usado
        self._parity_martingale_used = False  # segunda entrada (Martingale) já foi usada?
        self._last_parity_streak_type = None  # 'odd' ou 'even'
        self._matches_sequence_used = False
        self._matches_cooldown_until = 0

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
        return True, "OK"

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
            # cooldown específico por módulo
            if action.startswith('DIFFER'):
                self._apply_cooldown(5)
            elif action in ('CALL', 'PUT', 'BUY', 'SELL'):
                # Em paridade, se perdemos e o Martingale ainda não foi usado, NÃO aplicar cooldown longo
                # Apenas um cooldown curto para permitir reentrada rápida
                if not self._parity_martingale_used and self._last_parity_streak_type:
                    self._apply_cooldown(1)  # quase sem espera para reentrada
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
        last_three = recent[-3:]
        if len(set(last_three)) == 1:
            digit = last_three[0]
            available = digit not in self._differ_sequence_used
            return available, digit if available else None
        return False, None

    def _peek_parity(self):
        """Retorna (disponível, direção_recomendada, motivo)."""
        ok, _ = self.can_trade
        if not ok:
            return False, None, "Condições básicas não satisfeitas"
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 4:
            return False, None, "Aguardando dados"

        last_four = [d % 2 != 0 for d in recent[-4:]]

        # Verificar streak de ímpares
        if all(p == True for p in last_four):
            # Disponível se ainda não usado ou se Martingale permitido
            can_enter = not self._parity_odd_used or (
                self._parity_odd_used and not self._parity_martingale_used and self._last_parity_streak_type == 'odd'
            )
            return can_enter, 'even', "Streak de 4 ÍMPARES → apostar PAR"

        # Verificar streak de pares
        if all(p == False for p in last_four):
            can_enter = not self._parity_even_used or (
                self._parity_even_used and not self._parity_martingale_used and self._last_parity_streak_type == 'even'
            )
            return can_enter, 'odd', "Streak de 4 PARES → apostar ÍMPAR"

        # Sem streak
        return False, None, "Nenhum streak de 4"

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
            if count >= 25 and not self._matches_sequence_used:
                return True
        return False

    # -----------------------------------------------------------------
    # Módulo 1: DIFFER
    # -----------------------------------------------------------------
    def evaluate_differ(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 3:
            return None, "Aguardando dados"

        last_three = recent[-3:]
        if len(set(last_three)) == 1:
            digit = last_three[0]
            if digit in self._differ_sequence_used:
                return None, f"Dígito {digit} já utilizado nesta sequência"
            self._differ_sequence_used.add(digit)
            self._last_differ_digit = digit
            return digit, f"Repetição detetada: {digit}{digit}{digit}"
        else:
            self._differ_sequence_used.clear()
            return None, "Nenhuma repetição tripla"

    # -----------------------------------------------------------------
    # Módulo 2: PAR/ÍMPAR com suporte a Martingale
    # -----------------------------------------------------------------
    def evaluate_parity(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 4:
            return None, "Aguardando dados"

        last_four = [d % 2 != 0 for d in recent[-4:]]

        # Streak de ímpares
        if all(p == True for p in last_four):
            # Primeira entrada
            if not self._parity_odd_used:
                self._parity_odd_used = True
                self._last_parity_streak_type = 'odd'
                self._parity_martingale_used = False
                return 'even', "Streak de 4 ÍMPARES → apostar PAR"
            # Martingale (segunda entrada) — permitir se streak continuar e ainda não usado
            if self._parity_odd_used and not self._parity_martingale_used and self._last_parity_streak_type == 'odd':
                self._parity_martingale_used = True
                return 'even', "Martingale: streak ÍMPAR continua → apostar PAR novamente"
            return None, "Streak ÍMPAR já utilizado"

        # Streak de pares
        if all(p == False for p in last_four):
            if not self._parity_even_used:
                self._parity_even_used = True
                self._last_parity_streak_type = 'even'
                self._parity_martingale_used = False
                return 'odd', "Streak de 4 PARES → apostar ÍMPAR"
            if self._parity_even_used and not self._parity_martingale_used and self._last_parity_streak_type == 'even':
                self._parity_martingale_used = True
                return 'odd', "Martingale: streak PAR continua → apostar ÍMPAR novamente"
            return None, "Streak PAR já utilizado"

        # Se o streak quebrou, resetar os estados de paridade
        self._parity_odd_used = False
        self._parity_even_used = False
        self._parity_martingale_used = False
        self._last_parity_streak_type = None
        return None, "Nenhum streak de 4"

    # -----------------------------------------------------------------
    # Módulo 3: MATCHES
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
            if count >= 25 and not self._matches_sequence_used:
                self._matches_sequence_used = True
                self._last_matches_digit = digit
                return digit, f"Dígito {digit} ausente há {count} ticks"
        self._matches_sequence_used = False
        return None, "Nenhum dígito ausente ≥25 ticks"

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
            'parity_direction': parity_dir,       # 'odd' ou 'even' (direção da aposta)
            'parity_reason': parity_reason,
            'matches_available': matches_avail,
        }
