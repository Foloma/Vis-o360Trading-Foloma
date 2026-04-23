import os
from dotenv import load_dotenv
import logging

load_dotenv()

class Config:
    # Deriv API
    DERIV_APP_ID = os.getenv('DERIV_APP_ID', '1089')

    # Tokens
    DEMO_API_TOKEN = os.getenv('DEMO_API_TOKEN', '')
    REAL_API_TOKEN = os.getenv('REAL_API_TOKEN', '')

    # WebSocket
    WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    DERIV_WS_URL = WS_URL

    # Base de Dados
    DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///users.db')

    # Tipos de conta
    ACCOUNT_TYPES = {
        'demo': {'name': 'Conta Demo', 'token': DEMO_API_TOKEN, 'is_virtual': 1},
        'real': {'name': 'Conta Real', 'token': REAL_API_TOKEN, 'is_virtual': 0}
    }

    # Símbolos disponíveis
    AVAILABLE_SYMBOLS = {
        'R_100': 'Volatility 100',
        'R_75':  'Volatility 75',
        'R_50':  'Volatility 50'
    }

    # ── Configurações de trading ──────────────────────────────────
    DEFAULT_STAKE = 0.35
    MIN_STAKE = 0.35
    MAX_STAKE = 100

    # Duração para contratos normais (CALL/PUT)
    CONTRACT_DURATION = 5
    CONTRACT_DURATION_UNIT = 't'   # ticks

    # ✅ Duração para contratos de DÍGITO
    # É calculada dinamicamente pelo digit_analyzer (segundos até ao próximo dígito de 15s)
    # Este valor é o fallback caso o analyzer não esteja disponível
    DIGIT_CONTRACT_DURATION_FALLBACK = 15   # segundos
    DIGIT_CONTRACT_DURATION_UNIT = 's'      # segundos (não ticks!)

    # Intervalo de dígitos lentos (deve coincidir com DigitAnalyzer.DISPLAY_INTERVAL)
    DIGIT_DISPLAY_INTERVAL = 15   # segundos

    # ── Markup ────────────────────────────────────────────────────
    MARKUP_PERCENTAGE = 0.5

    # ── Martingale ────────────────────────────────────────────────
    MARTINGALE_CONFIG = {
        'enabled': True,
        'multiplier': 2.0,
        'max_steps': 2,
        'reset_on_win': True
    }

    # ── Limites de risco ──────────────────────────────────────────
    RISK_LIMITS = {
        'max_daily_loss_percent': 5,
        'max_consecutive_losses': 2,
        'min_confidence': 50,
        'min_confidence_digits': 0,
        'max_stake_percent': 5,
        'stop_loss_enabled': True,
        'take_profit_enabled': True,
        'daily_target_percent': 10
    }

    # ── Estratégia Avançada ───────────────────────────────────────
    ADVANCED_STRATEGY = {
        'momentum_threshold': 0.1,
        'digit_diff_threshold': 20,
        'hybrid_min_confidence': 60,
        'hybrid_mode_enabled': True
    }

    LOG_LEVEL = logging.INFO

config = Config()
    
