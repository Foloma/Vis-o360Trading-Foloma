import logging
import time

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Implementa os três módulos da Foloma Visão 360 Smart Flow.
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
        self._parity_odd_used = False
        self._parity_even_used = False
        self._parity_martingale_used = False
        self._last_parity_streak_type = None
        self._matches_sequence_used = False
        self._matches_cooldown_until = 0
        self._price_history = []

        self._differ_signal_at = 0
        self._parity_signal_at = 0
        self._matches_signal_at = 0

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
            return False, f"Cooldown ativo ({self._cooldown_until - time.time():.0f}s)"
        if not self.client or not self.client.authorized:
            return False, "Não autorizado"
        if not self.client.streaming:
            return False, "Sem streaming"
        if time.time() - getattr(self.client, '_last_reconnect_time', 0) < 10:
            return False, "Reconexão recente"
        if getattr(self.client, '_ping_ms', 0) > 250:
            return False, f"Latência alta ({self.client._ping_ms}ms)"
        stable, reason = self.is_market_stable()
        if not stable:
            return False, reason
        return True, "OK"

    def is_market_stable(self):
        if len(self._price_history) >= 5:
            recent = self._price_history[-5:]
            avg_price = sum(recent) / len(recent)
            for price in recent:
                variation = abs(price - avg_price) / avg_price if avg_price > 0 else 0
                if variation > 0.002:
                    return False, f"Spike detetado (variação {variation:.3%})"
        return True, "OK"

    def on_tick(self, tick):
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
        self._differ_signal_at = 0
        self._parity_signal_at = 0
        self._matches_signal_at = 0

    def _is_entry_window_valid(self, signal_at, max_age=20):
        if signal_at == 0:
            return True
        return time.time() - signal_at <= max_age

    def notify_result(self, action, is_win):
        if not is_win:
            self._consecutive_losses += 1
            if self._consecutive_losses >= 2:
                self._global_stop_until = time.time() + 180
                logger.warning("🛑 STOP GLOBAL: 2 perdas consecutivas — pausa 3 min")
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
        if not self._is_entry_window_valid(self._differ_signal_at):
            return False, None
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 2:
            return False, None
        last_two = recent[-2:]
        if last_two[0] == last_two[1]:
            digit = last_two[0]
            last_ten = recent[-10:] if len(recent) >= 10 else recent
            if last_ten.count(digit) >= 2:
                available = digit not in self._differ_sequence_used
                return available, digit if available else None
        return False, None

    def _peek_parity(self):
        ok, reason = self.can_trade
        if not ok:
            return False, None, reason
        if not self._is_entry_window_valid(self._parity_signal_at):
            return False, None, "Janela de entrada expirada"
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 4:
            return False, None, "Aguardando dados"
        last_four = [d % 2 != 0 for d in recent[-4:]]
        odd_count = sum(last_four)
        even_count = 4 - odd_count
        if odd_count >= 3:
            can_enter = not self._parity_odd_used or (
                self._parity_odd_used and not self._parity_martingale_used and self._last_parity_streak_type == 'odd'
            )
            return can_enter, 'even', f"Tendência ÍMPAR ({odd_count}/4)"
        if even_count >= 3:
            can_enter = not self._parity_even_used or (
                self._parity_even_used and not self._parity_martingale_used and self._last_parity_streak_type == 'even'
            )
            return can_enter, 'odd', f"Tendência PAR ({even_count}/4)"
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
    # Módulo 1: DIFFER
    # -----------------------------------------------------------------
    def evaluate_differ(self):
        ok, reason = self.can_trade
        if not ok:
            logger.info(f"⛔ DIFFER bloqueado: {reason}")
            return None, reason

        if not self._is_entry_window_valid(self._differ_signal_at):
            logger.info("⛔ DIFFER bloqueado: janela de entrada expirada")
            return None, "Janela de entrada expirada"

        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 2:
            return None, "Aguardando dados"

        last_two = recent[-2:]
        if last_two[0] == last_two[1]:
            digit = last_two[0]
            last_ten = recent[-10:] if len(recent) >= 10 else recent
            count = last_ten.count(digit)
            if count >= 2:
                if digit in self._differ_sequence_used:
                    logger.info(f"⛔ DIFFER bloqueado: dígito {digit} já utilizado")
                    return None, f"Dígito {digit} já utilizado nesta sequência"
                self._differ_sequence_used.add(digit)
                self._last_differ_digit = digit
                self._differ_signal_at = time.time()
                logger.info(f"✅ DIFFER SINAL: dígito {digit} — {last_two[0]}{last_two[1]} consecutivos + {count}/10 dominância")
                return digit, f"DIFFER {digit}: {last_two[0]}{last_two[1]} consecutivos + dominância {count}/10"

        if len(recent) >= 3 and recent[-3] != recent[-2]:
            self._differ_sequence_used.clear()
        logger.debug(f"⛔ DIFFER: nenhum padrão — últimos 2: {last_two}")
        return None, "Nenhum padrão DIFFER"

    # -----------------------------------------------------------------
    # Módulo 2: PAR/ÍMPAR com martingale condicional
    # -----------------------------------------------------------------
    def _can_martingale(self):
        ping = getattr(self.client, '_ping_ms', 0)
        if ping >= 150:
            logger.info(f"⛔ Martingale bloqueado: ping alto ({ping}ms)")
            return False
        stable, _ = self.is_market_stable()
        if not stable:
            logger.info("⛔ Martingale bloqueado: mercado instável")
            return False
        if time.time() - getattr(self.client, '_last_reconnect_time', 0) < 10:
            logger.info("⛔ Martingale bloqueado: reconexão recente")
            return False
        return True

    def evaluate_parity(self):
        ok, reason = self.can_trade
        if not ok:
            logger.info(f"⛔ PAR/ÍMPAR bloqueado: {reason}")
            return None, reason

        if not self._is_entry_window_valid(self._parity_signal_at):
            logger.info("⛔ PAR/ÍMPAR bloqueado: janela de entrada expirada")
            return None, "Janela de entrada expirada"

        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 4:
            return None, "Aguardando dados"

        last_four = [d % 2 != 0 for d in recent[-4:]]
        odd_count = sum(last_four)
        even_count = 4 - odd_count
        logger.info(f"🔍 PAR/ÍMPAR: últimos 4={recent[-4:]}, ímpares={odd_count}, pares={even_count}")

        if odd_count >= 3:
            if not self._parity_odd_used:
                self._parity_odd_used = True
                self._last_parity_streak_type = 'odd'
                self._parity_martingale_used = False
                self._parity_signal_at = time.time()
                logger.info(f"✅ PAR/ÍMPAR SINAL: {odd_count}/4 ímpares → ENTRAR PAR")
                return 'even', f"Tendência ÍMPAR ({odd_count}/4) → apostar PAR"
            if not self._parity_martingale_used and self._last_parity_streak_type == 'odd':
                if self._can_martingale():
                    self._parity_martingale_used = True
                    self._parity_signal_at = time.time()
                    logger.info("✅ PAR/ÍMPAR MARTINGALE: autorizado")
                    return 'even', "Martingale: streak ÍMPAR continua → apostar PAR novamente"
                else:
                    return None, "Martingale bloqueado por condições de mercado"
            return None, "Streak ÍMPAR já utilizado"

        if even_count >= 3:
            if not self._parity_even_used:
                self._parity_even_used = True
                self._last_parity_streak_type = 'even'
                self._parity_martingale_used = False
                self._parity_signal_at = time.time()
                logger.info(f"✅ PAR/ÍMPAR SINAL: {even_count}/4 pares → ENTRAR ÍMPAR")
                return 'odd', f"Tendência PAR ({even_count}/4) → apostar ÍMPAR"
            if not self._parity_martingale_used and self._last_parity_streak_type == 'even':
                if self._can_martingale():
                    self._parity_martingale_used = True
                    self._parity_signal_at = time.time()
                    logger.info("✅ PAR/ÍMPAR MARTINGALE: autorizado")
                    return 'odd', "Martingale: streak PAR continua → apostar ÍMPAR novamente"
                else:
                    return None, "Martingale bloqueado por condições de mercado"
            return None, "Streak PAR já utilizado"

        self._parity_odd_used = False
        self._parity_even_used = False
        self._parity_martingale_used = False
        self._last_parity_streak_type = None
        logger.debug("⛔ PAR/ÍMPAR: nenhuma tendência clara")
        return None, "Nenhuma tendência clara"

    # -----------------------------------------------------------------
    # Módulo 3: MATCHES
    # -----------------------------------------------------------------
    def evaluate_matches(self):
        ok, reason = self.can_trade
        if not ok:
            logger.info(f"⛔ MATCHES bloqueado: {reason}")
            return None, reason

        if self.is_matches_cooldown:
            remaining = self._matches_cooldown_until - time.time()
            logger.info(f"⛔ MATCHES bloqueado: cooldown ativo ({remaining:.0f}s restantes)")
            return None, f"Cooldown MATCHES ativo ({remaining:.0f}s)"

        absence = getattr(self.analyzer, 'get_digit_absence_counts', None)
        if not absence:
            return None, "Contador de ausência indisponível"

        for digit, count in absence().items():
            if count >= 15 and not self._matches_sequence_used:
                self._matches_sequence_used = True
                self._last_matches_digit = digit
                self._matches_signal_at = time.time()
                logger.info(f"✅ MATCHES SINAL: dígito {digit} ausente há {count} ticks")
                return digit, f"Dígito {digit} ausente há {count} ticks"
        self._matches_sequence_used = False
        logger.debug("⛔ MATCHES: nenhum dígito ausente ≥15 ticks")
        return None, "Nenhum dígito ausente ≥15 ticks"

    # -----------------------------------------------------------------
    # Status para o frontend — com matches_reason
    # -----------------------------------------------------------------
    def get_status(self):
        differ_avail, differ_digit = self._peek_differ()
        parity_avail, parity_dir, parity_reason = self._peek_parity()
        matches_avail = self._peek_matches()

        # Razão do MATCHES
        if self.is_matches_cooldown:
            matches_reason = f"Cooldown {self._matches_cooldown_until - time.time():.0f}s"
        elif not matches_avail:
            matches_reason = "Nenhum dígito ausente ≥15 ticks"
        else:
            matches_reason = "Disponível"

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
            'matches_reason': matches_reason,
        }
