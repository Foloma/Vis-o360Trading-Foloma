#!/usr/bin/env python3
"""
Testa aleatoriedade do digit_research_log usando APENAS Python puro.
Zero dependências externas (sem pandas, sem scipy) para não esgotar a RAM do Render.
"""
import sqlite3
import os
import math
from collections import Counter

DATA_PATH = os.environ.get('DATA_PATH', '/var/data')
DB_PATH = os.path.join(DATA_PATH, 'foloma.db')

MIN_SAMPLE = 500

def main():
    if not os.path.exists(DB_PATH):
        print(f"Erro: Base de dados não encontrada em {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        # CORREÇÃO: A coluna correta é 'digit' em vez de 'slow_digit'
        cursor = conn.execute("SELECT symbol, digit FROM digit_research_log ORDER BY symbol, timestamp")
        rows = cursor.fetchall()
    except Exception as e:
        print(f"Erro ao ler a base de dados: {e}")
        return
    finally:
        conn.close()

    if not rows:
        print("AVISO: digit_research_log está vazio.")
        return

    data_by_symbol = {}
    for symbol, digit in rows:
        if digit is None: continue
        if symbol not in data_by_symbol:
            data_by_symbol[symbol] = []
        data_by_symbol[symbol].append(int(digit))

    for symbol, digits in data_by_symbol.items():
        n = len(digits)
        print(f"\n=== {symbol} (n={n}) ===")
        if n < MIN_SAMPLE:
            print(f"  AVISO: amostra abaixo de {MIN_SAMPLE} — resultados preliminares.")

        # --- 1. Frequência (Qui-Quadrado) ---
        counts = Counter(digits)
        expected = n / 10.0
        chi2 = sum(((counts.get(d, 0) - expected) ** 2) / expected for d in range(10))
        is_sig = chi2 > 16.92  # Crítico para 9 graus de liberdade, p=0.05
        print(f"  [1] Frequência: chi2={chi2:.2f} (crítico 16.92)"
              f"  {'-> DESVIO SIGNIFICATIVO' if is_sig else '-> consistente com uniforme'}")
        print(f"      Contagens: {[counts.get(d, 0) for d in range(10)]}")

        # --- 2. Runs test na paridade ---
        parities = [d % 2 for d in digits]
        n1 = parities.count(0)
        n2 = parities.count(1)
        runs = 1 + sum(1 for i in range(1, n) if parities[i] != parities[i-1])
        
        if n1 > 0 and n2 > 0:
            mean_runs = (2 * n1 * n2) / (n1 + n2) + 1
            var_runs = (2 * n1 * n2 * (2 * n1 * n2 - n1 - n2)) / (((n1 + n2) ** 2) * (n1 + n2 - 1))
            z_runs = (runs - mean_runs) / math.sqrt(var_runs) if var_runs > 0 else 0
            print(f"  [2] Runs test (paridade): runs={runs}  esperado={mean_runs:.1f}  z={z_runs:.2f}"
                  f"  {'-> DESVIO (memória real)' if abs(z_runs) > 1.96 else '-> consistente com aleatório (falácia)'}")

        # --- 3. Distância entre ocorrências (Gaps) ---
        print("  [3] Distância média entre ocorrências (esperado ~10.0):")
        gap_means = []
        for digit in range(10):
            positions = [i for i, x in enumerate(digits) if x == digit]
            if len(positions) > 5:
                gaps = [positions[i] - positions[i-1] for i in range(1, len(positions))]
                gap_means.append(sum(gaps) / len(gaps))
                print(f"      dígito {digit}: n={len(positions)}  gap_médio={gap_means[-1]:.2f}")
        if gap_means:
            overall = sum(gap_means) / len(gap_means)
            print(f"      Média geral dos gaps: {overall:.2f} (desvio de 10.0 = {overall - 10:.2f})")

if __name__ == '__main__':
    main()
