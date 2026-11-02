# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "scipy", "matplotlib"]
# ///
"""What can one Doppler curve tell you, and why do two curves pin a location?

(a,b) speed sets the size of the frequency drop, CPA distance sets how fast it happens
(c)   Monte-Carlo fit of (f0, v, d, t_c) from a noisy traced ridge
(d)   size of the wrong-model error on a single curve after free shift + f0 refit
(e)   a straight pass has a left/right mirror ambiguity; a turn breaks it
(f-h) geometry: one curve with unknown clock -> two lines; two curves with a locked offset -> a point
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import CubicSpline
from scipy.optimize import least_squares

sys.path.insert(0, str(Path(__file__).parent))
from doppler_core import doppler, emission_time, radial_velocity, straight_line_curve as slc

OUT = Path(__file__).parents[1] / "plots"
rng = np.random.default_rng(3)
c = 343.0
SIG = 0.4                      # Hz, ridge-tracing noise
tk = np.arange(-60, 60, 1.0)   # 2 minutes of traced ridge, 1 sample/s

fig, ax = plt.subplots(2, 4, figsize=(21, 9.5))
ax = ax.ravel()

# (a,b)
for v in (55, 90, 130):
    ax[0].plot(tk, slc(tk, 110, v, 1500, 0), label=f"v = {v} m/s")
ax[0].set(title="(a) speed sets the drop (d = 1.5 km)", xlabel="s from heard CPA", ylabel="Hz")
for d in (400, 1500, 4000):
    ax[1].plot(tk, slc(tk, 110, 90, d, 0), label=f"d = {d} m")
ax[1].set(title="(b) CPA distance sets the steepness (v = 90 m/s)", xlabel="s from heard CPA")
for a in ax[:2]:
    a.legend(fontsize=8)

# (c) fit recovery
truth = np.array([110.0, 88.0, 1700.0, 0.0])
est = []
for _ in range(400):
    y = slc(tk, *truth) + rng.normal(0, SIG, tk.size)
    res = least_squares(lambda p: slc(tk, *p) - y, x0=[np.median(y), 70, 1000, 3.0],
                        bounds=([50, 20, 100, -30], [300, 200, 10000, 30]))
    est.append(res.x)
est = np.array(est)
sd = est.std(0)
print(f"(c) fit scatter with sigma={SIG} Hz over 120 s: f0 ±{sd[0]:.2f} Hz, v ±{sd[1]:.2f} m/s, "
      f"d ±{sd[2]:.0f} m, t_c ±{sd[3]:.2f} s; corr(v,d) = {np.corrcoef(est[:, 1], est[:, 2])[0, 1]:+.2f}")
ax[2].scatter(est[:, 1], est[:, 2], s=4, alpha=.5)
ax[2].plot(truth[1], truth[2], "r*", ms=14)
ax[2].set(title=f"(c) 400 noisy fits: v ±{sd[1]:.1f} m/s, d ±{sd[2]:.0f} m", xlabel="fitted v [m/s]",
          ylabel="fitted CPA distance d [m]")


def best_refit(target, model_fn, shifts):
    """min over time shift + closed-form f0 rescale of RMS(target - k*model(t+shift))"""
    out = np.inf
    for sh in shifts:
        g = model_fn(tk + sh)
        k = target @ g / (g @ g)
        out = min(out, np.sqrt(np.mean((target - k * g) ** 2)))
    return out


# (d) wrong model vs CPA distance, straight pass, v = 90
ds = np.array([300, 600, 1000, 1500, 2000, 3000, 4000, 6000])
wrong = []
for d in ds:
    pos = lambda t, d=d: np.stack([90 * t, np.full_like(t, d), np.zeros_like(t)], -1)
    vel = lambda t: np.stack([np.full_like(t, 90.0), 0 * t, 0 * t], -1)
    o = np.zeros(3)
    right = doppler(110, radial_velocity(emission_time(tk + d / c, pos, o), pos, vel, o))
    wrong.append(best_refit(right, lambda t: doppler(110, radial_velocity(t, pos, vel, o)),
                            np.arange(-5, 25, 0.05)))
wrong = np.array(wrong)
print("(d) wrong-model RMS after best shift+f0 refit vs d:", dict(zip(ds.tolist(), wrong.round(2).tolist())))
ax[3].plot(ds / 1000, wrong, "o-", color="C3")
ax[3].axhline(SIG, color="0.5", ls="--", label=f"ridge noise {SIG} Hz")
ax[3].set(title="(d) 'wrong' model, single curve: error ≈ noise level", xlabel="CPA slant distance [km]",
          ylabel="RMS residual after best refit [Hz]")
ax[3].legend()

# (e) mirror ambiguity vs turn rate
rates = np.array([0, 0.1, 0.25, 0.5, 1.0, 2.0])
mir = []
for rate in rates:
    T = np.arange(-150, 150, 0.05)
    psi = np.deg2rad(rate) * T
    xy = np.cumsum(np.c_[np.cos(psi), np.sin(psi)] * 80 * 0.05, axis=0)
    xy -= xy[np.argmin(abs(T))]
    s = CubicSpline(T, np.c_[xy, np.full(T.size, 1000.0)])
    o1, o2 = np.array([0, 1200.0, 0]), np.array([0, -1200.0, 0])
    curve = lambda t, o: doppler(110, radial_velocity(emission_time(t, s, o), s, s.derivative(), o))
    mir.append(best_refit(curve(tk + 5, o1), lambda t: curve(t, o2), np.arange(-15, 15, 0.05)))
mir = np.array(mir)
print("(e) left-vs-right mirror RMS difference vs turn rate [deg/s]:",
      dict(zip(rates.tolist(), mir.round(2).tolist())))
ax[4].plot(rates, mir, "o-")
ax[4].axhline(SIG, color="0.5", ls="--", label=f"ridge noise {SIG} Hz")
ax[4].set(title="(e) left vs right of track: distinguishable only if the plane turns",
          xlabel="turn rate [deg/s]", ylabel="RMS curve difference [Hz]")
ax[4].legend()

# (f-h) locus maps for two straight flights, clock unknown
L, n = 12000, 241
X, Y = np.meshgrid(np.linspace(-L, L, n), np.linspace(-L, L, n))
P = np.c_[X.ravel(), Y.ravel()]
flights = [  # point at t_ref, unit direction, speed, altitude AGL, f0
    dict(a=np.array([-3000.0, 1500]), u=np.array([1.0, 0.25]), v=85, h=1200, f0=118, tref=0.0),
    dict(a=np.array([4000.0, -2000]), u=np.array([-0.3, 1.0]), v=62, h=900, f0=102, tref=900.0),
]
for f in flights:
    f["u"] = f["u"] / np.linalg.norm(f["u"])


def geom(f, p):
    rel = p - f["a"]
    along = rel @ f["u"]
    perp = np.abs(rel @ np.array([-f["u"][1], f["u"][0]]))
    d = np.hypot(perp, f["h"])
    t_cpa = f["tref"] + along / f["v"]
    return d, t_cpa + d / c, t_cpa   # slant CPA distance, heard CPA time, CPA time


true_p = np.array([[1500.0, -700.0]])
obs = []
for f in flights:
    d, Th, tc = geom(f, true_p)
    obs.append((Th[0], slc(tk + Th[0], f["f0"], f["v"], d[0], tc[0]) + rng.normal(0, SIG, tk.size)))
T0_true = 5000.0  # recording clock offset (unknown to the solver)
tau = [Th - T0_true for Th, _ in obs]  # what the analyst measures: heard-CPA times on the recording clock


def cost(f, p, Th_pred, y, tc, d):
    t_abs = Th_pred[:, None] + tk[None, :]
    g = slc(t_abs, 1.0, f["v"], d[:, None], tc[:, None])
    k = (g * y).sum(1) / (g * g).sum(1)
    return np.sqrt(np.mean((y - k[:, None] * g) ** 2, axis=1))


costs = []
for i, f in enumerate(flights):
    d, Th, tc = geom(f, P)
    if i == 0:
        T0 = Th - tau[0]                         # clock fixed by event 1 at every candidate point
        costs.append(cost(f, P, Th, obs[0][1], tc, d))
    else:
        Th_pred = tau[1] + T0                    # locked: event 2 lands wherever geometry says
        k_sh = Th_pred - Th                      # mismatch shifts the curve
        t_abs = Th_pred[:, None] + tk[None, :]
        g = slc(t_abs, 1.0, f["v"], d[:, None], tc[:, None])
        y = obs[1][1]
        k = (g * y).sum(1) / (g * g).sum(1)
        costs.append(np.sqrt(np.mean((y - k[:, None] * g) ** 2, axis=1)))
        costs.append(cost(f, P, Th, obs[1][1], tc, d))   # event 2 alone, own clock
c1, c2_locked, c2_alone = costs
comb = np.sqrt((c1**2 + c2_locked**2) / 2)
best = P[np.argmin(comb)]
print(f"(h) two straight flights, clock unknown: best point {best.round(0)}, truth {true_p[0]}, "
      f"error {np.linalg.norm(best - true_p[0]):.0f} m (grid {2 * L / (n - 1):.0f} m)")
ex = [-L / 1000, L / 1000, -L / 1000, L / 1000]
for a, C, title in [(ax[5], c1, "(f) flight 1 alone, clock unknown → two lines"),
                    (ax[6], np.minimum(c1, 3) + np.minimum(c2_alone, 3), "(g) + flight 2, each with its own clock → 4 crossings"),
                    (ax[7], comb, "(h) + locked 920 s offset → one point")]:
    im = a.imshow(np.log10(C.reshape(n, n)), origin="lower", extent=ex, cmap="viridis_r")
    for f in flights:
        s = np.linspace(-30000, 30000, 2)
        a.plot((f["a"][0] + s * f["u"][0]) / 1000, (f["a"][1] + s * f["u"][1]) / 1000, "w-", lw=1)
    a.plot(*true_p[0] / 1000, "r*", ms=12)
    a.set(title=title, xlim=ex[:2], ylim=ex[2:], xlabel="km", ylabel="km")
fig.colorbar(im, ax=ax[7], label="log10 RMS residual [Hz]")
fig.tight_layout()
fig.savefig(OUT / "03_curve_information.png", dpi=120)
print("wrote plots/03_curve_information.png")
