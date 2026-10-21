#!/usr/bin/env python3
"""
Diagnóstico de Edge da Paridade - v3 (Auditoria Final Claude)
Python Puro, Zero Dependências, Read-Only DB.
Segue estritamente os protocolos de rigor estatístico.
"""
import sqlite3
import os
import math
import datetime
from collections import Counter

DB_PATH = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
CUTOFF_EPOCH = datetime.datetime(2026, 10, 16).timestamp()
MARTINGALE_WINDOW_SEC = 120  # Janela máxima entre trades para ser considerado Martingale

def wilson_interval(wins, n, z=1.96):
    if n == 0: return 0, 0
    p = wins / n
    denominator = 1 + z**2 / n
    centre_adj = p + z**2 / (2 * n)
    std_adj = math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    lower = (centre_adj - z * std_adj) / denominator
    upper = (centre_adj + z * std_adj) / denominator
    return lower, upper

def p_value_binomial(wins, n, p0):
    if n < 30: return 1.0 # Regra do n mínimo para teste
    p_hat = wins / n
    std_err = math.sqrt((p0 * (1 - p0)) / n)
    if std_err == 0: return 1.0
    z_stat = (p_hat - p0) / std_err
    p_val = 1.0 - (0.5 * (1.0 + math.erf(z_stat / math.sqrt(2))))
    return p_val

def normalize_timestamp(ts):
    """Converte ISO string ou ms para epoch em segundos (float)."""
    if isinstance(ts, str):
        try:
            # Tenta lidar com formato ISO "2026-10-16T12:00:00" ou similar
            return datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
        except Exception:
            return 0.0
    if ts > 1e11: # Se for maior que 100 mil milhões, é milissegundos
        return ts / 1000.0
    return float(ts)

def calculate_max_drawdown(profits):
    max_dd = 0
    peak = 0
    current_sum = 0
    for p in profits:
        current_sum += p
        if current_sum > peak:
            peak = current_sum
        dd = peak - current_sum
        if dd > max_dd:
            max_dd = dd
    return max_dd

