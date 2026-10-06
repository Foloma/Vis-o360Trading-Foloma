#!/usr/bin/env python3
"""
FASE C — Backtester C1 (Parity + DIFFER). Read-only.
Não altera produção, não escreve na DB, não toca em Forex.
"""
import os, sqlite3, json, math, random, csv
from collections import defaultdict, Counter

random.seed(42)
DB = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
OUT_DIR = '/tmp'
DEV_END = 3650
NET_PAYOUTS = [0.80, 0.85, 0.90, 0.95]

# ===== 1. CARREGAR + VALIDAR =====
conn = sqlite3.connect(DB, timeout=10)
rows = conn.execute(
    "SELECT digit, slow_number FROM digit_research_log "
    "WHERE symbol='R_100' ORDER BY slow_number ASC"
).fetchall()
conn.close()

digits = [d for d, _ in rows]
sns = [s for _, s in rows]
n = len(digits)

gaps = sum(1 for i in range(1, n) if sns[i] != sns[i-1] + 1)
dups = n - len(set(sns))
invalid = sum(1 for d in digits if d not in range(10))

report = []
def out(s=""):
    print(s)
    report.append(str(s))

out(f"INTEGRIDADE: total={n} first={sns[0]} last={sns[-1]} "
    f"gaps={gaps} dups={dups} invalid={invalid}")
if gaps or dups or invalid:
    out("INTEGRIDADE FALHOU — STOP"); raise SystemExit(1)

dev_digits = digits[:DEV_END]
test_digits = digits[DEV_END:]
out(f"dev={len(dev_digits)} test={len(test_digits)}")

# ===== 2. TRANSIÇÕES (só em dev, congeladas para test) =====
trans = defaultdict(lambda: Counter())
for i in range(len(dev_digits) - 1):
    trans[dev_digits[i]][dev_digits[i+1]] += 1

most_likely_next = {}
for c in range(10):
    tot = sum(trans[c].values())
    if tot < 50:
        most_likely_next[c] = None
    else:
        best_d, best_c = max(trans[c].items(), key=lambda x: x[1])
        most_likely_next[c] = best_d if best_c / tot > 0.15 else None

par_trans = defaultdict(lambda: Counter())
for i in range(len(dev_digits) - 1):
    par_trans[dev_digits[i] % 2][dev_digits[i+1] % 2] += 1
parity_probs = {}
for p in (0, 1):
    tot = sum(par_trans[p].values())
    parity_probs[p] = {
        'same': par_trans[p][p] / tot if tot else 0.5,
        'flip': par_trans[p][1-p] / tot if tot else 0.5,
    }

# ===== 3. SCORING =====
def differ_scores_at(i):
    scores = {x: 0 for x in range(10)}
    last_seen = {}
    for j in range(i, -1, -1):
        d = digits[j]
        if d not in last_seen:
            last_seen[d] = j
    current = digits[i]
    hist_c = Counter(digits[:i+1]); hist_t = i + 1
    rec20 = digits[max(0, i-19):i+1]
    r20c = Counter(rec20); r20t = len(rec20)

    for x in range(10):
        absence = (i - last_seen[x]) if x in last_seen else (i + 1)
        if absence >= 10: scores[x] += 2
        freq20 = r20c.get(x, 0)/r20t if r20t else 0
        if freq20 < 0.10: scores[x] += 1
        fh = hist_c.get(x, 0)/hist_t if hist_t else 0
        if fh < 0.10: scores[x] += 1
        if absence > 10: scores[x] += 1
        if most_likely_next.get(current) == x: scores[x] += 1
    return scores

def parity_scores_at(i):
    par = imp = 0
    current = digits[i]
    if current % 2 == 0: par += 1
    else: imp += 1
    streak = 1
    for j in range(i-1, -1, -1):
        if digits[j] % 2 == current % 2: streak += 1
        else: break
    if streak >= 4:
        if current % 2 == 0: imp += 1
        else: par += 1
    recent = digits[max(0, i-19):i+1]
    if recent:
        pf = sum(1 for d in recent if d % 2 == 0) / len(recent)
        if pf < 0.45: par += 1
        elif pf > 0.55: imp += 1
    pp = parity_probs.get(current % 2, {'same': 0.5, 'flip': 0.5})
    if pp['same'] > 0.5:
        if current % 2 == 0: par += 1
        else: imp += 1
    elif pp['flip'] > 0.5:
        if current % 2 == 0: imp += 1
        else: par += 1
    return par, imp

