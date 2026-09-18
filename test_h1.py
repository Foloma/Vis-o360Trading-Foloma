import csv, os, math
from collections import defaultdict

P = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'forex_outcome_fixed.csv')
rows = [r for r in csv.DictReader(open(P)) if r['outcome_fixed'] in ('win','loss')]

b = defaultdict(list)
for r in rows:
    b[(r['symbol'], int(float(r['timestamp'])//900)*900)].append(r)
uniq = [sorted(v, key=lambda x: float(x['timestamp']))[0] for v in b.values()]

def wr(s):
    if not s: return 0, 0
    w = sum(1 for r in s if r['outcome_fixed']=='win')
    return w/len(s)*100, len(s)

def rsi_ext(r, d):
    if not r.get('rsi'): return False
    x = float(r['rsi'])
    return (x>70 or x<30) if d=='BUY' else (x<30 or x>70)

def pctb_ext(r):
    if not r.get('pct_b'): return False
    x = float(r['pct_b'])
    return x<0 or x>1

def zt(wa,na,wb,nb):
    if na<30 or nb<30: return None
    pa,pb = wa/100, wb/100
    pp = (pa*na+pb*nb)/(na+nb)
    se = math.sqrt(pp*(1-pp)*(1/na+1/nb))
    if se==0: return 0, 1
    z = (pa-pb)/se
    return z, 0.5*math.erfc(abs(z)/math.sqrt(2))

hi = [r for r in uniq if r.get('confidence') and float(r['confidence'])>=80]
A = [r for r in hi if rsi_ext(r, r['direction']) or pctb_ext(r)]
B = [r for r in hi if not (rsi_ext(r, r['direction']) or pctb_ext(r))]

print(f"Dedup total: {len(uniq)}, hi>=80: {len(hi)}\n")
wa,na = wr(A); wb,nb = wr(B)
print("=== H1 ===")
print(f"A (reversao extrema): WR={wa:.1f}% n={na}")
print(f"B (reversao normal):  WR={wb:.1f}% n={nb}")
r = zt(wa,na,wb,nb)
if r: print(f"Z={r[0]:.3f} p={r[1]:.4f} -> {'SIG' if r[1]<0.0167 else 'NAO sig'}")
else: print("Amostra insuficiente (min 30)")

hi_adx = [r for r in hi if r.get('adx') and float(r['adx'])>40]
w1,n1 = wr(hi_adx); wb_all,nb_all = wr(hi)
print(f"\n=== S1: ADX>40 vs baseline ===")
print(f"ADX>40: WR={w1:.1f}% n={n1}")
print(f"Baseline: WR={wb_all:.1f}% n={nb_all}")
r = zt(w1,n1,wb_all,nb_all)
if r: print(f"Z={r[0]:.3f} p={r[1]:.4f}")

print(f"\n=== S2: reversao extrema vs baseline ===")
r = zt(wa,na,wb_all,nb_all)
if r: print(f"Z={r[0]:.3f} p={r[1]:.4f}")
