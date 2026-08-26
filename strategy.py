import logging
import os
import time
import uuid
import threading
import math

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Implementa os módulos da Foloma Visão 360 com sincronização por snapshots.
    - Agendamento de apostas (paridade e DIFFER) para o final do ciclo de 10 ticks.
    - Gate temporal: bloqueia agendamento nos últimos 2 ticks do ciclo.
    - Retorno de execução exposto no get_status.
    - Análise contínua de sequências para Differ (ausência curta).
    - Filtros de probabilidade estatística antes de agendar.
    - Versão robusta com proteções contra erros e validações.
    """

    def __init__(self, client, analyzer):
        self.client = client
        self.analyzer = analyzer
        self._lock = threading.RLock()

        # Estado interno
        self._last_differ_digit = None
        self._last_parity_action = None
        self._last_matches_digit = None
        self._last_zscore_digit = None
        self._last_zscore_action = None
        self._consecutive_losses = 0
        self._global_stop_until = 0
        self._cooldown_until = 0
        self._differ_sequence_used = set()

        self._consecutive_losses_differ = 0
        self._differ_cooldown_until = 0

        self._parity_last_used_tick = 0
        self._parity_martingale_used = False
        self._last_parity_streak_type = None

        self._matches_sequence_used = False
        self._matches_cooldown_until = 0
        self._zscore_sequence_used = False
        self._zscore_cooldown_until = 0
        self._price_history = []
        self._last_symbol = None

        self._active_signals = {
            'differ': None,
            'parity': None,
            'matches': None,
            'zscore': None
        }

        self.SIGNAL_VALIDITY_TICKS = int(os.getenv('SIGNAL_VALIDITY', 10))
        self._trade_locked = False
        self._trade_locked_at = 0
        self.TRADE_LOCK_TIMEOUT = 20

        self.MATCHES_ABSENCE_THRESHOLD = 45
        self.MARKET_SPIKE_THRESHOLD = 0.002

        self._pending_parity_bet = None
        self._pending_differ_bet = None

        self._last_execution_error = None

        self.parity_alternance_mode = True

        # Parâmetros de probabilidade
        self.MIN_SCORE_PARITY = 65.0
        self.MIN_SCORE_DIFFER = 65.0
        self.LATENCY_LIMIT_MS = 150.0
        self.MARTINGALE_AMOUNT_THRESHOLD = 1.0
        self.DIFFER_SHORT_ABSENCE_THRESHOLD = 8

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
        try:
            if self.is_global_stop:
                return False, "STOP GLOBAL ATIVO"
            if self.is_cooldown:
                return False, f"Cooldown ativo ({self._cooldown_until - time.time():.0f}s)"
            if not self.client or not getattr(self.client, 'authorized', False):
                return False, "Não autorizado"
            if not getattr(self.client, 'streaming', False):
                return False, "Sem streaming"

            last_tick_ago = getattr(self.client, 'get_last_tick_seconds_ago', lambda: 0)()
            if last_tick_ago > 2.5:
                return False, f"Tick desactualizado ({last_tick_ago:.1f}s atrás)"

            last_reconnect_time = getattr(self.client, '_last_reconnect_time', 0)
            if time.time() - last_reconnect_time < 3:
                return False, "Reconexão recente"

            raw_ping = getattr(self.client, '_ping_ms', 0)
            if raw_ping >= 9999:
                if getattr(self.client, 'streaming', False) and getattr(self.client, '_last_tick_time', 0):
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
        except Exception as e:
            logger.error(f"Erro em can_trade: {e}")
            return False, "Erro interno na verificação de trading"

    def _can_schedule(self):
        try:
            tr = self.analyzer.get_ticks_remaining() if hasattr(self.analyzer, 'get_ticks_remaining') else 10
            if tr < 2:
                return False, "Fim do ciclo iminente"
            return True, "OK"
        except Exception as e:
            logger.error(f"Erro em _can_schedule: {e}")
            return False, "Erro na verificação de ciclo"

    def is_market_stable(self):
        try:
            if len(self._price_history) >= 5:
                recent = self._price_history[-5:]
                avg_price = sum(recent) / len(recent)
                for price in recent:
                    variation = abs(price - avg_price) / avg_price if avg_price > 0 else 0
                    if variation > self.MARKET_SPIKE_THRESHOLD:
                        logger.warning(f"⚠️ SPIKE BLOQUEIO | preços={recent} | avg={avg_price:.5f} | variação={variation:.3%}")
                        return False, f"Spike detetado (variação {variation:.3%})"
            return True, "OK"
        except Exception as e:
            logger.error(f"Erro em is_market_stable: {e}")
            return False, "Erro na análise de estabilidade"

    # -----------------------------------------------------------------
    # Atualização a cada tick
    # -----------------------------------------------------------------
    def on_tick(self, tick):
        try:
            if not isinstance(tick, dict):
                logger.warning("Tick inválido (não é dicionário)")
                return

            symbol = tick.get('symbol', '')
            if symbol and self.client and symbol != getattr(self.client, 'current_symbol', ''):
                return

            price = tick.get('price', 0)
            with self._lock:
                if symbol and self._last_symbol is not None and self._last_symbol != symbol:
                    self._price_history = []
                    logger.info(f"🔄 Símbolo mudou de {self._last_symbol} para {symbol} — histórico de preço limpo")
                if symbol:
                    self._last_symbol = symbol
                if price:
                    try:
                        self._price_history.append(float(price))
                    except (TypeError, ValueError):
                        logger.warning(f"Preço inválido: {price}")
                    if len(self._price_history) > 20:
                        self._price_history.pop(0)

                self.refresh_signals()
                self._maybe_generate_signals()
                self._maybe_generate_sequence_differ()
        except Exception as e:
            logger.error(f"Erro em on_tick: {e}")

    def refresh_signals(self):
        try:
            with self._lock:
                self._check_trade_lock_timeout()
                for strategy in ('differ', 'parity', 'matches', 'zscore'):
                    signal = self._active_signals.get(strategy)
                    if signal and not self._is_signal_still_valid(signal):
                        self._active_signals[strategy] = None
                        logger.info(f"⏰ Sinal {strategy} expirado (ID {signal.get('id', '?'))}")
        except Exception as e:
            logger.error(f"Erro em refresh_signals: {e}")

    def _maybe_generate_sequence_differ(self):
        try:
            with self._lock:
                if self._trade_locked:
                    return
                if self.client and getattr(self.client, 'pending_trade', None) is not None:
                    return
                if self.client and getattr(self.client, 'active_trades', None):
                    return
                if self._active_signals.get('differ') and self._is_signal_still_valid(self._active_signals['differ']):
                    return  # já existe sinal válido

                seq_signal = self._analyze_digit_sequence()
                if seq_signal:
                    self._create_signal('differ', seq_signal['digit'], [seq_signal['digit']],
                                        seq_signal['reason'], mode='sequence')
        except Exception as e:
            logger.error(f"Erro em _maybe_generate_sequence_differ: {e}")

    def _analyze_digit_sequence(self):
        try:
            recent = self.analyzer.get_recent_digits(20)
            if not isinstance(recent, list) or len(recent) < 10:
                return None

            for digit in range(10):
                last_idx = -1
                for i, d in enumerate(reversed(recent)):
                    if d == digit:
                        last_idx = i
                        break
                ticks_absent = last_idx if last_idx != -1 else len(recent)

                if ticks_absent >= self.DIFFER_SHORT_ABSENCE_THRESHOLD:
                    # Verificar cluster recente
                    if recent[-5:].count(digit) >= 2:
                        continue
                    return {
                        'digit': digit,
                        'ticks_absent': ticks_absent,
                        'reason': f"Dígito {digit} ausente há {ticks_absent} ticks (sequência curta)"
                    }
            return None
        except Exception as e:
            logger.error(f"Erro em _analyze_digit_sequence: {e}")
            return None

    def _maybe_generate_signals(self):
        try:
            with self._lock:
                if self._trade_locked:
                    return
                if self.client and getattr(self.client, 'pending_trade', None) is not None:
                    return
                if self.client and getattr(self.client, 'active_trades', None):
                    return

                tpd = getattr(self.analyzer, 'TICKS_PER_DIGIT', 10)
                tr = self.analyzer.get_ticks_remaining() if hasattr(self.analyzer, 'get_ticks_remaining') else 10
                inicio_ciclo = tr >= tpd - 1

                if inicio_ciclo:
                    for strategy in ('parity', 'differ'):
                        if self._active_signals.get(strategy) and self._is_signal_still_valid(self._active_signals[strategy]):
                            continue
                        preview_func = getattr(self, f'_preview_{strategy}_signal', None)
                        if preview_func:
                            preview = preview_func()
                            if preview:
                                self._create_signal(strategy, preview['recommendation'],
                                                    preview.get('digits', []), preview['reason'],
                                                    mode=preview.get('mode', strategy))

                for strategy in ('matches', 'zscore'):
                    if self._active_signals.get(strategy) and self._is_signal_still_valid(self._active_signals[strategy]):
                        continue
                    preview_func = getattr(self, f'_preview_{strategy}_signal', None)
                    if preview_func:
                        preview = preview_func()
                        if preview:
                            self._create_signal(strategy, preview['recommendation'],
                                                preview.get('digits', []), preview['reason'],
                                                mode=preview.get('mode', strategy))
        except Exception as e:
            logger.error(f"Erro em _maybe_generate_signals: {e}")

    def _is_signal_still_valid(self, signal):
        if not isinstance(signal, dict):
            return False
        try:
            current_tick = self.analyzer.get_tick_count()
            created_tick = signal.get('tick_origin', 0)
            return (current_tick - created_tick) < self.SIGNAL_VALIDITY_TICKS
        except Exception as e:
            logger.error(f"Erro em _is_signal_still_valid: {e}")
            return False

    def _ticks_left(self, signal):
        if not isinstance(signal, dict):
            return 0
        try:
            current_tick = self.analyzer.get_tick_count()
            created_tick = signal.get('tick_origin', current_tick)
            elapsed = current_tick - created_tick
            return max(0, self.SIGNAL_VALIDITY_TICKS - elapsed)
        except Exception as e:
            logger.error(f"Erro em _ticks_left: {e}")
            return 0

    # -----------------------------------------------------------------
    # Trade Lock
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
        try:
            if self._trade_locked and time.time() - self._trade_locked_at > self.TRADE_LOCK_TIMEOUT:
                logger.warning("⏰ Timeout do trade lock – a destravar forçadamente")
                self.unlock_trade()
        except Exception as e:
            logger.error(f"Erro em _check_trade_lock_timeout: {e}")

    # -----------------------------------------------------------------
    # Criação de snapshot de sinal
    # -----------------------------------------------------------------
    def _create_signal(self, strategy, recommendation, digits, reason='', mode=''):
        try:
            current_tick = self.analyzer.get_tick_count()
            signal = {
                'id': uuid.uuid4().hex[:8],
                'strategy': strategy,
                'mode': mode,
                'digits': digits.copy() if isinstance(digits, list) else digits,
                'recommendation': recommendation,
                'reason': reason,
                'created_at': time.time(),
                'tick_origin': current_tick,
                'expires_in_ticks': self.SIGNAL_VALIDITY_TICKS
            }
            self._active_signals[strategy] = signal
            return signal
        except Exception as e:
            logger.error(f"Erro ao criar sinal: {e}")
            return None

    # -----------------------------------------------------------------
    # Agendamento de apostas
    # -----------------------------------------------------------------
    def schedule_parity_bet(self, direction, amount=0.35):
        try:
            with self._lock:
                ok, reason = self._can_schedule()
                if not ok:
                    return False, reason

                if self._trade_locked:
                    return False, "Trade em curso"
                if self.client and getattr(self.client, 'pending_trade', None) is not None:
                    return False, "Já existe um trade pendente"

                last_digit = self.analyzer.get_current_digit()
                if last_digit is None:
                    return False, "Sem dígito atual"

                score, approved, prob_reason = self.evaluate_probability(last_digit, 'parity', amount)
                if not approved:
                    logger.warning(f"🚫 Paridade recusada: {prob_reason}")
                    return False, f"Entrada recusada: {prob_reason}"

                current_tick = self.analyzer.get_tick_count()
                ticks_remaining = self.analyzer.get_ticks_remaining()
                if ticks_remaining <= 0:
                    return False, "Ciclo já fechou, aguarde o próximo"

                target_tick = current_tick + ticks_remaining
                self._pending_parity_bet = {
                    'direction': direction,
                    'target_tick': target_tick,
                    'amount': amount,
                    'created_at': time.time(),
                    'score': score,
                    'tick_origin': current_tick
                }
                logger.info(f"📅 Aposta de paridade agendada: {direction} no tick {target_tick} "
                            f"(score {score:.1f}%, faltam {ticks_remaining} ticks)")
                return True, f"Aposta {direction} agendada para o tick {target_tick} (score {score:.1f}%)"
        except Exception as e:
            logger.error(f"Erro em schedule_parity_bet: {e}")
            return False, "Erro interno ao agendar paridade"

    def schedule_differ_bet(self, digit, amount=0.35):
        try:
            with self._lock:
                ok, reason = self._can_schedule()
                if not ok:
                    return False, reason

                if self._trade_locked:
                    return False, "Trade em curso"
                if self.client and getattr(self.client, 'pending_trade', None) is not None:
                    return False, "Já existe um trade pendente"

                if time.time() < self._differ_cooldown_until:
                    remaining = self._differ_cooldown_until - time.time()
                    return False, f"Pausa DIFFER {remaining:.0f}s restantes"

                score, approved, prob_reason = self.evaluate_probability(digit, 'differ', amount)
                if not approved:
                    logger.warning(f"🚫 Differ recusado: {prob_reason}")
                    return False, f"Entrada recusada: {prob_reason}"

                current_tick = self.analyzer.get_tick_count()
                ticks_remaining = self.analyzer.get_ticks_remaining()
                if ticks_remaining <= 0:
                    return False, "Ciclo já fechou, aguarde o próximo"

                target_tick = current_tick + ticks_remaining
                self._pending_differ_bet = {
                    'digit': digit,
                    'target_tick': target_tick,
                    'amount': amount,
                    'created_at': time.time(),
                    'score': score,
                    'tick_origin': current_tick
                }
                logger.info(f"📅 Aposta DIFFER agendada: dígito {digit} no tick {target_tick} "
                            f"(score {score:.1f}%, faltam {ticks_remaining} ticks)")
                return True, f"DIFFER {digit} agendado para o tick {target_tick} (score {score:.1f}%)"
        except Exception as e:
            logger.error(f"Erro em schedule_differ_bet: {e}")
            return False, "Erro interno ao agendar DIFFER"

    def _check_pending_bets(self):
        executed = False
        try:
            with self._lock:
                current_tick = self.analyzer.get_tick_count()
                if self._pending_parity_bet:
                    if current_tick >= self._pending_parity_bet['target_tick']:
                        logger.info(f"⏰ Executando aposta agendada de paridade: {self._pending_parity_bet['direction']}")
                        self._pending_parity_bet = None
                        executed = True

                if self._pending_differ_bet:
                    if current_tick >= self._pending_differ_bet['target_tick']:
                        logger.info(f"⏰ Executando aposta agendada DIFFER: {self._pending_differ_bet['digit']}")
                        self._pending_differ_bet = None
                        executed = True
        except Exception as e:
            logger.error(f"Erro em _check_pending_bets: {e}")
        return executed

    def get_pending_bets(self):
        try:
            with self._lock:
                parity = dict(self._pending_parity_bet) if self._pending_parity_bet else None
                differ = dict(self._pending_differ_bet) if self._pending_differ_bet else None
            return parity, differ
        except Exception as e:
            logger.error(f"Erro em get_pending_bets: {e}")
            return None, None

    def set_execution_error(self, error_msg):
        with self._lock:
            self._last_execution_error = error_msg
            logger.error(f"❌ Erro de execução agendada: {error_msg}")

    def clear_execution_error(self):
        with self._lock:
            self._last_execution_error = None

    # -----------------------------------------------------------------
    # Métodos de preview
    # -----------------------------------------------------------------
    def _preview_differ_signal(self):
        try:
            signal = self._active_signals.get('differ')
            if signal and self._is_signal_still_valid(signal):
                return {'recommendation': signal['recommendation'],
                        'digits': signal['digits'],
                        'reason': signal['reason'],
                        'mode': signal['mode']}
            recent = self.analyzer.get_recent_digits(1)
            if recent and isinstance(recent, list) and len(recent) > 0:
                digit = recent[0]
                return {'recommendation': digit,
                        'digits': [digit],
                        'reason': f"DIFFER {digit}: aposta manual",
                        'mode': 'manual'}
            return None
        except Exception as e:
            logger.error(f"Erro em _preview_differ_signal: {e}")
            return None

    def _preview_parity_signal(self):
        try:
            signal = self._active_signals.get('parity')
            if signal and self._is_signal_still_valid(signal):
                return {'recommendation': signal['recommendation'],
                        'digits': signal['digits'],
                        'reason': signal['reason'],
                        'mode': signal['mode']}
            recent = self.analyzer.get_recent_digits(1)
            if recent and isinstance(recent, list) and len(recent) > 0:
                last = recent[0]
                is_odd = last % 2 != 0
                rec = 'even' if is_odd else 'odd'
                return {'recommendation': rec,
                        'digits': [last],
                        'reason': f"Último dígito {last} → apostar {rec}",
                        'mode': 'alternancia'}
            return None
        except Exception as e:
            logger.error(f"Erro em _preview_parity_signal: {e}")
            return None

    def _preview_matches_signal(self):
        try:
            if self.is_matches_cooldown:
                return None
            absence = getattr(self.analyzer, 'get_digit_absence_counts', None)
            if not absence:
                return None
            for digit, count in absence().items():
                if count >= self.MATCHES_ABSENCE_THRESHOLD and not self._matches_sequence_used:
                    return {'recommendation': digit,
                            'digits': [],
                            'reason': f"Dígito {digit} ausente há {count} ticks",
                            'mode': 'absence'}
            return None
        except Exception as e:
            logger.error(f"Erro em _preview_matches_signal: {e}")
            return None

    def _preview_zscore_signal(self):
        try:
            if self._zscore_sequence_used or time.time() < self._zscore_cooldown_until:
                return None
            z_diff, digit_diff, z_match, digit_match = self.analyzer.get_zscore_digit()
            if z_diff is not None and digit_diff is not None:
                return {'recommendation': digit_diff,
                        'action': 'DIFFER',
                        'digits': [],
                        'reason': f"Z‑Score +{z_diff:.2f} → DIFFER {digit_diff}",
                        'mode': 'zscore'}
            if z_match is not None and digit_match is not None:
                return {'recommendation': digit_match,
                        'action': 'MATCHES',
                        'digits': [],
                        'reason': f"Z‑Score {z_match:.2f} → MATCHES {digit_match}",
                        'mode': 'zscore'}
            return None
        except Exception as e:
            logger.error(f"Erro em _preview_zscore_signal: {e}")
            return None

    # -----------------------------------------------------------------
    # Métodos de leitura para get_status
    # -----------------------------------------------------------------
    def _peek_differ(self):
        try:
            signal = self._active_signals.get('differ')
            if signal and self._is_signal_still_valid(signal):
                return True, signal['recommendation']
            preview = self._preview_differ_signal()
            if preview:
                return True, preview['recommendation']
            return False, None
        except Exception as e:
            logger.error(f"Erro em _peek_differ: {e}")
            return False, None

    def _peek_parity(self):
        try:
            signal = self._active_signals.get('parity')
            if signal and self._is_signal_still_valid(signal):
                return True, signal['recommendation'], signal['reason']
            preview = self._preview_parity_signal()
            if preview:
                return True, preview['recommendation'], preview['reason']
            return False, None, "Nenhum dígito disponível"
        except Exception as e:
            logger.error(f"Erro em _peek_parity: {e}")
            return False, None, "Erro ao obter sinal de paridade"

    def _peek_matches(self):
        try:
            if self.is_matches_cooldown:
                return False
            signal = self._active_signals.get('matches')
            if signal and self._is_signal_still_valid(signal):
                return True
            preview = self._preview_matches_signal()
            if preview:
                return True
            return False
        except Exception as e:
            logger.error(f"Erro em _peek_matches: {e}")
            return False

    def _peek_zscore(self):
        try:
            signal = self._active_signals.get('zscore')
            if signal and self._is_signal_still_valid(signal):
                action = 'DIFFER' if self._last_zscore_action == 'Z_DIFFER' else 'MATCHES'
                return True, action, signal['recommendation'], signal['reason']
            preview = self._preview_zscore_signal()
            if preview:
                return True, preview['action'], preview['recommendation'], preview['reason']
            return False, None, None, "Nenhum sinal Z‑Score disponível"
        except Exception as e:
            logger.error(f"Erro em _peek_zscore: {e}")
            return False, None, None, "Erro ao obter sinal Z-Score"

    # -----------------------------------------------------------------
    # Métodos de entrada para MATCHES e ZSCORE
    # -----------------------------------------------------------------
    def evaluate_differ(self):
        return None, "DIFFER agora é manual — use schedule_differ_bet"

    def _generate_differ_signal(self):
        return None, "DIFFER agora é manual"

    def evaluate_parity(self):
        return None, "Paridade agora é manual — use schedule_parity_bet"

    def _generate_parity_signal(self):
        return None, "Paridade agora é manual"

    def _can_martingale(self):
        try:
            raw = getattr(self.client, '_ping_ms', 0)
            ping = 0 if (raw >= 9999 and getattr(self.client, 'streaming', False)
                         and getattr(self.client, '_last_tick_time', 0)
                         and time.time() - self.client._last_tick_time < 10) else raw
            if ping >= 150:
                return False
            stable, _ = self.is_market_stable()
            return stable and (time.time() - getattr(self.client, '_last_reconnect_time', 0) >= 3)
        except Exception as e:
            logger.error(f"Erro em _can_martingale: {e}")
            return False

    def evaluate_matches(self):
        try:
            with self._lock:
                self._check_trade_lock_timeout()
                ok, reason = self.can_trade
                if not ok:
                    return None, reason
                if self._trade_locked:
                    return None, "Trade em curso"
                if self.is_matches_cooldown:
                    return None, "Cooldown MATCHES ativo"
                signal = self._active_signals.get('matches')
                if signal and self._is_signal_still_valid(signal):
                    preview = self._preview_matches_signal()
                    if not preview:
                        self._active_signals['matches'] = None
                        ticks_elapsed = self.analyzer.get_tick_count() - signal.get('tick_origin', 0)
                        logger.info("⚠️ Sinal MATCHES invalidado na execução — condição desapareceu")
                        return None, f"Sinal expirou ({ticks_elapsed} ticks passados)"
                    self._matches_sequence_used = True
                    self._last_matches_digit = signal['recommendation']
                    logger.info(f"✅ MATCHES executado: snapshot {signal['id']}")
                    return signal['recommendation'], signal['reason']
                return self._generate_matches_signal()
        except Exception as e:
            logger.error(f"Erro em evaluate_matches: {e}")
            return None, "Erro interno no MATCHES"

    def _generate_matches_signal(self):
        try:
            absence = getattr(self.analyzer, 'get_digit_absence_counts', None)
            if not absence:
                return None, "Contador de ausência indisponível"
            for digit, count in absence().items():
                if count >= self.MATCHES_ABSENCE_THRESHOLD and not self._matches_sequence_used:
                    self._matches_sequence_used = True
                    self._last_matches_digit = digit
                    signal = self._create_signal('matches', digit, [],
                                                f"Dígito {digit} ausente há {count} ticks",
                                                mode='absence')
                    logger.info(f"✅ MATCHES SINAL GERADO: {signal['id'] if signal else 'erro'}")
                    return digit, signal['reason'] if signal else 'Sinal não criado'
            self._matches_sequence_used = False
            return None, f"Nenhum dígito ausente ≥{self.MATCHES_ABSENCE_THRESHOLD} ticks"
        except Exception as e:
            logger.error(f"Erro em _generate_matches_signal: {e}")
            return None, "Erro ao gerar sinal MATCHES"

    def evaluate_zscore(self):
        try:
            with self._lock:
                self._check_trade_lock_timeout()
                ok, reason = self.can_trade
                if not ok:
                    return None, None, reason
                if self._trade_locked:
                    return None, None, "Trade em curso"
                if self._zscore_sequence_used:
                    return None, None, "Sinal Z‑Score já utilizado"
                if time.time() < self._zscore_cooldown_until:
                    remaining = self._zscore_cooldown_until - time.time()
                    return None, None, f"Cooldown Z‑Score ({remaining:.0f}s)"
                signal = self._active_signals.get('zscore')
                if signal and self._is_signal_still_valid(signal):
                    preview = self._preview_zscore_signal()
                    if not preview:
                        self._active_signals['zscore'] = None
                        ticks_elapsed = self.analyzer.get_tick_count() - signal.get('tick_origin', 0)
                        logger.info("⚠️ Sinal Z‑Score invalidado na execução — condição desapareceu")
                        return None, None, f"Sinal expirou ({ticks_elapsed} ticks passados)"
                    self._zscore_sequence_used = True
                    self._zscore_cooldown_until = time.time() + 300
                    action = self._last_zscore_action
                    if not action:
                        action = 'DIFFER' if signal['reason'].startswith('Z‑Score +') else 'MATCHES'
                    logger.info(f"✅ Z‑Score executado: snapshot {signal['id']}, ação={action}")
                    return action, signal['recommendation'], signal['reason']
                return self._generate_zscore_signal()
        except Exception as e:
            logger.error(f"Erro em evaluate_zscore: {e}")
            return None, None, "Erro interno no Z-Score"

    def _generate_zscore_signal(self):
        try:
            z_diff, digit_diff, z_match, digit_match = self.analyzer.get_zscore_digit()
            if z_diff is not None and digit_diff is not None:
                self._last_zscore_action = 'Z_DIFFER'
                self._last_zscore_digit = digit_diff
                signal = self._create_signal('zscore', digit_diff, [],
                                            f"Z‑Score +{z_diff:.2f} → DIFFER {digit_diff}",
                                            mode='zscore')
                logger.info(f"✅ Z‑Score SINAL GERADO: {signal['id'] if signal else 'erro'}")
                return 'DIFFER', digit_diff, signal['reason'] if signal else 'Sinal não criado'
            if z_match is not None and digit_match is not None:
                self._last_zscore_action = 'Z_MATCH'
                self._last_zscore_digit = digit_match
                signal = self._create_signal('zscore', digit_match, [],
                                            f"Z‑Score {z_match:.2f} → MATCHES {digit_match}",
                                            mode='zscore')
                logger.info(f"✅ Z‑Score SINAL GERADO: {signal['id'] if signal else 'erro'}")
                return 'MATCHES', digit_match, signal['reason'] if signal else 'Sinal não criado'
            return None, None, "Nenhum desvio estatístico significativo"
        except Exception as e:
            logger.error(f"Erro em _generate_zscore_signal: {e}")
            return None, None, "Erro ao gerar sinal Z-Score"

    def _apply_cooldown(self, ticks):
        self._cooldown_until = time.time() + ticks

    def reset_sequence_state(self):
        try:
            with self._lock:
                self._differ_sequence_used.clear()
                self._parity_martingale_used = False
                self._last_parity_streak_type = None
                self._matches_sequence_used = False
                self._zscore_sequence_used = False
                for key in self._active_signals:
                    self._active_signals[key] = None
                self._trade_locked = False
        except Exception as e:
            logger.error(f"Erro em reset_sequence_state: {e}")

    # -----------------------------------------------------------------
    # notify_result
    # -----------------------------------------------------------------
    def notify_result(self, action, is_win):
        try:
            with self._lock:
                logger.info(
                    f"📊 notify_result: action='{action}', is_win={is_win}, "
                    f"losses={self._consecutive_losses}"
                )

                action_upper = str(action).upper() if action else ''

                if not is_win:
                    self._consecutive_losses += 1
                    if self._consecutive_losses >= 3:
                        self._global_stop_until = time.time() + 180
                        logger.warning("🛑 STOP GLOBAL: 3 perdas consecutivas — pausa 3 min")
                        self._consecutive_losses = 0
                        self._consecutive_losses_differ = 0
                        self._differ_cooldown_until = 0
                        self.reset_sequence_state()
                        return

                    if action_upper.startswith('DIFFER') or action_upper.startswith('Z_DIFFER'):
                        self._apply_cooldown(12)
                        self._consecutive_losses_differ += 1
                        if self._consecutive_losses_differ >= 2:
                            self._differ_cooldown_until = time.time() + 300
                            self._consecutive_losses_differ = 0
                            logger.warning("⏸️ Pausa DIFFER por 5 minutos — usar apenas PARIDADE")
                    elif action_upper in ('CALL', 'PUT', 'DIGITODD', 'DIGITEVEN'):
                        if not self._parity_martingale_used and self._last_parity_streak_type:
                            self._apply_cooldown(1)
                            logger.info("🔄 Janela de entrada reiniciada para martingale")
                        else:
                            self._apply_cooldown(5)
                    elif action_upper.startswith('MATCH') or action_upper.startswith('Z_MATCH'):
                        self._matches_cooldown_until = time.time() + 45
                        self._apply_cooldown(10)
                    else:
                        logger.warning(f"⚠️ Ação não reconhecida '{action}' – cooldown padrão de 5s")
                        self._apply_cooldown(5)
                else:
                    self._consecutive_losses = 0
                    self._differ_sequence_used.clear()
                    self._parity_martingale_used = False
                    self._last_parity_streak_type = None
                    self._matches_sequence_used = False
                    self._zscore_sequence_used = False
                    for key in self._active_signals:
                        self._active_signals[key] = None
                    self._trade_locked = False

                    if action_upper.startswith('DIFFER') or action_upper.startswith('Z_DIFFER'):
                        self._consecutive_losses_differ = 0
                        self._differ_cooldown_until = 0
                        self._apply_cooldown(1)
                    elif action_upper in ('CALL', 'PUT', 'DIGITODD', 'DIGITEVEN'):
                        self._apply_cooldown(2)
                    elif action_upper.startswith('MATCH') or action_upper.startswith('Z_MATCH'):
                        self._apply_cooldown(1)
                    else:
                        self._apply_cooldown(1)

            self.unlock_trade()
        except Exception as e:
            logger.error(f"Erro em notify_result: {e}")
            self.unlock_trade()

    # -----------------------------------------------------------------
    # Status para o frontend
    # -----------------------------------------------------------------
    def get_status(self):
        try:
            with self._lock:
                differ_avail, differ_digit = self._peek_differ()
                parity_avail, parity_dir, parity_reason = self._peek_parity()
                matches_avail = self._peek_matches()
                zscore_avail, zscore_action, zscore_digit, zscore_reason = self._peek_zscore()

                if self.is_matches_cooldown:
                    matches_reason = f"Cooldown {self._matches_cooldown_until - time.time():.0f}s"
                elif not matches_avail:
                    matches_reason = f"Nenhum dígito ausente ≥{self.MATCHES_ABSENCE_THRESHOLD} ticks"
                else:
                    matches_reason = "Disponível"

                if self._trade_locked:
                    trade_lock_remaining = max(0, round(self.TRADE_LOCK_TIMEOUT - (time.time() - self._trade_locked_at)))
                else:
                    trade_lock_remaining = 0

                return {
                    'global_stop': self.is_global_stop,
                    'cooldown': self.is_cooldown,
                    'matches_cooldown': self.is_matches_cooldown,
                    'consecutive_losses': self._consecutive_losses,
                    'trade_locked': self._trade_locked,
                    'trade_lock_seconds_remaining': trade_lock_remaining,
                    'differ_available': differ_avail,
                    'differ_digit': differ_digit,
                    'differ_ticks_left': self._ticks_left(self._active_signals.get('differ')),
                    'parity_available': parity_avail,
                    'parity_direction': parity_dir,
                    'parity_reason': parity_reason,
                    'parity_ticks_left': self._ticks_left(self._active_signals.get('parity')),
                    'matches_available': matches_avail,
                    'matches_reason': matches_reason,
                    'matches_ticks_left': self._ticks_left(self._active_signals.get('matches')),
                    'zscore_available': zscore_avail,
                    'zscore_action': zscore_action,
                    'zscore_digit': zscore_digit,
                    'zscore_reason': zscore_reason,
                    'zscore_ticks_left': self._ticks_left(self._active_signals.get('zscore')),
                    'pending_parity_bet': self._pending_parity_bet is not None,
                    'pending_differ_bet': self._pending_differ_bet is not None,
                    'last_execution_error': self._last_execution_error,
                }
        except Exception as e:
            logger.error(f"Erro em get_status: {e}")
            return {}

    # -----------------------------------------------------------------
    # Avaliação de probabilidade estatística
    # -----------------------------------------------------------------
    def evaluate_probability(self, digit, strategy_type, amount):
        """
        Avalia se a entrada é estatisticamente favorável.
        Retorna (score: float, approved: bool, reason: str)
        """
        try:
            if not self.analyzer:
                return 0.0, False, "Analisador indisponível"

            recent_digits = self.analyzer.get_recent_digits()
            if not isinstance(recent_digits, list) or len(recent_digits) < 30:
                return 0.0, False, "Dados insuficientes (mínimo 30 ticks)"

            if strategy_type == 'parity':
                return self._evaluate_parity(recent_digits, amount)
            elif strategy_type == 'differ':
                if digit is None:
                    return 0.0, False, "Dígito alvo não informado para Differ"
                return self._evaluate_differ(digit, recent_digits, amount)
            else:
                return 0.0, False, "Tipo de estratégia desconhecido"
        except Exception as e:
            logger.error(f"Erro em evaluate_probability: {e}")
            return 0.0, False, "Erro na avaliação de probabilidade"

    def _evaluate_parity(self, recent_digits, amount):
        try:
            if not recent_digits:
                return 0.0, False, "Sem dígitos recentes"

            window = recent_digits[-20:]
            odd_count = sum(1 for d in window if d % 2 != 0)
            even_count = len(window) - odd_count
            ratio = odd_count / len(window) if len(window) > 0 else 0.5

            last_parity = recent_digits[-1] % 2
            seq_len = 1
            for d in reversed(recent_digits[:-1]):
                if d % 2 == last_parity:
                    seq_len += 1
                else:
                    break

            if seq_len >= 6:
                return 0.0, False, f"Sequência de {seq_len} { 'ímpares' if last_parity else 'pares' } – risco alto"

            balance_score = 100 - abs(ratio - 0.5) * 200
            seq_bonus = 20 if seq_len <= 2 else 0
            seq_penalty = max(0, (seq_len - 3) * 10)

            score = balance_score + seq_bonus - seq_penalty
            score = max(0, min(100, score))

            if amount > self.MARTINGALE_AMOUNT_THRESHOLD:
                lat = getattr(self.client, 'last_trade_latency_ms', 0)
                if lat > self.LATENCY_LIMIT_MS:
                    return score, False, f"Latência alta ({lat}ms) – abortando para evitar bad fill"

            approved = score >= self.MIN_SCORE_PARITY
            reason = f"Score {score:.1f}% (balance={ratio:.2f}, seq={seq_len})" if approved else f"Score baixo {score:.1f}%"
            return score, approved, reason
        except Exception as e:
            logger.error(f"Erro em _evaluate_parity: {e}")
            return 0.0, False, "Erro na avaliação de paridade"

    def _evaluate_differ(self, digit, recent_digits, amount):
        try:
            N = 50
            window = recent_digits[-N:]
            freq = self._get_digit_frequency(digit, window)
            expected = 0.1
            std = math.sqrt(expected * (1 - expected) / N) if N > 0 else 0
            z_score = (freq - expected) / std if std > 0 else 0

            ticks_since_last = self._ticks_since_last_appearance(digit, recent_digits)

            recent_5 = recent_digits[-5:]
            if recent_5.count(digit) >= 2:
                return 0.0, False, f"Cluster detectado: dígito {digit} apareceu {recent_5.count(digit)}x nos últimos 5 ticks"

            z_score_component = max(0, -z_score) * 100
            absence_component = min(40, ticks_since_last * 5)
            freq_20 = self._get_digit_frequency(digit, recent_digits[-20:])
            freq_penalty = max(0, (freq_20 - 0.15) * 200)

            score = z_score_component + absence_component - freq_penalty
            score = max(0, min(100, score))

            if amount > self.MARTINGALE_AMOUNT_THRESHOLD:
                lat = getattr(self.client, 'last_trade_latency_ms', 0)
                if lat > self.LATENCY_LIMIT_MS:
                    return score, False, f"Latência alta ({lat}ms) – abortando"

            approved = score >= self.MIN_SCORE_DIFFER
            reason = f"Score {score:.1f}% (z={z_score:.2f}, ausência={ticks_since_last})" if approved else f"Score baixo {score:.1f}%"
            return score, approved, reason
        except Exception as e:
            logger.error(f"Erro em _evaluate_differ: {e}")
            return 0.0, False, "Erro na avaliação de DIFFER"

    def _get_digit_frequency(self, digit, digit_list):
        try:
            if not digit_list:
                return 0.0
            return digit_list.count(digit) / len(digit_list)
        except Exception as e:
            logger.error(f"Erro em _get_digit_frequency: {e}")
            return 0.0

    def _ticks_since_last_appearance(self, digit, digit_list):
        try:
            for i, d in enumerate(reversed(digit_list)):
                if d == digit:
                    return i
            return len(digit_list)
        except Exception as e:
            logger.error(f"Erro em _ticks_since_last_appearance: {e}")
            return len(digit_list) if digit_list else 0
