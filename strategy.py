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
        self._last_differ_digit = None       # dígito usado no último DIFFER
        self._last_parity_action = None      # 'odd' ou 'even' da última entrada
        self._last_matches_digit = None
        self._consecutive_losses = 0
        self._global_stop_until = 0
        self._cooldown_until = 0             # cooldown genérico pós-perda/ganho
        self._differ_sequence_used = set()   # dígitos já usados na sequência atual
        self._parity_sequence_used = False
        self._matches_sequence_used = False

    # -----------------------------------------------------------------
    # Propriedades de estado (usadas pelo frontend)
    # -----------------------------------------------------------------
    @property
    def is_global_stop(self):
        return time.time() < self._global_stop_until

    @property
    def is_cooldown(self):
        return time.time() < self._cooldown_until

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
        """Converte ticks em segundos (assumindo ~1 segundo por tick)."""
        self._cooldown_until = time.time() + ticks

    def reset_sequence_state(self):
        self._differ_sequence_used.clear()
        self._parity_sequence_used = False
        self._matches_sequence_used = False

    def notify_result(self, action, is_win):
        """Chamado pelo TradingBot após cada trade."""
        if not is_win:
            self._consecutive_losses += 1
            if self._consecutive_losses >= 2:
                self._global_stop_until = time.time() + 180  # 3 minutos
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
                self._apply_cooldown(10)
        else:
            self._consecutive_losses = 0
            if action.startswith('DIFFER'):
                self._apply_cooldown(1)   # pequeno cooldown pós-ganho
            elif action in ('CALL', 'PUT', 'BUY', 'SELL'):
                self._apply_cooldown(2)
            # MATCHES não tem cooldown pós-ganho, pois é raro

    # -----------------------------------------------------------------
    # Módulo 1: DIFFER por repetição (>=3 iguais consecutivos)
    # -----------------------------------------------------------------
    def evaluate_differ(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        recent = self.analyzer.get_recent_digits(min(20, len(self.analyzer.slow_digits)))
        if len(recent) < 3:
            return None, "Aguardando dados"

        last_three = recent[-3:]
        if len(set(last_three)) == 1:
            digit = last_three[0]
            # limitador: não repetir o mesmo dígito na mesma sequência
            if digit in self._differ_sequence_used:
                return None, f"Dígito {digit} já utilizado nesta sequência"
            self._differ_sequence_used.add(digit)
            self._last_differ_digit = digit
            return digit, f"Repetição detetada: {digit}{digit}{digit}"
        else:
            # reset do limitador quando a sequência quebra
            self._differ_sequence_used.clear()
            return None, "Nenhuma repetição tripla"

    # -----------------------------------------------------------------
    # Módulo 2: PAR/ÍMPAR por streak (>=4 consecutivos)
    # -----------------------------------------------------------------
    def evaluate_parity(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        recent = self.analyzer.get_recent_digits(min(20, len(self.analyzer.slow_digits)))
        if len(recent) < 4:
            return None, "Aguardando dados"

        # obter paridades dos últimos 4 dígitos (True=Ímpar, False=Par)
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
    # Módulo 3: MATCHES por ausência (>=25 ticks)
    # -----------------------------------------------------------------
    def evaluate_matches(self):
        ok, reason = self.can_trade
        if not ok:
            return None, reason

        # O contador de ausência será mantido pelo DigitAnalyzer (synthetics.py)
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
        return {
            'global_stop': self.is_global_stop,
            'cooldown': self.is_cooldown,
            'consecutive_losses': self._consecutive_losses,
            'differ_available': self.evaluate_differ()[0] is not None,
            'parity_available': self.evaluate_parity()[0] is not None,
            'matches_available': self.evaluate_matches()[0] is not None,
        }
