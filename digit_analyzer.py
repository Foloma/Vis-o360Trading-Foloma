from collections import deque
from decimal import Decimal, InvalidOperation
import time
import logging
import threading
import math

logger = logging.getLogger(__name__)


class DigitAnalyzer:
    """
    Analisador de dígitos para contratos DIGITODD/DIGITEVEN e DIGITDIFFER.

    CONVENÇÃO:
    - 'BUY'  / 'CALL'  = ÍMPAR (DIGITODD)
    - 'SELL' / 'PUT'   = PAR   (DIGITEVEN)
    - 'DIFFER'         = Aposta que um dígito específico NÃO sairá (DIGITDIFF)
    """

    TICKS_PER_DIGIT = 10

    def __init__(self, max_digits=1000):
        self.slow_digits   = deque(maxlen=max_digits)
        self.current_digit  = None
        self.current_parity = '---'

        self._tick_count       = 0
        self._ticks_in_cycle   = 0
        self._digit_counter    = 0

        self._lock = threading.RLock()

        # NOVO: rastreamento de frequência individual por dígito (0-9)
        self._digit_window = deque(maxlen=100)  # últimos 100 dígitos para frequência
        self._digit_counts = {i: 0 for i in range(10)}

        self.last_analysis = {
            'streak': 0, 'streak_parity': '---',
            'recommended_action': None, 'confidence': 0,
            'pattern_type': None, 'alert': None,
            'reason': 'Aguardando primeiros dígitos...',
            'ticks_remaining': self.TICKS_PER_DIGIT,
            'ticks_in_cycle': 0,
            'ticks_per_digit': self.TICKS_PER_DIGIT,
            'odd_pct': 0, 'even_pct': 0,
            'recent_parity': [], 'total_digits': 0,
            'all_signals': [], 'digit_counter': 0,
            'sequences': None,
            'entropy': 0.0,
            'entropy_verdict': '---',
            'least_frequent_digit': None,
            'digit_frequencies': {i: 0 for i in range(10)}
        }

    def _extract_last_digit(self, price):
        try:
            s = str(Decimal(str(price)).normalize())
            if 'E' in s or 'e' in s:
                s = f"{float(price):.6f}".rstrip('0')
            for ch in reversed(s):
                if ch.isdigit():
                    return int(ch)
            return 0
        except Exception:
            try:
                return int(f"{float(price):.3f}"[-1])
            except:
                return 0

    def _calculate_entropy(self, digits):
        if not digits or len(digits) < 2:
            return 0.0
        total = len(digits)
        frequencies = {}
        for d in digits:
            frequencies[d] = frequencies.get(d, 0) + 1
        entropy = 0.0
        for count in frequencies.values():
            if count > 0:
                prob = count / total
                entropy -= prob * math.log2(prob)
        max_entropy = math.log2(10)
        if max_entropy > 0:
            normalized = entropy / max_entropy
        else:
            normalized = 0.0
        return min(normalized, 1.0)

    def add_tick(self, price):
        try:
            digit  = self._extract_last_digit(price)
            parity = 'IMPAR' if digit % 2 != 0 else 'PAR'
            should_analyse = False
            snap = None

            with self._lock:
                self._tick_count += 1
                self._ticks_in_cycle = self._tick_count % self.TICKS_PER_DIGIT
                self.current_digit  = digit
                self.current_parity = parity
                ticks_remaining = self.TICKS_PER_DIGIT - self._ticks_in_cycle
                if ticks_remaining == 0:
                    ticks_remaining = self.TICKS_PER_DIGIT
                self.last_analysis['ticks_remaining'] = ticks_remaining
                self.last_analysis['ticks_in_cycle']  = self._ticks_in_cycle

                if self._ticks_in_cycle == 0:
                    self._digit_counter += 1
                    self.slow_digits.append(digit)
                    snap = list(self.slow_digits)
                    self.last_analysis['digit_counter'] = self._digit_counter

                    # NOVO: atualizar janela de frequência
                    self._update_frequency(digit)

                    should_analyse = True

            if should_analyse:
                logger.info(f"⏱️ [tick #{self._tick_count}] Dígito lento #{self._digit_counter}: {digit} ({parity})")
                self._analyse(snap)

            return True, digit
        except Exception as e:
            logger.error(f"Erro tick: {e}")
            return False, None

    def _update_frequency(self, digit):
        """Atualiza a contagem de frequência individual de dígitos (janela de 100)."""
        with self._lock:
            if len(self._digit_window) >= 100:
                old = self._digit_window[0]
                self._digit_counts[old] = max(0, self._digit_counts[old] - 1)
            self._digit_window.append(digit)
            self._digit_counts[digit] = self._digit_counts.get(digit, 0) + 1

    # ... (métodos _find_sequences, _analyse, _calc_streak, etc. mantidos exatamente iguais ao último envio)

    # ---------- NOVOS MÉTODOS PARA DIGITDIFFER ----------

    def get_least_frequent_digit(self):
        """
        Retorna o dígito (0-9) que menos apareceu nos últimos 100 ticks.
        Requer pelo menos 50 ticks acumulados para ser fiável.
        """
        with self._lock:
            if len(self._digit_window) < 50:
                return None
            # Contar frequências atuais
            total = len(self._digit_window)
            frequencies = {}
            for d in list(self._digit_window):
                frequencies[d] = frequencies.get(d, 0) + 1
            # Encontrar o menos frequente
            if len(frequencies) < 10:
                return None  # ainda não temos todos os dígitos
            least = min(frequencies.items(), key=lambda x: x[1])
            # Só retorna se for significativamente abaixo do esperado (10%)
            expected = total / 10
            if least[1] < expected * 0.6:  # 40% abaixo do esperado
                return least[0]
            return None

    def get_digit_frequencies(self):
        """Retorna as frequências percentuais de cada dígito (0-9) para a UI."""
        with self._lock:
            total = len(self._digit_window) if len(self._digit_window) > 0 else 1
            return {d: round(self._digit_counts[d] / total * 100, 1) for d in range(10)}

    # ... (resto dos métodos mantidos)
