import sqlite3
import pandas as pd

def rodar_teste_de_estresse():
    # 1. Conectar à base de dados
    conn = sqlite3.connect('foloma.db')
    
    # 2. Carregar trades (considerando colunas que existem no seu app.py)
    df = pd.read_sql_query("SELECT profit, result, latency_ms FROM trades", conn)
    
    # 3. Definição das Regras de "Filtro de Assertividade"
    # Regra A: Latência > 200ms bloqueia (evita trades em redes lentas)
    # Regra B: Trades com profit negativo/loss
    
    print("--- ANÁLISE DE IMPACTO DE FILTROS ---")
    total_trades = len(df)
    total_losses = len(df[df['result'] == 'loss'])
    
    print(f"Total de trades registrados: {total_trades}")
    print(f"Losses atuais: {total_losses} ({ (total_losses/total_trades)*100:.1f}%)")
    
    # Simulação: "O que teria acontecido se tivéssemos ignorado trades com alta latência?"
    trades_altas_latencia = df[df['latency_ms'].astype(float) > 150]
    losses_alta_latencia = trades_altas_latencia[trades_altas_latencia['result'] == 'loss']
    
    print(f"
[Filtro de Latência > 150ms]:")
    print(f"- Trades filtrados (bloqueados): {len(trades_altas_latencia)}")
    print(f"- Quantos eram LOSS?: {len(losses_alta_latencia)}")
    print(f"- Potencial redução de perdas: {(len(losses_alta_latencia)/total_losses)*100:.1f}%")
    
    conn.close()

if __name__ == "__main__":
    rodar_teste_de_estresse()
