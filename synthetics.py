from collections import deque
from decimal import Decimal, InvalidOperation
import time
import logging
import threading
import math

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

        self._lock = threading.RLock()

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
            'entropy_verdict': '---'
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

    # ── Cálculo da entropia de Shannon ─────────────────────────
    def _calculate_entropy(self, digits):
        """
        Calcula a entropia de Shannon normalizada (0 a 1) para uma sequência de dígitos.
        Entropia alta = aleatoriedade alta = previsão difícil.
        """
        if not digits or len(digits) < 2:
            return 0.0
        
        # Contar frequências de cada dígito (0-9)
        total = len(digits)
        frequencies = {}
        for d in digits:
            frequencies[d] = frequencies.get(d, 0) + 1
        
        # Calcular entropia
        entropy = 0.0
        for count in frequencies.values():
            if count > 0:
                prob = count / total
                entropy -= prob * math.log2(prob)
        
        # Normalizar (entropia máxima para 10 dígitos é log2(10) ≈ 3.3219)
        max_entropy = math.log2(min(10, len(frequencies) + 1))
        if max_entropy > 0:
            normalized = entropy / max_entropy
        else:
            normalized = 0.0
        
        return min(normalized, 1.0)

    # ── Receber tick (chamado pelo deriv_client a cada tick) ────
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
                    should_analyse = True

            if should_analyse:
                logger.info(f"⏱️ [tick #{self._tick_count}] Dígito lento #{self._digit_counter}: {digit} ({parity})")
                self._analyse(snap)

            return True, digit
        except Exception as e:
            logger.error(f"Erro tick: {e}")
            return False, None

    # ── Sequências (chama _calc_streak e formata) ──────────────
    def _find_sequences(self, digits):
        if not digits:
            return None
        streak, parity = self._calc_streak(digits)
        descricao = f"{streak} {parity}S consecutivos" if streak >= 2 else None
        return {
            'atual': streak,
            'tipo': parity,
            'descricao': descricao
        }

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
        seq_info = self._find_sequences(snap)

        # Calcular entropia de Shannon
        entropy = self._calculate_entropy(snap[-50:])  # analisa os últimos 50 dígitos

        candidates = []

        if w < 3:
            self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                streak=streak, streak_parity=sp,
                                reason=f'Aguardando ({total}/3 mínimo)...',
                                sequences=seq_info, entropy=entropy)
            return

        # 1. Streak consecutivo
        if streak >= 3:
            base_conf = min(60 + (streak - 3) * 8, 92)
            conf = self._apply_entropy_penalty(base_conf, entropy)
            if sp == 'PAR':
                candidates.append((conf, 'BUY', 'streak', f'🔥 {streak} PARES seguidos → aposte ÍMPAR ({conf:.0f}%)'))
            else:
                candidates.append((conf, 'SELL', 'streak', f'🔥 {streak} ÍMPARES seguidos → aposte PAR ({conf:.0f}%)'))

        # 2. Dominância nos últimos 20
        if w >= 10:
            if odd_pct >= 70:
                base_conf = min(55 + int((odd_pct - 70) * 1.5), 85)
                conf = self._apply_entropy_penalty(base_conf, entropy)
                candidates.append((conf, 'SELL', 'dominance', f'📊 {odd_pct}% ÍMPARES → reversão PAR ({conf:.0f}%)'))
            elif even_pct >= 70:
                base_conf = min(55 + int((even_pct - 70) * 1.5), 85)
                conf = self._apply_entropy_penalty(base_conf, entropy)
                candidates.append((conf, 'BUY', 'dominance', f'📊 {even_pct}% PARES → reversão ÍMPAR ({conf:.0f}%)'))

        # 3. Alternância
        if w >= 6:
            alt = self._calc_alternating(window)
            if alt >= 5:
                base_conf = min(55 + (alt - 5) * 5, 80)
                conf = self._apply_entropy_penalty(base_conf, entropy)
                if window[-1] % 2 != 0:
                    candidates.append((conf, 'SELL', 'alternating', f'🔄 Alternância {alt} → PAR ({conf:.0f}%)'))
                else:
                    candidates.append((conf, 'BUY', 'alternating', f'🔄 Alternância {alt} → ÍMPAR ({conf:.0f}%)'))

        # 4. Desequilíbrio moderado (agora com confiança 55)
        if w >= 15 and not candidates:
            base_conf = 55
            conf = self._apply_entropy_penalty(base_conf, entropy)
            if odd_pct >= 62:
                candidates.append((conf, 'SELL', 'imbalance', f'⚠️ {odd_pct}% ÍMPAR → possível PAR'))
            elif even_pct >= 62:
                candidates.append((conf, 'BUY', 'imbalance', f'⚠️ {even_pct}% PAR → possível ÍMPAR'))

        # ✅ Consolidação final — um único bloco with self._lock
        with self._lock:
            if candidates:
                best = max(candidates, key=lambda x: x[0])
                conf, action, ptype, reason = best
                
                # Verificar se entropia é demasiado alta
                entropy_verdict = self._get_entropy_verdict(entropy)
                
                self.last_analysis.update({
                    'streak': streak, 'streak_parity': sp,
                    'recommended_action': action if conf >= 55 else None,
                    'confidence': conf if conf >= 55 else 0,
                    'pattern_type': ptype if conf >= 55 else None,
                    'alert': 'SINAL ATIVO' if conf >= 60 else ('AVISO' if conf >= 55 else None),
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
            else:
                self._set_no_signal(snap, odd_pct, even_pct, rec_par,
                                    streak=streak, streak_parity=sp,
                                    reason=f'Equilíbrio: {odd_pct}% ímpar / {even_pct}% par. Streak: {streak}.',
                                    sequences=seq_info, entropy=entropy)

    def _apply_entropy_penalty(self, base_confidence, entropy):
        """
        Aplica penalização à confiança com base na entropia de Shannon.
        Entropia > 0.95: sequência quase aleatória → reduz confiança em até 25%
        Entropia > 0.85: sequência pouco previsível → reduz confiança em até 15%
        """
        if entropy > 0.95:
            penalty = 0.25
            logger.info(f"🎲 Entropia alta ({entropy:.3f}) → penalização de {penalty*100:.0f}%")
        elif entropy > 0.85:
            penalty = 0.15
            logger.info(f"🎲 Entropia elevada ({entropy:.3f}) → penalização de {penalty*100:.0f}%")
        else:
            penalty = 0.0
        
        return base_confidence * (1 - penalty)

    def _get_entropy_verdict(self, entropy):
        """Retorna um veredito qualitativo sobre a entropia."""
        if entropy > 0.95:
            return 'ALEATÓRIO (não operar)'
        elif entropy > 0.85:
            return 'IMPREVISÍVEL (cautela)'
        elif entropy > 0.70:
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

    # ── API pública ────────────────────────────────────────────
    def get_ticks_remaining(self):
        with self._lock:
            tr = self.TICKS_PER_DIGIT - (self._tick_count % self.TICKS_PER_DIGIT)
            return tr if tr > 0 else self.TICKS_PER_DIGIT

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


# Singleton de retrocompatibilidade (já não é usado pelo trading_bot)
digit_analyzer = DigitAnalyzer(max_digits=500)
