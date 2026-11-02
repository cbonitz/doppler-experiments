# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "scipy", "matplotlib"]
# ///
"""End-to-end synthetic test of the video's geolocation pipeline.

World:  40x40 km region with hilly terrain, an airport, ~240 propeller flights over 6 hours.
Truth:  an observer records 25 minutes of audio. Two flights pass (one loud, one faint), nothing else
        audible in between. Audio is synthesised from propagation delay in a WINDY, COLD atmosphere.
Analyst sees: the audio file (no clock, no location) + 'ADS-B' tracks (2 s samples, correlated
        position noise, barometric altitude bias, 25 ft quantisation).
Analyst does (as in the video): spectrogram -> ridge -> per-event (v, d, t_c) fits -> offset / speed /
        distance / silence filters on a 250 m grid -> full retarded-time forward model with locked
        event-2 timing -> best candidate. Model assumes c = 343 m/s and still air.
Also: single faint flight with the clock known to +-1 minute (video's 2nd example), and the
      'wrong' (non-retarded) model as a control.

Run with --ideal to switch off all model mismatches.
"""
import argparse
import sys
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import CubicSpline
from scipy.optimize import least_squares, minimize

sys.path.insert(0, str(Path(__file__).parent))
from doppler_core import emission_time, straight_line_curve

ap = argparse.ArgumentParser()
ap.add_argument("--ideal", action="store_true")
ap.add_argument("--seed", type=int, default=7)
ap.add_argument("--ex2-window", type=float, default=1800,
                help="example 2: half-width [s] of the known-time window (video slide: 1 hour -> 1800)")
args = ap.parse_args()
TAG = ("ideal" if args.ideal else "realistic") + (f"_seed{args.seed}" if args.seed != 7 else "")
OUT = Path(__file__).parents[1] / "plots"
rng = np.random.default_rng(args.seed)
t_start = time.time()
log = lambda *a: print(f"[{time.time() - t_start:6.1f}s]", *a, flush=True)

# ---------------------------------------------------------------- configuration
HALF, GRID = 20_000.0, 250.0
C_MODEL = 343.0
C_TRUE = C_MODEL if args.ideal else 331.3 * np.sqrt(1 + 8 / 273.15)    # 8 °C -> 336.2 m/s
WIND = np.zeros(3) if args.ideal else np.array([6.0, -3.0, 0.0])      # m/s
BARO_BIAS = 0.0 if args.ideal else 45.0                                 # baro vs geometric alt, m
POS_NOISE = 0.0 if args.ideal else 8.0                                  # ADS-B correlated error, m
DAY = 6 * 3600
FS = 1000
REC_DUR = 1500.0
T0_TRUE = 9000.0                     # absolute time of recording start (unknown to analyst)
OBS_XY = np.array([7300.0, -4600.0])
AIRPORT = np.array([-9000.0, 6000.0])
R_SILENCE = 3000.0                   # anything closer than this would certainly have been heard
TOL_DT = 8.0                         # s tolerance on the event offset


def ground(x, y):
    return (320 + 70 * np.sin(x / 7000) * np.cos(y / 9000) + 35 * np.sin((x + 2 * y) / 3500)
            - 60 * np.exp(-((x - AIRPORT[0]) ** 2 + (y - AIRPORT[1]) ** 2) / 4e7))


OBS = np.array([*OBS_XY, ground(*OBS_XY) + 1.5])


# ---------------------------------------------------------------- flights
def fly(x0, heading, speed, z0, t0, dur, vz_mean=0.0, turn_sd=0.35, dt=0.5):
    n = int(dur / dt)
    om = np.zeros(n); sp = np.zeros(n); vz = np.zeros(n)
    om_i, sp_i, vz_i = 0.0, 0.0, 0.0
    for k in range(n):  # Ornstein-Uhlenbeck turn rate, speed and climb-rate perturbations
        om_i += -om_i * dt / 60 + np.deg2rad(turn_sd) * np.sqrt(2 * dt / 60) * rng.normal()
        sp_i += -sp_i * dt / 120 + 3.0 * np.sqrt(2 * dt / 120) * rng.normal()
        vz_i += -vz_i * dt / 90 + 1.2 * np.sqrt(2 * dt / 90) * rng.normal()
        om[k], sp[k], vz[k] = om_i, sp_i, vz_i
    psi = heading + np.cumsum(om) * dt
    v = speed + sp
    P = np.zeros((n, 3))
    P[:, 0] = x0[0] + np.cumsum(v * np.cos(psi)) * dt
    P[:, 1] = x0[1] + np.cumsum(v * np.sin(psi)) * dt
    P[:, 2] = z0 + np.cumsum(vz_mean + vz) * dt
    P[:, 2] = np.maximum(P[:, 2], ground(P[:, 0], P[:, 1]) + 250)
    return dict(t=t0 + np.arange(n) * dt, P=P)


