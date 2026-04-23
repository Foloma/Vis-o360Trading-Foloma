from collections import deque
import time
import logging
from datetime import datetime
import threading

logger = logging.getLogger(__name__)

class DigitAnalyzer:
    def __init__(self, max_digits=50, analysis_interval=15):
        # ✅ FIX: maxlen aumentado para 50 para ter dados suficientes
        self.slow_digits = deque(maxlen=50)
        self.analysis_interval = analysis_interval
        self.last_analysis = time.time()
        self.countdown = analysis_interval
        self.analysis_in_progress = False
        self.current_display_digit = None
        self.current_display_parity = '---'
        self.next_display_time = 0

        # ✅ FIX: display_interval reduzido — 1 dígito por tick (não a cada 15s)
        # Cada tick da Deriv é ~1s, por isso registamos todos os ticks relevantes
        self.display_interval = 1

        # ✅ FIX: Lock para thread safety
        self._lock = threading.Lock()

        self.last_analysis_data = {
            'streak': 0,
            'streak_parity': '---',
            'recommended_action': None,
            'confidence': 0,
            'pattern': 'Aguardando análise...',
            'alert': None,
            'reason': 'Aguardando dados...',
            'countdown': analysis_interval,
            'odd_pct': 0,
            'even_pct': 0,
            'recent_parity': []
        }

        self.countdown_thread_running = True
        self.start_countdown_thread()

    def start_countdown_thread(self):
        def update_countdown():
            while self.countdown_thread_running:
                time.sleep(1)
                elapsed = time.time() - self.last_analysis
                self.countdown = max(0, self.analysis_interval - int(elapsed))
                with self._lock:
                    self.last_analysis_data['countdown'] = self.countdown
                if self.countdown <= 0 and not self.analysis_in_progress:
                    self.trigger_analysis()
        threading.Thread(target=update_countdown, daemon=True).start()

    def add_tick(self, price):
        try:
            price_str = f"{price:.5f}"  # ✅ FIX: usar 5 casas decimais para maior precisão
            last_digit = int(price_str[-1])
            parity = 'IMPAR' if last_digit % 2 != 0 else 'PAR'

            now = time.time()

            # ✅ FIX: Registar dígito a cada tick (não a cada 15 segundos)
            with self._lock:
                self.current_display_digit = last_digit
                self.current_display_parity = parity
                self.slow_digits.append(last_digit)

            if now >= self.next_display_time:
                self.next_display_time = now + self.display_interval
                self._generate_recommendation()

            return True, last_digit
        except Exception as e:
            logger.error(f"Erro ao processar tick: {e}")
            return False, None

    def _generate_recommendation(self):
        with self._lock:
            digits_snapshot = list(self.slow_digits)

        if len(digits_snapshot) < 3:
            return

        streak, streak_parity = self._calc_streak(digits_snapshot)

        if streak >= 3:
            confidence = min(65 + (streak - 3) * 10, 95)
            if streak_parity == 'PAR':
                recommended_action = 'BUY'   # próximo ÍMPAR (DIGITODD)
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} PARES seguidos! Próximo ÍMPAR (conf. {confidence}%)'
                pattern_desc = f'{streak} PARES consecutivos'
            else:
                recommended_action = 'SELL'  # próximo PAR (DIGITEVEN)
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} ÍMPARES seguidos! Próximo PAR (conf. {confidence}%)'
                pattern_desc = f'{streak} ÍMPARES consecutivos'

            self._update_analysis(
                recommended_action, confidence, alert,
                reason, pattern_desc, streak, streak_parity,
                digits_snapshot
            )
            logger.info(f"⚡ Recomendação gerada: {reason}")
        else:
            # ✅ FIX CRÍTICO: Resetar recomendação quando não há padrão forte
            self._reset_recommendation(streak, streak_parity, digits_snapshot)

    def _reset_recommendation(self, streak, streak_parity, digits_snapshot):
        """Limpa a recomendação quando não há padrão suficiente."""
        odd_count = sum(1 for d in digits_snapshot if d % 2 != 0)
        total = len(digits_snapshot)
        odd_pct = round((odd_count / total) * 100, 1) if total > 0 else 0
        even_pct = round(100 - odd_pct, 1)

        with self._lock:
            self.last_analysis_data.update({
                'streak': streak,
                'streak_parity': streak_parity,
                'recommended_action': None,      # ✅ Sem recomendação ativa
                'confidence': 0,
                'pattern': f'Streak atual: {streak} ({streak_parity})',
                'alert': None,
                'reason': f'Padrão insuficiente (mín. 3 consecutivos). Streak atual: {streak}',
                'last_digit': self.current_display_digit,
                'last_parity': self.current_display_parity,
                'recent_parity': ['IMPAR' if d % 2 != 0 else 'PAR' for d in digits_snapshot[-20:]],
                'odd_pct': odd_pct,
                'even_pct': even_pct,
                'countdown': self.countdown
            })

    def _calc_streak(self, digits_list):
        """Calcula streak a partir de uma lista (thread-safe)."""
        if len(digits_list) < 2:
            return 0, '---'
        streak = 1
        last_parity = 'IMPAR' if digits_list[-1] % 2 != 0 else 'PAR'
        for i in range(len(digits_list) - 2, -1, -1):
            parity = 'IMPAR' if digits_list[i] % 2 != 0 else 'PAR'
            if parity == last_parity:
                streak += 1
            else:
                break
        return streak, last_parity

    def get_streak_info(self):
        with self._lock:
            digits_snapshot = list(self.slow_digits)
        return self._calc_streak(digits_snapshot)

    def _update_analysis(self, action, confidence, alert, reason, pattern, streak, streak_parity, digits_snapshot):
        odd_count = sum(1 for d in digits_snapshot if d % 2 != 0)
        total = len(digits_snapshot)
        odd_pct = round((odd_count / total) * 100, 1) if total > 0 else 0
        even_pct = round(100 - odd_pct, 1)

        with self._lock:
            self.last_analysis_data.update({
                'streak': streak,
                'streak_parity': streak_parity,
                'recommended_action': action,
                'confidence': confidence,
                'pattern': pattern,
                'alert': alert,
                'reason': reason,
                'last_digit': self.current_display_digit,
                'last_parity': self.current_display_parity,
                'recent_parity': ['IMPAR' if d % 2 != 0 else 'PAR' for d in digits_snapshot[-20:]],
                'odd_pct': odd_pct,
                'even_pct': even_pct,
                'countdown': self.countdown
            })

    def trigger_analysis(self):
        # ✅ FIX: Guard duplo com lock
        if self.analysis_in_progress:
            return
        self.analysis_in_progress = True
        self.last_analysis = time.time()
        try:
            self._generate_recommendation()
            with self._lock:
                self.last_analysis_data['countdown'] = self.analysis_interval
            logger.info(f"📊 ANÁLISE PERIÓDICA concluída. Streak: {self.last_analysis_data.get('streak')}")
        except Exception as e:
            logger.error(f"Erro na análise: {e}")
        finally:
            # ✅ FIX: sempre liberar o flag, mesmo em caso de erro
            self.analysis_in_progress = False

    def get_next_display_digit(self):
        now = time.time()
        remaining = max(0, int(self.next_display_time - now)) if self.next_display_time > 0 else 0
        return self.current_display_digit, self.current_display_parity, remaining

    def get_recent_digits(self, count=20):
        with self._lock:
            return list(self.slow_digits)[-count:] if self.slow_digits else []

    def get_current_digit(self):
        return self.current_display_digit

    def get_current_parity(self):
        return self.current_display_parity

    def get_analysis(self):
        with self._lock:
            return dict(self.last_analysis_data)

    def get_stats(self):
        with self._lock:
            digits_snapshot = list(self.slow_digits)

        if not digits_snapshot:
            return {
                'total': 0, 'odd_pct': 0, 'even_pct': 0,
                'current_streak': 0, 'streak_parity': '---', 'recent': []
            }

        total = len(digits_snapshot)
        odd_count = sum(1 for d in digits_snapshot if d % 2 != 0)
        streak, streak_parity = self._calc_streak(digits_snapshot)

        return {
            'total': total,
            'odd_pct': round((odd_count / total) * 100, 1),
            'even_pct': round(100 - (odd_count / total) * 100, 1),
            'current_streak': streak,
            'streak_parity': streak_parity,
            'recent': digits_snapshot[-20:]
        }

    def get_countdown(self):
        return self.countdown

digit_analyzer = DigitAnalyzer(max_digits=50, analysis_interval=15)
                
