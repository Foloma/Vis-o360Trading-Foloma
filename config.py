import os
from dotenv import load_dotenv
import logging

load_dotenv()


class Config:
    DERIV_APP_ID = os.getenv('DERIV_APP_ID', '1089')

    DEMO_API_TOKEN = os.getenv('DEMO_API_TOKEN', '')
    REAL_API_TOKEN = os.getenv('REAL_API_TOKEN', '')

    WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    DERIV_WS_URL = WS_URL

    DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///users.db')

    ACCOUNT_TYPES = {
        'demo': {'name': 'Conta Demo', 'token': DEMO_API_TOKEN, 'is_virtual': 1},
        'real': {'name': 'Conta Real', 'token': REAL_API_TOKEN, 'is_virtual': 0}
    }

    AVAILABLE_SYMBOLS = {
        'R_100': 'Volatility 100',
        'R_75': 'Volatility 75',
        'R_50': 'Volatility 50'
    }

    DEFAULT_STAKE = 0.35
    MIN_STAKE = 0.35
    MAX_STAKE = 100

    # Contratos normais (CALL/PUT) – 5 ticks
    CONTRACT_DURATION = 5
    CONTRACT_DURATION_UNIT = 't'

    # Contratos de DÍGITO – 10 ticks (1 ciclo lento completo)
    DIGIT_CONTRACT_DURATION = 10
    DIGIT_CONTRACT_DURATION_UNIT = 't'

    MARKUP_PERCENTAGE = 0.5

    MARTINGALE_CONFIG = {
        'enabled': True,
        'multiplier': 2.0,
        'max_steps': 2,
        'reset_on_win': True
    }

    RISK_LIMITS = {
        'max_daily_loss_percent': 5,
        'max_consecutive_losses': 2,
        'min_confidence': 50,
        'min_confidence_digits': 42,
        'max_stake_percent': 5,
        'stop_loss_enabled': True,
        'take_profit_enabled': True,
        'daily_target_percent': 10
    }

    ADVANCED_STRATEGY = {
        'momentum_threshold': 0.1,
        'digit_diff_threshold': 20,
        'hybrid_min_confidence': 60,
        'hybrid_mode_enabled': True
    }

    LOG_LEVEL = logging.INFO


config = Config()