def transit(t0):
    a = rng.uniform(0, 2 * np.pi)
    x0 = 30_000 * np.array([np.cos(a), np.sin(a)])
    aim = rng.uniform(-15_000, 15_000, 2)
    hd = np.arctan2(*(aim - x0)[::-1])
    return fly(x0, hd, rng.uniform(55, 125), rng.uniform(900, 3500), t0, 60_000 / 80)


def airport_op(t0):
    f = fly(AIRPORT, rng.uniform(0, 2 * np.pi), rng.uniform(55, 80), ground(*AIRPORT) + 200, t0,
            rng.uniform(500, 900), vz_mean=3.0)
    if rng.random() < 0.5:  # arrival = time-reversed departure
        f["P"] = f["P"][::-1].copy()
    return f


def heard_cpa(f, o, c=C_MODEL):
    r = np.linalg.norm(f["P"] - o, axis=1)
    k = np.argmin(r)
    return f["t"][k] + r[k] / c, r[k], k


def plant(t_heard, speed, h_agl, offset, side):
    """A flight whose CPA to OBS happens with a given horizontal offset and heard-CPA time."""
    f = fly(np.zeros(2), rng.uniform(0, 2 * np.pi), speed, 0.0, 0.0, 2000, turn_sd=0.45)
    m = len(f["t"]) // 2
    v = f["P"][m + 1, :2] - f["P"][m - 1, :2]
    perp = side * np.array([-v[1], v[0]]) / np.linalg.norm(v)
    f["P"][:, :2] += OBS_XY + offset * perp - f["P"][m, :2]
    f["P"][:, 2] += OBS[2] + h_agl - f["P"][m, 2]
    th, r, k = heard_cpa(f, OBS, C_TRUE)
    f["t"] += t_heard - th
    return f


A = plant(T0_TRUE + 310, 88, 1150, 1300, +1)
B = plant(T0_TRUE + 310 + 921, 64, 950, 2200, -1)
A["f0"], A["amp"], B["f0"], B["amp"] = 118.0, 0.55, 102.0, 0.5

flights = [A, B]
while len(flights) < 242:
    t0 = rng.uniform(-900, DAY)
    f = transit(t0) if rng.random() < 0.65 else airport_op(t0)
    rec = (f["t"] > T0_TRUE - 300) & (f["t"] < T0_TRUE + REC_DUR + 300)
    if rec.any() and np.linalg.norm(f["P"][rec] - OBS, axis=1).min() < 7000:
        continue  # world-building only: keep the recording to exactly two events, like the video
    f["f0"], f["amp"] = rng.uniform(85, 135), rng.uniform(0.2, 0.6)
    flights.append(f)
TRUE_PAIR = (0, 1)
log(f"world: {len(flights)} flights over {DAY / 3600:.0f} h, observer at {OBS.round(0)}")

# ---------------------------------------------------------------- ADS-B as the analyst sees it
def ou_noise(n, sd, tau, dt):
    e = np.zeros(n)
    for k in range(1, n):
        e[k] = e[k - 1] * (1 - dt / tau) + sd * np.sqrt(2 * dt / tau) * rng.normal()
    return e


for f in flights:
    t, P = f["t"][::4], f["P"][::4].copy()          # 2 s reports
    P[:, 0] += ou_noise(len(t), POS_NOISE, 60, 2)
    P[:, 1] += ou_noise(len(t), POS_NOISE, 60, 2)
    P[:, 2] = np.round((P[:, 2] + BARO_BIAS) / 7.62) * 7.62   # 25 ft quantisation
    f["adsb_t"], f["adsb_P"] = t, P
    f["spl"] = CubicSpline(t, P)
    f["dspl"] = f["spl"].derivative()

