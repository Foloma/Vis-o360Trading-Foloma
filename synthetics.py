from collections import deque
from decimal import Decimal
import time
import logging
import threading

logger = logging.getLogger(__name__)


class DigitAnalyzer:
    """
    Captura 1 dígito a cada 15 segundos.
    Analisa os últimos 20 dígitos com múltiplos padrões:
      1. Streak consecutivo (3+)
      2. Dominância (ex: 15/20 são PAR)
      3. Alternância (0-1-0-1-0-1...)
      4. Distribuição equilibrada (sinal neutro)
    """

    DISPLAY_INTERVAL = 15

    def __init__(self, max_digits=50):
        self.slow_digits = deque(maxlen=max_digits)
        self.current_display_digit = None
        self.current_display_parity = '---'
        self._next_capture_time = time.time() + self.DISPLAY_INTERVAL
        self.countdown = self.DISPLAY_INTERVAL
        self._lock = threading.Lock()

        self.last_analysis_data = {
            'streak': 0,
            'streak_parity': '---',
            'recommended_action': None,
            'confidence': 0,
            'pattern': 'Aguardando primeiros dígitos...',
            'pattern_type': None,
            'alert': None,
            'reason': f'Próximo dígito em {self.DISPLAY_INTERVAL}s...',
            'countdown': self.DISPLAY_INTERVAL,
            'seconds_to_next': self.DISPLAY_INTERVAL,
            'odd_pct': 0,
            'even_pct': 0,
            'recent_parity': [],
            'total_digits': 0
        }

        self._running = True
        self._start_countdown_thread()

    # ── Countdown thread ────────────────────────────────────────
    def _start_countdown_thread(self):
        def _run():
            while self._running:
                time.sleep(0.5)
                remaining = max(1, int(self._next_capture_time - time.time()) + 1)
                with self._lock:
                    self.countdown = remaining
                    self.last_analysis_data['countdown'] = remaining
                    self.last_analysis_data['seconds_to_next'] = remaining
        threading.Thread(target=_run, daemon=True).start()

    # ── Extração correta do dígito ──────────────────────────────
    def _extract_last_digit(self, price):
        """
        ✅ Usa Decimal para evitar zeros artificiais.
        Ex: 1234.567 → '1234.567' → último dígito = 7
            1234.5670 (float) → Decimal → '1234.567' → 7
        """
        try:
            price_str = str(Decimal(str(price)).normalize())
            # Remover notação científica se existir
            if 'E' in price_str or 'e' in price_str:
                price_str = f"{price:.5f}".rstrip('0')
            return int(price_str[-1]) if price_str[-1].isdigit() else 0
        except Exception:
            try:
                return int(f"{price:.3f}"[-1])
            except Exception:
                return 0

    # ── Receber tick ────────────────────────────────────────────
    def add_tick(self, price):
        try:
            now = time.time()
            last_digit = self._extract_last_digit(price)
            parity = 'IMPAR' if last_digit % 2 != 0 else 'PAR'

            with self._lock:
                self.current_display_digit = last_digit
                self.current_display_parity = parity

            if now >= self._next_capture_time:
                self._next_capture_time = now + self.DISPLAY_INTERVAL
                with self._lock:
                    self.slow_digits.append(last_digit)
                    snap = list(self.slow_digits)

                logger.info(f"⏱️ [15s] Dígito: {last_digit} ({parity}) | Total: {len(snap)}")
                self._analyse(snap)

            return True, last_digit
        except Exception as e:
            logger.error(f"Erro no tick: {e}")
            return False, None

    # ── Análise principal ───────────────────────────────────────
    def _analyse(self, snap):
        """
        Analisa os últimos 20 dígitos com 4 padrões distintos.
        O de maior confiança é usado como recomendação.
        """
        total = len(snap)
        window = snap[-20:] if total >= 20 else snap
        w = len(window)

        odd_count  = sum(1 for d in window if d % 2 != 0)
        even_count = w - odd_count
        odd_pct    = round((odd_count / w) * 100, 1) if w > 0 else 0
        even_pct   = round(100 - odd_pct, 1)
        recent_parity = ['IMPAR' if d % 2 != 0 else 'PAR' for d in window]

        candidates = []  # (confidence, action, pattern_type, reason)

        if w < 3:
            self._set_no_signal(snap, odd_pct, even_pct, recent_parity,
                reason=f'Aguardando mais dígitos ({total}/3 mínimo)...')
            return

        # ── 1. STREAK CONSECUTIVO ──────────────────────────────
        streak, streak_parity = self._calc_streak(snap)
        if streak >= 3:
            conf = min(60 + (streak - 3) * 10, 92)
            if streak_parity == 'PAR':
                candidates.append((conf, 'BUY', 'streak',
                    f'🔥 {streak} PARES seguidos → aposte ÍMPAR ({conf}%)'))
            else:
                candidates.append((conf, 'SELL', 'streak',
                    f'🔥 {streak} ÍMPARES seguidos → aposte PAR ({conf}%)'))

        # ── 2. DOMINÂNCIA NOS ÚLTIMOS 20 ──────────────────────
        if w >= 10:
            dom_threshold = 0.70  # 70%+ da mesma paridade
            if odd_pct >= dom_threshold * 100:
                conf = min(55 + int((odd_pct - 70) * 1.5), 85)
                candidates.append((conf, 'SELL', 'dominance',
                    f'📊 {odd_pct}% ÍMPARES nos últimos {w} dígitos → reversão para PAR ({conf}%)'))
            elif even_pct >= dom_threshold * 100:
                conf = min(55 + int((even_pct - 70) * 1.5), 85)
                candidates.append((conf, 'BUY', 'dominance',
                    f'📊 {even_pct}% PARES nos últimos {w} dígitos → reversão para ÍMPAR ({conf}%)'))

        # ── 3. ALTERNÂNCIA (0-1-0-1-0-1...) ──────────────────
        if w >= 6:
            alt_streak = self._calc_alternating(window)
            if alt_streak >= 5:
                conf = min(55 + (alt_streak - 5) * 5, 80)
                # Na alternância, o próximo é o oposto do atual
                last_par = 'IMPAR' if window[-1] % 2 != 0 else 'PAR'
                if last_par == 'IMPAR':
                    candidates.append((conf, 'SELL', 'alternating',
                        f'🔄 Alternância perfeita ({alt_streak} dígitos) → próximo PAR ({conf}%)'))
                else:
                    candidates.append((conf, 'BUY', 'alternating',
                        f'🔄 Alternância perfeita ({alt_streak} dígitos) → próximo ÍMPAR ({conf}%)'))

        # ── 4. DESEQUILÍBRIO MODERADO (aviso) ─────────────────
        if w >= 15 and not candidates:
            if odd_pct >= 60:
                candidates.append((45, 'SELL', 'imbalance',
                    f'⚠️ Leve dominância ÍMPAR ({odd_pct}%) → possível PAR'))
            elif even_pct >= 60:
                candidates.append((45, 'BUY', 'imbalance',
                    f'⚠️ Leve dominância PAR ({even_pct}%) → possível ÍMPAR'))

        if candidates:
            # Usar o candidato de maior confiança
            best = max(candidates, key=lambda x: x[0])
            conf, action, ptype, reason = best

            with self._lock:
                self.last_analysis_data.update({
                    'streak': streak,
                    'streak_parity': streak_parity,
                    'recommended_action': action,
                    'confidence': conf,
                    'pattern': ptype,
                    'pattern_type': ptype,
                    'alert': 'SINAL ATIVO' if conf >= 60 else 'AVISO',
                    'reason': reason,
                    'last_digit': self.current_display_digit,
                    'last_parity': self.current_display_parity,
                    'recent_parity': recent_parity,
                    'odd_pct': odd_pct,
                    'even_pct': even_pct,
                    'total_digits': total,
                    'all_signals': [
                        {'type': c[2], 'action': c[1], 'confidence': c[0], 'reason': c[3]}
                        for c in candidates
                    ]
                })
            logger.info(f"⚡ [{ptype.upper()}] {reason}")
        else:
            self._set_no_signal(snap, odd_pct, even_pct, recent_parity,
                streak=streak, streak_parity=streak_parity,
                reason=f'Distribuição equilibrada ({odd_pct}% ímpar / {even_pct}% par). Streak: {streak}.')

    def _set_no_signal(self, snap, odd_pct=0, even_pct=0,
                       recent_parity=None, streak=0, streak_parity='---', reason=''):
        if recent_parity is None:
            recent_parity = []
        with self._lock:
            self.last_analysis_data.update({
                'streak': streak,
                'streak_parity': streak_parity,
                'recommended_action': None,
                'confidence': 0,
                'pattern': 'equilibrado',
                'pattern_type': None,
                'alert': None,
                'reason': reason,
                'last_digit': self.current_display_digit,
                'last_parity': self.current_display_parity,
                'recent_parity': recent_parity,
                'odd_pct': odd_pct,
                'even_pct': even_pct,
                'total_digits': len(snap),
                'all_signals': []
            })

    # ── Cálculos ────────────────────────────────────────────────
    def _calc_streak(self, digits_list):
        if not digits_list:
            return 0, '---'
        streak = 1
        last_parity = 'IMPAR' if digits_list[-1] % 2 != 0 else 'PAR'
        for i in range(len(digits_list) - 2, -1, -1):
            p = 'IMPAR' if digits_list[i] % 2 != 0 else 'PAR'
            if p == last_parity:
                streak += 1
            else:
                break
        return streak, last_parity

    def _calc_alternating(self, window):
        """Conta quantos dígitos finais formam padrão alternante."""
        if len(window) < 2:
            return 0
        count = 1
        for i in range(len(window) - 1, 0, -1):
            curr_odd = window[i] % 2 != 0
            prev_odd = window[i-1] % 2 != 0
            if curr_odd != prev_odd:
                count += 1
            else:
                break
        return count

    # ── API pública ─────────────────────────────────────────────
    def get_seconds_to_next_digit(self):
        return max(1, int(self._next_capture_time - time.time()) + 1)

    def get_countdown(self):
        return self.countdown

    def get_current_digit(self):
        return self.current_display_digit

    def get_current_parity(self):
        return self.current_display_parity

    def get_next_display_digit(self):
        return self.current_display_digit, self.current_display_parity, self.countdown

    def get_analysis(self):
        with self._lock:
            return dict(self.last_analysis_data)

    def get_recent_digits(self, count=20):
        with self._lock:
            return list(self.slow_digits)[-count:]

    def get_streak_info(self):
        with self._lock:
            snap = list(self.slow_digits)
        return self._calc_streak(snap)

    def get_stats(self):
        with self._lock:
            snap = list(self.slow_digits)
        if not snap:
            return {'total': 0, 'odd_pct': 0, 'even_pct': 0,
                    'current_streak': 0, 'streak_parity': '---', 'recent': []}
        total = len(snap)
        odd_count = sum(1 for d in snap if d % 2 != 0)
        streak, streak_parity = self._calc_streak(snap)
        return {
            'total': total,
            'odd_pct': round((odd_count / total) * 100, 1),
            'even_pct': round(100 - (odd_count / total) * 100, 1),
            'current_streak': streak,
            'streak_parity': streak_parity,
            'recent': snap[-20:]
        }


digit_analyzer = DigitAnalyzer(max_digits=50)