def main():
    if not os.path.exists(DB_PATH):
        print(f"Erro: Base de dados não encontrada em {DB_PATH}")
        return

    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except Exception as e:
        print(f"Erro ao abrir DB em read-only: {e}")
        return

    # 1. SCHEMA CHECK
    print("\n--- 1. PRAGMA table_info(trades) ---")
    columns = conn.execute("PRAGMA table_info(trades)").fetchall()
    has_account_col = False
    account_col_name = None
    for col in columns:
        print(f"  {col[1]} ({col[2]})")
        if col[1].lower() in ('account_type', 'is_demo', 'loginid', 'account'):
            has_account_col = True
            account_col_name = col[1]

    print(f"\n-> Coluna de conta por trade encontrada? {'SIM ('+account_col_name+')' if has_account_col else 'NÃO (Resultados podem misturar demo e real)'}")
    
    # 2. FETCH TRADES
    query = """
        SELECT id, user_id, symbol, action, amount, profit, result, timestamp 
        FROM trades 
        WHERE symbol IN ('R_100', 'R_75', 'R_50') 
        AND action IN ('CALL', 'PUT', 'DIGITODD', 'DIGITEVEN')
        ORDER BY timestamp ASC
    """
    raw_rows = conn.execute(query).fetchall()
    conn.close()

    if not raw_rows:
        print("\nAVISO: Nenhum trade de Paridade (R_100/R_75/R_50) encontrado.")
        return

    # 3. SANITY CHECKS (Counters e Amostras)
    print("\n--- 2. DISTRIBUIÇÃO DE `action` ---")
    print(Counter(r[3] for r in raw_rows))
    print("\n--- 2. DISTRIBUIÇÃO DE `symbol` ---")
    print(Counter(r[2] for r in raw_rows))
    print("\n--- 2. DISTRIBUIÇÃO DE `result` ---")
    print(Counter(r[6] for r in raw_rows))
    
    print("\n--- 2. AMOSTRA DE TIMESTAMPS (3 primeiros) ---")
    for r in raw_rows[:3]:
        print(f"  {r[7]} -> Normalizado: {normalize_timestamp(r[7])}")
        
    print("\n--- 2. AMOSTRA DE LOSSES (3 primeiros) ---")
    losses_sample = [r for r in raw_rows if r[6] == 'loss'][:3]
    for r in losses_sample:
        print(f"  amount: {r[4]}, profit: {r[5]}, result: {r[6]}")
    
    if any(r[5] is None or r[5] >= 0 for r in losses_sample):
        print("  ⚠️ AVISO: `profit` nas perdas não é negativo. O script irá calcular o P&L manualmente (-amount).")

    # Processar dados
    trades = []
    for r in raw_rows:
        if r[6] not in ('win', 'loss'): continue
        
        ts_norm = normalize_timestamp(r[7])
        amount = float(r[4]) if r[4] else 0.0
        raw_profit = float(r[5]) if r[5] is not None else 0.0
        
        # Normalizar P&L se necessário
        if r[6] == 'loss' and raw_profit >= 0:
            real_profit = -amount
        else:
            real_profit = raw_profit

        trades.append({
            'id': r[0], 'user_id': r[1], 'symbol': r[2], 'action': r[3],
            'amount': amount, 'profit': real_profit, 'result': r[6],
            'timestamp': ts_norm, 'account_type': r[8] if has_account_col else 'unknown'
        })

    # Agrupar por user_id (sem emails)
    users = list(set(t['user_id'] for t in trades))
    
    for uid in users:
        u_trades = [t for t in trades if t['user_id'] == uid]
        print(f"\n\n==================================================")
        print(f"👤 USER ID: {uid} | TOTAL TRADES: {len(u_trades)}")
        print(f"==================================================")

        # Classificar Martingale
        initial_trades = []
        martingale_trades = []
        
        prev_t = None
        for t in u_trades:
            is_mg = False
            if prev_t and prev_t['result'] == 'loss' and t['amount'] > prev_t['amount']:
                if (t['timestamp'] - prev_t['timestamp']) <= MARTINGALE_WINDOW_SEC:
                    is_mg = True
            
            if is_mg:
                martingale_trades.append(t)
            else:
                initial_trades.append(t)
            prev_t = t

        # Função de relatório
        def print_metrics(label, data_list, is_primary=False, show_pnl=False):
            n = len(data_list)
            if n == 0:
                print(f"\n  [{label}] Nenhum trade.")
                return

            wins = sum(1 for x in data_list if x['result'] == 'win')
            wr = wins / n
            
            win_profits = [x['profit'] for x in data_list if x['result'] == 'win']
            win_stakes = [x['amount'] for x in data_list if x['result'] == 'win']
            payout = (sum(win_profits) / sum(win_stakes)) if sum(win_stakes) > 0 else 0.85
            breakeven = 1 / (1 + payout) if payout > 0 else 0.54
            
            lower, upper = wilson_interval(wins, n)
            p_val = p_value_binomial(wins, n, breakeven)

            print(f"\n  [{label}]")
            print(f"    n: {n} | Wins: {wins} | Losses: {n-wins}")
            print(f"    Win Rate: {wr*100:.2f}%")
            print(f"    Payout Efetivo: {payout*100:.2f}% | Breakeven: {breakeven*100:.2f}%")
            print(f"    IC 95%: [{lower*100:.2f}% - {upper*100:.2f}%]")
            print(f"    p-value: {p_val:.4f}")
            
            if show_pnl:
                total_p = sum(x['profit'] for x in data_list)
                max_dd = calculate_max_drawdown([x['profit'] for x in data_list])
                print(f"    Lucro Líquido: ${total_p:.2f} | Pior Drawdown: -${max_dd:.2f}")
            
            if is_primary:
                if n < 300:
                    print("    🛑 VEREDICTO: AMOSTRA INSUFICIENTE (n < 300).")
                elif p_val >= 0.05 or wr <= breakeven:
                    print("    ❌ VEREDICTO: SEM EDGE COMPROVADO.")
                else:
                    print("    ✅ VEREDICTO: ACIMA DO BREAKEVEN, a confirmar em amostra nova.")

        # Bloco 1: Global (Apenas descritivo, P&L Real)
        print_metrics("GLOBAL (Todas as entradas - P&L Real)", u_trades, is_primary=False, show_pnl=True)
        
        # Bloco 2: Entradas Iniciais (Isolando Martingale - Descritivo)
        print_metrics("ENTRADAS INICIAIS (Sem Martingale)", initial_trades, is_primary=False, show_pnl=False)
        if martingale_trades:
            print(f"    (Nota: {len(martingale_trades)} trades de Martingale excluídos desta métrica. O P&L real está no bloco GLOBAL).")

        # Bloco 3: Separação por Conta (Se existir coluna)
        accounts = list(set(t['account_type'] for t in u_trades))
        for acc in accounts:
            acc_initial = [t for t in initial_trades if t['account_type'] == acc]
            
            # Bloco 4 (TESTE PRIMÁRIO): Apenas Iniciais Pós-Corte e por Conta
            primary_after = [t for t in acc_initial if t['timestamp'] >= CUTOFF_EPOCH]
            
            if acc_initial:
                print(f"\n  -- CONTA: {acc.upper()} --")
                print_metrics(f"Iniciais TOTAIS ({acc})", acc_initial, is_primary=False)
            
            # ESTE É O ÚNICO TESTE COM VEREDICTO
            if primary_after:
                print_metrics(f"TESTE PRIMÁRIO: Iniciais PÓS-16/10 ({acc})", primary_after, is_primary=True)

    print("\n==================================================")
    print("Fim do Relatório. Se o teste primário falhou, a paridade não tem edge.")
    print("==================================================\n")

if __name__ == '__main__':
    main()
