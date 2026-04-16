import logging
import time
from collections import deque
from datetime import datetime
from indicators import TechnicalIndicators
from synthetics import digit_analyzer
from config import config

logger = logging.getLogger(__name__)

class TradingBot:
    def __init__(self):
        self.client = None
        self.indicators = TechnicalIndicators()
        self.current_price = 0
        self.current_symbol = 'R_100'
        self.balance = 0
        self.currency = 'USD'
        self.paused = False
        self.last_analysis = {}
        
        self.stats = {
            'total': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0,
            'profit_loss': 0,
            'total_invested': 0,
            'total_return': 0
        }
        
        self.daily_stats = {
            'date': datetime.now().date(),
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'profit_loss': 0,
            'start_balance': 0
        }
        
        self.trades = deque(maxlen=100)
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        
        self.martingale = {
            'active': False,
            'step': 0,
            'original_amount': 0,
            'last_result': None
        }
        
    def start(self, client):
        self.client = client
        self.daily_stats['start_balance'] = self.balance
        logger.info("🚀 Bot iniciado")
    
    def pause(self):
        self.paused = True
        logger.info("⏸️ Pausado")
    
    def resume(self):
        self.paused = False
        logger.info("▶️ Resumido")
    
    def on_tick(self, tick):
        self.current_price = tick['price']
        self.current_symbol = tick['symbol']
        self.indicators.add_price(self.current_price)
        if 'R_' in self.current_symbol:
            digit_analyzer.add_tick(self.current_price)
        self.last_analysis = self.indicators.get_all_indicators()
        if self.client:
            self.balance = self.client.balance
            self.currency = self.client.currency
            today = datetime.now().date()
            if self.daily_stats['date'] != today:
                self.reset_daily_stats()
    
    def reset_daily_stats(self):
        self.daily_stats = {
            'date': datetime.now().date(),
            'trades': 0, 'wins': 0, 'losses': 0,
            'profit_loss': 0, 'start_balance': self.balance
        }
        logger.info("📅 Estatísticas diárias resetadas")
    
    def get_momentum(self):
        if len(self.indicators.prices) < 5:
            return 0
        recent = list(self.indicators.prices)[-5:]
        momentum = (recent[-1] - recent[0]) / recent[0] * 100
        return momentum
    
    def calculate_signal(self):
        if not self.last_analysis:
            return 'NEUTRAL', 0

        analysis = self.last_analysis
        weights = {
            'trend': 0.35,
            'rsi': 0.30,
            'macd': 0.20,
            'bollinger': 0.15
        }

        buy_score = 0
        sell_score = 0

        # 1. Tendência
        if 'ALTA' in analysis['trend']['desc']:
            buy_score += analysis['trend']['score'] * weights['trend']
        elif 'BAIXA' in analysis['trend']['desc']:
            sell_score += analysis['trend']['score'] * weights['trend']

        # 2. RSI
        rsi = analysis['rsi']['score']
        if rsi < 30:
            buy_score += (30 - rsi) * weights['rsi']
        elif rsi > 70:
            sell_score += (rsi - 70) * weights['rsi']
        elif rsi < 40:
            buy_score += (40 - rsi) * weights['rsi'] * 0.5
        elif rsi > 60:
            sell_score += (rsi - 60) * weights['rsi'] * 0.5

        # 3. MACD
        if 'COMPRA' in analysis['macd']['desc']:
            buy_score += analysis['macd']['score'] * weights['macd']
        elif 'VENDA' in analysis['macd']['desc']:
            sell_score += analysis['macd']['score'] * weights['macd']

        # 4. Bollinger Bands
        if 'COMPRA' in analysis['bollinger']['desc']:
            buy_score += analysis['bollinger']['score'] * weights['bollinger']
        elif 'VENDA' in analysis['bollinger']['desc']:
            sell_score += analysis['bollinger']['score'] * weights['bollinger']

        if buy_score > 0 and sell_score > 0:
            buy_score *= 0.5
            sell_score *= 0.5

        total = buy_score + sell_score
        if total == 0:
            return 'NEUTRAL', 0

        if buy_score > sell_score:
            signal = 'BUY'
            confidence = (buy_score / total) * 100
        else:
            signal = 'SELL'
            confidence = (sell_score / total) * 100

        # Ajuste pelo momentum (mais conservador)
momentum = self.get_momentum()
threshold = config.ADVANCED_STRATEGY.get('momentum_threshold', 0.1)

if signal == 'BUY':
    if momentum > threshold:
        confidence = min(confidence + 5, 85)   # bónus reduzido e limite 85%
    elif momentum < -threshold:
        confidence = max(confidence - 10, 0)