# ---------------------------------------------------------------- the recording
t_r = T0_TRUE + np.arange(0, REC_DUR, 1 / FS)
audio = rng.normal(0, 0.5, t_r.size)
for f in flights:
    spl_ = CubicSpline(f["t"], f["P"])
    true_spl = lambda t, spl_=spl_, f=f: spl_(np.clip(t, f["t"][0], f["t"][-1]))
    if f["t"][0] > t_r[-1] or f["t"][-1] < t_r[0] - 60:
        continue
    te = emission_time(t_r, true_spl, OBS, C_TRUE, WIND)
    ok = (te > f["t"][0]) & (te < f["t"][-1])
    if not ok.any():
        continue
    r = np.linalg.norm(true_spl(te[ok]) - OBS, axis=1)
    ph = 2 * np.pi * f["f0"] * te[ok]
    audio[ok] += f["amp"] * 1000 / r * (np.sin(ph) + 0.5 * np.sin(2 * ph) + 0.3 * np.sin(3 * ph))
pk = [f["amp"] * 1000 / np.linalg.norm(CubicSpline(f["t"], f["P"])(f["t"]) - OBS, axis=1).min() for f in (A, B)]
log("broadband SNR at CPA: " + ", ".join(f"{10 * np.log10(a**2 * 1.89 / 2 / 0.25):.0f} dB" for a in pk)
    + "  (negative = buried in noise)")

# ---------------------------------------------------------------- spectrogram + ridge
NPER, HOP, NFFT = 2048, 250, 16384
win = np.hanning(NPER)
fr = np.fft.rfftfreq(NFFT, 1 / FS)
band = (fr > 70) & (fr < 180)
starts = np.arange(0, audio.size - NPER, HOP)
spec = np.empty((band.sum(), starts.size), np.float32)
for i in range(0, starts.size, 400):
    idx = starts[i:i + 400, None] + np.arange(NPER)
    spec[:, i:i + 400] = (np.abs(np.fft.rfft(audio[idx] * win, NFFT, axis=1)) ** 2)[:, band].T
tau_frames = (starts + NPER / 2) / FS                             # recording clock, s
fb = fr[band]
kmax = spec.argmax(0)
snr = spec[kmax, np.arange(spec.shape[1])] / np.median(spec, 0)
ka = np.clip(kmax, 1, fb.size - 2)
l0, l1, l2 = (np.log(spec[ka + d, np.arange(ka.size)]) for d in (-1, 0, 1))
ridge = fb[ka] + 0.5 * (l0 - l2) / (l0 - 2 * l1 + l2) * (fb[1] - fb[0])
good = snr > 35
log("max ridge SNR per minute: " + " ".join(f"{snr[(tau_frames>=m)&(tau_frames<m+60)].max():.0f}" for m in range(0, int(REC_DUR), 60)))
# events = clusters of good frames
gt = tau_frames[good]
splits = np.where(np.diff(gt) > 20)[0]
clusters = [c for c in np.split(np.arange(gt.size), splits + 1) if gt[c[-1]] - gt[c[0]] > 40]
events = []
for c in clusters:
    tt_, ff_ = gt[c], ridge[good][c]
    med = np.array([np.median(ff_[max(0, i - 6):i + 7]) for i in range(ff_.size)])
    keep = np.abs(ff_ - med) < 1.5
    tt_, ff_ = tt_[keep][::4], ff_[keep][::4]                   # ~1 sample / s
    x0 = [np.median(ff_), C_MODEL * np.ptp(ff_) / (ff_.max() + ff_.min()), 1500,
          tt_[np.argmin(abs(ff_ - (ff_.max() + ff_.min()) / 2))]]
    fit = least_squares(lambda p: straight_line_curve(tt_, *p) - ff_, x0,
                        bounds=([50, 20, 200, tt_[0]], [300, 200, 10_000, tt_[-1]]))
    events.append(dict(tau=tt_, f=ff_, f0=fit.x[0], v=fit.x[1], d=fit.x[2], tc=fit.x[3],
                       rms=np.sqrt(np.mean(fit.fun**2))))
