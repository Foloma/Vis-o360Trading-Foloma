from collections import deque
from decimal import Decimal
import time
import logging
import threading

logger = logging.getLogger(__name__)


class DigitAnalyzer:
    """
    Captura 1 dígito a cada 15 segundos (dígito lento).
    - Exibe apenas esses dígitos na interface.
    - Analisa padrões PAR/ÍMPAR nesses dígitos.
    - Expõe countdown preciso até ao próximo dígito.
    """

    DISPLAY_INTERVAL = 15  # segundos entre cada dígito lento

    def __init__(self, max_digits=50):
        self.slow_digits = deque(maxlen=max_digits)

        # Dígito atualmente em exibição (último dígito lento capturado)
        self.current_display_digit = None
        self.current_display_parity = '---'

        # Controlo de tempo para captura do próximo dígito lento
        self._next_capture_time = time.time() + self.DISPLAY_INTERVAL

        # Countdown em segundos até ao próximo dígito
        self.countdown = self.DISPLAY_INTERVAL

        # Lock para thread safety
        self._lock = threading.Lock()

        # Análise atual
        self.last_analysis_data = {
            'streak': 0,
            'streak_parity': '---',
            'recommended_action': None,
            'confidence': 0,
            'pattern': 'Aguardando primeiros dígitos...',
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

    # ─────────────────────────────────────────────────────────────
    # THREAD DE COUNTDOWN
    # ─────────────────────────────────────────────────────────────
    def _start_countdown_thread(self):
        def _run():
            while self._running:
                time.sleep(0.5)
                now = time.time()
                remaining = max(1, int(self._next_capture_time - now) + 1)
                with self._lock:
                    self.countdown = remaining
                    self.last_analysis_data['countdown'] = remaining
                    self.last_analysis_data['seconds_to_next'] = remaining
        threading.Thread(target=_run, daemon=True).start()

    # ─────────────────────────────────────────────────────────────
    # EXTRAIR DÍGITO CORRETAMENTE DO PREÇO
    # ─────────────────────────────────────────────────────────────
    def _extract_last_digit(self, price):
        """
        ✅ FIX CRÍTICO: Extrai o último dígito do preço corretamente.

        Problema anterior: f"{price:.5f}" → sempre 5 casas decimais →
        preços com 3 casas (ex: 1234.567) ficavam 1234.56700 → último
        dígito sempre 0 → sempre PAR.

        Solução: usar Decimal(str(price)) que preserva as casas decimais
        reais sem adicionar zeros extra.
        """
        try:
            price_str = str(Decimal(str(price)))
            if '.' in price_str:
                last_digit = int(price_str[-1])
            else:
                last_digit = int(price_str[-1]) if price_str else 0
            return last_digit
        except Exception:
            # Fallback: usar 3 casas decimais (R_100 padrão)
            try:
                return int(f"{price:.3f}"[-1])
            except Exception:
                return 0

    # ─────────────────────────────────────────────────────────────
    # RECEBER TICK DA DERIV
    # ─────────────────────────────────────────────────────────────
    def add_tick(self, price):
        """
        Recebe cada tick da Deriv (~1s).
        Só captura o dígito lento quando os 15 segundos passam.
        """
        try:
            now = time.time()

            # ✅ Extrair dígito corretamente
            last_digit = self._extract_last_digit(price)
            parity = 'IMPAR' if last_digit % 2 != 0 else 'PAR'

            # Atualizar estado atual (para visualização em tempo real)
            with self._lock:
                self.current_display_digit = last_digit
                self.current_display_parity = parity

            # ✅ Só captura o dígito lento quando os 15 segundos terminam
            if now >= self._next_capture_time:
                self._next_capture_time = now + self.DISPLAY_INTERVAL

                with self._lock:
                    self.slow_digits.append(last_digit)
                    digits_snap = list(self.slow_digits)

                logger.info(
                    f"⏱️ [15s] Dígito capturado: {last_digit} ({parity}) "
                    f"| Total: {len(digits_snap)} | Streak: {self._calc_streak(digits_snap)}"
                )
                self._generate_recommendation(digits_snap)

            return True, last_digit

        except Exception as e:
            logger.error(f"Erro ao processar tick: {e}")
            return False, None

    # ─────────────────────────────────────────────────────────────
    # ANÁLISE DE PADRÕES
    # ─────────────────────────────────────────────────────────────
    def _generate_recommendation(self, digits_snap):
        total = len(digits_snap)
        odd_count = sum(1 for d in digits_snap if d % 2 != 0)
        odd_pct = round((odd_count / total) * 100, 1) if total > 0 else 0
        even_pct = round(100 - odd_pct, 1)
        recent_parity = ['IMPAR' if d % 2 != 0 else 'PAR' for d in digits_snap[-20:]]

        if total < 3:
            self._set_no_signal(digits_snap, odd_pct, even_pct, recent_parity,
                reason=f'Aguardando mais dígitos ({total}/3 mínimo)...')
            return

        streak, streak_parity = self._calc_streak(digits_snap)

        if streak >= 3:
            confidence = min(60 + (streak - 3) * 10, 95)

            if streak_parity == 'PAR':
                action = 'BUY'   # → DIGITODD (próximo ÍMPAR)
                reason = (f'🔥 {streak} PARES seguidos! '
                          f'Aposte em ÍMPAR. Confiança: {confidence}%')
                pattern = f'{streak} PARES consecutivos → próximo ÍMPAR'
            else:
                action = 'SELL'  # → DIGITEVEN (próximo PAR)
                reason = (f'🔥 {streak} ÍMPARES seguidos! '
                          f'Aposte em PAR. Confiança: {confidence}%')
                pattern = f'{streak} ÍMPARES consecutivos → próximo PAR'

            with self._lock:
                self.last_analysis_data.update({
                    'streak': streak,
                    'streak_parity': streak_parity,
                    'recommended_action': action,
                    'confidence': confidence,
                    'pattern': pattern,
                    'alert': 'SINAL ATIVO',
                    'reason': reason,
                    'last_digit': self.current_display_digit,
                    'last_parity': self.current_display_parity,
                    'recent_parity': recent_parity,
                    'odd_pct': odd_pct,
                    'even_pct': even_pct,
                    'total_digits': total
                })
            logger.info(f"⚡ {reason}")
        else:
            self._set_no_signal(digits_snap, odd_pct, even_pct, recent_parity,
                streak=streak, streak_parity=streak_parity,
                reason=f'Streak atual: {streak} {streak_parity}. Aguarda 3+ consecutivos para sinal.')

    def _set_no_signal(self, digits_snap, odd_pct=0, even_pct=0,
                       recent_parity=None, streak=0, streak_parity='---', reason=''):
        if recent_parity is None:
            recent_parity = []
        with self._lock:
            self.last_analysis_data.update({
                'streak': streak,
                'streak_parity': streak_parity,
                'recommended_action': None,
                'confidence': 0,
                'pattern': f'Streak: {streak} ({streak_parity})',
                'alert': None,
                'reason': reason,
                'last_digit': self.current_display_digit,
                'last_parity': self.current_display_parity,
                'recent_parity': recent_parity,
                'odd_pct': odd_pct,
                'even_pct': even_pct,
                'total_digits': len(digits_snap)
            })

    # ─────────────────────────────────────────────────────────────
    # STREAK
    # ─────────────────────────────────────────────────────────────
    def _calc_streak(self, digits_list):
        if not digits_list:
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

    # ─────────────────────────────────────────────────────────────
    # API PÚBLICA
    # ─────────────────────────────────────────────────────────────
    def get_seconds_to_next_digit(self):
        remaining = max(1, int(self._next_capture_time - time.time()) + 1)
        return remaining

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


# Instância global
digit_analyzer = DigitAnalyzer(max_digits=50)
