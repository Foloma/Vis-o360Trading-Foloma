import os
from dotenv import load_dotenv
import logging

load_dotenv()


class Config:
    # ID da aplicação OAuth (usado apenas no fluxo PKCE e REST)
    DERIV_APP_ID = os.getenv('DERIV_APP_ID', '133674')

    # URL base da API REST da Deriv (para pedidos OTP, etc.)
    DERIV_REST_URL = "https://api.derivws.com"

    # WebSocket URL **genérico** – será substituído pelo URL retornado pelo OTP
    # (o cliente WebSocket usará o URL personalizado quando disponível)
    WS_URL = "wss://ws.derivws.com/websockets/v3"

    AVAILABLE_SYMBOLS = {
        'R_100': 'Volatility 100',
        'R_75':  'Volatility 75',
        'R_50':  'Volatility 50'
    }

    DEFAULT_STAKE = 0.35
    MIN_STAKE = 0.35
    MAX_STAKE = 100

    CONTRACT_DURATION = 10
    CONTRACT_DURATION_UNIT = 't'

    DIGIT_CONTRACT_DURATION = 5
    DIGIT_CONTRACT_DURATION_UNIT = 't'

    REFERRAL_COMMISSION_PERCENTAGE = 0.5  # Renomeado (era MARKUP_PERCENTAGE)

    MARTINGALE_CONFIG = {
        'multiplier': 2.0,
        'max_steps': 4,
        'user_configurable': True
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

    SYNTHETIC_TECHNICAL_WEIGHT = 0.3
    SYNTHETIC_DIGIT_WEIGHT = 0.7

    CANDLE_CACHE_TTL = 10

    INVERT_SIGNAL = False
    DIAGNOSTIC_MODE = True


config = Config()