assert len(events) == 2, f"expected 2 events, found {len(events)}"
E1, E2 = events
DELTA = E2["tc"] - E1["tc"]
for n_, e in zip("12", events):
    log(f"event {n_}: {e['tau'].size} ridge samples, straight-line fit v={e['v']:.1f} m/s d={e['d']:.0f} m "
        f"f0={e['f0']:.1f} Hz t_c={e['tc']:.1f} s (rms {e['rms']:.2f} Hz)")
log(f"measured offset between heard CPAs: {DELTA:.1f} s")
VR = [(0.8 * e["v"], 1.25 * e["v"]) for e in events]
DR = [(0.6 * e["d"], 1.6 * e["d"]) for e in events]

# ---------------------------------------------------------------- per-flight CPA tables on the grid
gx = np.arange(-HALF, HALF + 1, GRID)
GX, GY = np.meshgrid(gx, gx)
G = np.c_[GX.ravel(), GY.ravel(), ground(GX.ravel(), GY.ravel()) + 1.5]
NP, NF = len(G), len(flights)
T = np.full((NP, NF), np.nan, np.float32); D = np.full((NP, NF), np.inf, np.float32)
V = np.zeros((NP, NF), np.float32)
for j, f in enumerate(flights):
    S = f["adsb_P"][::5]; St = f["adsb_t"][::5]                # 10 s segments
    a, ab = S[:-1], np.diff(S, axis=0)
    L2 = (ab**2).sum(1)
    for i0 in range(0, NP, 4000):
        Q = G[i0:i0 + 4000]
        u = np.clip(np.einsum("nsk,sk->ns", Q[:, None] - a[None], ab) / L2, 0, 1)
        dist = np.linalg.norm(a[None] + u[..., None] * ab[None] - Q[:, None], axis=2)
        s = dist.argmin(1)
        r = np.arange(len(Q))
        D[i0:i0 + 4000, j] = dist[r, s]
        tc = St[s] + u[r, s] * (St[s + 1] - St[s])
        T[i0:i0 + 4000, j] = tc + dist[r, s] / C_MODEL
        V[i0:i0 + 4000, j] = np.sqrt(L2[s]) / (St[s + 1] - St[s])
log(f"CPA tables: {NP} grid points x {NF} flights")

# ---------------------------------------------------------------- funnel
def triples(stage):
    out = []
    near = D < 10_000
    if stage >= 2:
        ok1 = near & (V > VR[0][0]) & (V < VR[0][1]) & (D > DR[0][0]) & (D < DR[0][1])
        ok2 = near & (V > VR[1][0]) & (V < VR[1][1]) & (D > DR[1][0]) & (D < DR[1][1])
    else:
        ok1 = ok2 = near
    for i0 in range(0, NP, 300):
        sl = slice(i0, i0 + 300)
        dt = T[sl, None, :] - T[sl, :, None]                     # [p, i, j] = T_j - T_i
        m = ok1[sl, :, None] & ok2[sl, None, :] & (np.abs(dt - DELTA) < TOL_DT)
        p, i, j = np.nonzero(m)
        out.append(np.c_[p + i0, i, j])
    return np.concatenate(out)


funnel = {"grid points": NP}
t1 = triples(1)
funnel["1: offset only"] = len(np.unique(t1[:, 0]))
t2 = triples(2)
funnel["2: + speed & distance"] = len(np.unique(t2[:, 0]))
p, i, j = t2.T
T0c = T[p, i] - E1["tc"]
Dk, Tk = D[p], T[p]
loud = (Dk < R_SILENCE) & (Tk > T0c[:, None] - 30) & (Tk < T0c[:, None] + REC_DUR + 30)
loud[np.arange(len(p)), i] = False; loud[np.arange(len(p)), j] = False
t3 = t2[~loud.any(1)]
funnel["3: + silence"] = len(np.unique(t3[:, 0]))
log("funnel (unique grid points): " + ", ".join(f"{k}={v}" for k, v in funnel.items()))
log(f"hypotheses (point, flight pair): offset {len(t1)}, +speed/dist {len(t2)}, +silence {len(t3)}; "
    f"distinct flight pairs after silence: {len(np.unique(t3[:, 1:], axis=0))}")


