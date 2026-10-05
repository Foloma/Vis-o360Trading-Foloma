#!/usr/bin/env python3
"""FASE B — Edge discovery em digit_research_log. Read-only."""
import os, sqlite3, math
from collections import Counter, defaultdict

DB = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
PAYOUT_NET = float(os.environ.get('PAYOUT_NET', '0.09'))
OUT = '/tmp/fase_b_report.txt'

conn = sqlite3.connect(DB, timeout=10)
rows = conn.execute(
    "SELECT digit, slow_number FROM digit_research_log "
    "WHERE symbol='R_100' ORDER BY slow_number ASC"
).fetchall()
conn.close()

digits = [d for _, d in rows]
sns = [s for s, _ in rows]
n = len(digits)
assert n >= 500, f"amostra insuficiente: {n}"

gaps = sum(1 for i in range(1, n) if sns[i] != sns[i-1] + 1)
dups = n - len(set(sns))
invalid = sum(1 for d in digits if d not in range(10))
breakeven = 1 / (1 + PAYOUT_NET)

def binom_z(w, n_, p0):
    if n_ == 0: return 0.0, 1.0
    p = w / n_
    se = math.sqrt(p0 * (1 - p0) / n_)
    if se == 0: return 0.0, 1.0
    z = (p - p0) / se
    return z, math.erfc(abs(z) / math.sqrt(2))

def ev(p_win, r):
    return p_win * r - (1 - p_win)

lines = []
def out(s=""):
    lines.append(str(s))

out("=" * 72)
out("FASE B — EDGE DISCOVERY (R_100)")
out("=" * 72)
out(f"symbol: R_100  total: {n}")
out(f"first_slow: {sns[0]}  last_slow: {sns[-1]}")
out(f"gaps: {gaps}  duplicates: {dups}  invalid: {invalid}")
out(f"payout_net: {PAYOUT_NET}  breakeven: {breakeven*100:.3f}%")
out()

# BASELINE
out("--- BASELINE ---")
dc = Counter(digits)
for d in range(10):
    out(f"  digit {d}: {dc.get(d,0):5d} ({dc.get(d,0)/n*100:.2f}%)")
odd_c = sum(d % 2 for d in digits)
z_par, p_par = binom_z(odd_c, n, 0.5)
chi2 = sum((dc.get(d, 0) - n/10)**2 / (n/10) for d in range(10))
out(f"  PAR: {n-odd_c} ({(n-odd_c)/n*100:.2f}%)  IMPAR: {odd_c} ({odd_c/n*100:.2f}%)")
out(f"  chi2={chi2:.2f} (crit 16.92)  z_parity={z_par:.2f}  p={p_par:.4f}")
out()

# B-1 PARITY STREAKS
out("--- B-1: PARITY STREAKS ---")
run_at = [1] * n
for i in range(1, n):
    run_at[i] = run_at[i-1] + 1 if digits[i] % 2 == digits[i-1] % 2 else 1
st = defaultdict(lambda: {'tot': 0, 'cont': 0})
for i in range(n - 1):
    L = run_at[i]
    st[L]['tot'] += 1
    if digits[i+1] % 2 == digits[i] % 2:
        st[L]['cont'] += 1
hyps = []
out(f"{'L':>3} {'n':>5} {'rev':>5} {'p_rev':>7} {'cont':>5} {'p_cont':>7} {'p':>8}")
for L in sorted(st.keys()):
    if L > 12 or st[L]['tot'] < 30:
        continue
    c = st[L]
    rev = c['tot'] - c['cont']
    p_r = rev / c['tot']
    p_c = c['cont'] / c['tot']
    _, pv = binom_z(rev, c['tot'], 0.5)
    out(f"{L:>3} {c['tot']:>5} {rev:>5} {p_r*100:>6.1f}% {c['cont']:>5} {p_c*100:>6.1f}% {pv:>8.4f}")
    hyps.append({'name': f'par_L{L}_rev', 'n': c['tot'], 'w': rev, 'p0': 0.5, 'ev': ev(p_r, PAYOUT_NET)})
    hyps.append({'name': f'par_L{L}_cont', 'n': c['tot'], 'w': c['cont'], 'p0': 0.5, 'ev': ev(p_c, PAYOUT_NET)})
out()

# B-4 DIFFER
out("--- B-4: DIFFER por ausencia ---")
out(f"{'d':>2} {'abs':>4} {'n':>5} {'w':>4} {'hit':>7} {'lift':>6} {'p':>8} {'EV':>8}")
st2 = defaultdict(lambda: defaultdict(lambda: {'tot': 0, 'w': 0}))
last_pos = {k: -1 for k in range(10)}
for i in range(n - 1):
    last_pos[digits[i]] = i
    nxt = digits[i+1]
    for x in range(10):
        abs_ = i - last_pos[x] if last_pos[x] >= 0 else i + 1
        for th in (5, 8, 10, 12, 15, 20):
            if abs_ >= th:
                st2[x][th]['tot'] += 1
                if nxt == x:
                    st2[x][th]['w'] += 1
for x in range(10):
    for th in sorted(st2[x].keys()):
        c = st2[x][th]
        if c['tot'] < 50:
            continue
        hit = c['w'] / c['tot']
        _, pv = binom_z(c['w'], c['tot'], 0.10)
        e = ev(hit, PAYOUT_NET)
        out(f"{x:>2} {th:>4} {c['tot']:>5} {c['w']:>4} {hit*100:>6.2f}% {hit/0.10:>6.3f} {pv:>8.4f} {e:>+8.4f}")
        hyps.append({'name': f'diff_d{x}_abs{th}', 'n': c['tot'], 'w': c['w'], 'p0': 0.10, 'ev': e})
out()

# FDR
out("--- FDR-BH (alpha=0.05) ---")
for h in hyps:
    _, h['p'] = binom_z(h['w'], h['n'], h['p0'])
hyps.sort(key=lambda h: h['p'])
m = len(hyps)
passed = 0
for i, h in enumerate(hyps):
    bh = (i + 1) / m * 0.05
    h['fdr'] = h['p'] <= bh
    if h['fdr']:
        passed = i + 1
raw_sig = sum(1 for h in hyps if h['p'] < 0.05)
out(f"  hypotheses_tested: {m}")
out(f"  raw_significant (p<0.05): {raw_sig}")
out(f"  FDR_significant: {passed}")
out()

# TOP 10
out("--- TOP 10 (por p-value bruto) ---")
out(f"{'#':>3} {'name':<22} {'n':>5} {'w':>4} {'hit':>7} {'EV':>8} {'p':>8} {'FDR':>4}")
for i, h in enumerate(hyps[:10], 1):
    hit = h['w'] / h['n']
    out(f"{i:>3} {h['name']:<22} {h['n']:>5} {h['w']:>4} {hit*100:>6.2f}% {h['ev']:>+8.4f} {h['p']:>8.4f} {'S' if h['fdr'] else 'N':>4}")
out()

# DECISION
out("=" * 72)
out("DECISAO")
out("=" * 72)
if passed == 0:
    out("RESULTADO 1: NENHUM EDGE DETECTADO")
elif passed < 5:
    out(f"RESULTADO 2: {passed} hipotese(s) interessante(s), evidencia insuficiente")
else:
    out(f"RESULTADO 3: {passed} hipoteses sobreviveram a Fase B")

with open(OUT, 'w') as f:
    f.write("\n".join(lines))
print("\n".join(lines))
print(f"\n[relatorio salvo em {OUT}]")