elif signal == 'SELL':
    if momentum < -threshold:
        confidence = min(confidence + 5, 85)
    elif momentum > threshold:
        confidence = max(confidence - 10, 0)

        return signal, min(confidence, 98)
    
    def register_trade(self, trade_data):
        trade_data['timestamp'] = datetime.now()
        self.trades.append(trade_data)
        self.stats['total'] += 1
        self.stats['total_invested'] += trade_data['amount']
        self.daily_stats['trades'] += 1
        self.update_stats()
    
    def update_stats(self):
        wins = 0
        losses = 0
        profit_loss = 0
        for trade in self.trades:
            if trade.get('result') == 'win':
                wins += 1
                profit_loss += trade.get('profit', 0)
            elif trade.get('result') == 'loss':
                losses += 1
                profit_loss -= trade.get('amount', 0)
        self.stats['wins'] = wins
        self.stats['losses'] = losses
        self.stats['win_rate'] = (wins / self.stats['total']) * 100 if self.stats['total'] > 0 else 0
        self.stats['profit_loss'] = profit_loss
        self.stats['total_return'] = (profit_loss / self.stats['total_invested']) * 100 if self.stats['total_invested'] > 0 else 0
    
    def on_trade_result(self, result):
        try:
            logger.info(f"📊 [BOT] Processando resultado: {result}")
            if len(self.trades) == 0:
                logger.warning("Nenhum trade pendente")
                return
            last_trade = self.trades[-1]
            is_win = result.get('is_win', False)
            if is_win:
                profit = result.get('profit', last_trade['amount'] * 0.85)
                last_trade['result'] = 'win'
                last_trade['profit'] = profit
                self.daily_stats['wins'] += 1
                self.daily_stats['profit_loss'] += profit
                logger.info(f"✅ GANHO! +${profit:.2f}")
            else:
                last_trade['result'] = 'loss'
                last_trade['profit'] = 0
                self.daily_stats['losses'] += 1
                self.daily_stats['profit_loss'] -= last_trade['amount']
                logger.info(f"❌ PERDA! -${last_trade['amount']:.2f}")
            self.update_stats()
            if self.client:
                self.client.get_balance()
        except Exception as e:
            logger.error(f"Erro ao processar resultado: {e}")
    
    def get_trade_report(self):
        hoje = datetime.now().date()
        trades_hoje = [t for t in self.trades if t['timestamp'].date() == hoje]
        return {
            'resumo': {
                'total_trades': self.stats['total'],
                'trades_hoje': len(trades_hoje),
                'wins': self.stats['wins'],
                'losses': self.stats['losses'],
                'win_rate': round(self.stats['win_rate'], 2),
                'profit_loss': round(self.stats['profit_loss'], 2),
                'total_invested': round(self.stats['total_invested'], 2),
                'total_return': round(self.stats['total_return'], 2)
            },
            'historico': [{
                'time': t['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                'symbol': t.get('symbol', ''),
                'action': t.get('action', ''),
                'amount': t.get('amount', 0),
                'result': t.get('result', 'pending'),
                'profit': t.get('profit', 0)
            } for t in list(self.trades)[-50:]]
        }
    
    def get_status(self):
        signal, confidence = self.calculate_signal()
        return {
            'connected': self.client.connected if self.client else False,
            'authorized': self.client.authorized if self.client else False,
            'price': self.current_price,
            'symbol': self.current_symbol,
            'balance': self.balance,
            'currency': self.currency,
            'signal': signal,
            'confidence': round(confidence, 1),
            'analysis': self.last_analysis,
            'stats': self.stats,
            'paused': self.paused,
            'martingale': self.get_martingale_status(),
            'daily_stats': self.daily_stats
        }
    
    def get_martingale_status(self):
        return {
            'active': self.martingale['active'],
            'step': self.martingale['step'],
            'original_amount': self.martingale['original_amount'],
            'next_amount': self.get_martingale_amount(config.DEFAULT_STAKE),
            'max_steps': config.MARTINGALE_CONFIG.get('max_steps', 2),
            'multiplier': config.MARTINGALE_CONFIG.get('multiplier', 2.0),
            'enabled': config.MARTINGALE_CONFIG.get('enabled', True)
        }
    
    def get_martingale_amount(self, base_amount):
        if not self.martingale['active'] or self.martingale['step'] == 0:
            return base_amount
        multiplier = config.MARTINGALE_CONFIG.get('multiplier', 2.0)
        step = self.martingale['step']
        return base_amount * (multiplier ** step)
    
    def apply_martingale_after_loss(self, last_trade_amount):
        if not config.MARTINGALE_CONFIG.get('enabled', True):
            return False, "Martingale desativado"
        max_steps = config.MARTINGALE_CONFIG.get('max_steps', 2)
        if self.martingale['step'] >= max_steps:
            return False, f"Máximo de {max_steps} perdas consecutivas atingido"
        self.martingale['step'] += 1
        self.martingale['active'] = True
        self.martingale['original_amount'] = last_trade_amount
        next_amount = self.get_martingale_amount(last_trade_amount)
        return True, {
            'step': self.martingale['step'],
            'next_amount': next_amount,
            'multiplier': config.MARTINGALE_CONFIG.get('multiplier', 2.0),
            'message': f"📈 Martingale ativo - Passo {self.martingale['step']}/{max_steps} | Próximo valor: ${next_amount:.2f}"
        }
    
    def reset_martingale(self):
        self.martingale = {'active': False, 'step': 0, 'original_amount': 0, 'last_result': None}

trading_bot = TradingBot()