# ---------------------------------------------------------------- forward model scoring
def g_model(f, t_abs, P, naive=False):
    """c/(c+v_r) at reception times t_abs (shape (m, ...)) for observers P (m,3)."""
    sh = t_abs.shape
    tt = t_abs.reshape(sh[0], -1)
    o = P[:, None, :]
    te = tt if naive else emission_time(tt, f["spl"], o, C_MODEL, iters=6)
    te = np.clip(te, f["adsb_t"][0], f["adsb_t"][-1])
    d = f["spl"](te) - o
    vr = (f["dspl"](te) * d).sum(-1) / np.linalg.norm(d, axis=-1)
    return (C_MODEL / (C_MODEL + vr)).reshape(sh)


def fit_rms(y, g):
    k = (g * y).sum(-1) / (g * g).sum(-1)
    return np.sqrt(np.mean((y - k[..., None] * g) ** 2, -1)), k


def score(fi, fj, P, T_anchor, naive=False, sweep=np.arange(-12, 12.01, 0.5), fine=True):
    """Locked two-event score. T_anchor: model time of event-1 heard CPA at each P (m,)."""
    f1, f2 = flights[fi], flights[fj]
    T0 = T_anchor[:, None] - E1["tc"] + sweep[None, :]
    r1, _ = fit_rms(E1["f"], g_model(f1, T0[..., None] + E1["tau"], P, naive))
    best = T0[np.arange(len(P)), r1.argmin(1)]
    if fine:
        T0 = best[:, None] + np.arange(-0.5, 0.51, 0.1)[None]
        r1, _ = fit_rms(E1["f"], g_model(f1, T0[..., None] + E1["tau"], P, naive))
        best = T0[np.arange(len(P)), r1.argmin(1)]
    r1 = r1.min(1)
    r2, _ = fit_rms(E2["f"], g_model(f2, best[:, None] + E2["tau"], P, naive))
    n1, n2 = E1["f"].size, E2["f"].size
    return r1, r2, np.sqrt((n1 * r1**2 + n2 * r2**2) / (n1 + n2)), best


sub = np.array([[0, 0], [62.5, 62.5], [62.5, -62.5], [-62.5, 62.5], [-62.5, -62.5]])
rows = []
for (fi, fj) in np.unique(t3[:, 1:], axis=0):
    sel = t3[(t3[:, 1] == fi) & (t3[:, 2] == fj), 0]
    P = (G[sel][:, None, :2] + sub[None]).reshape(-1, 2)
    P = np.c_[P, ground(P[:, 0], P[:, 1]) + 1.5]
    anchor = np.repeat(T[sel, fi], len(sub))
    for c0 in range(0, len(P), 400):
        r1, r2, s, _ = score(fi, fj, P[c0:c0 + 400], anchor[c0:c0 + 400])
        rows.append(np.c_[np.full(len(s), fi), np.full(len(s), fj), P[c0:c0 + 400, :2], r1, r2, s,
                          anchor[c0:c0 + 400]])
R = np.concatenate(rows)
log(f"forward-model scored {len(R)} candidate points")
order = np.argsort(R[:, 6])
# best per flight pair
pairs = {}
for row in R[order]:
    key = (int(row[0]), int(row[1]))
    pairs.setdefault(key, row)
ranked = sorted(pairs.values(), key=lambda r: r[6])
for k, r in enumerate(ranked[:6]):
    tag = "  <- TRUE PAIR" if (int(r[0]), int(r[1])) == TRUE_PAIR else ""
    log(f"  #{k + 1} pair {int(r[0]):3d},{int(r[1]):3d}: rms1 {r[4]:.2f} rms2 {r[5]:.2f} total {r[6]:.2f} Hz "
        f"at ({r[2]:.0f},{r[3]:.0f}), {np.hypot(r[2] - OBS[0], r[3] - OBS[1]):.0f} m from truth{tag}")

# continuous refinement of the winner
w = ranked[0]
fi, fj = int(w[0]), int(w[1])


def obj(xy, naive=False):
    P = np.array([[xy[0], xy[1], ground(*xy) + 1.5]])
    return score(fi, fj, P, np.array([w[7]]), naive, sweep=np.arange(-12, 12.01, 0.5))[2][0]


