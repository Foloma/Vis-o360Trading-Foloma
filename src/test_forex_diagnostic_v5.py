#!/usr/bin/env python3
"""
Diagnóstico Forex V5 - Resposta Final ao Briefing do Claude
Implementa P0.1, P1.3, P1.4, P1.5, P1.6, P1.7, P1.8, P1.9, P1.10
"""
import sqlite3
import os
import time
import datetime

DB_PATH = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')

# DATA DE CORTE: Quando o pipeline foi corrigido (Ajusta se souberes a data exata, ex: 25/09/2026)
CUTOFF_DATE = datetime.datetime(2026, 9, 25, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
SCORE_THRESHOLD = 80

def to_epoch(val):
    """Normaliza timestamp (string ISO ou float) para epoch em segundos."""
    if isinstance(val, str):
        try:
            # Tenta ISO 8601
            return datetime.datetime.fromisoformat(val.replace('Z', '+00:00')).timestamp()
        except Exception:
            return 0.0
    if val > 1e11: # Se for maior que 100 mil milhões, é ms
        return val / 1000.0
    return float(val)

def is_market_open(ts):
    """Retorna True se o mercado Forex estiver aberto (Dom 21:00 UTC a Sex 21:00 UTC)."""
    dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    day = dt.weekday() # 0=Seg, 5=Sab, 6=Dom
    hour = dt.hour
    
    if day == 5: # Sábado
        return False
    if day == 6 and hour < 21: # Domingo antes das 21h
        return False
    if day == 4 and hour >= 21: # Sexta depois das 21h
        return False
    return True

def get_market_hours_between(start_ts, end_ts):
    """Calcula o número de horas de mercado aberto entre dois timestamps."""
    total = 0
    curr = start_ts
    while curr < end_ts:
        if is_market_open(curr):
            total += 1
        curr += 3600 # Incrementa 1 hora
    return total

def main():
    if not os.path.exists(DB_PATH):
        print(f"Erro: DB não encontrada em {DB_PATH}")
        return

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    # --- P0.1: VERIFICAÇÃO DE TIPO DE TIMESTAMP ---
    print("\n--- P0.1: AMOSTRA DE TIMESTAMPS ---")
    print("Query: SELECT timestamp, typeof(timestamp) FROM forex_signal_log LIMIT 3")
    samples = conn.execute("SELECT timestamp, typeof(timestamp) FROM forex_signal_log LIMIT 3").fetchall()
    for r in samples:
        print(f"  Valor: {r[0]} | Tipo SQLite: {r[1]}")

    # --- A1: SINAIS FOREX ---
    print(f"\n--- A1: SINAIS FOREX DESDE {datetime.datetime.fromtimestamp(CUTOFF_DATE, datetime.timezone.utc).strftime('%Y-%m-%d')} ---")
    
    # Fetch all ensemble signals
    rows = conn.execute(
        "SELECT timestamp, confidence FROM forex_signal_log WHERE strategy_used='ensemble'"
    ).fetchall()
    
    valid_signals = []
    for ts_raw, conf in rows:
        ts = to_epoch(ts_raw)
        if ts >= CUTOFF_DATE:
            valid_signals.append((ts, conf))

    if not valid_signals:
        print("⚠️ Nenhum sinal registado desde a data de corte.")
    else:
        print(f"Total de sinais no período: {len(valid_signals)}")
        days = {}
        for ts, conf in valid_signals:
            d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%d')
            if d not in days: days[d] = []
            days[d].append(conf)
            
        print(f"\n{'Data':<12} | {'Total':<5} | {'Max':<5} | {'Média':<5} | <50 | 50-65 | 65-80 | >=80")
        print("-" * 65)
        for d, confs in sorted(days.items()):
            max_c = max(confs)
            avg_c = sum(confs)/len(confs)
            b1 = sum(1 for c in confs if c < 50)
            b2 = sum(1 for c in confs if 50 <= c < 65)
            b3 = sum(1 for c in confs if 65 <= c < 80)
            b4 = sum(1 for c in confs if c >= 80)
            print(f"{d:<12} | {len(confs):<5} | {max_c:<5} | {avg_c:>4.1f}  | {b1:<3} | {b2:<5} | {b3:<5} | {b4}")

    # --- A2: FRESCURA DOS CANDLES ---
    print("\n--- A2: FRESCURA DOS CANDLES ---")
    print("Nota (P1.9): A tabela `candles` pode conter dados de backfill. Se o loop ao vivo não escrever nela, o atraso é falso.")
    
    try:
        candles = conn.execute("SELECT symbol, granularity, MAX(epoch) FROM candles GROUP BY symbol, granularity").fetchall()
        if not candles:
            print("Tabela `candles` está vazia.")
        else:
            now = time.time()
            print(f"{'Symbol':<12} | {'Gran':<6} | {'Último Candle (UTC)':<20} | {'Atraso':<10} | Status")
            print("-" * 75)
            for sym, gran, max_epoch in candles:
                if not max_epoch: continue
                dt_str = datetime.datetime.fromtimestamp(max_epoch, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')
                delay_min = (now - max_epoch) / 60
                
                # Limites por granularidade (P1.8)
                limit = 30 if gran == 900 else (90 if gran == 3600 else 10)
                
                # Verificar se o mercado está aberto agora
                is_open = is_market_open(now)
                
                status = "OK"
                if is_open and delay_min > limit:
                    status = "⚠️ ATRASADO"
                elif not is_open:
                    status = "Mercado Fechado"
                    
                print(f"{sym:<12} | {gran:<6} | {dt_str:<20} | {delay_min:>5.0f} min | {status}")
                
    except sqlite3.OperationalError as e:
        print(f"Erro ao ler tabela candles: {e}")

    # --- PARTE B: JANELAS DESLIZANTES 72H ---
    print("\n--- PARTE B: JANELAS DESLIZANTES 72H ---")
    signals_80 = [ts for ts, conf in valid_signals if conf >= SCORE_THRESHOLD]
    signals_80.sort()
    
    if not signals_80:
        print("Nenhum sinal >= 80 no período. A janela atual está aberta desde a data de corte.")
        signals_80 = [CUTOFF_DATE]
        
    last_signal_80 = signals_80[-1]
    now = time.time()
    
    # P1.7: Gap atual
    current_gap_hours = get_market_hours_between(last_signal_80, now)
    print(f"Gap atual (desde o último >=80): {current_gap_hours:.1f} horas de mercado aberto.")
    
    # P1.6: Janelas deslizantes
    total_windows = 0
    empty_windows = 0
    
    current_ts = CUTOFF_DATE
    while current_ts < now - (72 * 3600):
        window_end = current_ts + (72 * 3600)
        # Verificar se há sinal >= 80 na janela
        has_signal = any(current_ts <= s <= window_end for s in signals_80)
        if not has_signal:
            empty_windows += 1
        total_windows += 1
        current_ts += 3600
        
    if total_windows > 0:
        fraction = (empty_windows / total_windows) * 100
        print(f"Janelas de 72h (passo 1h): {empty_windows}/{total_windows} sem sinal ({fraction:.1f}%)")
        print("Nota: janelas consecutivas sobrepõem-se, não são independentes.")

    # Comparação com gaps fechados (P1.5)
    closed_gaps = []
    for i in range(1, len(signals_80)):
        gap_h = get_market_hours_between(signals_80[i-1], signals_80[i])
        closed_gaps.append(gap_h)
        
    if closed_gaps:
        max_closed = max(closed_gaps)
        print(f"\nGaps fechados: n={len(closed_gaps)} | Maior gap fechado: {max_closed:.1f}h")
        if current_gap_hours > max_closed:
            print("⚠️ O gap atual é um recorde histórico (acima do máximo fechado).")
        else:
            print("✅ O gap atual está dentro do histórico.")
    else:
        print("\nSem gaps fechados suficientes para comparação.")

    conn.close()
    print("\nFim do Relatório V5.\n")

if __name__ == '__main__':
    main()
