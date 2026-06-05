import logging
import time

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Implementa os três módulos da Foloma Visão 360 Safe Flow.
    Cada módulo decide se uma entrada deve ser autorizada com base em:
      - padrões estatísticos (repetição, streak, ausência)
      - filtros de latência e estabilidade
      - cooldowns e limitadores de sequência
      - stop global após perdas consecutivas
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
        self._parity_sequence_used = False
        self._matches_sequence_used = False
        # Cooldown específico para MATCHES (gerido internamente)
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
        """Verifica condições básicas para qualquer trade."""
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
        self._parity_sequence_used = False
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
                self._apply_cooldown(5)
            elif action.startswith('MATCH'):
                self._matches_cooldown_until = time.time() + 150
                self._apply_cooldown(10)
        else:
            self._consecutive_losses = 0
            self.reset_sequence_state()       # Bug #6: reset em vitória
            if action.startswith('DIFFER'):
                self._apply_cooldown(1)
            elif action in ('CALL', 'PUT', 'BUY', 'SELL'):
                self._apply_cooldown(2)
            # MATCHES sem cooldown adicional em vitória

    # -----------------------------------------------------------------
    # Métodos de leitura pura para o frontend (Bug #3)
    # -----------------------------------------------------------------
    def _peek_differ(self):
        """Versão somente leitura — NÃO altera estado."""
        ok, _ = self.can_trade
        if not ok:
            return False
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 3:
            return False
        last_three = recent[-3:]
        if len(set(last_three)) == 1:
            return last_three[0] not in self._differ_sequence_used
        return False

    def _peek_parity(self):
        """Versão somente leitura — NÃO altera estado."""
        ok, _ = self.can_trade
        if not ok:
            return False
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 4:
            return False
        last_four = [d % 2 != 0 for d in recent[-4:]]
        if all(p == True for p in last_four) and not self._parity_sequence_used:
            return True
        if all(p == False for p in last_four) and not self._parity_sequence_used:
            return True
        return False

    def _peek_matches(self):
        """Versão somente leitura — NÃO altera estado."""
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
    # Módulo 1: DIFFER por repetição (>=3 iguais consecutivos)
    # -----------------------------------------------------------------
    def evaluate_differ(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        recent = self.analyzer.get_recent_digits(20)    # Bug #2 corrigido
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
    # Módulo 2: PAR/ÍMPAR por streak (>=4 consecutivos)
    # -----------------------------------------------------------------
    def evaluate_parity(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        recent = self.analyzer.get_recent_digits(20)    # Bug #2 corrigido
        if len(recent) < 4:
            return None, "Aguardando dados"

        last_four = [d % 2 != 0 for d in recent[-4:]]
        if all(p == True for p in last_four):
            if self._parity_sequence_used:
                return None, "Streak ÍMPAR já utilizado"
            self._parity_sequence_used = True
            self._last_parity_action = 'odd'
            return 'even', "Streak de 4 ÍMPARES → apostar PAR"
        elif all(p == False for p in last_four):
            if self._parity_sequence_used:
                return None, "Streak PAR já utilizado"
            self._parity_sequence_used = True
            self._last_parity_action = 'even'
            return 'odd', "Streak de 4 PARES → apostar ÍMPAR"
        else:
            self._parity_sequence_used = False
            return None, "Nenhum streak de 4"

    # -----------------------------------------------------------------
    # Módulo 3: MATCHES por ausência (>=25 ticks) — Bug #8 unificado
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
    # Status para o frontend (Bug #3 corrigido — usa _peek)
    # -----------------------------------------------------------------
    def get_status(self):
        return {
            'global_stop': self.is_global_stop,
            'cooldown': self.is_cooldown,
            'matches_cooldown': self.is_matches_cooldown,
            'consecutive_losses': self._consecutive_losses,
            'differ_available': self._peek_differ(),
            'parity_available': self._peek_parity(),
            'matches_available': self._peek_matches(),
        }