opt = minimize(obj, w[2:4], method="Nelder-Mead", options=dict(xatol=1, fatol=1e-4, initial_simplex=[
    w[2:4], w[2:4] + [80, 0], w[2:4] + [0, 80]]))
err = np.hypot(*(opt.x - OBS[:2]))
log(f"WINNER pair {fi},{fj} refined to ({opt.x[0]:.0f},{opt.x[1]:.0f}), rms {opt.fun:.3f} Hz, "
    f"ERROR {err:.0f} m")
margin = ranked[1][6] / ranked[0][6]
log(f"runner-up / winner score ratio: {margin:.2f}")

# ---------------------------------------------------------------- landscape near truth: right vs naive model
fi, fj = TRUE_PAIR
L, step = 1200, 30
ax_ = np.arange(-L, L + 1, step)
LX, LY = np.meshgrid(ax_ + OBS[0], ax_ + OBS[1])
LP = np.c_[LX.ravel(), LY.ravel(), ground(LX.ravel(), LY.ravel()) + 1.5]
anchor_true = heard_cpa(flights[fi], OBS)[0]


def anchor_for(P, naive):
    f = flights[fi]
    r = np.linalg.norm(f["spl"](f["adsb_t"])[None] - P[:, None], axis=2)
    k = r.argmin(1)
    return f["adsb_t"][k] + (0 if naive else r[np.arange(len(P)), k] / C_MODEL)


land = {}
for naive in (False, True):
    sc = np.concatenate([score(fi, fj, LP[c:c + 500], anchor_for(LP[c:c + 500], naive), naive,
                               sweep=np.arange(-20, 20.01, 0.5) if naive else np.arange(-12, 12.01, 0.5))[2]
                         for c in range(0, len(LP), 500)])
    land[naive] = sc.reshape(LX.shape)
    k = sc.argmin()
    log(f"landscape ({'naive' if naive else 'retarded'} model): min {sc.min():.3f} Hz at "
        f"{np.hypot(*(LP[k, :2] - OBS[:2])):.0f} m from truth")
dd = np.hypot(LX - OBS[0], LY - OBS[1])
ring = land[False][(dd > 125) & (dd < 160)]
log(f"retarded-model score 141 m from truth: median {np.median(ring):.2f} Hz vs best {land[False].min():.2f} Hz")

# ---------------------------------------------------------------- example 2: one faint flight, clock known to +-W
W2 = args.ex2_window
T0_GUESS = T0_TRUE + 37
cand = np.nonzero((np.abs(T - (E2["tc"] + T0_GUESS)) < W2 + 15) & (V > VR[1][0]) & (V < VR[1][1])
                  & (D > DR[1][0]) & (D < DR[1][1]))
log(f"example 2: {len(cand[0])} (point, flight) hypotheses within ±{W2:.0f} s of the guessed clock")
rows2 = []
for fk in np.unique(cand[1]):
    pts = cand[0][cand[1] == fk]
    P = (G[pts][:, None, :2] + sub[None]).reshape(-1, 2)
    P = np.c_[P, ground(P[:, 0], P[:, 1]) + 1.5]
    anchor = np.repeat(T[pts, fk], len(sub))           # model heard-CPA time at the grid point
    for c0 in range(0, len(P), 200):
        Pc = P[c0:c0 + 200]
        T0 = anchor[c0:c0 + 200, None] - E2["tc"] + np.arange(-12, 12.1, 0.5)[None]   # align, then sweep
        tt = T0[..., None] + E2["tau"]
        r2, _ = fit_rms(E2["f"], g_model(flights[fk], tt, Pc))
        r2[np.abs(T0 - T0_GUESS) > W2] = np.inf          # the clock must lie in the known window
        rows2.append(np.c_[np.full(len(Pc), fk), Pc[:, :2], r2.min(1)])
R2 = np.concatenate(rows2)
R2 = R2[np.isfinite(R2[:, 3])]
best2 = {}
for row in R2[np.argsort(R2[:, 3])]:
    best2.setdefault(int(row[0]), row)
