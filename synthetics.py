from collections import deque
import time
import logging
from datetime import datetime
import threading

logger = logging.getLogger(__name__)

class DigitAnalyzer:
    def __init__(self, max_digits=20, analysis_interval=15):
        self.slow_digits = deque(maxlen=20)  # apenas dígitos lentos (15s)
        self.analysis_interval = analysis_interval
        self.last_analysis = time.time()
        self.countdown = analysis_interval
        self.analysis_in_progress = False
        self.current_display_digit = None
        self.current_display_parity = '---'
        self.next_display_time = 0
        self.display_interval = 15

        self.last_analysis_data = {
            'streak': 0, 'streak_parity': '---',
            'recommended_action': None, 'confidence': 0,
            'pattern': 'Aguardando análise...', 'alert': None,
            'reason': 'Aguardando dados...',
            'countdown': analysis_interval,
            'odd_pct': 0, 'even_pct': 0,
            'recent_parity': []
        }

        self.countdown_thread_running = True
        self.start_countdown_thread()

    def start_countdown_thread(self):
        def update_countdown():
            while self.countdown_thread_running:
                time.sleep(1)
                if not self.analysis_in_progress:
                    elapsed = time.time() - self.last_analysis
                    self.countdown = max(0, self.analysis_interval - int(elapsed))
                    self.last_analysis_data['countdown'] = self.countdown
                    if self.countdown <= 0 and not self.analysis_in_progress:
                        self.trigger_analysis()
        threading.Thread(target=update_countdown, daemon=True).start()

    def add_tick(self, price):
        """Recebe um tick (chamado a cada segundo). Só guarda dígito a cada 15s."""
        try:
            price_str = f"{price:.2f}"
            last_digit = int(price_str[-1])
            parity = 'IMPAR' if last_digit % 2 != 0 else 'PAR'

            now = time.time()
            if now >= self.next_display_time:
                # Este é o dígito lento
                self.current_display_digit = last_digit
                self.current_display_parity = parity
                self.slow_digits.append(last_digit)
                self.next_display_time = now + self.display_interval
                # Dispara alerta com base nos últimos dígitos lentos
                self._generate_recommendation()
            return True, last_digit
        except Exception as e:
            logger.error(f"Erro ao processar tick: {e}")
            return False, None

    def _generate_recommendation(self):
        """Gera recomendação baseada nos últimos dígitos lentos"""
        if len(self.slow_digits) < 3:
            return
        streak, streak_parity = self.get_streak_info()
        if streak >= 3:
            confidence = 65 + min((streak - 3) * 10, 30)
            if streak_parity == 'PAR':
                recommended_action = 'BUY'   # próximo ÍMPAR
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} PARES consecutivos! Próximo ÍMPAR (confiança {confidence}%)'
                pattern_desc = f'{streak} PARES consecutivos'
            else:
                recommended_action = 'SELL'  # próximo PAR
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} ÍMPARES consecutivos! Próximo PAR (confiança {confidence}%)'
                pattern_desc = f'{streak} ÍMPARES consecutivos'
            self._update_analysis(recommended_action, confidence, alert, reason, pattern_desc, streak, streak_parity)
            logger.info(f"⚡ Recomendação: {reason}")

    def get_streak_info(self):
        if len(self.slow_digits) < 2:
            return 0, '---'
        streak = 1
        last_parity = 'IMPAR' if self.slow_digits[-1] % 2 != 0 else 'PAR'
        for i in range(len(self.slow_digits)-2, -1, -1):
            parity = 'IMPAR' if self.slow_digits[i] % 2 != 0 else 'PAR'
            if parity == last_parity:
                streak += 1
            else:
                break
        return streak, last_parity

    def _update_analysis(self, action, confidence, alert, reason, pattern, streak, streak_parity):
        odd_count = sum(1 for d in self.slow_digits if d % 2 != 0)
        total = len(self.slow_digits)
        odd_pct = round((odd_count / total) * 100, 1) if total > 0 else 0
        even_pct = round(100 - odd_pct, 1)
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
            'recent_parity': ['IMPAR' if d % 2 != 0 else 'PAR' for d in list(self.slow_digits)[-20:]],
            'odd_pct': odd_pct,
            'even_pct': even_pct,
            'countdown': self.countdown
        })

    def trigger_analysis(self):
        if self.analysis_in_progress:
            return
        self.analysis_in_progress = True
        self.last_analysis = time.time()
        try:
            # Análise periódica (pode repetir a recomendação)
            self._generate_recommendation()
            self.last_analysis_data['countdown'] = self.analysis_interval
            logger.info(f"📊 ANÁLISE PERIÓDICA: {self.last_analysis_data}")
        except Exception as e:
            logger.error(f"Erro na análise: {e}")
        self.analysis_in_progress = False

    def get_next_display_digit(self):
        now = time.time()
        remaining = max(0, int(self.next_display_time - now)) if self.next_display_time > 0 else 0
        return self.current_display_digit, self.current_display_parity, remaining

    def get_recent_digits(self, count=20):
        return list(self.slow_digits)[-count:] if self.slow_digits else []

    def get_current_digit(self):
        return self.current_display_digit

    def get_current_parity(self):
        return self.current_display_parity

    def get_analysis(self):
        return self.last_analysis_data

    def get_stats(self):
        if not self.slow_digits:
            return {'total': 0, 'odd_pct': 0, 'even_pct': 0,
                    'current_streak': 0, 'streak_parity': '---', 'recent': []}
        total = len(self.slow_digits)
        odd_count = sum(1 for d in self.slow_digits if d % 2 != 0)
        streak, streak_parity = self.get_streak_info()
        return {
            'total': total,
            'odd_pct': round((odd_count / total) * 100, 1),
            'even_pct': round(100 - (odd_count / total) * 100, 1),
            'current_streak': streak,
            'streak_parity': streak_parity,
            'recent': list(self.slow_digits)[-20:]
        }

    def get_countdown(self):
        return self.countdown

digit_analyzer = DigitAnalyzer(max_digits=20, analysis_interval=15)
