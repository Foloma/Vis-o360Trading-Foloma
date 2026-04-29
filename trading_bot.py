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
        self.consecutive_losses = 0   # ✅ FIX: agora são atualizados corretamente
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

        vote_trend = 0
        vote_rsi = 0
        vote_macd = 0
        vote_bb = 0
        vote_stoch = 0

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

        # Estocástico
        stoch_desc = analysis.get('stochastic', {}).get('desc', '---')
        if 'SOBREVENDIDO' in stoch_desc:
            vote_stoch = 1
        elif 'SOBRECOMPRADO' in stoch_desc:
            vote_stoch = -1
        else:
            vote_stoch = 0

        votes = [v for v in [vote_trend, vote_rsi, vote_macd, vote_bb, vote_stoch] if v != 0]
        buy_votes = sum(1 for v in votes if v > 0)
        sell_votes = sum(1 for v in votes if v < 0)
        total_votes = len(votes)

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

        momentum = self.get_momentum()
        if signal == 'BUY' and momentum > 0.1:
            confidence = min(confidence + 5, 100)
        elif signal == 'SELL' and momentum < -0.1:
            confidence = min(confidence + 5, 100)

        return signal, min(confidence, 100)

    def register_trade(self, trade_data):
        trade_data['timestamp'] = datetime.now()
        self.trades.append(trade_data)
        self.stats['total'] += 1
        self.stats['total_invested'] += trade_data['amount']
        self.daily_stats['trades'] += 1
        logger.info(f"📋 Trade registado: {trade_data.get('action')} ${trade_data.get('amount')} | ID: {trade_data.get('contract_id', 'N/A')}")
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
                # ✅ FIX: Subtrair apenas o valor apostado (amount), não buy_price
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
                elapsed = (now - trade['timestamp']).total_seconds()
                if elapsed > 60:  # ✅ FIX: Aumentado para 60s (5 ticks podem demorar mais)
                    trade['result'] = 'loss'
                    trade['profit'] = 0
                    updated = True
                    logger.warning(f"⚠️ Trade pendente expirado: {trade.get('action')} ${trade.get('amount')} | ID: {trade.get('contract_id', 'N/A')}")
        if updated:
            self.update_stats()

    def on_trade_result(self, result):
        try:
            logger.info(f"📊 [BOT] Processando resultado: {result}")

            contract_id = result.get('contract_id')
            profit = result.get('profit', 0)
            is_win = profit > 0

            # ✅ FIX CRÍTICO: Encontrar o trade correto pelo contract_id
            target_trade = None
            if contract_id:
                for trade in reversed(list(self.trades)):
                    if trade.get('contract_id') == contract_id:
                        target_trade = trade
                        break

            # Fallback: último trade pendente (se não há contract_id)
            if target_trade is None:
                for trade in reversed(list(self.trades)):
                    if trade.get('result') == 'pending':
                        target_trade = trade
                        logger.warning("⚠️ Usando fallback: último trade pendente")
                        break

            if target_trade is None:
                logger.warning("⚠️ Nenhum trade pendente encontrado para este resultado")
                return

            # ✅ FIX: Verificar se já foi processado (evitar duplicação)
            if target_trade.get('result') != 'pending':
                logger.warning(f"⚠️ Trade {contract_id} já tem resultado '{target_trade.get('result')}'. Ignorando duplicado.")
                return

            if is_win:
                target_trade['result'] = 'win'
                target_trade['profit'] = profit
                self.daily_stats['wins'] += 1
                self.daily_stats['profit_loss'] += profit
                # ✅ FIX: Atualizar consecutive stats
                self.consecutive_wins += 1
                self.consecutive_losses = 0
                logger.info(f"✅ GANHO! +${profit:.2f} | Vitórias consecutivas: {self.consecutive_wins}")
            else:
                loss = target_trade.get('amount', 0)
                target_trade['result'] = 'loss'
                target_trade['profit'] = 0
                self.daily_stats['losses'] += 1
                self.daily_stats['profit_loss'] -= loss
                # ✅ FIX: Atualizar consecutive stats
                self.consecutive_losses += 1
                self.consecutive_wins = 0
                logger.info(f"❌ PERDA! -${loss:.2f} | Perdas consecutivas: {self.consecutive_losses}")

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
            'daily_stats': self.daily_stats,
            'consecutive_wins': self.consecutive_wins,
            'consecutive_losses': self.consecutive_losses
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
            'message': f"📈 Martingale ativo - Passo {self.martingale['step']}/{max_steps} | Próximo: ${next_amount:.2f}"
        }

    def reset_martingale(self):
        self.martingale = {'active': False, 'step': 0, 'original_amount': 0, 'last_result': None}
        self.consecutive_losses = 0
        self.consecutive_wins = 0

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
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        logger.info("📊 Estatísticas e histórico resetados")

trading_bot = TradingBot()
