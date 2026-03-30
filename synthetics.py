from collections import deque
import time
import logging
from datetime import datetime
import threading

logger = logging.getLogger(__name__)

class DigitAnalyzer:
    def __init__(self, max_digits=20, analysis_interval=10):
        self.digits = deque(maxlen=100)
        self.timestamps = deque(maxlen=100)
        self.max_display = max_digits
        self.analysis_interval = analysis_interval
        self.last_analysis = time.time()
        self.current_digit = None
        self.current_parity = '---'
        self.countdown = analysis_interval
        self.analysis_in_progress = False
        
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
        try:
            price_str = f"{price:.2f}"
            last_digit = int(price_str[-1])
            parity = 'IMPAR' if last_digit % 2 != 0 else 'PAR'
            self.digits.append(last_digit)
            self.timestamps.append(datetime.now())
            self.current_digit = last_digit
            self.current_parity = parity
            return True, self.current_digit
        except Exception as e:
            logger.error(f"Erro ao processar tick: {e}")
            return False, None
    
    def get_recent_parity_sequence(self):
        recent = list(self.digits)[-20:]
        return ['IMPAR' if d % 2 != 0 else 'PAR' for d in recent]
    
    def get_streak_info(self):
        if len(self.digits) < 2:
            return 0, '---'
        streak = 1
        last_parity = 'IMPAR' if self.digits[-1] % 2 != 0 else 'PAR'
        for i in range(len(self.digits)-2, -1, -1):
            parity = 'IMPAR' if self.digits[i] % 2 != 0 else 'PAR'
            if parity == last_parity:
                streak += 1
            else:
                break
        return streak, last_parity
    
    def analyze_trend(self):
        recent = list(self.digits)[-20:]
        if len(recent) < 10:
            return None
        odd_count = sum(1 for d in recent if d % 2 != 0)
        odd_pct = (odd_count / 20) * 100
        even_pct = 100 - odd_pct
        if odd_pct >= 65:
            return {'trend': 'IMPAR', 'strength': odd_pct, 'message': f'Fortemente tendendo para ÍMPAR ({odd_pct:.0f}%)'}
        if even_pct >= 65:
            return {'trend': 'PAR', 'strength': even_pct, 'message': f'Fortemente tendendo para PAR ({even_pct:.0f}%)'}
        if odd_pct >= 55:
            return {'trend': 'IMPAR', 'strength': odd_pct, 'message': f'Levemente tendendo para ÍMPAR ({odd_pct:.0f}%)'}
        if even_pct >= 55:
            return {'trend': 'PAR', 'strength': even_pct, 'message': f'Levemente tendendo para PAR ({even_pct:.0f}%)'}
        return {'trend': 'NEUTRO', 'strength': 50, 'message': f'Sem tendência clara ({odd_pct:.0f}% ÍMPAR / {even_pct:.0f}% PAR)'}
    
    def trigger_analysis(self):
        if self.analysis_in_progress:
            return
        self.analysis_in_progress = True
        self.last_analysis = time.time()
        try:
            analysis = self._perform_analysis()
            self.last_analysis_data = analysis
            self.last_analysis_data['countdown'] = self.analysis_interval
            logger.info(f"📊 ANÁLISE COMPLETADA: {analysis}")
        except Exception as e:
            logger.error(f"Erro na análise: {e}")
        self.analysis_in_progress = False
    
    def _perform_analysis(self):
        if len(self.digits) < 10:
            return {
                'streak': 0, 'streak_parity': '---',
                'recommended_action': None, 'confidence': 0,
                'pattern': 'Acumulando dados...', 'alert': None,
                'reason': f'Aguardando mais dados ({len(self.digits)}/10)',
                'odd_pct': 0, 'even_pct': 0, 'recent_parity': [],
                'trend_analysis': None,
                'countdown': self.analysis_interval
            }
        
        recent_parity = self.get_recent_parity_sequence()
        streak, streak_parity = self.get_streak_info()
        trend = self.analyze_trend()
        
        last_20 = list(self.digits)[-20:]
        odd_count = sum(1 for d in last_20 if d % 2 != 0)
        odd_pct = round((odd_count / 20) * 100, 1)
        even_pct = round(100 - odd_pct, 1)
        
        # ========== FILTRO DE DIFERENÇA ==========
        diff = abs(odd_pct - even_pct)
        diff_threshold = getattr(__import__('config'), 'config', None)
        if diff_threshold:
            diff_threshold = diff_threshold.ADVANCED_STRATEGY.get('digit_diff_threshold', 20)
        else:
            diff_threshold = 20
        
        # Aplica filtro se não houver sequência longa
        if diff < diff_threshold and streak < 3:
            # Sem recomendação
            return {
                'streak': streak,
                'streak_parity': streak_parity,
                'recommended_action': None,
                'confidence': 0,
                'pattern': 'Diferença pequena',
                'alert': 'NEUTRO',
                'reason': f'📊 Diferença entre ÍMPAR e PAR é pequena ({diff:.1f}%) → sem recomendação clara',
                'odd_pct': odd_pct,
                'even_pct': even_pct,
                'last_digit': self.current_digit,
                'last_parity': self.current_parity,
                'recent_parity': recent_parity[-20:],
                'trend_analysis': trend,
                'countdown': self.analysis_interval
            }
        
        # Se passou no filtro, aplica regras
        recommended_action = None
        confidence = 0
        alert = None
        reason = ''
        pattern_desc = ''

        # 1. Sequência longa (>=3)
        if streak >= 3:
            if streak_parity == 'PAR':
                recommended_action = 'BUY'   # próximo ÍMPAR
                confidence = min(65 + (streak - 3) * 10, 95)
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} PARES consecutivos! Próximo provavelmente ÍMPAR'
                pattern_desc = f'{streak} PARES consecutivos'
            else:
                recommended_action = 'SELL'  # próximo PAR
                confidence = min(65 + (streak - 3) * 10, 95)
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} ÍMPARES consecutivos! Próximo provavelmente PAR'
                pattern_desc = f'{streak} ÍMPARES consecutivos'

        # 2. Padrão de reversão (ex: I I P P)
        elif len(self.digits) >= 4:
            last4 = [d % 2 for d in list(self.digits)[-4:]]
            if last4[0] == last4[1] and last4[2] == last4[3] and last4[0] != last4[2]:
                recommended_action = 'BUY' if last4[3] == 1 else 'SELL'
                confidence = 70
                alert = 'RECOMENDADO'
                reason = '🔄 Reversão após dois pares/ímpares consecutivos'
                pattern_desc = 'Padrão de reversão'

        # 3. Tendência forte
        elif trend and trend['strength'] >= 65:
            confidence = 70
            if trend['trend'] == 'IMPAR':
                recommended_action = 'BUY'
                alert = 'SUGESTÃO'
                reason = trend['message']
                pattern_desc = f'Tendência para ÍMPAR ({trend["strength"]:.0f}%)'
            else:
                recommended_action = 'SELL'
                alert = 'SUGESTÃO'
                reason = trend['message']
                pattern_desc = f'Tendência para PAR ({trend["strength"]:.0f}%)'

        # 4. Tendência leve
        elif trend and trend['strength'] >= 55:
            confidence = 60
            if trend['trend'] == 'IMPAR':
                recommended_action = 'BUY'
                alert = 'LEVE'
                reason = trend['message']
                pattern_desc = 'Leve tendência para ÍMPAR'
            else:
                recommended_action = 'SELL'
                alert = 'LEVE'
                reason = trend['message']
                pattern_desc = 'Leve tendência para PAR'

        # 5. Neutro
        else:
            alert = 'NEUTRO'
            reason = f'📊 Últimos 20: {odd_pct}% ÍMPAR / {even_pct}% PAR → Sem tendência clara'
            pattern_desc = 'Sem padrão claro'

        return {
            'streak': streak,
            'streak_parity': streak_parity,
            'recommended_action': recommended_action,
            'confidence': confidence,
            'pattern': pattern_desc,
            'alert': alert,
            'reason': reason,
            'odd_pct': odd_pct,
            'even_pct': even_pct,
            'last_digit': self.current_digit,
            'last_parity': self.current_parity,
            'recent_parity': recent_parity[-20:],
            'trend_analysis': trend,
            'countdown': self.analysis_interval
        }
    
    def get_current_digit(self):
        return self.current_digit
    
    def get_current_parity(self):
        return self.current_parity
    
    def get_recent_digits(self, count=20):
        return list(self.digits)[-count:] if self.digits else []
    
    def get_analysis(self):
        return self.last_analysis_data
    
    def get_stats(self):
        if not self.digits:
            return {'total': 0, 'odd_pct': 0, 'even_pct': 0,
                    'current_streak': 0, 'streak_parity': '---', 'recent': []}
        total = len(self.digits)
        odd_count = sum(1 for d in self.digits if d % 2 != 0)
        even_count = total - odd_count
        streak, streak_parity = self.get_streak_info()
        return {
            'total': total,
            'odd_pct': round((odd_count / total) * 100, 1),
            'even_pct': round((even_count / total) * 100, 1),
            'current_streak': streak,
            'streak_parity': streak_parity,
            'recent': self.get_recent_digits(20)
        }
    
    def get_countdown(self):
        return self.countdown

digit_analyzer = DigitAnalyzer(max_digits=20, analysis_interval=10)
