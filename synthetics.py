from collections import deque
from decimal import Decimal, InvalidOperation
import time
import logging
import threading
import math

logger = logging.getLogger(__name__)


class DigitAnalyzer:
    TICKS_PER_DIGIT = 10

    def __init__(self, max_digits=1000):
        self.slow_digits   = deque(maxlen=max_digits)
        self.current_digit  = None
        self.current_parity = '---'

        self._tick_count       = 0
        self._ticks_in_cycle   = 0
        self._digit_counter    = 0

        self._lock = threading.RLock()

        # Rastreamento de frequência individual por dígito (0-9) — janela de 70 ticks
        self._digit_window = deque(maxlen=70)
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
            'most_frequent_digit': None,
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
        with self._lock:
            if len(self._digit_window) >= 70:
                old = self._digit_window[0]
                self._digit_counts[old] = max(0, self._digit_counts[old] - 1)
            self._digit_window.append(digit)
            self._digit_counts[digit] = self._digit_counts.get(digit, 0) + 1

    def get_least_frequent_digit(self):
        with self._lock:
            if len(self._digit_window) < 20:
                return None
            total = len(self._digit_window)
            least_digit = min(self._digit_counts, key=self._digit_counts.get)
            least_count = self._digit_counts[least_digit]
            expected = total / 10
            if least_count < expected * 0.8:
                return least_digit
            return None

    def get_most_frequent_digit(self):
        """
        Retorna o dígito mais frequente para MATCHES apenas se:
        - Janela >= 50 ticks
        - Frequência >= 20%
        - Apareceu pelo menos 2 vezes nos últimos 5 dígitos lentos
        """
        with self._lock:
            if len(self._digit_window) < 50:
                return None
            total = len(self._digit_window)
            most_digit = max(self._digit_counts, key=self._digit_counts.get)
            most_count = self._digit_counts[most_digit]
            pct = (most_count / total) * 100

            if pct < 20:
                return None

            recent_5 = list(self.slow_digits)[-5:] if len(self.slow_digits) >= 5 else []
            if recent_5.count(most_digit) < 2:
                return None

            logger.info(f"MATCHES disponível: dígito {most_digit} ({most_count}/{total} = {pct:.1f}%)")
            return most_digit

    def get_digit_frequencies(self):
        with self._lock:
            total = len(self._digit_window) if len(self._digit_window) > 0 else 1
            return {d: round(self._digit_counts[d] / total * 100, 1) for d in range(10)}

    def _find_sequences(self, digits):
        if not digits:
            return None
        streak, parity = self._calc_streak(digits)
        descricao = f"{streak} {parity}S consecutivos" if streak >= 2 else None
        return {'atual': streak, 'tipo': parity, 'descricao': descricao}

    def _analyse(self, snap):
        total  = len(snap)
        window = snap[-100:]
        w      = len(window)
        odd_c  = sum(1 for d in window if d % 2 != 0)
        even_c = w - odd_c
        odd_pct  = round(odd_c  / w * 100, 1) if w else 0
        even_pct = round(even_c / w * 100, 1) if w else 0
        rec_par  = ['IMPAR' if d % 2 != 0 else 'PAR' for d in window]
        streak, sp = self._calc_streak(snap)
        seq_info = self._find_sequences(snap)

        entropy = self._calculate_entropy(snap[-100:])

        last10 = snap[-10:] if len(snap) >= 10 else snap
        if len(last10) == 10:
            odd_in_10 = sum(1 for d in last10 if d % 2 != 0)
            if odd_in_10 >= 7 or odd_in_10 <= 3:
                self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                    streak=streak, streak_parity=sp,
                                    reason='Rajada anómala nos últimos 10 dígitos',
                                    sequences=seq_info, entropy=entropy)
                return

        candidates = []

        if w < 5:
            self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                streak=streak, streak_parity=sp,
                                reason=f'Aguardando ({total}/5 mínimo)...',
                                sequences=seq_info, entropy=entropy)
            return

        if streak >= 4:
            base_conf = min(65 + (streak - 4) * 10, 90)
            conf = self._apply_entropy_penalty(base_conf, entropy)
            if conf >= 55:
                if sp == 'PAR':
                    candidates.append((conf, 'BUY', 'streak', f'🔥 {streak} PARES seguidos → aposte ÍMPAR ({conf:.0f}%)'))
                else:
                    candidates.append((conf, 'SELL', 'streak', f'🔥 {streak} ÍMPARES seguidos → aposte PAR ({conf:.0f}%)'))

        if w >= 20:
            if odd_pct >= 75:
                base_conf = min(60 + int((odd_pct - 75) * 1.8), 85)
                conf = self._apply_entropy_penalty(base_conf, entropy)
                if conf >= 55:
                    candidates.append((conf, 'SELL', 'dominance', f'📊 {odd_pct}% ÍMPARES → reversão PAR ({conf:.0f}%)'))
            elif even_pct >= 75:
                base_conf = min(60 + int((even_pct - 75) * 1.8), 85)
                conf = self._apply_entropy_penalty(base_conf, entropy)
                if conf >= 55:
                    candidates.append((conf, 'BUY', 'dominance', f'📊 {even_pct}% PARES → reversão ÍMPAR ({conf:.0f}%)'))

        if w >= 10:
            alt = self._calc_alternating(window)
            if alt >= 6:
                base_conf = min(55 + (alt - 6) * 7, 80)
                conf = self._apply_entropy_penalty(base_conf, entropy)
                if conf >= 55:
                    if window[-1] % 2 != 0:
                        candidates.append((conf, 'SELL', 'alternating', f'🔄 Alternância {alt} → PAR ({conf:.0f}%)'))
                    else:
                        candidates.append((conf, 'BUY', 'alternating', f'🔄 Alternância {alt} → ÍMPAR ({conf:.0f}%)'))

        if w >= 30:
            if odd_pct >= 65 and streak >= 2:
                base_conf = 60
                conf = self._apply_entropy_penalty(base_conf, entropy)
                if conf >= 55:
                    candidates.append((conf, 'SELL', 'imbalance', f'⚠️ {odd_pct}% ÍMPAR + streak {streak} → possível PAR'))
            elif even_pct >= 65 and streak >= 2:
                base_conf = 60
                conf = self._apply_entropy_penalty(base_conf, entropy)
                if conf >= 55:
                    candidates.append((conf, 'BUY', 'imbalance', f'⚠️ {even_pct}% PAR + streak {streak} → possível ÍMPAR'))

        with self._lock:
            if len(candidates) >= 2:
                best_two = sorted(candidates, key=lambda x: x[0], reverse=True)[:2]
                if best_two[0][1] == best_two[1][1]:
                    conf = (best_two[0][0] + best_two[1][0]) / 2
                    action = best_two[0][1]
                    ptype = f"{best_two[0][2]}+{best_two[1][2]}"
                    reason = f"🤝 Consenso: {best_two[0][3]} | {best_two[1][3]}"
                else:
                    self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                        streak=streak, streak_parity=sp,
                                        reason='Padrões discordantes',
                                        sequences=seq_info, entropy=entropy)
                    return
            elif len(candidates) == 1:
                best = candidates[0]
                if best[0] >= 65:
                    conf, action, ptype, reason = best
                else:
                    self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                        streak=streak, streak_parity=sp,
                                        reason=f'Confiança insuficiente ({best[0]:.0f}%)',
                                        sequences=seq_info, entropy=entropy)
                    return
            else:
                self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                    streak=streak, streak_parity=sp,
                                    reason='Nenhum padrão significativo',
                                    sequences=seq_info, entropy=entropy)
                return

            entropy_verdict = self._get_entropy_verdict(entropy)
            self.last_analysis.update({
                'streak': streak, 'streak_parity': sp,
                'recommended_action': action,
                'confidence': round(conf, 1),
                'pattern_type': ptype,
                'alert': 'SINAL ATIVO' if conf >= 60 else 'AVISO',
                'reason': reason,
                'recent_parity': rec_par,
                'odd_pct': odd_pct, 'even_pct': even_pct,
                'total_digits': total,
                'all_signals': [{'type':c[2],'action':c[1],'confidence':c[0],'reason':c[3]} for c in candidates],
                'digit_counter': self._digit_counter,
                'sequences': seq_info,
                'entropy': round(entropy, 3),
                'entropy_verdict': entropy_verdict
            })

    def _apply_entropy_penalty(self, base_confidence, entropy):
        if entropy > 0.97:
            return 0.0
        elif entropy > 0.93:
            penalty = 0.35
        elif entropy > 0.88:
            penalty = 0.20
        else:
            penalty = 0.0
        return base_confidence * (1 - penalty)

    def _get_entropy_verdict(self, entropy):
        if entropy > 0.97:
            return 'ALEATÓRIO (não operar)'
        elif entropy > 0.93:
            return 'IMPREVISÍVEL (cautela)'
        elif entropy > 0.88:
            return 'PREVISÍVEL'
        else:
            return 'BEM DEFINIDO'

    def _set_no_signal(self, snap, odd_pct=0, even_pct=0, rec_par=None,
                       streak=0, streak_parity='---', reason='', sequences=None, entropy=0.0):
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
                'digit_counter': self._digit_counter,
                'sequences': sequences,
                'entropy': round(entropy, 3),
                'entropy_verdict': self._get_entropy_verdict(entropy)
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

    def get_ticks_remaining(self):
        with self._lock:
            tr = self.TICKS_PER_DIGIT - (self._tick_count % self.TICKS_PER_DIGIT)
            return tr if tr > 0 else self.TICKS_PER_DIGIT

    def get_digit_counter(self):
        with self._lock:
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


digit_analyzer = DigitAnalyzer(max_digits=1000)
