#!/usr/bin/env python3
"""
Testa aleatoriedade do digit_stream_log: frequência, runs de paridade,
e distribuição de ausência por dígito.
"""
import sqlite3
import os
import numpy as np
import pandas as pd
from scipy import stats

DATA_PATH = os.environ.get('DATA_PATH', '/var/data')
DB_PATH = os.path.join(DATA_PATH, 'foloma.db')

MIN_SAMPLE = 500  # abaixo disto, os testes não têm poder suficiente

def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, digit, tick_count, timestamp FROM digit_stream_log "
            "ORDER BY symbol, timestamp", conn
        )
    except Exception as e:
        print(f"Erro ao ler a base de dados: {e}")
        return
    finally:
        conn.close()

    if df.empty:
        print("AVISO: digit_stream_log está vazio. Confirma se ENABLE_DIGIT_STREAM_LOG=true esteve ativo.")
        return

    for symbol, group in df.groupby('symbol'):
        digits = group['digit'].to_numpy()
        n = len(digits)
        print(f"\n=== {symbol} (n={n}) ===")
        if n < MIN_SAMPLE:
            print(f"  AVISO: amostra abaixo de {MIN_SAMPLE} — resultados pouco fiáveis, tratar como preliminar.")

        # --- 1. Frequência (qui-quadrado vs uniforme) ---
        counts = np.bincount(digits, minlength=10)[:10]
        expected = n / 10
        chi2, p_freq = stats.chisquare(counts, f_exp=[expected] * 10)
        print(f"  [1] Frequência: chi2={chi2:.2f}  p={p_freq:.4f}"
              f"  {'-> DESVIO SIGNIFICATIVO' if p_freq < 0.05 else '-> consistente com uniforme'}")
        print(f"      Contagens: {counts.tolist()}")

        # --- 2. Runs test na paridade ---
        parity = digits % 2
        n1 = int(np.sum(parity == 0))
        n2 = int(np.sum(parity == 1))
        runs = 1 + int(np.sum(parity[1:] != parity[:-1]))
        if n1 > 0 and n2 > 0 and (n1 + n2) > 1:
            mean_runs = (2 * n1 * n2) / (n1 + n2) + 1
            var_runs = (2 * n1 * n2 * (2 * n1 * n2 - n1 - n2)) / (((n1 + n2) ** 2) * (n1 + n2 - 1))
            z_runs = (runs - mean_runs) / np.sqrt(var_runs) if var_runs > 0 else 0.0
            p_runs = 2 * (1 - stats.norm.cdf(abs(z_runs)))
            print(f"  [2] Runs test (paridade): runs={runs}  esperado={mean_runs:.1f}  z={z_runs:.2f}  p={p_runs:.4f}"
                  f"  {'-> DESVIO SIGNIFICATIVO (memória real)' if p_runs < 0.05 else '-> consistente com IID (falácia)'}")
        else:
            print("  [2] Runs test: amostra insuficiente")

        # --- 3. Distância entre ocorrências por dígito ---
        print("  [3] Distância média entre ocorrências (esperado IID: ~10.0):")
        gap_means = []
        for digit in range(10):
            positions = np.where(digits == digit)[0]
            if len(positions) > 5:
                gaps = np.diff(positions)
                gap_means.append(gaps.mean())
                print(f"      dígito {digit}: n={len(positions)}  gap_médio={gaps.mean():.2f}  std={gaps.std():.2f}")
        if gap_means:
            overall = np.mean(gap_means)
            print(f"      Média geral dos gaps: {overall:.2f} (desvio de 10.0 = {overall - 10:.2f})")

if __name__ == '__main__':
    main()
