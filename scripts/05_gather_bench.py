# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""Micro-benchmark: how fast can a laptop score modelled Doppler curves against a spectrogram?
Scoring = sum of spectrogram brightness along a curve, swept over time shifts and f0 scalings.
Single-threaded numpy, vectorised over the sweep (no numba/GPU). Run: uv run scripts/05_gather_bench.py
"""
import time
import numpy as np

rng = np.random.default_rng(0)
S = rng.random((2400, 512), dtype=np.float32)          # 240 s at 10 frames/s, 512 freq bins
n_samp = 1200                                          # one 120 s event at 10 frames/s
curve = 300 + 40 * np.tanh(np.linspace(-3, 3, n_samp))  # model curve in bins (any shape)
shifts = np.arange(-30, 31)                            # +-3 s at 0.1 s
scales = np.linspace(0.98, 1.02, 41)                   # f0 sweep, +-2 %
t0 = 600 + np.arange(n_samp)
best = None
for rep in range(3):
    tic = time.perf_counter()
    for _ in range(20):                                # 20 candidates
        rows = (t0[None, None, :] + shifts[:, None, None])              # (61,1,1200)
        cols = np.rint(curve[None, None, :] * scales[None, :, None]).astype(np.int32)  # (1,41,1200)
        score = S[rows, cols].sum(-1)                                   # (61,41)
        _ = np.unravel_index(score.argmax(), score.shape)
    dt = time.perf_counter() - tic
    best = dt if best is None else min(best, dt)
g = 20 * 61 * 41 * n_samp
print(f"{g:.2e} gathers in {best:.3f} s -> {g / best:.2e} gathers/s single-thread numpy "
      f"({best / 20 * 1e3:.1f} ms per candidate-event)")