rk2 = sorted(best2.values(), key=lambda r: r[3])
for k, r in enumerate(rk2[:4]):
    tag = "  <- TRUE FLIGHT" if int(r[0]) == 1 else ""
    log(f"  ex2 #{k + 1} flight {int(r[0])}: rms {r[3]:.2f} Hz at {np.hypot(r[1] - OBS[0], r[2] - OBS[1]):.0f} m "
        f"from truth{tag}")
# where does the winning flight's score dip, within that flight? (along-track ambiguity check)
win2 = R2[R2[:, 0] == rk2[0][0]]
far = win2[np.hypot(win2[:, 1] - rk2[0][1], win2[:, 2] - rk2[0][2]) > 750]
log(f"  ex2: best score {rk2[0][3]:.2f} Hz; best score >750 m away on same flight: "
    f"{far[:, 3].min() if len(far) else np.nan:.2f} Hz")

# ---------------------------------------------------------------- figures
ext = [-HALF / 1000, HALF / 1000, -HALF / 1000, HALF / 1000]
fig, ax = plt.subplots(1, 2, figsize=(17, 7), gridspec_kw=dict(width_ratios=[1, 1.5]))
ax[0].imshow(ground(GX, GY), origin="lower", extent=ext, cmap="terrain", alpha=.6)
for f in flights[2:]:
    ax[0].plot(f["adsb_P"][:, 0] / 1000, f["adsb_P"][:, 1] / 1000, color="0.3", lw=.3, alpha=.6)
for f, col in [(A, "C1"), (B, "C3")]:
    ax[0].plot(f["adsb_P"][:, 0] / 1000, f["adsb_P"][:, 1] / 1000, color=col, lw=1.5)
ax[0].plot(*AIRPORT / 1000, "ks", ms=8, label="airport")
ax[0].plot(*OBS[:2] / 1000, "r*", ms=15, label="hidden observer")
ax[0].set(xlim=ext[:2], ylim=ext[2:], title=f"synthetic world: {NF} prop flights / 6 h (event flights coloured)",
          xlabel="km", ylabel="km")
ax[0].legend(loc="lower left")
db = 10 * np.log10(spec[:, ::8] + 1e-9)
ax[1].imshow(db, origin="lower", aspect="auto", extent=[0, REC_DUR, fb[0], fb[-1]], cmap="magma",
             vmin=np.percentile(db, 75), vmax=np.percentile(db, 99.95))
for e in events:
    ax[1].plot(e["tau"], e["f"] - 6, "c.", ms=1.5)
ax[1].set(title=f"25-min recording, 70–180 Hz (ridge trace drawn 6 Hz low); offset Δ = {DELTA:.1f} s",
          xlabel="recording time [s]", ylabel="Hz")
fig.tight_layout(); fig.savefig(OUT / f"04_world_{TAG}.png", dpi=110); plt.close(fig)

fig, ax = plt.subplots(1, 3, figsize=(20, 6.3))
ax[0].bar(range(len(funnel)), list(funnel.values()), color=["0.6", "C0", "C1", "C2"])
ax[0].set_yscale("log"); ax[0].set_xticks(range(len(funnel)), list(funnel.keys()), rotation=12)
for k_, v_ in enumerate(funnel.values()):
    ax[0].text(k_, v_ * 1.1, f"{v_:,}", ha="center")
ax[0].set(title="candidate grid points surviving each filter (250 m grid)")
ax[1].imshow(ground(GX, GY), origin="lower", extent=ext, cmap="Greys", alpha=.3)
for tt_, col, lab in [(t1, "0.75", "offset"), (t2, "C1", "+speed/dist"), (t3, "C2", "+silence")]:
    u_ = np.unique(tt_[:, 0])
    ax[1].scatter(G[u_, 0] / 1000, G[u_, 1] / 1000, s=1, color=col, label=lab)
ax[1].plot(*OBS[:2] / 1000, "r*", ms=15, label="truth")
ax[1].plot(*opt.x / 1000, "bx", ms=12, mew=2, label=f"estimate ({err:.0f} m off)")
ax[1].set(xlim=ext[:2], ylim=ext[2:], title="where candidates survive", xlabel="km", ylabel="km")
lg = ax[1].legend(loc="lower left")
for h in lg.legend_handles[:3]:
    h.set_sizes([30])
