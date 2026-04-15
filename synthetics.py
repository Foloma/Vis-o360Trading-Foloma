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
        
        # Para exibição com atraso (10 segundos entre cada dígito mostrado)
        self.display_digits = deque(maxlen=20)      # buffer de dígitos para exibição
        self.last_display_time = time.time()
        self.display_interval = 10                  # segundos entre cada dígito exibido
        self.current_display_digit = None
        self.current_display_parity = '---'
        self.next_display_time = 0
        
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
            
            # Adiciona ao buffer de exibição (para ser mostrado com atraso)
            self.display_digits.append(last_digit)
            
            # Verifica padrões imediatamente (alerta rápido)
            self._check_immediate_pattern()
            return True, self.current_digit
        except Exception as e:
            logger.error(f"Erro ao processar tick: {e}")
            return False, None

    def get_next_display_digit(self):
        """Retorna o próximo dígito a ser exibido (com atraso de display_interval segundos)"""
        now = time.time()
        
        # Se ainda não atingiu o intervalo, retorna o mesmo dígito
        if now < self.next_display_time and self.current_display_digit is not None:
            remaining = int(self.next_display_time - now)
            return self.current_display_digit, self.current_display_parity, remaining
        
        # Se há dígitos no buffer para exibir, mostra o próximo
        if self.display_digits:
            digit = self.display_digits.popleft()
            self.current_display_digit = digit
            self.current_display_parity = 'IMPAR' if digit % 2 != 0 else 'PAR'
            self.next_display_time = now + self.display_interval
            return self.current_display_digit, self.current_display_parity, self.display_interval
        
        # Se não há dígitos, retorna o último conhecido (ou None)
        return self.current_display_digit, self.current_display_parity, 0

    def _check_immediate_pattern(self):
        """Verifica sequência logo após cada tick (alerta rápido)"""
        if len(self.digits) < 3:
            return
        streak, streak_parity = self.get_streak_info()
        if streak >= 3:
            confidence = 65 + min((streak - 3) * 5, 20)
            if streak_parity == 'PAR':
                recommended_action = 'BUY'
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} PARES consecutivos! Próximo ÍMPAR (confiança {confidence}%)'
                pattern_desc = f'{streak} PARES consecutivos'
            else:
                recommended_action = 'SELL'
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} ÍMPARES consecutivos! Próximo PAR (confiança {confidence}%)'
                pattern_desc = f'{streak} ÍMPARES consecutivos'
            self._update_analysis(recommended_action, confidence, alert, reason, pattern_desc, streak, streak_parity)
            logger.info(f"⚡ Alerta: {reason}")

    def _update_analysis(self, action, confidence, alert, reason, pattern, streak, streak_parity):
        self.last_analysis_data.update({
            'streak': streak,
            'streak_parity': streak_parity,
            'recommended_action': action,
            'confidence': confidence,
            'pattern': pattern,
            'alert': alert,
            'reason': reason,
            'last_digit': self.current_digit,
            'last_parity': self.current_parity,
            'recent_parity': self.get_recent_parity_sequence()[-20:],
            'countdown': self.countdown
        })
    
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
        diff = abs(odd_pct - even_pct)
        if diff >= 20:
            if odd_pct > even_pct:
                return {'trend': 'IMPAR', 'strength': odd_pct, 'message': f'Tendência ÍMPAR ({odd_pct:.0f}% vs {even_pct:.0f}%)'}
            else:
                return {'trend': 'PAR', 'strength': even_pct, 'message': f'Tendência PAR ({even_pct:.0f}% vs {odd_pct:.0f}%)'}
        return None
    
    def trigger_analysis(self):
        if self.analysis_in_progress:
            return
        self.analysis_in_progress = True
        self.last_analysis = time.time()
        try:
            analysis = self._perform_analysis()
            self.last_analysis_data.update(analysis)
            self.last_analysis_data['countdown'] = self.analysis_interval
            logger.info(f"📊 ANÁLISE PERIÓDICA: {analysis}")
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
        
        recommended_action = None
        confidence = 0
        alert = None
        reason = ''
        pattern_desc = ''

        if streak >= 3:
            confidence = 65 + min((streak - 3) * 5, 20)
            if streak_parity == 'PAR':
                recommended_action = 'BUY'
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} PARES consecutivos! Próximo ÍMPAR'
                pattern_desc = f'{streak} PARES consecutivos'
            else:
                recommended_action = 'SELL'
                alert = 'RECOMENDADO'
                reason = f'⚠️ {streak} ÍMPARES consecutivos! Próximo PAR'
                pattern_desc = f'{streak} ÍMPARES consecutivos'
        elif len(self.digits) >= 4:
            last4 = [d % 2 for d in list(self.digits)[-4:]]
            if last4[0] == last4[1] and last4[2] == last4[3] and last4[0] != last4[2]:
                recommended_action = 'BUY' if last4[3] == 1 else 'SELL'
                confidence = 70
                alert = 'RECOMENDADO'
                reason = '🔄 Padrão de reversão (2 iguais + 2 opostos)'
                pattern_desc = 'Reversão'
        elif trend:
            confidence = 65
            if trend['trend'] == 'IMPAR':
                recommended_action = 'BUY'
                alert = 'SUGESTÃO'
                reason = trend['message']
                pattern_desc = 'Tendência estatística'
            else:
                recommended_action = 'SELL'
                alert = 'SUGESTÃO'
                reason = trend['message']
                pattern_desc = 'Tendência estatística'
        else:
            alert = 'NEUTRO'
            reason = f'📊 Últimos 20: {odd_pct}% ÍMPAR / {even_pct}% PAR → sem padrão'
            pattern_desc = 'Aguardando'

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
