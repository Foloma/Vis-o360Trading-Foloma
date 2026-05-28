import os
from dotenv import load_dotenv
import logging

load_dotenv()


class Config:
    DERIV_APP_ID = os.getenv('DERIV_APP_ID', '133674')

    WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

    AVAILABLE_SYMBOLS = {
        'R_100': 'Volatility 100',
        'R_75':  'Volatility 75',
        'R_50':  'Volatility 50'
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

    # MARKUP_PERCENTAGE padrão; será sobrescrito pela base de dados no arranque
    MARKUP_PERCENTAGE = 0.5

    MARTINGALE_CONFIG = {
        'multiplier': 2.0,
        'max_steps': 2
    }

    RISK_LIMITS = {
        'max_daily_loss_percent': 5,
        'max_consecutive_losses': 2,
        'min_confidence': 60,
        'min_confidence_digits': 60,
        'max_stake_percent': 5,
        'stop_loss_enabled': True,
        'take_profit_enabled': True,
        'daily_target_percent': 10
    }

    ADVANCED_STRATEGY = {
        'momentum_threshold': 0.1,
        'hybrid_min_confidence': 65,
        'hybrid_mode_enabled': True
    }

    # Pesos para combinação de sinais técnicos e de dígitos
    SYNTHETIC_TECHNICAL_WEIGHT = 0.3
    SYNTHETIC_DIGIT_WEIGHT = 0.7

    # Cache de velas (segundos)
    CANDLE_CACHE_TTL = 10


config = Config()
