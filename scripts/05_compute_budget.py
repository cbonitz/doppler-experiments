# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""Back-of-envelope data volumes, false-match counts and compute budgets for the
ADS-B x Doppler geolocation pipeline (docs/05-algorithms-and-compute.md).

Every number tagged [SRC] comes from a cited public source; [ASSUME] is an assumption
chosen by us; everything else is derived. Run:  uv run scripts/05_compute_budget.py
Part 4 is a small Monte-Carlo that checks the analytic false-match formula against
brute-force grid matching on synthetic random straight tracks.
"""
import math

import numpy as np

SQMI = 2.589988  # km^2 per sq mi

# ---------------------------------------------------------------- inputs
CONUS_LAND_SQMI = 2_959_064          # [SRC] Wikipedia "Contiguous United States"
A = CONUS_LAND_SQMI * SQMI          # km^2
FAA_FLIGHTS_DAY = 44_360             # [SRC] FAA Air Traffic By the Numbers (FY2024)
GA_HOURS_YR = 29.0e6                 # [SRC] FAA GA survey CY2024 (via GA News)
PISTON_HOURS_YR = 16.16e6            # [SRC] same
IFR_MEAN_HOURS_OVER_CONUS = 1.8      # [ASSUME] mean airborne time of an FAA-handled flight
PROP_HOURS_VISIBLE = 35e3            # [ASSUME] prop/non-jet aircraft-hours/day in ADS-B over CONUS (range 20-60k)
V_PROP = 60.0                        # [ASSUME] mean ground speed of those aircraft, m/s (~117 kt)
C = 343.0

def hdr(s):
    print("\n" + "=" * 78 + "\n" + s + "\n" + "=" * 78)

# ---------------------------------------------------------------- 1. area and grids
hdr("1. CONUS land area and grid sizes")
print(f"CONUS land area             : {A:,.0f} km^2")
for g in (200, 100, 30):
    print(f"  cells at {g:>3} m             : {A * 1e6 / g**2:,.3e}")
test = 0.0005 * A
print(f"0.05% test area             : {test:,.0f} km^2 = {test * 25:,.0f} points at 200 m"
      f" -> 35k survivors = {35e3 / (test * 25):.0%} of points (lower bound, area was '<0.05%')")
print(f"DEM 1 arc-sec (~30x~23 m at 40N) cells over CONUS land: {A * 1e6 / (30.9 * 23.7):,.2e}"
      f" -> int16 {A * 1e6 / (30.9 * 23.7) * 2 / 1e9:.0f} GB, float32 {A * 1e6 / (30.9 * 23.7) * 4 / 1e9:.0f} GB")
print(f"DEM resampled to 100 m int16: {A * 1e6 / 1e4 * 2 / 1e9:.1f} GB ; to 200 m: {A * 1e6 / 4e4 * 2 / 1e9:.2f} GB")

# ---------------------------------------------------------------- 2. traffic and data volume
hdr("2. Traffic and ADS-B data volume (one day, CONUS)")
ifr_h = FAA_FLIGHTS_DAY * IFR_MEAN_HOURS_OVER_CONUS
ga_h = GA_HOURS_YR / 365
pist_h = PISTON_HOURS_YR / 365
print(f"FAA-handled flights x {IFR_MEAN_HOURS_OVER_CONUS} h  : {ifr_h:,.0f} aircraft-h/day")
print(f"GA all types (survey)        : {ga_h:,.0f} aircraft-h/day ; piston {pist_h:,.0f}")
tot_h = ifr_h + 0.6 * ga_h   # [ASSUME] ~60% of GA hours not already in IFR count and ADS-B visible
print(f"Total ADS-B aircraft-h/day   : ~{tot_h:,.0f} (assume 60% of GA hours visible & non-overlapping)")
print(f"Prop/non-jet visible (assume): {PROP_HOURS_VISIBLE:,.0f} aircraft-h/day")
for label, dt in (("raw 1 Hz (hires/feeder)", 1), ("5 s snapshots (ADSBx readsb-hist)", 5),
                  ("~12 s mean trace spacing", 12), ("30 s max trace interval", 30)):
    n_all = tot_h * 3600 / dt
    n_prop = PROP_HOURS_VISIBLE * 3600 / dt
    print(f"  {label:<34}: all {n_all:,.2e} rows ({n_all * 40 / 1e9:6.1f} GB @40 B/row) ;"
          f" props {n_prop:,.2e} rows ({n_prop * 24 / 1e9:5.2f} GB @24 B/row t,x,y,z,id)")
# adsb.lol global reference: 38.67M rows from 47k traces on 2023-11-19 [SRC marksblogg]
print("adsb.lol global 2023-11-19: 38.67e6 rows / 47e3 traces = "
      f"{38.67e6 / 47e3:.0f} points per aircraft-day")
print(f"adsb.lol 2024 archive 1455 GiB / 366 days = {1455 / 366:.1f} GiB/day (gz JSON, all instances)")

# ---------------------------------------------------------------- 3. traffic density, pass rates
hdr("3. Track-length density and pass rates for a random observer")
L_day = PROP_HOURS_VISIBLE * 3600 * V_PROP / 1e3          # km of prop track per day
lam_day = L_day / A                                     # km/km^2/day
lam = lam_day / 86400                                   # km/km^2/s
print(f"prop track length/day: {L_day:,.3e} km ; density lambda = {lam_day:.2f} km/km^2/day")
print("rate of passes with horizontal CPA <= R is 2*R*lambda (Crofton):")
for R in (2, 5, 10):
    print(f"  R = {R:>2} km : {2 * R * lam_day:6.1f} passes/day = {2 * R * lam_day / 24:5.2f}/h")


def band_width(d, delta, hs):
    """Horizontal width (both sides) of the ground strip whose slant CPA distance to a
    track at height h lies in [d-delta, d+delta], averaged over altitude samples hs."""
    lo = np.sqrt(np.clip((d - delta) ** 2 - hs ** 2, 0, None))
    hi = np.sqrt(np.clip((d + delta) ** 2 - hs ** 2, 0, None))
    return 2 * np.mean(hi - lo)


rng = np.random.default_rng(0)
h_samp = rng.uniform(0.3, 1.5, 100_000)  # [ASSUME] prop altitude AGL, km (1000-5000 ft)
TOL = {  # 'generous' video-like filter windows vs fit-precision windows (scripts/03)
    "generous": dict(d1=1.5, dd1=0.30, fv1=0.25, d2=1.0, dd2=0.30, fv2=0.35, tau=5.0),
    "realistic": dict(d1=1.5, dd1=0.10, fv1=0.25 * 3 / 30, d2=1.0, dd2=0.10, fv2=0.35 * 3 / 30, tau=0.5),
    "precise ": dict(d1=1.5, dd1=0.04, fv1=0.25 * 0.6 / 30, d2=1.0, dd2=0.04, fv2=0.35 * 0.6 / 30, tau=0.2),
}
SPAN = 86400.0
N200 = A * 25
print("\nper-observer expected matching (event1, event2 @ +920 s) pairs, uniform traffic:")
p_by = {}
for name, t in TOL.items():
    w1 = band_width(t["d1"], t["dd1"], h_samp)
    w2 = band_width(t["d2"], t["dd2"], h_samp)
    r1 = lam * w1 * t["fv1"]
    r2 = lam * w2 * t["fv2"]
    p = r1 * SPAN * r2 * 2 * t["tau"]
    p_by[name] = p
    print(f"  {name}: w1={w1:.2f} km w2={w2:.2f} km  r1={r1 * 86400:.3f}/day r2={r2 * 86400:.3f}/day"
          f"  p={p:.2e}  -> {p * N200:,.3g} of {N200:.2e} 200 m points (x K=18 clustering: {p * N200 * 18:,.3g})")
print("  (realistic: v +-1.5 m/s, d +-100 m, offset +-0.5 s = fit precision inflated ~5x for wind/temp/altitude systematics)")
print("  (precise: 'v' window +-0.3 m/s of a ~30 m/s-wide speed population, d +-20 m, offset +-0.1 s;"
      " v-d correlation r=0.91 would shrink the joint window a further ~2.4x per event)")

# heterogeneity: pair coincidences ~ lambda^2, silence survival ~ exp(-2 R_s lambda_all T_s)
hdr("3b. Clustering (airports) and the silence filter, lognormal traffic density")
print("K = E[lam^2]/E[lam]^2 = exp(sigma^2). Silence: no prop pass with horizontal CPA <= R_s"
      " in the ~800 s between events. Survival weighted by where pair-matches occur (lam^2).")
z = rng.standard_normal(2_000_000)
for sigma in (1.0, 1.5, 1.72, 2.0):
    lam_i = lam * np.exp(sigma * z - sigma ** 2 / 2)       # mean preserved
    K = np.mean(lam_i ** 2) / lam ** 2
    row = [f"sigma={sigma:4.2f} K={K:5.1f} generous-match points={p_by['generous'] * N200 * K:9,.0f}"]
    for Rs in (1.0, 2.0, 3.0):
        surv = np.mean(lam_i ** 2 * np.exp(-2 * Rs * lam_i * 800)) / np.mean(lam_i ** 2)
        row.append(f"R_s={Rs:.0f}km surv={surv:4.0%}")
    print("  " + " | ".join(row))
print("  video: 73k -> 15k (20% survive); K~15-20 reproduces 73k with our generous windows")

# ---------------------------------------------------------------- 4. Monte-Carlo check of the formula
hdr("4. Monte-Carlo: synthetic random straight tracks vs analytic pair-match formula")
box = 100.0       # km
g = 0.2           # 200 m grid
T_sim = 6 * 3600.0
xs = np.arange(g / 2, box, g)
X, Y = np.meshgrid(xs, xs)
P = np.stack([X.ravel(), Y.ravel()], 1)
lam_sim_day = 20.0                       # km/km^2/day, an 'airport-ish' density
L_target = lam_sim_day * box * box * T_sim / 86400
segs, Ltot = [], 0.0
while Ltot < L_target:                   # isotropic uniform random lines through the box
    th = rng.uniform(0, np.pi)
    pp = rng.uniform(-box / math.sqrt(2), box / math.sqrt(2))
    n = np.array([math.cos(th), math.sin(th)])
    u = np.array([-n[1], n[0]])
    c0 = np.array([box / 2, box / 2]) + pp * n
    # chord length inside box (approx via sampling)
    s = np.linspace(-80, 80, 3201)
    pts = c0 + s[:, None] * u
    inside = (pts >= 0).all(1) & (pts <= box).all(1)
    if not inside.any():
        continue
    Ltot += inside.sum() * 0.05
    v = rng.uniform(0.040, 0.140)        # km/s, 40-140 m/s uniform
    h = rng.uniform(0.3, 1.5)
    t0 = rng.uniform(-1800, T_sim + 1800)  # time at s=0
    segs.append((c0, u, v, h, t0))
tg = TOL["generous"]
ev1, ev2 = [], []
for c0, u, v, h, t0 in segs:
    rel = P - c0
    s_c = rel @ u
    rho = np.abs(rel @ np.array([u[1], -u[0]]))
    d = np.sqrt(rho ** 2 + h ** 2)
    t_heard = t0 + s_c / v + d * 1e3 / C
    ok = (t_heard >= 0) & (t_heard <= T_sim)
    if 0.085 <= v <= 0.115:   # 30 m/s window, fraction 0.3 of uniform 40-140
        m = ok & (np.abs(d - tg["d1"]) <= tg["dd1"])
        ev1.append((np.nonzero(m)[0], t_heard[m]))
    if 0.055 <= v <= 0.085:
        m = ok & (np.abs(d - tg["d2"]) <= tg["dd2"])
        ev2.append((np.nonzero(m)[0], t_heard[m]))
# pair join via per-point dict of event-2 times (the 'cell x time' hash join)
from collections import defaultdict
e2 = defaultdict(list)
for idx, tt in ev2:
    for i, t in zip(idx, tt):
        e2[i].append(t)
hits = 0
for idx, tt in ev1:
    for i, t in zip(idx, tt):
        if any(abs(t2 - t - 920) <= tg["tau"] for t2 in e2.get(i, ())):
            hits += 1
lam_s = (Ltot / box ** 2) / T_sim
w1 = band_width(tg["d1"], tg["dd1"], h_samp)
w2 = band_width(tg["d2"], tg["dd2"], h_samp)
r1 = lam_s * w1 * 0.3
r2 = lam_s * w2 * 0.3
pred = r1 * (T_sim - 920) * r2 * 2 * tg["tau"] * len(P)
print(f"{len(segs)} tracks, density {lam_s * 86400:.1f} km/km^2/day, {len(P):,} grid points")
print(f"brute-force matched (point, pair) hits = {hits:,} ; analytic prediction = {pred:,.0f}")

# ---------------------------------------------------------------- 5. compute budgets
hdr("5. Compute budget, Example 1 (one day CONUS, 2 events, unknown time of day)")
GATHER_CPU, GATHER_GPU = 3e9, 1e11      # [ASSUME] spectrogram gathers/s: 14-core laptop (numba), one modern GPU
FLOP_CPU, FLOP_GPU = 2e11, 2e13         # [ASSUME] sustained fp32 FLOP/s
fv = 0.25
naive_area = L_day * 20                  # 10 km each side, every prop flight
naive_rec = naive_area * 25
band_area = L_day * (fv * band_width(1.5, .3, h_samp) + 0.35 * band_width(1.0, .3, h_samp))
band_rec = band_area * 25
print(f"naive per-flight 10 km corridors: {naive_rec:,.2e} (point,flight) records, "
      f"{naive_rec * 16 / 1e9:,.0f} GB @16 B ; CPA cost ~{naive_rec * 400:,.1e} FLOP")
print(f"speed-first + distance band     : {band_rec:,.2e} records ({band_rec * 16 / 1e9:.2f} GB)"
      f" -> {naive_rec / band_rec:,.0f}x fewer")
n_cand = 35e3
samples = 2 * 120 * 10                   # 2 events x 120 s x 10 spectrogram frames/s
fp_flops = n_cand * samples * 6 * 40
sweep = n_cand * 61 * 82 * samples / 2  # 61 time shifts x (41 f0 for ev1 + 41 for ev2)
print(f"forward model: {n_cand:,.0f} candidates x {samples} samples x 6 iters -> {fp_flops:.1e} FLOP"
      f" ({fp_flops / FLOP_CPU:.2f} s CPU)")
print(f"score sweep  : {sweep:.1e} gathers -> CPU {sweep / GATHER_CPU:,.0f} s, GPU {sweep / GATHER_GPU:.1f} s,"
      f" pure-Python ({1 / 5e7:.0e} s/gather) {sweep / 5e7 / 3600:,.1f} h")

hdr("6. Example 2 (one event, time known +-1 min, CONUS)")
airborne_mean = PROP_HOURS_VISIBLE / 24
airborne_peak = 2.5 * airborne_mean      # [ASSUME] daytime peak factor
win = 120 + 60                           # CPA must fall in a 2 min window; + slack
along = win * V_PROP / 1e3
naive2 = airborne_peak * 20 * along * 25
band2 = airborne_peak * fv * band_width(1.5, .3, h_samp) * along * 25
print(f"airborne props mean {airborne_mean:,.0f}, peak ~{airborne_peak:,.0f}; along-track window {along:.1f} km")
print(f"candidates @200 m: naive {naive2:,.2e}, speed+band {band2:,.2e}; @100 m x4 = {4 * band2:,.2e}")
c2 = 4 * band2
print(f"trace-line scoring (61 shifts x 1200 samples, closed-form f0): {c2 * 61 * 1200 * 5:.1e} FLOP "
      f"-> CPU {c2 * 61 * 1200 * 5 / FLOP_CPU:,.0f} s")
print(f"brightness scoring (61 x 41 x 1200 gathers): {c2 * 61 * 41 * 1200:.1e} -> CPU "
      f"{c2 * 61 * 41 * 1200 / GATHER_CPU / 60:,.0f} min, GPU {c2 * 61 * 41 * 1200 / GATHER_GPU:,.0f} s")
p_single_gen = lam * band_width(1.5, .3, h_samp) * fv * 120
p_single_pre = lam * band_width(1.5, .04, h_samp) * fv * (0.6 / 30) * 120
p_single_real = lam * band_width(1.5, .1, h_samp) * fv * (3 / 30) * 120
print(f"false single-event matches CONUS @200 m: generous {p_single_gen * N200:,.0f}, realistic {p_single_real * N200:,.0f}, "
      f"precise (v,d only) {p_single_pre * N200:,.0f} -> curve SHAPE (turns) must break the tie")

hdr("7. Scaling")
for label, area_f, days in (("CONUS, 1 day", 1, 1), ("CONUS, 1 week", 1, 7), ("city 1000 km^2, 1 day", 1000 / A, 1),
                            ("city 1000 km^2, 1 week", 1000 / A, 7), ("state 200k km^2, 1 week", 2e5 / A, 7)):
    # linear terms: data, band records, single-event candidates ; pair term ~ area * days (p ~ T)
    k = area_f * days
    print(f"  {label:<24}: data/records x{k:9.2e} ; generous pair candidates ~{p_by['generous'] * N200 * 15 * k:12,.1f}"
          f" ; Ex-1 forward model CPU ~{max(k * sweep / GATHER_CPU, 0.01):9.1f} s")
