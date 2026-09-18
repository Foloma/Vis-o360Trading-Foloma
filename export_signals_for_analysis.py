#!/usr/bin/env python3
"""
Exporta sinais do ensemble de forex_signal_log para CSV,
com indicadores individuais achatados do breakdown_json.

Uso:
    python3 export_signals_for_analysis.py
        → exporta TUDO para forex_signals_export_full.csv

    EXPORT_SINCE=2026-09-05 python3 export_signals_for_analysis.py
        → exporta só sinais com timestamp > 2026-09-05T00:00:00Z para
          forex_signals_export_post_20260905.csv

    EXPORT_SINCE=1757038282 python3 export_signals_for_analysis.py
        → aceita também epoch direto

Saída: ficheiro CSV no diretório atual.
"""
import sqlite3, json, csv, os, sys, time
from datetime import datetime, timezone

DATA_PATH = os.environ.get('DATA_PATH', '/var/data')
DATABASE_PATH = os.path.join(DATA_PATH, 'foloma.db')


def parse_since_env():
    """Lê EXPORT_SINCE do ambiente. Aceita ISO 8601 ou epoch. Devolve epoch ou None."""
    raw = os.environ.get('EXPORT_SINCE', '').strip()
    if not raw:
        return None, None
    # Tentar epoch primeiro
    try:
        return float(raw), raw
    except ValueError:
        pass
    # Tentar ISO 8601
    try:
        # Aceita "2026-09-05" ou "2026-09-05T03:31:22" (assume UTC)
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp(), dt.isoformat()
    except ValueError:
        print(f"ERRO: EXPORT_SINCE inválido: '{raw}'. Use ISO 8601 (ex: 2026-09-05) ou epoch.")
        sys.exit(1)


def export_signals():
    if not os.path.exists(DATABASE_PATH):
        print(f"ERRO: base de dados não encontrada em {DATABASE_PATH}")
        sys.exit(1)

    since_epoch, since_label = parse_since_env()

    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    query = """
    SELECT id, symbol, direction, confidence, breakdown_json, price_at_signal, timestamp, active_duration_seconds
    FROM forex_signal_log
    WHERE strategy_used = 'ensemble'
    """
    params = []
    if since_epoch is not None:
        query += " AND timestamp > ?"
        params.append(since_epoch)
    query += " ORDER BY timestamp;"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("AVISO: nenhum sinal do ensemble encontrado para os filtros aplicados.")
        return

    if since_epoch is None:
        output_csv = 'forex_signals_export_full.csv'
    else:
        label_clean = since_label.replace(':', '').replace('-', '').split('+')[0][:15]
        output_csv = f'forex_signals_export_post_{label_clean}.csv'

    parse_errors = 0
    empty_indicators = 0

    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'id', 'symbol', 'direction', 'confidence', 'price_at_signal', 'timestamp',
            'active_duration_seconds', 'rsi_14', 'adx_14', 'atr_14', 'pct_b',
            'ema_dist_pct', 'momentum_10', 'macd_line', 'signal_line'
        ])

        for row in rows:
            id_, symbol, direction, confidence, breakdown_json, price, ts, duration = row
            rsi = adx = atr = pct_b = ema_dist = mom = macd = sig = None

            if not breakdown_json:
                empty_indicators += 1
            else:
                try:
                    data = json.loads(breakdown_json)
                    raw = data.get('_raw_indicators', {})
                    if not raw:
                        empty_indicators += 1
                    rsi = raw.get('rsi_14')
                    adx = raw.get('adx_14')
                    atr = raw.get('atr_14')
                    pct_b = raw.get('pct_b')
                    ema_dist = raw.get('ema_dist_pct')
                    mom = raw.get('momentum_10')
                    macd = raw.get('macd_line')
                    sig = raw.get('signal_line')
                except (json.JSONDecodeError, TypeError, AttributeError):
                    parse_errors += 1

            writer.writerow([
                id_, symbol, direction, confidence, price, ts, duration,
                rsi, adx, atr, pct_b, ema_dist, mom, macd, sig
            ])

    print(f"✅ Exportação concluída: {output_csv}")
    print(f"   Total de sinais exportados: {len(rows)}")
    if since_epoch is not None:
        print(f"   Filtro temporal: > {since_label} (epoch {since_epoch})")
    print()
    print(f"   ⚠️  Linhas com JSON inválido (indicadores ficaram None): {parse_errors}")
    print(f"   ⚠️  Linhas sem _raw_indicators (indicadores ficaram None): {empty_indicators}")
    if parse_errors > 0 or empty_indicators > 0:
        print(f"   Nota: a soma destes dois pode indicar contaminação da amostra.")
    print()
    print("NOTA: Este CSV NÃO inclui a coluna 'outcome'.")
    print("      Para cruzar com outcome, faz JOIN por 'id' com forex_signal_log.")


if __name__ == '__main__':
    export_signals()
