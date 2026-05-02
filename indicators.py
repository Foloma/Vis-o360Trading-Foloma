from collections import deque
from decimal import Decimal, InvalidOperation
import time
import logging
import threading

logger = logging.getLogger(__name__)


class DigitAnalyzer:
    TICKS_PER_DIGIT = 10   # 1 dígito lento a cada 10 ticks ≈ 10 segundos

    def __init__(self, max_digits=500):
        self.slow_digits   = deque(maxlen=max_digits)
        self.current_digit  = None
        self.current_parity = '---'

        # Contagem de ticks para sincronização
        self._tick_count       = 0   # ticks totais recebidos
        self._ticks_in_cycle   = 0   # posição dentro do ciclo actual (0..N-1)
        self._digit_counter    = 0   # número de dígitos lentos capturados

        self._lock = threading.RLock()  # ✅ reentrant lock

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
            'all_signals': [], 'digit_counter': 0
        }

    # ── Extracção correcta do dígito ───────────────────────────
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

    # ── Receber tick (chamado pelo deriv_client a cada tick) ────
    def add_tick(self, price):
        """
        Chamado a cada tick do WebSocket.
        Conta ticks reais. A cada TICKS_PER_DIGIT ticks captura um dígito lento.
        ✅ Protegido contra race conditions.
        """
        try:
            digit  = self._extract_last_digit(price)
            parity = 'IMPAR' if digit % 2 != 0 else 'PAR'
            should_analyse = False
            snap = None

            # Toda a atualização de estado fica dentro do lock
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
                    should_analyse = True

            # Chamar a análise fora do lock para não prender o resto
            if should_analyse:
                logger.info(f"⏱️ [tick #{self._tick_count}] Dígito lento #{self._digit_counter}: {digit} ({parity})")
                self._analyse(snap)

            return True, digit
        except Exception as e:
            logger.error(f"Erro tick: {e}")
            return False, None

    # ── Análise dos últimos 20 dígitos ─────────────────────────
    def _analyse(self, snap):
        total  = len(snap)
        window = snap[-20:]
        w      = len(window)
        odd_c  = sum(1 for d in window if d % 2 != 0)
        even_c = w - odd_c
        odd_pct  = round(odd_c  / w * 100, 1) if w else 0
        even_pct = round(even_c / w * 100, 1) if w else 0
        rec_par  = ['IMPAR' if d % 2 != 0 else 'PAR' for d in window]
        streak, sp = self._calc_streak(snap)
        candidates = []

        if w < 3:
            self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                reason=f'Aguardando ({total}/3 mínimo)...')
            return

        # 1. Streak consecutivo
        if streak >= 3:
            conf = min(60 + (streak - 3) * 8, 92)
            if sp == 'PAR':
                candidates.append((conf, 'BUY', 'streak', f'🔥 {streak} PARES seguidos → aposte ÍMPAR ({conf}%)'))
            else:
                candidates.append((conf, 'SELL', 'streak', f'🔥 {streak} ÍMPARES seguidos → aposte PAR ({conf}%)'))

        # 2. Dominância nos últimos 20
        if w >= 10:
            if odd_pct >= 70:
                conf = min(55 + int((odd_pct - 70) * 1.5), 85)
                candidates.append((conf, 'SELL', 'dominance', f'📊 {odd_pct}% ÍMPARES → reversão PAR ({conf}%)'))
            elif even_pct >= 70:
                conf = min(55 + int((even_pct - 70) * 1.5), 85)
                candidates.append((conf, 'BUY', 'dominance', f'📊 {even_pct}% PARES → reversão ÍMPAR ({conf}%)'))

        # 3. Alternância
        if w >= 6:
            alt = self._calc_alternating(window)
            if alt >= 5:
                conf = min(55 + (alt - 5) * 5, 80)
                if window[-1] % 2 != 0:
                    candidates.append((conf, 'SELL', 'alternating', f'🔄 Alternância {alt} → PAR ({conf}%)'))
                else:
                    candidates.append((conf, 'BUY', 'alternating', f'🔄 Alternância {alt} → ÍMPAR ({conf}%)'))

        # 4. Desequilíbrio moderado
        if w >= 15 and not candidates:
            if odd_pct >= 62:
                candidates.append((42, 'SELL', 'imbalance', f'⚠️ {odd_pct}% ÍMPAR → possível PAR'))
            elif even_pct >= 62:
                candidates.append((42, 'BUY', 'imbalance', f'⚠️ {even_pct}% PAR → possível ÍMPAR'))

        with self._lock:
            if candidates:
                best = max(candidates, key=lambda x: x[0])
                conf, action, ptype, reason = best
                self.last_analysis.update({
                    'streak': streak, 'streak_parity': sp,
                    'recommended_action': action, 'confidence': conf,
                    'pattern_type': ptype,
                    'alert': 'SINAL ATIVO' if conf >= 60 else 'AVISO',
                    'reason': reason,
                    'recent_parity': rec_par,
                    'odd_pct': odd_pct, 'even_pct': even_pct,
                    'total_digits': total,
                    'all_signals': [{'type':c[2],'action':c[1],'confidence':c[0],'reason':c[3]} for c in candidates],
                    'digit_counter': self._digit_counter
                })
            else:
                self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                    streak=streak, streak_parity=sp,
                                    reason=f'Equilíbrio: {odd_pct}% ímpar / {even_pct}% par. Streak: {streak}.')

    def _set_no_signal(self, snap, odd_pct=0, even_pct=0, rec_par=None,
                       streak=0, streak_parity='---', reason=''):
        if rec_par is None:
            rec_par = []
        with self._lock:
            self.last_analysis.update({
                'streak': streak, 'streak_parity': streak_parity,
                'recommended_action': None, 'confidence': 0,
                'pattern_type': None, 'alert': None, 'reason': reason,
                'recent_parity': rec_par,
                'odd_pct': odd_pct, 'even_pct': even_pct,
                'total_digits': len(snap), 'all_signals': [],
                'digit_counter': self._digit_counter
            })

    def _calc_streak(self, lst):
        if not lst:
            return 0, '---'
        streak = 1
        lp = 'IMPAR' if lst[-1] % 2 != 0 else 'PAR'
        for i in range(len(lst) - 2, -1, -1):
            p = 'IMPAR' if lst[i] % 2 != 0 else 'PAR'
            if p == lp:
                streak += 1
            else:
                break
        return streak, lp

    def _calc_alternating(self, window):
        if len(window) < 2:
            return 0
        count = 1
        for i in range(len(window) - 1, 0, -1):
            if (window[i] % 2 != 0) != (window[i-1] % 2 != 0):
                count += 1
            else:
                break
        return count

    # ── API pública ────────────────────────────────────────────
    def get_ticks_remaining(self):
        with self._lock:
            tr = self.TICKS_PER_DIGIT - (self._tick_count % self.TICKS_PER_DIGIT)
            return tr if tr > 0 else self.TICKS_PER_DIGIT

    def get_seconds_to_next_digit(self):
        return self.get_ticks_remaining()

    def get_countdown(self):
        return self.get_ticks_remaining()

    def get_digit_counter(self):
        return self._digit_counter

    def get_current_digit(self):
        return self.current_digit

    def get_current_parity(self):
        return self.current_parity

    def get_next_display_digit(self):
        return self.current_digit, self.current_parity, self.get_ticks_remaining()

    def get_analysis(self):
        with self._lock:
            return dict(self.last_analysis)

    def get_recent_digits(self, count=500):
        with self._lock:
            return list(self.slow_digits)

    def get_streak_info(self):
        with self._lock:
            snap = list(self.slow_digits)
        return self._calc_streak(snap)

    def get_stats(self):
        with self._lock:
            snap = list(self.slow_digits)
        if not snap:
            return {'total':0,'odd_pct':0,'even_pct':0,'current_streak':0,'streak_parity':'---','recent':[]}
        total = len(snap)
        odd_c = sum(1 for d in snap if d % 2 != 0)
        streak, sp = self._calc_streak(snap)
        return {'total':total,'odd_pct':round(odd_c/total*100,1),
                'even_pct':round((total-odd_c)/total*100,1),
                'current_streak':streak,'streak_parity':sp,'recent':snap[-20:]}


digit_analyzer = DigitAnalyzer(max_digits=500)