# ===== 4. SIMULAÇÃO =====
def evaluate(start, end):
    signals = []
    for i in range(start, end - 1):
        ds = differ_scores_at(i)
        ps, pi = parity_scores_at(i)
        sd = sorted(ds.items(), key=lambda x: -x[1])
        differ_sig = None
        if len(sd) >= 2:
            td, ts = sd[0]
            ss = sd[1][1]
            if ts >= 4 and (ts - ss) >= 1: differ_sig = td
        parity_sig = None
        if max(ps, pi) >= 3 and abs(ps - pi) >= 1:
            parity_sig = 0 if ps > pi else 1
        nxt = digits[i+1]
        if differ_sig is not None and parity_sig is None:
            signals.append(('DIFFER', differ_sig, nxt != differ_sig))
        elif parity_sig is not None and differ_sig is None:
            signals.append(('PARITY', parity_sig, (nxt % 2) == parity_sig))
        elif differ_sig is not None and parity_sig is not None:
            if (differ_sig % 2) != parity_sig:
                signals.append(('CONFIRMATION', differ_sig, nxt != differ_sig))
            else:
                signals.append(('NO_TRADE', None, None))
        else:
            signals.append(('NO_TRADE', None, None))
    return signals

def summarize(sigs):
    s = {'N':0,'W':0,'dN':0,'dW':0,'pN':0,'pW':0,'cN':0,'cW':0,
         'no_trade':0,'eq':[0.0],'mdd':0.0,'mls':0,'cur_ls':0}
    for typ, tgt, win in sigs:
        if typ == 'NO_TRADE': s['no_trade'] += 1; continue
        s['N'] += 1
        if win:
            s['W'] += 1; s['cur_ls'] = 0
            s['eq'].append(s['eq'][-1] + 0.90)
        else:
            s['cur_ls'] += 1; s['mls'] = max(s['mls'], s['cur_ls'])
            s['eq'].append(s['eq'][-1] - 1.0)
        if typ == 'DIFFER': s['dN'] += 1; s['dW'] += win
        elif typ == 'PARITY': s['pN'] += 1; s['pW'] += win
        elif typ == 'CONFIRMATION': s['cN'] += 1; s['cW'] += win
    peak = 0
    for v in s['eq']:
        peak = max(peak, v)
        s['mdd'] = max(s['mdd'], peak - v)
    return s

# ===== 5. EXECUTAR =====
out("\n=== DESENVOLVIMENTO ===")
dev_sig = evaluate(20, DEV_END)
ds = summarize(dev_sig)
out(f"op={len(dev_sig)} no_trade={ds['no_trade']} signals={ds['N']}")
out(f"total: W={ds['W']} hit={ds['W']/max(1,ds['N'])*100:.2f}%")
out(f"DIFFER: n={ds['dN']} hit={ds['dW']/max(1,ds['dN'])*100:.2f}%")
out(f"PARITY: n={ds['pN']} hit={ds['pW']/max(1,ds['pN'])*100:.2f}%")
out(f"CONF:   n={ds['cN']} hit={ds['cW']/max(1,ds['cN'])*100:.2f}%")
out(f"max_loss_streak={ds['mls']}  max_dd={ds['mdd']:.2f}")
for p in NET_PAYOUTS:
    h = ds['W']/max(1,ds['N'])
    out(f"  EV@pay={p}: {h*p-(1-h):+.4f}")

out("\n=== TESTE INTERNO ===")
test_sig = evaluate(DEV_END + 20, n)
ts = summarize(test_sig)
out(f"op={len(test_sig)} no_trade={ts['no_trade']} signals={ts['N']}")
out(f"total: W={ts['W']} hit={ts['W']/max(1,ts['N'])*100:.2f}%")
out(f"DIFFER: n={ts['dN']} hit={ts['dW']/max(1,ts['dN'])*100:.2f}%")
out(f"PARITY: n={ts['pN']} hit={ts['pW']/max(1,ts['pN'])*100:.2f}%")
out(f"CONF:   n={ts['cN']} hit={ts['cW']/max(1,ts['cN'])*100:.2f}%")
out(f"max_loss_streak={ts['mls']}  max_dd={ts['mdd']:.2f}")
for p in NET_PAYOUTS:
    h = ts['W']/max(1,ts['N'])
    out(f"  EV@pay={p}: {h*p-(1-h):+.4f}")

