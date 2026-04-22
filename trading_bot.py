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
        self.indicators.add_price(self.current_price, self.current_symbol)
        if 'R_' in self.current_symbol:
            digit_analyzer.add_tick(self.current_price)
        self.last_analysis = self.indicators.get_all_indicators(self.current_symbol)
        if self.client:
            self.balance = self.client.balance
            self.currency = self.client.currency
            today = datetime.now().date()
            if self.daily_stats['date'] != today:
                self.reset_daily_stats()

    def reset_daily_stats(self):
        self.daily_stats = {
            'date': datetime.now().date(),
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'profit_loss': 0,
            'start_balance': self.balance
        }
        logger.info("📅 Estatísticas diárias resetadas")

    def get_momentum(self):
        prices = self.indicators.get_prices(self.current_symbol)
        if len(prices) < 5:
            return 0
        recent = list(prices)[-5:]
        momentum = (recent[-1] - recent[0]) / recent[0] * 100
        return momentum

    def calculate_signal(self):
        if not self.last_analysis:
            return 'NEUTRAL', 0

        analysis = self.last_analysis

        # Votos: 1 = BUY, -1 = SELL, 0 = neutro
        vote_trend = 0
        vote_rsi = 0
        vote_macd = 0
        vote_bb = 0

        # Tendência
        if 'ALTA' in analysis['trend']['desc']:
            vote_trend = 1
        elif 'BAIXA' in analysis['trend']['desc']:
            vote_trend = -1

        # RSI
        rsi = analysis['rsi']['score']
        if rsi < 30:
            vote_rsi = 1
        elif rsi > 70:
            vote_rsi = -1
        elif rsi < 40:
            vote_rsi = 0.5
        elif rsi > 60:
            vote_rsi = -0.5
        else:
            vote_rsi = 0

        # MACD
        if 'COMPRA' in analysis['macd']['desc']:
            vote_macd = 1
        elif 'VENDA' in analysis['macd']['desc']:
            vote_macd = -1

        # Bollinger
        if 'COMPRA' in analysis['bollinger']['desc']:
            vote_bb = 1
        elif 'VENDA' in analysis['bollinger']['desc']:
            vote_bb = -1

        # Contagem de votos positivos e negativos
        buy_votes = sum(1 for v in [vote_trend, vote_rsi, vote_macd, vote_bb] if v > 0)
        sell_votes = sum(1 for v in [vote_trend, vote_rsi, vote_macd, vote_bb] if v < 0)
        total_votes = buy_votes + sell_votes

        if total_votes == 0:
            return 'NEUTRAL', 0

        if buy_votes > sell_votes:
            signal = 'BUY'
            confidence = (buy_votes / total_votes) * 100
        elif sell_votes > buy_votes:
            signal = 'SELL'
            confidence = (sell_votes / total_votes) * 100
        else:
            return 'NEUTRAL', 0

        # Confiança máxima 95% e nunca 100%
        return signal, min(confidence, 95)

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

    def check_pending_trades(self):
        now = datetime.now()
        updated = False
        for trade in self.trades:
            if trade.get('result') == 'pending':
                if (now - trade['timestamp']).total_seconds() > 30:
                    trade['result'] = 'loss'
                    trade['profit'] = 0
                    updated = True
                    logger.warning(f"⚠️ Trade pendente expirado: {trade.get('action')} ${trade.get('amount')}")
        if updated:
            self.update_stats()

    def on_trade_result(self, result):
        try:
            logger.info(f"📊 [BOT] Processando resultado: {result}")
            if len(self.trades) == 0:
                logger.warning("Nenhum trade pendente")
                return
            last_trade = self.trades[-1]
            profit = result.get('profit', 0)
            is_win = profit > 0

            if is_win:
                last_trade['result'] = 'win'
                last_trade['profit'] = profit
                self.daily_stats['wins'] += 1
                self.daily_stats['profit_loss'] += profit
                logger.info(f"✅ GANHO! +${profit:.2f}")
            else:
                loss = last_trade['amount']
                last_trade['result'] = 'loss'
                last_trade['profit'] = 0
                self.daily_stats['losses'] += 1
                self.daily_stats['profit_loss'] -= loss
                logger.info(f"❌ PERDA! -${loss:.2f}")

            self.update_stats()
            if self.client:
                self.client.get_balance()
        except Exception as e:
            logger.error(f"Erro ao processar resultado: {e}")

    def get_trade_report(self):
        self.check_pending_trades()
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
        self.check_pending_trades()
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

    def reset_stats(self):
        self.stats = {
            'total': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0,
            'profit_loss': 0,
            'total_invested': 0,
            'total_return': 0
        }
        self.trades.clear()
        logger.info("📊 Estatísticas e histórico resetados")

trading_bot = TradingBot()
