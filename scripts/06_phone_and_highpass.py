# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "scipy", "matplotlib"]
# ///
"""Do the video's 'countermeasure' claims hold up physically?

(1) 'A high-pass filter doesn't remove it; boost and it comes back.'
    A linear filter scales the tone AND the noise at that frequency by the same factor, so the
    narrow-band SNR is unchanged until something adds a noise floor after the filter (quantisation).
    Re-normalising each frequency bin by its own noise level (what 'boosting' does) recovers it.
(2) 'It survives a phone call.' Narrow-band telephony passes ~300-3400 Hz (G.711) or ~200-3400 Hz
    (AMR-NB), so a ~110 Hz fundamental is removed, but harmonics 3..n pass. Every harmonic carries
    the same Doppler ratio c/(c+v_r), so harmonic n divided by n traces the same curve.
"""
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import argparse
from scipy.signal import butter, resample_poly, sosfiltfilt, stft

sys.path.insert(0, str(Path(__file__).parent))
from doppler_core import straight_line_curve

OUT = Path(__file__).parents[1] / "plots"
rng = np.random.default_rng(5)
fs, f0 = 16000, 110.0
t = np.arange(-60, 60, 1 / fs)
f_true = straight_line_curve(t, f0, 85, 1400, 0)
phase = 2 * np.pi * np.cumsum(f_true) / fs
ap = argparse.ArgumentParser()
ap.add_argument("--harmonics", choices=["weak", "strong"], default="weak",
                help="weak: harmonics fall off 4-16 dB below the fundamental; strong: 1-4 dB")
HARM = ap.parse_args().harmonics
amps = dict(weak=[1, .6, .45, .3, .2, .15], strong=[1, .9, .8, .75, .7, .65])[HARM]
tone = sum(a * np.sin((h + 1) * phase) for h, a in enumerate(amps))
x = 0.05 * tone + rng.normal(0, 0.3, t.size)          # broadband SNR about -14 dB


def ridge(sig, fs_, harmonics=(1,), lo=70, hi=170):
    """Harmonic-sum ridge: for each candidate fundamental f in [lo, hi], add the power at h*f."""
    f, tt, Z = stft(sig, fs_, nperseg=int(fs_ * 1.0), noverlap=int(fs_ * 0.75), nfft=int(fs_ * 8))
    P = np.abs(Z) ** 2
    P = P / np.median(P, axis=1, keepdims=True)   # per-frequency noise normalisation ("boost" / whitening)
    df = f[1] - f[0]
    cand = np.arange(lo, hi, df)
    S = sum(P[np.round(h * cand / df).astype(int)] for h in harmonics)
    k = S.argmax(0)
    snr = S[k, np.arange(S.shape[1])] / np.median(S, 0)
    return tt + t[0], cand[k], snr


def mu_law_8bit(sig):
    """G.711-style 8-bit mu-law companding round trip."""
    s = np.clip(sig / np.max(abs(sig)), -1, 1)
    y = np.sign(s) * np.log1p(255 * abs(s)) / np.log1p(255)
    y = np.round(y * 127) / 127
    return np.sign(y) * ((1 + 255) ** abs(y) - 1) / 255 * np.max(abs(sig))


def report(name, tt, fr, snr):
    m = abs(tt) < 50
    e = np.abs(fr[m] - np.interp(tt[m], t, f_true))
    print(f"{name:52s} median SNR {np.median(snr[m]):6.1f} | median |err| {np.median(e):6.2f} Hz | "
          f"frames within 1 Hz {100 * np.mean(e < 1):5.1f} %")
    return tt, fr


band = butter(4, [90, 140], "band", fs=fs, output="sos")
level = lambda sig: 20 * np.log10(np.std(sosfiltfilt(band, sig)) / np.std(sosfiltfilt(band, x)))
to16 = lambda s_: np.round(s_ / np.max(abs(s_)) * 32767) / 32767 * np.max(abs(s_))
res = {}
res["original, fundamental only"] = report("original, fundamental only", *ridge(x, fs))

# (1a) typical 'low cut' in an editor: 4th order (24 dB/oct) at 200 Hz, single pass, 16-bit export
lc = to16(sosfiltfilt(butter(2, 200, "highpass", fs=fs, output="sos"), x))
print(f"low-cut 24 dB/oct @200 Hz: fundamental band level {level(lc):.1f} dB")
res["low-cut 200 Hz, 16-bit, then boosted"] = report("low-cut 200 Hz, 16-bit, fundamental only", *ridge(lc, fs))

# (1b) brick-wall: 8th order zero-phase at 250 Hz (~ -110 dB at 110 Hz), 16-bit export
bw = to16(sosfiltfilt(butter(8, 250, "highpass", fs=fs, output="sos"), x))
print(f"brick-wall @250 Hz: fundamental band level {level(bw):.1f} dB")
report("brick-wall 250 Hz, fundamental only", *ridge(bw, fs))
res["brick-wall 250 Hz: harmonics 3-6"] = report("brick-wall 250 Hz, harmonic sum 3..6", *ridge(bw, fs, (3, 4, 5, 6)))

# (2) narrow-band phone: 8 kHz, 300-3400 Hz band, mu-law
ph = resample_poly(x, 1, 2)
ph = sosfiltfilt(butter(6, [300, 3400], "band", fs=8000, output="sos"), ph)
ph = mu_law_8bit(ph)
report("phone 300-3400 Hz + mu-law, fundamental only", *ridge(ph, 8000))
tt, fr = report("phone 300-3400 Hz + mu-law, harmonic sum 3..6", *ridge(ph, 8000, (3, 4, 5, 6)))

fig, ax = plt.subplots(1, 4, figsize=(21, 4.6))
for a, (name, (tt_, fr_)) in zip(ax, list(res.items()) + [("phone (8 kHz, 300–3400 Hz, μ-law): harmonics 3–6", (tt, fr))]):
    a.plot(t[::800], f_true[::800], "0.7", lw=4, label="true received fundamental")
    a.plot(tt_, fr_, "C0.", ms=3, label="traced")
    a.set(title=name, xlabel="s", ylabel="Hz", ylim=(80, 150))
    a.legend()
fig.tight_layout()
fig.savefig(OUT / f"06_phone_highpass_{HARM}.png", dpi=120)
print(f"wrote plots/06_phone_highpass_{HARM}.png")
