# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "scipy", "matplotlib"]
# ///
"""First-principles check of the Doppler model used in the video.

1. Build an audio waveform ONLY from 'sound emitted at t_e arrives at t_e + r/c' (no Doppler formula).
2. Measure its instantaneous frequency and compare with f = f0*c/(c+v_r(t_e)) (screenshot formula).
3. Compare with the 'wrong' model the video warns about (v_r evaluated at reception time) and show
   that no time shift / f0 rescale turns the wrong curve into the right one.
4. Show how much wind and temperature (ignored by the model) change the curve.
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import CubicSpline
from scipy.signal import butter, hilbert, sosfiltfilt, spectrogram

sys.path.insert(0, str(Path(__file__).parent))
from doppler_core import doppler, emission_time, radial_velocity

OUT = Path(__file__).parents[1] / "plots"
rng = np.random.default_rng(1)
c, f0 = 343.0, 118.0
obs = np.array([0.0, 0.0, 0.0])

# --- trajectory: 90 m/s, 1000 m AGL, passes 800 m to the side, starts a 1.2 deg/s turn after CPA
tt = np.arange(-140, 140, 0.01)
omega = np.deg2rad(1.2) / (1 + np.exp(-(tt - 15) / 6))           # smooth onset of a turn
psi = np.cumsum(omega) * 0.01
speed = 90 + 4 * np.tanh(tt / 40)                                 # slight acceleration
xy = np.cumsum(np.c_[speed * np.cos(psi), speed * np.sin(psi)], axis=0) * 0.01
xy -= xy[np.argmin(abs(tt))]                                     # at t=0 the plane is at x=0 ...
xy[:, 1] += 800                                                  # ... 800 m to the side
z = 1000 + 2.0 * tt                                              # slow climb
P = np.c_[xy, z]
spl = CubicSpline(tt, P)
pos, vel = spl, spl.derivative()

# --- 1. synthesise audio from propagation delay only
fs = 2000
t_r = np.arange(-100, 100, 1 / fs)
t_e = emission_time(t_r, pos, obs, c)
r = np.linalg.norm(pos(t_e) - obs, axis=1)
phase = 2 * np.pi * f0 * t_e
clean = np.sin(phase) * 1000 / r
audio = sum(a * np.sin(h * phase) for h, a in [(1, 1.0), (2, 0.5), (3, 0.3)]) * 1000 / r
audio += rng.normal(0, 2.0, audio.size)                          # broadband SNR ~ -10 dB: hard to hear

# --- 2. instantaneous frequency of the clean fundamental vs the screenshot formula
sos = butter(4, [60, 200], btype="band", fs=fs, output="sos")
inst = np.gradient(np.unwrap(np.angle(hilbert(sosfiltfilt(sos, clean))))) * fs / (2 * np.pi)
f_formula = doppler(f0, radial_velocity(t_e, pos, vel, obs), c)
mid = slice(5 * fs, -5 * fs)                                     # trim Hilbert edge effects
err = np.abs(inst[mid] - f_formula[mid])
print(f"[waveform vs formula] max |Δf| = {err.max():.4f} Hz, median = {np.median(err):.5f} Hz")

# --- 3. the 'wrong' model: radial velocity at reception time
f_wrong = doppler(f0, radial_velocity(t_r, pos, vel, obs), c)
tk = np.arange(-70, 70, 0.5)                                     # audible window, 2 Hz sampling
F_right = np.interp(tk, t_r, f_formula)
best = (np.inf, 0, 0)
for sh in np.arange(-20, 20, 0.02):
    g = np.interp(tk + sh, t_r, f_wrong)
    k = np.dot(F_right, g) / np.dot(g, g)                        # best f0 rescale (closed form)
    rms = np.sqrt(np.mean((F_right - k * g) ** 2))
    if rms < best[0]:
        best = (rms, sh, k)
rms, sh, k = best
resid = F_right - k * np.interp(tk + sh, t_r, f_wrong)
print(f"[wrong model] best shift {sh:+.2f} s, f0 scale {k:.4f}: RMS {rms:.2f} Hz, max {abs(resid).max():.2f} Hz")
d_cpa = np.linalg.norm(pos(tt) - obs, axis=1).min()
print(f"  (CPA slant distance {d_cpa:.0f} m, r/c = {d_cpa / c:.2f} s)")

# --- 4. wind + temperature not in the model
c_true = 331.3 * np.sqrt(1 + 5 / 273.15)                         # 5 °C
wind = np.array([8.0, 0, 0])                                     # 8 m/s along track (tailwind for sound ahead)
te_w = emission_time(tk, pos, obs, c_true, wind)
F_wind = f0 * np.gradient(te_w, tk)                              # f = f0 * dt_e/dt_r, exact in any medium
best_w = (np.inf, 0, 0)
for sh_ in np.arange(-3, 3, 0.01):
    g = np.interp(tk + sh_, t_r, f_formula)
    kk = np.dot(F_wind, g) / np.dot(g, g)
    rr = np.sqrt(np.mean((F_wind - kk * g) ** 2))
    if rr < best_w[0]:
        best_w = (rr, sh_, kk)
print(f"[wind 8 m/s + 5 °C vs model c=343, still air] after shift {best_w[1]:+.2f} s and "
      f"f0 rescale {best_w[2]:.4f}: RMS {best_w[0]:.3f} Hz")

# --- figure
fig, ax = plt.subplots(1, 3, figsize=(17, 5.2))
f, ts, S = spectrogram(audio, fs, nperseg=2048, noverlap=2048 - 200, nfft=8192)
ts = ts + t_r[0]
band = (f > 60) & (f < 420)
ax[0].pcolormesh(ts, f[band], 10 * np.log10(S[band] + 1e-12), shading="auto", cmap="magma",
                 vmin=np.percentile(10 * np.log10(S[band]), 80))
ax[0].plot(t_r[::200], f_formula[::200] - 12, "c:", lw=1, label="formula, $v_r$ at emission time (drawn 12 Hz low)")
ax[0].plot(t_r[::200], f_wrong[::200] - 24, "w:", lw=1, label="wrong: $v_r$ at reception time (drawn 24 Hz low)")
ax[0].set(title="Synthetic recording (built from propagation delay only)", xlabel="reception time t_r [s]",
          ylabel="Hz")
ax[0].legend(loc="upper right", fontsize=8)

ax[1].plot(t_r[mid][::50], inst[mid][::50], color="0.6", lw=3, label="measured (Hilbert) from waveform")
ax[1].plot(t_r[::50], f_formula[::50], "C0", lw=1.2, label="$f_0 c/(c+v_r(t_e))$")
ax[1].plot(tk, k * np.interp(tk + sh, t_r, f_wrong), "C3--", lw=1.2,
           label=f"wrong model, best shift {sh:+.1f}s & f0 refit")
ax[1].set(title=f"Formula matches waveform to {err.max():.3f} Hz", xlabel="t_r [s]", ylabel="Hz",
          xlim=(-70, 70))
ax[1].legend(fontsize=8)

ax[2].plot(tk, resid, "C3", label=f"wrong model residual (RMS {rms:.2f} Hz)")
ax[2].plot(tk, F_wind - best_w[2] * np.interp(tk + best_w[1], t_r, f_formula), "C2",
           label=f"wind 8 m/s + 5°C, unmodelled (RMS {best_w[0]:.2f} Hz)")
ax[2].axhspan(-0.25, 0.25, color="0.85", label="~STFT bin (0.25 Hz)")
ax[2].set(title="Residual after best time shift + f0 refit", xlabel="t_r [s]", ylabel="Hz")
ax[2].legend(fontsize=8)
fig.tight_layout()
fig.savefig(OUT / "02_first_principles.png", dpi=130)
print("wrote plots/02_first_principles.png")