# ===== 6. PERMUTATION TEST =====
out("\n=== PERMUTATION TEST (500) ===")
sig_idx = [(i, typ, tgt) for i, (typ, tgt, _) in zip(range(DEV_END+20, n-1), test_sig)
           if typ != 'NO_TRADE']
obs_h = ts['W']/max(1, ts['N'])
null_h = []
rng = random.Random(42)
for _ in range(500):
    sh = digits[:]
    rng.shuffle(sh)
    if not sig_idx: break
    w = 0
    for i, typ, tgt in sig_idx:
        if i+1 >= len(sh): continue
        nxt = sh[i+1]
        if typ in ('DIFFER', 'CONFIRMATION'):
            if nxt != tgt: w += 1
        elif typ == 'PARITY':
            if (nxt % 2) == tgt: w += 1
    null_h.append(w / len(sig_idx))
null_h.sort()
if null_h:
    pval = sum(1 for h in null_h if h >= obs_h) / len(null_h)
    out(f"observed={obs_h*100:.2f}%")
    out(f"null_mean={sum(null_h)/len(null_h)*100:.2f}%  null_p95={null_h[int(0.95*len(null_h))]*100:.2f}%")
    out(f"p={pval:.4f}  => {'ACASO' if pval>0.05 else 'INCOMUM'}")

# ===== 7. DECISÃO =====
dev_h = ds['W']/max(1,ds['N']); test_h = ts['W']/max(1,ts['N'])
dev_ev = dev_h*0.90 - (1-dev_h); test_ev = test_h*0.90 - (1-test_h)

if ds['N'] < 30 or ts['N'] < 30:
    status, reason = "C1_INCONCLUSIVA", f"amostra pequena (dev={ds['N']}, test={ts['N']})"
elif dev_ev > 0 and test_ev > 0:
    status, reason = "C1_PROMISSORA", "EV>0 em ambos"
else:
    status, reason = "C1_REJEITADA", f"EV dev={dev_ev:+.3f}, test={test_ev:+.3f}"

out("\n" + "="*72)
out("DECISÃO")
out("="*72)
out(f"STATUS: {status}")
out(f"MOTIVO: {reason}")
out(f"EV_DEV: {dev_ev:+.4f}  EV_TEST: {test_ev:+.4f}")
out(f"HIT_DEV: {dev_h*100:.2f}%  HIT_TEST: {test_h*100:.2f}%")
out(f"DRAWDOWN_DEV: {ds['mdd']:.2f}  DRAWDOWN_TEST: {ts['mdd']:.2f}")
out(f"MAX_LOSS_STREAK: dev={ds['mls']} test={ts['mls']}")
out(f"RECOMENDAÇÃO: {'CONGELAR PARA OOS' if status=='C1_PROMISSORA' else 'DESCARTAR'}")

# ===== 8. OUTPUT FILES =====
with open(os.path.join(OUT_DIR,'fase_c_report.txt'), 'w') as f:
    f.write("\n".join(report))

with open(os.path.join(OUT_DIR,'C1_RULES.json'), 'w') as f:
    json.dump({
        "RULE_VERSION": "C1",
        "differ_weights": {"D1":2,"D2":1,"D3":1,"D4":1,"D5":1},
        "differ_threshold": 4, "differ_min_gap": 1,
        "parity_threshold": 3, "parity_min_gap": 1,
        "payout_scenarios": NET_PAYOUTS,
        "dev_range": [1, 3650], "test_range": [3651, n],
    }, f, indent=2)

with open(os.path.join(OUT_DIR,'fase_c_results.csv'), 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['set','typ','n','wins','hit','ev_080','ev_085','ev_090','ev_095'])
    for st, name in [(ds,'dev'), (ts,'test')]:
        n_ = max(1, st['N']); h = st['W']/n_
        w.writerow([name,'all',st['N'],st['W'],round(h,4),
                    round(h*0.8-(1-h),4), round(h*0.85-(1-h),4),
                    round(h*0.9-(1-h),4), round(h*0.95-(1-h),4)])
        for typ, key in [('DIFFER','d'), ('PARITY','p'), ('CONF','c')]:
            nn = max(1, st[key+'N']); hh = st[key+'W']/nn
            w.writerow([name, typ, st[key+'N'], st[key+'W'], round(hh,4),
                        round(hh*0.8-(1-hh),4), round(hh*0.85-(1-hh),4),
                        round(hh*0.9-(1-hh),4), round(hh*0.95-(1-hh),4)])

out("\n[Ficheiros: /tmp/fase_c_report.txt /tmp/C1_RULES.json /tmp/fase_c_results.csv]")
