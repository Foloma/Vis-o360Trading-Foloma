import logging
import time
import uuid
import threading

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Implementa os módulos da Foloma Visão 360 com sincronização por snapshots.
    - Sinais gerados automaticamente (differ/parity no início do ciclo; matches/zscore sempre).
    - Preview sem efeitos colaterais.
    - Cache de sinais expira após 5 ticks.
    - Entrada apenas no clique (evaluate_*), que consome o estado.
    - Trade Lock com timeout de 20 segundos.
    - Paridade exige 5 ou 6 ocorrências do mesmo tipo nos últimos 6 dígitos.
    - Antes de executar, revalida a condição para evitar trades com sinal desatualizado.
    - Geração de sinais bloqueada quando há trade pendente no cliente.
    - Gates Hard: tick desactualizado (>2.5s) e fim de ciclo (<7 ticks restantes).
    - Snapshots com metadados (mode, expires_in_ticks).
    - DIFFER apenas no modo REPEAT (consecutivo). RARITY removido.
    - Histórico de preços reiniciado ao mudar de símbolo (evita falsos spikes).
    - FIX F3: Paridade usa cooldown de 15 ticks em vez de uma entrada por ciclo.
    - FIX F4: Validade de sinal aumentada para 8 ticks.
    - FIX F5: Filtro de spike relaxado para 0.4%.
    - FIX F6: Cooldown MATCHES reduzido para 45s.
    - FIX F7: Geração de sinais: parity antes de differ.
    """

    def __init__(self, client, analyzer):
        self.client = client
        self.analyzer = analyzer
        self._lock = threading.RLock()

        self._last_differ_digit = None
        self._last_parity_action = None
        self._last_matches_digit = None
        self._last_zscore_digit = None
        self._last_zscore_action = None
        self._consecutive_losses = 0
        self._global_stop_until = 0
        self._cooldown_until = 0
        self._differ_sequence_used = set()

        # FIX F3: substitui flags de paridade por tick do último uso
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

        # FIX F4: validade de sinal passou de 5 para 8 ticks
        self.SIGNAL_VALIDITY_TICKS = 8
        self._trade_locked = False
        self._trade_locked_at = 0
        self.TRADE_LOCK_TIMEOUT = 20

        # FIX F1: MATCHES threshold 45 (era 20)
        self.MATCHES_ABSENCE_THRESHOLD = 45

        # FIX F5: spike de 0.2% -> 0.4%
        self.MARKET_SPIKE_THRESHOLD = 0.004

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

        last_tick_ago = getattr(self.client, 'get_last_tick_seconds_ago', lambda: 0)()
        if last_tick_ago > 2.5:
            return False, f"Tick desactualizado ({last_tick_ago:.1f}s atrás)"

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

        tr = self.analyzer.get_ticks_remaining() if hasattr(self.analyzer, 'get_ticks_remaining') else 10
        if tr < 7:
            return False, f"Aguardar novo ciclo ({tr} ticks restantes)"

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
                if variation > self.MARKET_SPIKE_THRESHOLD:
                    logger.warning(
                        f"⚠️ SPIKE BLOQUEIO | preços={recent} | avg={avg_price:.5f} | variação={variation:.3%}"
                    )
                    return False, f"Spike detetado (variação {variation:.3%})"
        return True, "OK"

    # -----------------------------------------------------------------
    # Atualização a cada tick
    # -----------------------------------------------------------------
    def on_tick(self, tick):
        # CORREÇÃO: ignorar ticks de outros símbolos (ex: Forex)
        symbol = tick.get('symbol', '')
        if symbol and self.client and symbol != self.client.current_symbol:
            return

        price = tick.get('price', 0)
        with self._lock:
            if symbol and self._last_symbol is not None and self._last_symbol != symbol:
                self._price_history = []
                logger.info(f"🔄 Símbolo mudou de {self._last_symbol} para {symbol} — histórico de preço limpo")
            if symbol:
                self._last_symbol = symbol
            if price:
                self._price_history.append(price)
                if len(self._price_history) > 20:
                    self._price_history.pop(0)
            self.refresh_signals()
            self._maybe_generate_signals()

    def refresh_signals(self):
        with self._lock:
            self._check_trade_lock_timeout()
            for strategy in ('differ', 'parity', 'matches', 'zscore'):
                signal = self._active_signals.get(strategy)
                if signal and not self._is_signal_still_valid(signal):
                    self._active_signals[strategy] = None
                    logger.info(f"⏰ Sinal {strategy} expirado (ID {signal['id']})")

    def _maybe_generate_signals(self):
        with self._lock:
            if self._trade_locked:
                return
            if self.client and self.client.pending_trade is not None:
                return
            if self.client and self.client.active_trades:
                return

            tpd = self.analyzer.TICKS_PER_DIGIT if hasattr(self.analyzer, 'TICKS_PER_DIGIT') else 10
            tr = self.analyzer.get_ticks_remaining()
            inicio_ciclo = tr >= tpd - 1

            # FIX F7: parity primeiro
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

    def _is_signal_still_valid(self, signal):
        if not signal:
            return False
        current_tick = self.analyzer.get_tick_count()
        created_tick = signal.get('tick_origin', 0)
        return (current_tick - created_tick) < self.SIGNAL_VALIDITY_TICKS

    def _ticks_left(self, signal):
        if not signal:
            return 0
        current_tick = self.analyzer.get_tick_count()
        created_tick = signal.get('tick_origin', current_tick)
        elapsed = current_tick - created_tick
        return max(0, self.SIGNAL_VALIDITY_TICKS - elapsed)

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
        if self._trade_locked and time.time() - self._trade_locked_at > self.TRADE_LOCK_TIMEOUT:
            logger.warning("⏰ Timeout do trade lock – a destravar forçadamente")
            self.unlock_trade()

    # -----------------------------------------------------------------
    # Criação de snapshot de sinal
    # -----------------------------------------------------------------
    def _create_signal(self, strategy, recommendation, digits, reason='', mode=''):
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

    # -----------------------------------------------------------------
    # Métodos de preview SEM efeitos colaterais
    # -----------------------------------------------------------------
    def _preview_differ_signal(self):
        """DIFFER apenas no modo REPEAT — dígito repetido consecutivamente (XX)."""
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) >= 2:
            last_two = recent[-2:]
            if last_two[0] == last_two[1]:
                digit = last_two[0]
                last_ten = recent[-10:] if len(recent) >= 10 else recent
                if last_ten.count(digit) >= 2 and digit not in self._differ_sequence_used:
                    return {'recommendation': digit,
                            'digits': last_two[-2:],
                            'reason': f"DIFFER {digit}: repetição consecutiva",
                            'mode': 'repeat'}
        return None

    def _preview_parity_signal(self):
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 6:
            return None
        last_six = [d % 2 != 0 for d in recent[-6:]]
        odd_count = sum(last_six)
        even_count = 6 - odd_count

        # FIX F3: sinais gerados sempre que a condição estiver presente,
        # a restrição por tick é feita na execução
        if odd_count >= 5:
            return {'recommendation': 'even',
                    'digits': recent[-6:],
                    'reason': f"Tendência ÍMPAR ({odd_count}/6) → PAR",
                    'mode': 'parity'}
        if even_count >= 5:
            return {'recommendation': 'odd',
                    'digits': recent[-6:],
                    'reason': f"Tendência PAR ({even_count}/6) → ÍMPAR",
                    'mode': 'parity'}
        return None

    def _preview_matches_signal(self):
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

    def _preview_zscore_signal(self):
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

    # -----------------------------------------------------------------
    # Métodos de leitura para get_status (cache + preview)
    # -----------------------------------------------------------------
    def _peek_differ(self):
        signal = self._active_signals['differ']
        if signal and self._is_signal_still_valid(signal):
            return True, signal['recommendation']
        preview = self._preview_differ_signal()
        if preview:
            return True, preview['recommendation']
        return False, None

    def _peek_parity(self):
        signal = self._active_signals['parity']
        if signal and self._is_signal_still_valid(signal):
            return True, signal['recommendation'], signal['reason']
        preview = self._preview_parity_signal()
        if preview:
            return True, preview['recommendation'], preview['reason']
        return False, None, "Nenhuma tendência clara (5/6 necessários)"

    def _peek_matches(self):
        if self.is_matches_cooldown:
            return False
        signal = self._active_signals['matches']
        if signal and self._is_signal_still_valid(signal):
            return True
        preview = self._preview_matches_signal()
        if preview:
            return True
        return False

    def _peek_zscore(self):
        signal = self._active_signals['zscore']
        if signal and self._is_signal_still_valid(signal):
            action = 'DIFFER' if self._last_zscore_action == 'Z_DIFFER' else 'MATCHES'
            return True, action, signal['recommendation'], signal['reason']
        preview = self._preview_zscore_signal()
        if preview:
            return True, preview['action'], preview['recommendation'], preview['reason']
        return False, None, None, "Nenhum sinal Z‑Score disponível"

    # -----------------------------------------------------------------
    # Métodos de entrada (evaluate_*) – com REVALIDAÇÃO
    # -----------------------------------------------------------------
    def evaluate_differ(self):
        with self._lock:
            self._check_trade_lock_timeout()
            ok, reason = self.can_trade
            if not ok:
                return None, reason
            if self._trade_locked:
                return None, "Trade em curso"

            # FIX F2: limpa sequência usada se ainda houver muitos ticks no ciclo
            tr = self.analyzer.get_ticks_remaining()
            if tr >= 95:
                self._differ_sequence_used.clear()

            signal = self._active_signals['differ']
            if signal and self._is_signal_still_valid(signal):
                preview = self._preview_differ_signal()
                if not preview:
                    self._active_signals['differ'] = None
                    ticks_elapsed = self.analyzer.get_tick_count() - signal.get('tick_origin', 0)
                    logger.info("⚠️ Sinal DIFFER invalidado na execução — condição desapareceu")
                    return None, f"Sinal expirou ({ticks_elapsed} ticks passados)"
                rec = signal['recommendation']
                self._differ_sequence_used.add(rec)
                self._last_differ_digit = rec

                # FIX F9: log do dígito actual na tela
                logger.info(f"🎯 TELA | digit={self.analyzer.get_current_digit()} | count={self.analyzer.get_tick_count()} | aposta={rec}")
                logger.info(f"✅ DIFFER executado: snapshot {signal['id']}")
                return rec, signal['reason']
            return self._generate_differ_signal()

    def _generate_differ_signal(self):
        """DIFFER apenas no modo REPEAT."""
        # FIX F2: se ainda estamos no inicio do ciclo, limpar bloqueios antigos
        tr = self.analyzer.get_ticks_remaining()
        if tr >= 95:
            self._differ_sequence_used.clear()

        recent = self.analyzer.get_recent_digits(20)
        if len(recent) >= 2:
            last_two = recent[-2:]
            if last_two[0] == last_two[1]:
                digit = last_two[0]
                last_ten = recent[-10:] if len(recent) >= 10 else recent
                if last_ten.count(digit) >= 2:
                    if digit not in self._differ_sequence_used:
                        self._differ_sequence_used.add(digit)
                        self._last_differ_digit = digit
                        signal = self._create_signal('differ', digit, last_two[-2:],
                                                    f"DIFFER {digit}: repetição consecutiva",
                                                    mode='repeat')
                        logger.info(f"✅ DIFFER SINAL GERADO (REPEAT): {signal['id']}")
                        return digit, signal['reason']
        if len(recent) >= 3 and recent[-3] != recent[-2]:
            self._differ_sequence_used.clear()
        return None, "Nenhum padrão DIFFER"

    def evaluate_parity(self):
        with self._lock:
            self._check_trade_lock_timeout()
            ok, reason = self.can_trade
            if not ok:
                return None, reason
            if self._trade_locked:
                return None, "Trade em curso"

            # FIX F3: cooldown de 15 ticks em vez de restrição por ciclo
            current_tick = self.analyzer.get_tick_count()
            if current_tick - self._parity_last_used_tick < 15:
                return None, f"Paridade cooldown {15 - (current_tick - self._parity_last_used_tick)} ticks"

            signal = self._active_signals['parity']
            if signal and self._is_signal_still_valid(signal):
                preview = self._preview_parity_signal()
                if not preview:
                    self._active_signals['parity'] = None
                    ticks_elapsed = self.analyzer.get_tick_count() - signal.get('tick_origin', 0)
                    logger.info("⚠️ Sinal PAR/ÍMPAR invalidado na execução — condição desapareceu")
                    return None, f"Sinal expirou ({ticks_elapsed} ticks passados)"
                rec = signal['recommendation']
                self._parity_last_used_tick = current_tick
                self._last_parity_streak_type = 'odd' if rec == 'even' else 'even'

                # FIX F9: log do dígito actual na tela
                logger.info(f"🎯 TELA | digit={self.analyzer.get_current_digit()} | count={self.analyzer.get_tick_count()} | aposta={rec}")
                logger.info(f"✅ PAR/ÍMPAR executado: snapshot {signal['id']}")
                return rec, signal['reason']
            return self._generate_parity_signal()

    def _generate_parity_signal(self):
        recent = self.analyzer.get_recent_digits(20)
        if len(recent) < 6:
            return None, "Aguardando dados"
        last_six = [d % 2 != 0 for d in recent[-6:]]
        odd_count = sum(last_six)
        even_count = 6 - odd_count

        # FIX F3: já não usamos flags de paridade; a restrição é o cooldown de ticks
        if odd_count >= 5:
            if self._parity_martingale_used and not self._can_martingale():
                return None, "Martingale bloqueado"
            signal = self._create_signal('parity', 'even', recent[-6:],
                                        f"Tendência ÍMPAR ({odd_count}/6) → PAR",
                                        mode='parity')
            logger.info(f"✅ PAR/ÍMPAR SINAL GERADO: {signal['id']}")
            return 'even', signal['reason']
        if even_count >= 5:
            if self._parity_martingale_used and not self._can_martingale():
                return None, "Martingale bloqueado"
            signal = self._create_signal('parity', 'odd', recent[-6:],
                                        f"Tendência PAR ({even_count}/6) → ÍMPAR",
                                        mode='parity')
            logger.info(f"✅ PAR/ÍMPAR SINAL GERADO: {signal['id']}")
            return 'odd', signal['reason']
        return None, "Nenhuma tendência clara (5/6 necessários)"

    def _can_martingale(self):
        raw = getattr(self.client, '_ping_ms', 0)
        ping = 0 if (raw >= 9999 and self.client.streaming
                     and self.client._last_tick_time
                     and time.time() - self.client._last_tick_time < 10) else raw
        if ping >= 150:
            return False
        stable, _ = self.is_market_stable()
        return stable and (time.time() - getattr(self.client, '_last_reconnect_time', 0) >= 10)

    def evaluate_matches(self):
        with self._lock:
            self._check_trade_lock_timeout()
            ok, reason = self.can_trade
            if not ok:
                return None, reason
            if self._trade_locked:
                return None, "Trade em curso"
            if self.is_matches_cooldown:
                return None, "Cooldown MATCHES ativo"
            signal = self._active_signals['matches']
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

    def _generate_matches_signal(self):
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
                logger.info(f"✅ MATCHES SINAL GERADO: {signal['id']}")
                return digit, signal['reason']
        self._matches_sequence_used = False
        return None, f"Nenhum dígito ausente ≥{self.MATCHES_ABSENCE_THRESHOLD} ticks"

    def evaluate_zscore(self):
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
            signal = self._active_signals['zscore']
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

    def _generate_zscore_signal(self):
        z_diff, digit_diff, z_match, digit_match = self.analyzer.get_zscore_digit()
        if z_diff is not None and digit_diff is not None:
            self._last_zscore_action = 'Z_DIFFER'
            self._last_zscore_digit = digit_diff
            signal = self._create_signal('zscore', digit_diff, [],
                                        f"Z‑Score +{z_diff:.2f} → DIFFER {digit_diff}",
                                        mode='zscore')
            logger.info(f"✅ Z‑Score SINAL GERADO: {signal['id']}")
            return 'DIFFER', digit_diff, signal['reason']
        if z_match is not None and digit_match is not None:
            self._last_zscore_action = 'Z_MATCH'
            self._last_zscore_digit = digit_match
            signal = self._create_signal('zscore', digit_match, [],
                                        f"Z‑Score {z_match:.2f} → MATCHES {digit_match}",
                                        mode='zscore')
            logger.info(f"✅ Z‑Score SINAL GERADO: {signal['id']}")
            return 'MATCHES', digit_match, signal['reason']
        return None, None, "Nenhum desvio estatístico significativo"

    def _apply_cooldown(self, ticks):
        self._cooldown_until = time.time() + ticks

    def reset_sequence_state(self):
        with self._lock:
            self._differ_sequence_used.clear()
            self._parity_last_used_tick = 0   # FIX F3
            self._parity_martingale_used = False
            self._last_parity_streak_type = None
            self._matches_sequence_used = False
            self._zscore_sequence_used = False
            for key in self._active_signals:
                self._active_signals[key] = None
            self._trade_locked = False

    # ================================================================
    # notify_result — unlock_trade FORA do lock
    # ================================================================
    def notify_result(self, action, is_win):
        with self._lock:
            logger.info(
                f"📊 notify_result: action='{action}', is_win={is_win}, "
                f"losses={self._consecutive_losses}"
            )

            action_upper = action.upper()

            if not is_win:
                self._consecutive_losses += 1
                if self._consecutive_losses >= 3:
                    self._global_stop_until = time.time() + 180
                    logger.warning("🛑 STOP GLOBAL: 3 perdas consecutivas — pausa 3 min")
                    self._consecutive_losses = 0
                    self.reset_sequence_state()
                    return

                if action_upper.startswith('DIFFER') or action_upper.startswith('Z_DIFFER'):
                    self._apply_cooldown(5)
                elif action_upper in ('CALL', 'PUT', 'DIGITODD', 'DIGITEVEN'):
                    if not self._parity_martingale_used and self._last_parity_streak_type:
                        self._apply_cooldown(1)
                        logger.info("🔄 Janela de entrada reiniciada para martingale")
                    else:
                        self._apply_cooldown(5)
                elif action_upper.startswith('MATCH') or action_upper.startswith('Z_MATCH'):
                    # FIX F6: cooldown MATCHES reduzido de 150s para 45s
                    self._matches_cooldown_until = time.time() + 45
                    self._apply_cooldown(10)
                else:
                    logger.warning(f"⚠️ Ação não reconhecida '{action}' – cooldown padrão de 5s")
                    self._apply_cooldown(5)
            else:
                self._consecutive_losses = 0
                self.reset_sequence_state()
                if action_upper.startswith('DIFFER') or action_upper.startswith('Z_DIFFER'):
                    self._apply_cooldown(1)
                elif action_upper in ('CALL', 'PUT', 'DIGITODD', 'DIGITEVEN'):
                    self._apply_cooldown(2)
                elif action_upper.startswith('MATCH') or action_upper.startswith('Z_MATCH'):
                    self._apply_cooldown(1)
                else:
                    self._apply_cooldown(1)

        self.unlock_trade()

    # -----------------------------------------------------------------
    # Status para o frontend
    # -----------------------------------------------------------------
    def get_status(self):
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
            }
