#!/usr/bin/env python3
"""
Script de Análise Estatística do OOS Forex.
Implementa os cálculos de Win Rate, Wilson CI e EV, com separação de amostras.
"""
import sqlite3
import os
import math
from collections import defaultdict

DB_PATH = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')

def wilson_interval(wins, n, z=1.96):
    if n == 0: return 0, 0
    p = wins / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    adj_std = math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    lower = (centre - z * adj_std) / denom
    upper = (centre + z * adj_std) / denom
    return lower, upper

def calc_ev(wins, losses, payout_pct):
    # EV por $1 apostado: (win_rate * payout) - (loss_rate * 1)
    if wins + losses == 0: return 0
    p_win = wins / (wins + losses)
    payout = payout_pct / 100.0
    return (p_win * payout) - ((1 - p_win) * 1)

def main():
    if not os.path.exists(DB_PATH):
        print(f"Erro: DB não encontrada em {DB_PATH}")
        return

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    # Buscar eventos avaliados
    rows = conn.execute(
        "SELECT threshold, outcome, symbol, ts_cross, payout FROM forex_oos_events WHERE evaluated=1"
    ).fetchall()
    conn.close()

    if not rows:
        print("Nenhum evento avaliado encontrado.")
        return

    # Estruturas para guardar os dados
    # stats[threshold] = {'wins': 0, 'losses': 0, 'void': 0, 'payouts': []}
    stats_a = defaultdict(lambda: {'wins': 0, 'losses': 0, 'void': 0, 'payouts': []})
    
    # Para a amostra B (1 evento por hora/par/limiar)
    events_b = {} # key: (threshold, hour_utc, symbol) -> event
    
    # Processar dados
    for thresh, outcome, symbol, ts_cross, payout in rows:
        payout = payout if payout else 85.0 # Fallback se não houver payout real
        
        if outcome == 'void':
            stats_a[thresh]['void'] += 1
            continue
            
        # Amostra A
        stats_a[thresh]['payouts'].append(payout)
        if outcome == 'win':
            stats_a[thresh]['wins'] += 1
        else:
            stats_a[thresh]['losses'] += 1
            
        # Amostra B
        hour_utc = int(ts_cross // 3600)
        key_b = (thresh, hour_utc, symbol)
        if key_b not in events_b:
            events_b[key_b] = {'outcome': outcome, 'payout': payout}

    # Consolidar Amostra B
    stats_b = defaultdict(lambda: {'wins': 0, 'losses': 0, 'payouts': []})
    for (thresh, hour, sym), data in events_b.items():
        stats_b[thresh]['payouts'].append(data['payout'])
        if data['outcome'] == 'win':
            stats_b[thresh]['wins'] += 1
        else:
            stats_b[thresh]['losses'] += 1

    def print_stats(title, stats):
        print(f"\n--- {title} ---")
        print(f"{'Limiar':<8} | {'n':<5} | {'Wins':<5} | {'Losses':<6} | {'Void':<5} | {'WR':<7} | {'Breakeven':<10} | {'EV($1)':<8} | {'IC95% (Wilson)':<20}")
        print("-" * 105)
        
        for thresh in sorted(stats.keys()):
            s = stats[thresh]
            n = s['wins'] + s['losses']
            if n == 0:
                continue
            
            wr = s['wins'] / n
            avg_payout = sum(s['payouts']) / len(s['payouts']) if s['payouts'] else 85.0
            breakeven = 1 / (1 + (avg_payout / 100.0))
            ev = calc_ev(s['wins'], s['losses'], avg_payout)
            
            lower, upper = wilson_interval(s['wins'], n)
            
            void_str = s.get('void', '-')
            print(f"{thresh:<8} | {n:<5} | {s['wins']:<5} | {s['losses']:<6} | {void_str:<5} | {wr*100:>5.1f}% | {breakeven*100:>8.1f}% | {ev:>8.3f} | [{lower*100:>5.1f}% - {upper*100:>5.1f}%]")

    print_stats("AMOSTRA A (Todos os Eventos)", stats_a)
    print_stats("AMOSTRA B (1 Evento / Hora UTC / Par)", stats_b)
    
    print("\nNota: O Breakeven assume o payout médio registado. O EV é calculado por cada $1 apostado.")

if __name__ == '__main__':
    main()
