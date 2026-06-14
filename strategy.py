import logging
import time
import uuid
import threading

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Implementa os módulos da Foloma Visão 360 com:
    - Snapshots de sinal (ID, dígitos, recomendação, tick de origem).
    - Expiração por número de ticks (30 ticks).
    - Bloqueio de reanálise enquanto sinal ativo.
    - Trade Lock (verificado nas rotas, não em can_trade).
    - _peek_* apenas lêem; _generate_*_signal fazem a criação.
    - Geração automática de sinais (refresh_signals) a cada tick.
    - Destravamento automático do trade lock após timeout.
    """

    def __init__(self, client, analyzer):
        self.client = client
        self.analyzer = analyzer
        self._lock = threading.RLock()

        self._last_differ_digit = None
        self._last_parity_action = None
        self._last_matches_digit = None
        self._last_zscore_digit = None
        self._last_zscore_action = None   # 'Z_DIFFER' ou 'Z_MATCH'
        self._consecutive_losses = 0
        self._global_stop_until = 0
        self._cooldown_until = 0
        self._differ_sequence_used = set()
        self._parity_odd_used = False
        self._parity_even_used = False
        self._parity_martingale_used = False
        self._last_parity_streak_type = None
        self._matches_sequence_used = False
        self._matches_cooldown_until = 0
        self._zscore_sequence_used = False
        self._zscore_cooldown_until = 0
        self._price_history = []

        self._differ_signal_at = 0
        self._parity_signal_at = 0
        self._matches_signal_at = 0
        self._zscore_signal_at = 0

        # Snapshots dos sinais ativos
        self._active_signals = {
            'differ': None,
            'parity': None,
            'matches': None,
            'zscore': None
        }

        # Expiração por ticks (aumentado para dar mais tempo ao utilizador)
        self.SIGNAL_EXPIRY_TICKS = 30

        # Trade Lock com timeout
        self._trade_locked = False
        self._trade_locked_at = 0
        self.TRADE_LOCK_TIMEOUT = 60   # segundos

    # -----------------------------------------------------------------
    # Propriedades e verificações básicas
    # -----------------------------------------------------------------
    @property
    def is_global_stop(self):
        return time.time() < self._global_stop_until

    @property
    def is_cooldown(self):
        return time.time() < self._cooldown_until

    @property
    def is_matches_cooldown(self):
        return time.time() < self._matches_cooldown_until

    @property
    def can_trade(self):
        if self.is_global_stop:
            return False, "STOP GLOBAL ATIVO"
        if self.is_cooldown:
            return False, f"Cooldown ativo ({self._cooldown_until - time.time():.0f}s)"
        if not self.client or not self.client.authorized:
            return False, "Não autorizado"
        if not self.client.streaming:
            return False, "Sem streaming"
        if time.time() - getattr(self.client, '_last_reconnect_time', 0) < 10:
            return False, "Reconexão recente"

        raw_ping = getattr(self.client, '_ping_ms', 0)
        if raw_ping >= 9999:
            if self.client.streaming and self.client._last_tick_time:
                if time.time() - self.client._last_tick_time < 10:
                    effective_ping = 0
                else:
                    effective_ping = 250
            else:
                effective_ping = 250
        else:
            effective_ping = raw_ping

        if effective_ping > 250:
            return False, f"Latência alta ({effective_ping}ms)"

        stable, reason = self.is_market_stable()
        if not stable:
            return False, reason
        return True, "OK"

    def is_market_stable(self):
        if len(self._price_history) >= 5:
            recent = self._price_history[-5:]
            avg_price = sum(recent) / len(recent)
            for price in recent:
                variation = abs(price - avg_price) / avg_price if avg_price > 0 else 0
                if variation > 0.002:
                    return False, f"Spike detetado (variação {variation:.3%})"
        return True, "OK"

    # -----------------------------------------------------------------
    # Atualização automática (chamada a cada tick)
    # -----------------------------------------------------------------
    def on_tick(self, tick):
        price = tick.get('price', 0)
        if price:
            with self._lock:
                self._price_history.append(price)
                if len(self._price_history) > 20:
                    self._price_history.pop(0)
        self.refresh_signals()

    def refresh_signals(self):
        with self._lock:
            self._check_trade_lock_timeout()
            if self._trade_locked:
                return

            for strategy in ('differ', 'parity', 'matches', 'zscore'):
                if self._active_signals.get(strategy) and self._is_signal_valid(strategy):
                    continue
                generator = getattr(self, f'_generate_{strategy}_signal', None)
                if generator:
                    signal, _ = generator()
                    if signal:
                        self._active_signals[strategy] = signal

    # -----------------------------------------------------------------
    # Trade lock
    # -----------------------------------------------------------------
    def lock_trade(self):
        with self._lock:
            self._trade_locked = True
            self._trade_locked_at = time.time()
            logger.info("🔒 Trade Lock ATIVO")

    def unlock_trade(self):
        with self._lock:
            self._trade_locked = False
            self._trade_locked_at = 0
            logger.info("🔓 Trade Lock DESATIVADO")

    def _check_trade_lock_timeout(self):
        if self._trade_locked and time.time() - self._trade_locked_at > self.TRADE_LOCK_TIMEOUT:
            logger.warning("⏰ Timeout do trade lock – a destravar forçadamente")
            self.unlock_trade()

    # -----------------------------------------------------------------
    # Cooldowns e reset
    # -----------------------------------------------------------------
    def _apply_cooldown(self, ticks):
        self._cooldown_until = time.time() + ticks

    def reset_sequence_state(self):
        with self._lock:
            self._differ_sequence_used.clear()
            self._parity_odd_used = False
            self._parity_even_used = False
            self._parity_martingale_used = False
            self._last_parity_streak_type = None
            self._matches_sequence_used = False
            self._zscore_sequence_used = False
            self._differ_signal_at = 0
            self._parity_signal_at = 0
            self._matches_signal_at = 0
            self._zscore_signal_at = 0
            for key in self._active_signals:
                self._active_signals[key] = None
            self._trade_locked = False

    # -----------------------------------------------------------------
    # Criação e validação de sinais
    # -----------------------------------------------------------------
    def _create_signal(self, strategy, recommendation, digits, reason=''):
        current_tick = self.analyzer._tick_count
        signal = {
            'id': uuid.uuid4().hex[:8],
            'strategy': strategy,
            'digits': digits,
            'recommendation': recommendation,
            'reason': reason,
            'created_at': time.time(),
            'tick_origin': current_tick,
            'expires_at_tick': current_tick + self.SIGNAL_EXPIRY_TICKS
        }
        return signal

    def _is_signal_valid(self, strategy):
        signal = self._active_signals.get(strategy)
        if not signal:
            return False
        current_tick = self.analyzer._tick_count
        if current_tick >= signal['expires_at_tick']:
            self._active_signals[strategy] = None
            return False
        return True

    def _ticks_left(self, strategy):
        signal = self._active_signals.get(strategy)
        if not signal:
            return 0
        return max(0, signal['expires_at_tick'] - self.analyzer._tick_count)

    # -----------------------------------------------------------------
    # Resultado de trade
    # -----------------------------------------------------------------
    def notify_result(self, action, is_win):
        with self._lock:
            logger.info(f"📊 notify_result: action='{action}', is_win={is_win}")
            self.unlock_trade()
            if not is_win:
                self._consecutive_losses += 1
                if self._consecutive_losses >= 2:
                    self._global_stop_until = time.time() + 180
                    logger.warning("🛑 STOP GLOBAL: 2 perdas consecutivas — pausa 3 min")
                    self._consecutive_losses = 0
                    self.reset_sequence_state()
                    return
                if action.startswith('DIFFER') or action.startswith('Z_DIFFER'):
                    self._apply_cooldown(5)
                elif action in ('CALL', 'PUT', 'BUY', 'SELL', 'DIGITODD', 'DIGITEVEN'):
                    if not self._parity_martingale_used and self._last_parity_streak_type:
                        self._apply_cooldown(1)
                        self._parity_signal_at = time.time()
                        logger.info("🔄 Janela de entrada reiniciada para martingale")
                    else:
                        self._apply_cooldown(5)
                elif action.startswith('MATCH') or action.startswith('Z_MATCH'):
                    self._matches_cooldown_until = time.time() + 150
                    self._apply_cooldown(10)
            else:
                self._consecutive_losses = 0
                self.reset_sequence_state()
                if action.startswith('DIFFER') or action.startswith('Z_DIFFER'):
                    self._apply_cooldown(1)
                elif action in ('CALL', 'PUT', 'BUY', 'SELL', 'DIGITODD', 'DIGITEVEN'):
                    self._apply_cooldown(2)

    # -----------------------------------------------------------------
    # Leitura pura (peek)
    # -----------------------------------------------------------------
    def _peek_differ(self):
        with self._lock:
            if self._active_signals['differ'] and self._is_signal_valid('differ'):
                s = self._active_signals['differ']
                return True, s['recommendation'], s
            else:
                self._active_signals['differ'] = None
            return False, None, None

    def _peek_parity(self):
        with self._lock:
            if self._active_signals['parity'] and self._is_signal_valid('parity'):
                s = self._active_signals['parity']
                return True, s['recommendation'], s, s['reason']
            else:
                self._active_signals['parity'] = None
            return False, None, None, ""

    def _peek_matches(self):
        with self._lock:
            if self._active_signals['matches'] and self._is_signal_valid('matches'):
                return True, self._active_signals['matches']
            else:
                self._active_signals['matches'] = None
            return False, None

    def _peek_zscore(self):
        with self._lock:
            if self._active_signals['zscore'] and self._is_signal_valid('zscore'):
                s = self._active_signals['zscore']
                action = 'DIFFER' if self._last_zscore_action == 'Z_DIFFER' else 'MATCHES'
                return True, action, s['recommendation'], s
            else:
                self._active_signals['zscore'] = None
            return False, None, None, None

    # -----------------------------------------------------------------
    # Geração de sinais (com efeitos colaterais)
    # -----------------------------------------------------------------
    def _generate_differ_signal(self):
        with self._lock:
            recent = self.analyzer.get_recent_digits(20)
            if len(recent) < 2:
                return None, "Aguardando dados"
            last_two = recent[-2:]
            if last_two[0] == last_two[1]:
                digit = last_two[0]
                last_ten = recent[-10:] if len(recent) >= 10 else recent
                if last_ten.count(digit) >= 2:
                    if digit in self._differ_sequence_used:
                        return None, f"Dígito {digit} já utilizado nesta sequência"
                    self._differ_sequence_used.add(digit)
                    self._differ_signal_at = time.time()
                    signal = self._create_signal('differ', digit, last_two[-2:],
                                                f"DIFFER {digit}: {last_two[0]}{last_two[1]} consecutivos")
                    logger.info(f"✅ DIFFER SINAL: {signal['id']} dígito {digit}")
                    return signal, None
            if len(recent) >= 3 and recent[-3] != recent[-2]:
                self._differ_sequence_used.clear()
            return None, "Nenhum padrão DIFFER"

    def _generate_parity_signal(self):
        with self._lock:
            recent = self.analyzer.get_recent_digits(20)
            if len(recent) < 4:
                return None, "Aguardando dados"
            last_four = [d % 2 != 0 for d in recent[-4:]]
            odd_count = sum(last_four)
            even_count = 4 - odd_count
            if odd_count >= 3:
                rec = 'even'
                reason = f"Tendência ÍMPAR ({odd_count}/4)"
            elif even_count >= 3:
                rec = 'odd'
                reason = f"Tendência PAR ({even_count}/4)"
            else:
                return None, "Nenhuma tendência clara"

            if rec == 'even':
                if self._parity_odd_used and not self._parity_martingale_used and self._last_parity_streak_type == 'odd':
                    if not self._can_martingale():
                        return None, "Martingale bloqueado"
                    self._parity_martingale_used = True
                else:
                    if self._parity_odd_used:
                        return None, "Streak ÍMPAR já utilizado"
                    self._parity_odd_used = True
                    self._last_parity_streak_type = 'odd'
                    self._parity_martingale_used = False
            else:
                if self._parity_even_used and not self._parity_martingale_used and self._last_parity_streak_type == 'even':
                    if not self._can_martingale():
                        return None, "Martingale bloqueado"
                    self._parity_martingale_used = True
                else:
                    if self._parity_even_used:
                        return None, "Streak PAR já utilizado"
                    self._parity_even_used = True
                    self._last_parity_streak_type = 'even'
                    self._parity_martingale_used = False

            signal = self._create_signal('parity', rec, recent[-4:], reason)
            self._parity_signal_at = time.time()
            logger.info(f"✅ PAR/ÍMPAR SINAL: {signal['id']} {reason} → {rec}")
            return signal, None

    def _generate_matches_signal(self):
        with self._lock:
            if self.is_matches_cooldown:
                return None, "Cooldown MATCHES ativo"
            if self._matches_signal_at > 0 and time.time() - self._matches_signal_at > 300:
                return None, "Sinal expirado"
            absence = getattr(self.analyzer, 'get_digit_absence_counts', None)
            if not absence:
                return None, "Contador de ausência indisponível"
            for digit, count in absence().items():
                if count >= 15 and not self._matches_sequence_used:
                    self._matches_sequence_used = True
                    self._matches_signal_at = time.time()
                    signal = self._create_signal('matches', digit, [],
                                                f"Dígito {digit} ausente há {count} ticks")
                    logger.info(f"✅ MATCHES SINAL: {signal['id']} dígito {digit}")
                    return signal, None
            self._matches_sequence_used = False
            return None, "Nenhum dígito ausente ≥15 ticks"

    def _generate_zscore_signal(self):
        with self._lock:
            if self._zscore_sequence_used:
                return None, "Sinal Z‑Score já utilizado"
            if time.time() < self._zscore_cooldown_until:
                return None, f"Cooldown Z‑Score ativo"
            z_diff, digit_diff, z_match, digit_match = self.analyzer.get_zscore_digit()
            if z_diff is not None and digit_diff is not None:
                action = 'DIFFER'
                digit = digit_diff
                reason = f"Z‑Score +{z_diff:.2f} → DIFFER {digit}"
            elif z_match is not None and digit_match is not None:
                action = 'MATCHES'
                digit = digit_match
                reason = f"Z‑Score {z_match:.2f} → MATCHES {digit}"
            else:
                return None, "Nenhum desvio estatístico significativo"
            signal = self._create_signal('zscore', digit, [], reason)
            self._zscore_signal_at = time.time()
            self._last_zscore_digit = digit
            self._last_zscore_action = 'Z_DIFFER' if action == 'DIFFER' else 'Z_MATCH'
            logger.info(f"✅ Z‑Score SINAL: {signal['id']} {reason}")
            return signal, None

    def _can_martingale(self):
        raw = getattr(self.client, '_ping_ms', 0)
        ping = 0 if (raw >= 9999 and self.client.streaming
                     and self.client._last_tick_time
                     and time.time() - self.client._last_tick_time < 10) else raw
        if ping >= 150:
            logger.info(f"⛔ Martingale bloqueado: ping alto ({ping}ms)")
            return False
        stable, _ = self.is_market_stable()
        if not stable:
            logger.info("⛔ Martingale bloqueado: mercado instável")
            return False
        if time.time() - getattr(self.client, '_last_reconnect_time', 0) < 10:
            logger.info("⛔ Martingale bloqueado: reconexão recente")
            return False
        return True

    # -----------------------------------------------------------------
    # Métodos de avaliação (chamados pelas rotas)
    # -----------------------------------------------------------------
    def evaluate_differ(self):
        with self._lock:
            self._check_trade_lock_timeout()
            ok, reason = self.can_trade
            logger.info(f"📈 evaluate_differ: can_trade={ok}, reason={reason}")
            if not ok:
                return None, reason
            if self._trade_locked:
                logger.info("📈 evaluate_differ: trade_locked=True")
                return None, "Trade em curso"

            # Verificar cache
            if self._active_signals['differ'] and self._is_signal_valid('differ'):
                s = self._active_signals['differ']
                logger.info(f"📈 evaluate_differ: usando sinal cache {s['id']} -> {s['recommendation']}")
                return s['recommendation'], s['reason']
            else:
                self._active_signals['differ'] = None

            # Tentar gerar novo
            signal, err = self._generate_differ_signal()
            if signal:
                self._active_signals['differ'] = signal
                logger.info(f"📈 evaluate_differ: novo sinal gerado {signal['id']} -> {signal['recommendation']}")
                return signal['recommendation'], signal['reason']
            logger.info(f"📈 evaluate_differ: falhou -> {err}")
            return None, err or "Sinal não disponível"

    def evaluate_parity(self):
        with self._lock:
            self._check_trade_lock_timeout()
            ok, reason = self.can_trade
            logger.info(f"📈 evaluate_parity: can_trade={ok}, reason={reason}")
            if not ok:
                return None, reason
            if self._trade_locked:
                logger.info("📈 evaluate_parity: trade_locked=True")
                return None, "Trade em curso"

            if self._active_signals['parity'] and self._is_signal_valid('parity'):
                s = self._active_signals['parity']
                logger.info(f"📈 evaluate_parity: usando sinal cache {s['id']} -> {s['recommendation']}")
                return s['recommendation'], s['reason']
            else:
                self._active_signals['parity'] = None

            signal, err = self._generate_parity_signal()
            if signal:
                self._active_signals['parity'] = signal
                logger.info(f"📈 evaluate_parity: novo sinal gerado {signal['id']} -> {signal['recommendation']}")
                return signal['recommendation'], signal['reason']
            logger.info(f"📈 evaluate_parity: falhou -> {err}")
            return None, err or "Sinal não disponível"

    def evaluate_matches(self):
        with self._lock:
            self._check_trade_lock_timeout()
            ok, reason = self.can_trade
            logger.info(f"📈 evaluate_matches: can_trade={ok}, reason={reason}")
            if not ok:
                return None, reason
            if self._trade_locked:
                logger.info("📈 evaluate_matches: trade_locked=True")
                return None, "Trade em curso"

            if self._active_signals['matches'] and self._is_signal_valid('matches'):
                s = self._active_signals['matches']
                logger.info(f"📈 evaluate_matches: usando sinal cache {s['id']} -> {s['recommendation']}")
                return s['recommendation'], s['reason']
            else:
                self._active_signals['matches'] = None

            signal, err = self._generate_matches_signal()
            if signal:
                self._active_signals['matches'] = signal
                logger.info(f"📈 evaluate_matches: novo sinal gerado {signal['id']} -> {signal['recommendation']}")
                return signal['recommendation'], signal['reason']
            logger.info(f"📈 evaluate_matches: falhou -> {err}")
            return None, err or "Sinal não disponível"

    def evaluate_zscore(self):
        with self._lock:
            self._check_trade_lock_timeout()
            ok, reason = self.can_trade
            logger.info(f"📈 evaluate_zscore: can_trade={ok}, reason={reason}")
            if not ok:
                return None, None, reason
            if self._trade_locked:
                logger.info("📈 evaluate_zscore: trade_locked=True")
                return None, None, "Trade em curso"

            if self._active_signals['zscore'] and self._is_signal_valid('zscore'):
                s = self._active_signals['zscore']
                action = 'DIFFER' if self._last_zscore_action == 'Z_DIFFER' else 'MATCHES'
                logger.info(f"📈 evaluate_zscore: usando sinal cache {s['id']} -> {action} {s['recommendation']}")
                return action, s['recommendation'], s['reason']
            else:
                self._active_signals['zscore'] = None

            signal, err = self._generate_zscore_signal()
            if signal:
                action = 'DIFFER' if self._last_zscore_action == 'Z_DIFFER' else 'MATCHES'
                self._active_signals['zscore'] = signal
                logger.info(f"📈 evaluate_zscore: novo sinal gerado {signal['id']} -> {action} {signal['recommendation']}")
                return action, signal['recommendation'], signal['reason']
            logger.info(f"📈 evaluate_zscore: falhou -> {err}")
            return None, None, err or "Sinal não disponível"

    # -----------------------------------------------------------------
    # Status para o frontend
    # -----------------------------------------------------------------
    def get_status(self):
        with self._lock:
            differ_avail, differ_digit, differ_signal = self._peek_differ()
            parity_avail, parity_dir, parity_signal, parity_reason = self._peek_parity()
            matches_avail, matches_signal = self._peek_matches()
            zscore_avail, zscore_action, zscore_digit, zscore_signal = self._peek_zscore()

            if self.is_matches_cooldown:
                matches_reason = f"Cooldown {self._matches_cooldown_until - time.time():.0f}s"
            elif not matches_avail:
                matches_reason = "Nenhum dígito ausente ≥15 ticks"
            else:
                matches_reason = "Disponível"

            return {
                'global_stop': self.is_global_stop,
                'cooldown': self.is_cooldown,
                'matches_cooldown': self.is_matches_cooldown,
                'consecutive_losses': self._consecutive_losses,
                'trade_locked': self._trade_locked,
                'differ_available': differ_avail,
                'differ_digit': differ_digit,
                'differ_signal': differ_signal,
                'differ_ticks_left': self._ticks_left('differ'),
                'parity_available': parity_avail,
                'parity_direction': parity_dir,
                'parity_reason': parity_reason,
                'parity_signal': parity_signal,
                'parity_ticks_left': self._ticks_left('parity'),
                'matches_available': matches_avail,
                'matches_reason': matches_reason,
                'matches_signal': matches_signal,
                'matches_ticks_left': self._ticks_left('matches'),
                'zscore_available': zscore_avail,
                'zscore_action': zscore_action,
                'zscore_digit': zscore_digit,
                'zscore_signal': zscore_signal,
                'zscore_ticks_left': self._ticks_left('zscore'),
            }