arr = np.array(ranked)
ax[2].scatter(arr[:, 4], arr[:, 5], s=12, color="0.4", label="best point of each flight pair")
tp = [r for r in ranked if (int(r[0]), int(r[1])) == TRUE_PAIR]
if tp:
    ax[2].scatter(tp[0][4], tp[0][5], s=120, marker="*", color="r", label="true flight pair")
ax[2].set(xscale="log", yscale="log", xlabel="event-1 RMS misfit [Hz]", ylabel="event-2 RMS misfit [Hz] (locked)",
          title="forward-model scores (lower-left = better)")
ax[2].legend()
fig.tight_layout(); fig.savefig(OUT / f"04_funnel_{TAG}.png", dpi=110); plt.close(fig)

fig, ax = plt.subplots(1, 4, figsize=(22, 5.3))
for a, e, k_ in [(ax[0], E1, 0), (ax[1], E2, 1)]:
    a.plot(e["tau"], e["f"], "k.", ms=3, label="traced ridge")
    for n_, r in enumerate(ranked[:3]):
        P = np.array([[r[2], r[3], ground(r[2], r[3]) + 1.5]])
        _, _, _, T0b = score(int(r[0]), int(r[1]), P, np.array([r[7]]))
        f_ = flights[int(r[k_])]
        g = g_model(f_, T0b[:, None] + e["tau"], P)[0]
        a.plot(e["tau"], (e["f"] @ g / (g @ g)) * g, lw=1.3, label=f"#{n_ + 1} ({r[6]:.2f} Hz)")
    a.set(title=f"event {k_ + 1}: top-3 candidates' modelled curves", xlabel="recording time [s]", ylabel="Hz")
    a.legend(fontsize=8)
for a, naive in [(ax[2], False), (ax[3], True)]:
    im = a.imshow(np.log10(land[naive]), origin="lower", extent=[-L, L, -L, L], cmap="viridis_r",
                  vmin=np.log10(land[False].min()), vmax=np.log10(land[True].max()))
    a.add_patch(plt.Circle((0, 0), 141, fill=False, color="w", ls="--"))
    a.plot(0, 0, "r*", ms=12)
    k = land[naive].argmin()
    a.plot(LX.ravel()[k] - OBS[0], LY.ravel()[k] - OBS[1], "wx", ms=10, mew=2)
    a.set(title=f"{'WRONG (no emission delay)' if naive else 'correct'} model: min {land[naive].min():.2f} Hz",
          xlabel="m east of truth", ylabel="m north of truth")
fig.colorbar(im, ax=ax[3], label="log10 RMS misfit [Hz]")
fig.tight_layout(); fig.savefig(OUT / f"04_scoring_{TAG}.png", dpi=110); plt.close(fig)

fig, ax = plt.subplots(1, 2, figsize=(14, 5))
ax[0].bar(range(len(rk2)), [r[3] for r in rk2], color=["r" if int(r[0]) == 1 else "0.5" for r in rk2])
ax[0].set_xticks(range(len(rk2)), [f"flight {int(r[0])}" for r in rk2])
ax[0].set(title="example 2: best misfit per candidate flight (red = true)", ylabel="RMS misfit [Hz]")
w2 = R2[R2[:, 0] == 1]
sc_ = ax[1].scatter((w2[:, 1] - OBS[0]) / 1000, (w2[:, 2] - OBS[1]) / 1000, c=np.log10(w2[:, 3]), s=6,
                    cmap="viridis_r")
ax[1].plot(0, 0, "r*", ms=14, label="truth")
ax[1].plot((rk2[0][1] - OBS[0]) / 1000, (rk2[0][2] - OBS[1]) / 1000, "bx", ms=10, mew=2, label="best")
ax[1].legend()
ax[1].set(title="example 2: candidate points along the true flight (colour = log10 misfit)",
          xlabel="km east of truth", ylabel="km north of truth", aspect="equal")
fig.colorbar(sc_, ax=ax[1])
fig.tight_layout(); fig.savefig(OUT / f"04_example2_{TAG}_w{W2:.0f}.png", dpi=110); plt.close(fig)
log(f"wrote plots/04_*_{TAG}.png")
