# Deep dive 5: Algorithms, data structures and compute budget

[← back to report](../README.md) · scripts: [`scripts/05_compute_budget.py`](../scripts/05_compute_budget.py), [`scripts/05_gather_bench.py`](../scripts/05_gather_bench.py)

How expensive is it to do what the video does: match two faint propeller Doppler curves 920 s apart against one day of ADS-B for the contiguous US (CONUS)? And how would you build it so it runs fast? This document estimates the data volumes, proposes an algorithm and data structures for each stage, gives a compute budget, and checks the video's candidate counts (35k / 73k / 15k) with a false-match model.

**Conventions.** Numbers tagged **[src]** come from the sources listed at the end. **[assume]** marks an assumption we chose. Everything else is derived from those two by `scripts/05_compute_budget.py` (output in the appendix). The per-curve fit precisions come from this project's `scripts/02` and `scripts/03`: speed ±0.3 m/s, CPA slant distance ±20 m, heard-CPA time ±0.07 s, with v and d correlated at r = +0.91.

---

## 1. Summary

- **The data is small.** A day of non-jet ADS-B over CONUS is about 10⁷–10⁸ positions, or 0.3–3 GB once reduced to (t, x, y, z, id). The DEM collapses to a 1.5 GB int16 raster at 100 m. Everything fits in a laptop's RAM.
- **The video's per-flight 10 km grid is the expensive part.** It produces about 4×10⁹ (point, flight) records, roughly 60 GB. Filtering by speed first and laying points only in the fitted distance band cuts that 23-fold. Joining flight pairs directly (pairs first, grid last) avoids a global grid altogether.
- **The forward model is cheap if vectorised.** Our measured rate is 8.5 ms per candidate-event in single-threaded numpy. Example 1, at about 35k candidates, takes about 1 minute on a 14-core laptop or a few seconds on one GPU. A pure-Python per-sample loop would take hours, which fits the video's "super computationally expensive".
- **End to end, Example 1 takes about 10 minutes on a laptop, and reading the 50 GB input is most of it.** Example 2 takes 3 s to 30 min on a laptop depending on the scoring method, or under 1 min on a GPU. A week of CONUS data multiplies everything by about 7 and is limited by I/O. A city-sized search is trivial even by brute force.
- **The video's counts are plausible.** A simple random-traffic model with the video's loose windows predicts about 4×10³ matching points nationwide if traffic were spread evenly. Airport clustering (a factor of about 18) brings that to about 7×10⁴, matching the video's 73k. A silence radius of about 2 km keeps about 20%, matching 73k → 15k.
- **A unique winner is plausible for two events.** With windows set to realistic fit precision, the model expects fewer than 10 false 200 m points nationwide, which is a handful of flight pairs. One event with the time known to ±1 min leaves thousands of (v, d)-consistent points, so only the turn content of the curve can pick out the right one. That is weaker evidence.

---

## 2. What has to be processed

| Quantity | Value | Basis |
|---|---|---|
| CONUS land area | 7.66×10⁶ km² (2,959,064 sq mi) | [src] |
| 200 m / 100 m / 30 m cells over land | 1.9×10⁸ / 7.7×10⁸ / 8.5×10⁹ | derived |
| Video's test area (<0.05 %) | <3,832 km², about 96k points at 200 m; the 35k survivors are ≥37 % of them | derived |
| FAA-handled flights per day | 44,360 (FY2024) | [src] |
| Peak aircraft airborne | about 5,500 (IFR) | [src] |
| GA fleet / hours (CY2024) | 213,756 active aircraft, 29.0 M h/yr (about 79k h/day); piston 138,960 aircraft, 16.16 M h/yr (about 44k h/day) | [src] |
| ADS-B aircraft-hours/day over CONUS | about 1.3×10⁵ (IFR 44k × 1.8 h, plus 60 % of GA) | [assume] |
| Non-jet aircraft-hours/day visible in ADS-B | 3.5×10⁴ (plausible range 2–6×10⁴) | [assume] |
| Mean non-jet ground speed | 60 m/s (≈117 kt) | [assume] |
| adsb.lol, one global day (2023-11-19) | 47k traces, 38.67 M rows, 1.1 GB gz JSON, 1.1 GB Parquet. That is about 820 points per aircraft-day, i.e. ~10–15 s spacing | [src] |
| adsb.lol 2024 archive | 1455 GiB/yr, about 4 GiB/day across all instances | [src] |
| ADSBx readsb-hist / hires-traces | snapshots every 5 s (since 2020) / 2 Hz | [src] |
| readsb trace interval | a point is guaranteed at least every 30 s (default `--json-trace-interval`) | [src] |
| 3DEP 1″ DEM | about 30 m, full CONUS coverage, NAVD88, public domain | [src] |
| Copernicus GLO-30 | 1°×1° COG tiles of 3600×3600 px, EGM2008, <4 m absolute vertical error (LE90) | [src] |

Derived volumes, from the script's section 2:

| Sampling | All aircraft (rows / GB @40 B) | Non-jets (rows / GB @24 B) |
|---|---|---|
| 1 Hz (raw / hires) | 4.6×10⁸ / 18 GB | 1.3×10⁸ / 3.0 GB |
| 5 s snapshots | 9.2×10⁷ / 3.7 GB | 2.5×10⁷ / 0.6 GB |
| ~12 s traces | 3.8×10⁷ / 1.5 GB | 1.1×10⁷ / 0.25 GB |

**DEM.** At 1″ over CONUS land, the DEM has about 1.05×10¹⁰ cells: 21 GB as int16 or 42 GB as float32. You only ever need elevation at candidate points, so resample once to a 100 m int16 raster (1.5 GB, or 0.38 GB at 200 m). Alternatively, sample the cloud-optimised GeoTIFFs lazily at the ~10⁵–10⁶ surviving points.

The video's "50 GB Parquet" fits either of two things: raw 1–5 s data with the full readsb schema and whole-flight paths beyond CONUS, or the per-point intermediate table of the naive grid (estimated at 60 GB below). Either way, that size is not intrinsic to the problem.

---

## 3. Stage-by-stage algorithm and data structures

### 3.1 Ingest and normalise (I/O-bound)
- Load the data with DuckDB or Polars, reading only the columns needed (t, lat, lon, alt_baro, alt_geom, gs, track, source flag, hex, type). Drop known jet type codes, then clip to a CONUS polygon with a 10 km buffer.
- Project to an equal-area or local metric frame (CONUS Albers, or a per-flight local ENU frame).
- Correct altitude. Use geometric altitude (GNSS) when the trace carries it; readsb traces include geometric altitude and a baro-versus-geometric flag from 2022 onward. Otherwise correct baro altitude with the nearest altimeter setting (see §6).
- Resample each flight to 1 s with cubic Hermite interpolation, using ADS-B ground speed and track as the end-point derivatives. Clamp to the observed time span, because a spline extrapolates wildly outside it (gotcha found in this project's `scripts/03`).
- Output: a columnar array (flight_id, t, x, y, z, vx, vy, vz) of about 10⁷–10⁸ rows, ≤3 GB.

### 3.2 Per-event flight classes (cheap, very selective)
For each event, select segments whose local speed is inside that event's speed window. This costs one pass, O(N_rows). It keeps about 25–35 % with the video's loose ranges [assume], and about 2 % with the ±0.3 m/s fit precision. Apply this before any geometry: it is the cheapest filter and the most selective one per unit of work.

### 3.3 Candidate geometry: distance bands, not a 10 km grid
**Naive (the video's approach).** For every non-jet flight, put a 200 m grid over a 20 km-wide corridor and compute CPA and delayed-CPA time at every point. That is 7.6×10⁶ km of track × 20 km × 25 points/km², or **3.8×10⁹ (point, flight) records (~60 GB)**, plus about 1.5×10¹² FLOP of CPA arithmetic. It is memory/IO-bound, needs out-of-core sorting, and takes tens of minutes on a laptop.

**Band-limited.** The fitted CPA slant distance d ± Δ means a track at height h is heard only from two ground strips. These lie at horizontal offset √(d²−h²), and each is about 2Δ·d/ρ wide. Rasterise only those strips (swept-corridor rasterisation per segment), and only for speed-qualified segments. With the video's loose windows, the combined strip width is 1.7 km for event 1 and 1.3 km for event 2, giving **1.65×10⁸ records (2.6 GB), 23× fewer**. CPA is analytic: for each 1 s segment, project the point onto the segment and clamp. A point takes the minimum over the few segments whose buffered bounding boxes cover it.

**Cell keys.** Points must be shared across flights to join them, so give them a stable key. Options are integer (i, j) on a fixed Albers 100/200 m lattice, or H3 resolution 9 (mean area 0.105 km², edge length ~200 m) or resolution 10 (0.015 km², ~76 m) [src]. Hexagons have uniform neighbour distances, which helps refinement and smoothing.

### 3.4 The 920 s offset join: hash join, or pairs first
**Cell × time hash join.** Put the event-2 records in a hash map from cell to a sorted array of delayed-CPA times. For each event-1 record, binary-search for t₁ + 920 ± τ. Cost is O(R log k) with R ≈ 10⁸, which is seconds to a minute. A sort-merge on (cell, t) works just as well and spills to disk gracefully.

**Pairs first, grid last (recommended).** Invert the loop order so you iterate over flight pairs, not grid points:
1. Index event-2-class segments in a spatio-temporal hash keyed by (5 km cell, 60 s bucket), or in a 3-D R-tree over (x, y, t) boxes.
2. For each event-1-class segment, probe the neighbouring cells in the time buckets covering t + 920 ± (τ + slack). The slack covers propagation delay (≤ 30 s at 10 km) and along-track motion.
3. For each candidate segment pair, compute the locus exactly. Each distance band is a pair of strips (polygons). Over a straight segment, t_b(x) − t_a(x) is affine in x to first order, so the offset condition is a third strip. The intersection of these convex polygons is the full answer for that pair.
4. Rasterise only those small polygons, at 100 m or finer. Do not rasterise the 7.7×10⁸ global cells.

This costs O(N_seg · probes + K), where K is the number of candidate pairs. That is ~10⁷ probes, which takes seconds. It never materialises the per-point table, and it carries the flight-pair identity forward, which the video needed later anyway when it grouped points by pair.

### 3.5 The silence filter is a range query
A candidate (point p, heard times t₁, t₂) fails if some other flight would have been audible between the two events. That means a pass with slant CPA ≤ R_s, a loudness proxy, or an aircraft class whose delayed-CPA time falls in (t₁ + δ, t₂ − δ). Implementation:
- Query the same (cell, time) hash or 3-D R-tree, built over all aircraft, jets included if desired. Use the box p ± R_s × [t₁ + δ − R_s/c, t₂ − δ], then run the exact CPA test on the hits. Each query is O(log N + k), so 73k queries take under a second.
- Better still, work per pair-polygon: subtract each interfering pass's "audible footprint" (a buffered strip over the window) from the polygon.
- Make it a soft penalty rather than a hard veto. Audibility depends on wind and temperature gradients and on masking (§6). A false veto destroys the true answer, while a false pass only costs compute. You can also check the spectrogram directly: forward-model the would-be interferer and see whether its ridge is absent.

### 3.6 Forward model and scoring
For each candidate (point, flight pair), and for each spectrogram frame t_r of each event:
1. **Emission time.** Solve t_r = t_e + r(t_e)/c. Fixed-point iteration contracts by |v|/c ≈ 0.2–0.4 per step. It reaches sub-millisecond accuracy in about 6 iterations from cold, or 2–3 when warm-started from the previous frame. Alternatively, on a piecewise-linear 1 s track the condition is a quadratic in t_e within each segment, so solve it in closed form like the straight-line formula, with no iteration.
2. **Doppler.** Compute v_r = v·(x − o)/|x − o| and f = f₀·c/(c + v_r).
3. **Score.** Sum spectrogram brightness along the curve. Sweep the event-1 time alignment (±3 s at 0.1 s = 61 shifts) and f₀ (41 values). Event 2 is time-locked to event 1 and only gets its own f₀ sweep.

Making it fast:
- **The curve shape does not depend on the sweep.** Compute it once per candidate (2,400 samples × 6 iterations × ~40 FLOP ≈ 6×10⁵ FLOP). The sweep is pure gathers: 61 × 82 × 1,200 ≈ 6×10⁶ per candidate.
- **Use a log-frequency spectrogram.** Scaling f₀ becomes a vertical shift, so the 2-D sweep is a correlation of the spectrogram with a one-pixel-wide curve mask. That is cache-friendly, and an FFT does it when the sweep is large.
- **Pre-score cheaply.** Read the ADS-B geometry at CPA for each candidate: v_cpa, d_cpa and the local turn rate. Gate on a Mahalanobis distance to the fitted (v, d), using the r = 0.91 covariance. A straight pass has a closed-form curve (`straight_line_curve` in `scripts/doppler_core.py`), which also serves as a cheap pre-score.
- **Coarse to fine, branch and bound.** Score at 200 m. The video reports that the right answer up to 141 m off scores poorly, so the score kernel is about 100 m wide. Keep only cells whose score plus a smoothness bound beats the current best minus a margin, then refine to 100 → 50 → 25 m. Prune by flight pair: keep the top N pairs and drop whole pairs early.
- **It is embarrassingly parallel.** The spectrogram (2,400 × 512 float32 ≈ 5 MB) fits in GPU L2 cache or CPU caches. Any of numba (CPU), CuPy, JAX or PyTorch (GPU) is enough.

**Measured** (`scripts/05_gather_bench.py`, Apple M4 Pro, single-thread numpy): 3.5×10⁸ gathers/s, or **8.5 ms per candidate-event for the full 61 × 41 sweep**. Across 14 cores that is about 3–5×10⁹ gathers/s. One modern GPU should manage about 10¹¹ gathers/s [assume].

### 3.7 Pruning order

| # | Filter | Cost | Survivors (Ex. 1, loose windows) |
|---|---|---|---|
| 1 | Drop jets and clip to CONUS | O(N) scan | about 25–40 % of rows |
| 2 | Speed window per event | O(N) | about 25–35 % of segments (about 2 % if tight) |
| 3 | Distance band rasterisation | O(band area) | 1.7×10⁸ records instead of 3.8×10⁹ |
| 4 | 920 s ± τ pair join | O(R log k) or pairs first | about 7×10⁴ points (video: 73k) |
| 5 | Silence range query | O(M log N) | about 20 % (video: 15k) |
| 6 | Straight-line (v, d) pre-score | O(M) | 10–100× fewer [assume] |
| 7 | Full forward model, coarse to fine | 8.5 ms/candidate-event | top pair(s) |

---

## 4. Compute budget

### Example 1: one day, CONUS, two events, unknown time of day

| Stage | Work | Memory | Laptop (14-core) | 1 GPU | Cloud |
|---|---|---|---|---|---|
| Read the 50 GB Parquet and filter to non-jets | I/O 50 GB → 0.3–3 GB | streamed | 3–8 min (SSD 1–3 GB/s plus decode) [assume] | same (I/O-bound) | not needed |
| DEM to 100 m raster (one-off) | 21–42 GB read | 1.5 GB | 10–30 min once [assume] | – | – |
| Naive 10 km grid (the video's) | 3.8×10⁹ records, 1.5×10¹² FLOP | ~60 GB, spills | 10–30 min | compute in seconds, but needs out-of-core | – |
| Speed-first band grid + hash join | 1.7×10⁸ records | 2.6 GB | ~1 min | seconds | – |
| Pairs-first join | ~10⁷ probes | <1 GB | seconds | – | – |
| Silence filter | 7×10⁴ range queries | index ~1 GB | <1 s | – | – |
| Forward model, 35k candidates × 2 events | 2×10¹⁰ FLOP + 2.1×10¹¹ gathers | <1 GB | **~70 s** (10 min single-thread; ~1.2 h as a pure-Python loop) | **~2 s** | – |
| **Total (excluding the one-off DEM)** | | ≤ 4 GB RAM | **~5–10 min** | I/O-bound | unnecessary |

### Example 2: one event, time known to ±1 min, CONUS
The prop flights airborne in that minute number about 1,500 on average and about 3,600 at peak [assume]. The CPA must fall within a window of about 3 min, which is about 11 km of track. With the speed window and distance band, that gives **4.2×10⁵ candidates at 200 m and 1.7×10⁶ at 100 m**. The naive 10 km corridor gives 2×10⁷.

| Scoring | Work | Laptop | GPU |
|---|---|---|---|
| Distance to a traced ridge, 61 shifts, closed-form f₀ | 6×10¹¹ FLOP | ~3 s | <1 s |
| Spectrogram brightness, 61 × 41 sweep | 5×10¹² gathers | ~28 min | ~1 min |
| Same, but pre-scored and coarse to fine (≈10× fewer) [assume] | 5×10¹¹ | ~3 min | ~5 s |

### Scaling

| Scope | Data and records | Loose-window pair candidates | Ex. 1 forward model (laptop) | Bottleneck |
|---|---|---|---|---|
| CONUS, 1 day | ×1 | ~6×10⁴ | ~70 s | read I/O |
| CONUS, 1 week | ×7 (~350 GB raw) | ~4×10⁵ | ~8 min | read I/O (~1 h); worth a 32–64-core VM or a GPU |
| State, 200k km², 1 week | ×0.18 | ~10⁴ | ~13 s | – |
| City, 1000 km², 1 week | ×10⁻³ | ~60 at mean density; ×10–100 near a busy airport | <1 s | none; brute force over all pairs is fine |

Pair candidates grow linearly with time span and area (p ∝ T). The forward model grows with the candidate count. Restricting the area to a city lets you open the time window to weeks, as the video says. The arithmetic is trivial; the issue is getting the archive.

---

## 5. Error budget and its effect on tolerances

| Source | Size | Effect | Handling |
|---|---|---|---|
| ADS-B position (NACp ≥ 8 rule) | <0.05 NM (93 m) bound; GNSS is typically a few m [assume] | d error up to tens of m | Weight by NACp; down-weight MLAT/TIS-B points |
| ADS-B latency | ≤0.6 s uncompensated, ≤2.0 s total [src] | ~36 m along track at 60 m/s; **±0.6 s on each event**, which feeds into the locked offset | Offset tolerance ≥ ±0.5–1 s |
| Trace sparsity | up to 30 s between points [src]; 3°/s standard-rate turn [src] | turn radius v/ω ≈ 1.15 km; a 30 s gap cuts a 336 m chord across the arc (5 s gap: 10 m) | Use 1–5 s data plus Hermite interpolation with gs and track |
| Baro altitude | referenced to 29.92 inHg; ≈1000 ft per inHg [src] | ±0.5 inHg [assume] gives ±150 m in h, so δd ≈ (h/d)·δh ≈ **80 m**, four times the fit precision | Use geometric altitude (and geoid), or correct with METAR QNH |
| Non-ISA temperature on altitude | ≈3.5 % of height per 10 °C (T/T_ISA) | ~20–30 m at 800 m AGL | Use reanalysis temperature |
| DEM | GLO-30 <4 m LE90 [src]; mixing datums (NAVD88 vs EGM2008 vs ellipsoid) | metres to tens of metres if datums are mixed | Use one vertical datum consistently |
| Speed of sound | c ≈ 331.3 + 0.606·T [src]; ±15 °C → ±2.6 % | ±2.6 % on fitted speed (±2.6 m/s); ±0.23 s delay at 3 km | Use the local analysed temperature; let c float within a prior |
| Wind | along-ray w shifts c_eff = c + w·n̂ asymmetrically | 8 m/s + 5 °C gives about 0.5 Hz RMS after refit (`scripts/03`), comparable to the noise | Absorb in tolerances, or model the wind (`emission_time(wind=…)`) |
| Refraction and shadow zones | wind shear bends sound up or down [src] | changes audibility, causing **false vetoes in the silence filter** | Soft silence penalty |
| ADS-B mandate gaps | not required in Class E below 10,000 ft MSL outside B/C airspace and the 30 NM areas [src]; 44 % of expected GA equipped by 2019 [src]; ~57k GA still unequipped (FAA 2025 estimate) [src] | the heard aircraft may be **absent**, an unrecoverable false negative | Use a city or week-scale search with several events |
| Receiver coverage | radio horizon ≈ 4.12(√h₁ + √h₂) km [src]; 300 m AGL to a 10 m mast ≈ 84 km | rural low-level GA has gaps; UAT (978 MHz) traffic needs UAT feeders | Merge archives (adsb.lol, ADSBx, OpenSky); flag gaps instead of treating them as silence |

How the tolerances trade off:
- **False positives** scale with the product of all five windows (v₁, d₁, v₂, d₂, τ). Doubling every window multiplies false candidates by 32, but that only costs linear forward-model compute.
- **False negatives** are catastrophic. If the true aircraft is missing from the data, or the silence filter vetoes the true location, no later stage can recover it.
- **So keep the cheap filters loose** (systematics-inflated windows, soft silence) and let the full curve shape carry the discrimination. This is the design the video ended up with.

---

## 6. False-match probability: sanity check of 35k / 73k / 15k

**Model.** Treat traffic as random straight tracks with track-length density λ (km per km² per day). By Crofton's formula, an observer gets 2Rλ passes per day with horizontal CPA ≤ R. Event class i, with distance-band width wᵢ and speed fraction fᵢ, therefore occurs at rate rᵢ = λ·wᵢ·fᵢ. The expected number of coincidences per point is

  p = r₁ · T · r₂ · 2τ  (T = one day, τ = offset half-window),

and E[N] = N_points · p · K, where K = E[λ²]/E[λ]² accounts for clustering: pair matches scale with λ². A Monte-Carlo run with 655 random tracks on a 250k-point 200 m grid, matched by brute force, gave **501 hits against an analytic prediction of 543**, so the formula holds.

**Inputs.** Mean λ = 0.99 km/km²/day of non-jet track, which gives about 20 passes per day within 10 km and about 4 within 2 km at an average CONUS point. Prop altitudes are 300–1500 m AGL [assume]. Event 1 is at about 1.5 km and event 2 at about 1.0 km slant [assume].

| Windows | Δd, Δv, τ | p per point | Points nationwide (uniform) | × K = 18 |
|---|---|---|---|---|
| Video-like loose | ±300 m, ±15 m/s, ±5 s | 2.2×10⁻⁵ | 4,100 | **74,000** |
| Realistic (fit precision ×5 for systematics) | ±100 m, ±1.5 m/s, ±0.5 s | 2.6×10⁻⁹ | 0.5 | **~9** |
| Fit precision | ±20 m, ±0.3 m/s, ±0.1 s | 6.9×10⁻¹² | 0.001 | 0.02 |

**Checking each of the video's counts:**
- **35k with the offset filter only, in a test area under 0.05 % of CONUS.** At least 37 % of points matched. Under Poisson statistics that needs about 63 non-jet passes per day within 10 km, about 3× the CONUS mean. That is consistent with a test area near an airfield.
- **73k with speed and distance windows added.** Loose windows predict about 4×10³ if traffic were uniform. Clustering with K ≈ 18 (lognormal σ ≈ 1.7) reproduces 73k, and the video itself observed that the survivors cluster at airports.
- **15k after the silence filter.** Weighting by where pair matches occur (∝ λ²), a silence radius of 2 km keeps 23 %, and 1–3 km keeps 34–17 %. The video's 20 % fits. The filter works because it hits exactly the dense-traffic places where accidental pairs pile up.

**Is a unique winner plausible?** With two events and realistic windows, expect fewer than 10 false 200 m points nationwide, which is likely 1–3 flight pairs. The locked offset alone (±0.5 s against the video's ±5 s) removes another factor of 10 compared with the loose filter. Turn content then separates the remaining pairs: per `scripts/03`, a 0.5°/s turn already changes the curve by 3.4 Hz RMS against 0.4 Hz noise. The video's 20.5 versus 15 score gap fits this picture.

For **Example 2** (one event, time known ±1 min), (v, d) alone leaves about 4,000 points nationwide with realistic windows, or about 340 at fit precision. These belong to tens to hundreds of flights, and a straight pass also has a left/right mirror twin. Uniqueness therefore depends entirely on the track having enough turn or acceleration content. The video's second place being "close" is what this predicts. The fact that both examples converged on the same coordinate is strong independent evidence.

---

## 7. Recommended build

1. Ingest into columnar arrays (DuckDB or Polars → numpy). Build a 1 s Hermite-resampled track per flight and a 100 m DEM raster.
2. Fit each event and use the covariance windows from `scripts/03`, inflated for the systematics in §5.
3. Run the speed filter, then the pairs-first spatio-temporal hash join, then the exact polygon locus per pair.
4. Apply the soft silence penalty through a 3-D (x, y, t) index.
5. Pre-score with the straight-line closed form, then run the full retarded-time forward model on a GPU or numba, coarse to fine, grouped by flight pair.

On a laptop, expect minutes per CONUS-day, dominated by reading the input. The compute is not the hard part; the data coverage and the systematics are.

---

## Sources

**Traffic and fleet**
- FAA, Air Traffic By the Numbers (44,360 daily flights; ~5,500 aircraft at peak). The site blocks automated fetch, so the figures are as indexed by search: https://www.faa.gov/air_traffic/by_the_numbers and https://www.faa.gov/air_traffic/by_the_numbers/air-traffic-by-the-numbers-FY2024.pdf
- FAA GA and Part 135 Activity Survey CY2024 (official tables): https://www.faa.gov/data_research/aviation_data_statistics/general_aviation/cy2024 ; summary figures (213,756 active aircraft, 29 M h, piston 138,960 and 16.16 M h) via General Aviation News: https://generalaviationnews.com/2026/02/11/ga-doing-more-with-less/

**ADS-B data and archives**
- adsb.lol historical data (format, ODbL): https://www.adsb.lol/docs/open-data/historical/ ; 2024 archive size: https://github.com/adsblol/globe_history_2024
- One day of adsb.lol processed (47k traces, 38.67 M rows, sizes): https://tech.marksblogg.com/global-flight-tracking-adsb.html
- ADS-B Exchange sample data (5 s readsb-hist, 2 Hz hires-traces): https://www.adsbexchange.com/data-products/sample-data/
- readsb trace format and `--json-trace-interval` default of 30 s: https://github.com/wiedehopf/readsb/blob/dev/README-json.md and https://github.com/wiedehopf/readsb

**Regulation, accuracy, equipage**
- 14 CFR 91.227 (NACp < 0.05 NM, latency ≤ 0.6 s uncompensated / 2.0 s total): https://www.law.cornell.edu/cfr/text/14/91.227
- 14 CFR 91.225 (airspace where ADS-B Out is required): https://www.law.cornell.edu/cfr/text/14/91.225
- DOT OIG 2019 (44 % of expected GA equipped): https://www.oversight.gov/reports/audit/nextgen-equipage-ads-b-out-equipage-rates-are-increasing-faa-must-address-airspace and https://generalaviationnews.com/2019/09/19/only-44-of-ga-aircraft-equipped-with-ads-b
- FAA ICR 2120-0728 (2025; ~57,000 GA aircraft not yet equipped): https://omb.report/icr/202504-2120-019

**Geography and elevation**
- CONUS land area: https://en.wikipedia.org/wiki/Contiguous_United_States
- USGS 3DEP 1 arc-second DEM: https://www.sciencebase.gov/catalog/item/4f70aa71e4b058caae3f8de1
- Copernicus DEM on AWS (tiling, 3600 px COGs): https://copernicus-dem-30m.s3.amazonaws.com/readme.html and https://registry.opendata.aws/copernicus-dem/
- Copernicus GLO-30 accuracy (<4 m LE90): https://cis2.eea.europa.eu/data/488 and the product handbook https://dataspace.copernicus.eu/sites/default/files/media/files/2024-06/geo1988-copernicusdem-spe-002_producthandbook_i5.0.pdf
- H3 cell sizes: https://h3geo.org/docs/core-library/restable/

**Physics and aviation conventions**
- Speed of sound vs temperature, and wind refraction: https://en.wikipedia.org/wiki/Speed_of_sound
- Pressure altitude (≈1000 ft per inHg): https://en.wikipedia.org/wiki/Pressure_altitude
- Radio horizon (d ≈ 4.12√h): https://en.wikipedia.org/wiki/Radio_horizon
- Standard-rate turn (3°/s): https://en.wikipedia.org/wiki/Standard_rate_turn

**Video:** colsto, https://www.youtube.com/watch?v=UytYrhYLOKI.

---

## Appendix: script outputs (abridged)

`uv run scripts/05_compute_budget.py`
```
CONUS land area             : 7,663,940 km^2
  cells at 200 m / 100 m / 30 m : 1.916e+08 / 7.664e+08 / 8.515e+09
0.05% test area : 3,832 km^2 = 95,799 points at 200 m -> 35k survivors = 37% of points
DEM 1 arc-sec cells over CONUS land: 1.05e+10 -> int16 21 GB, float32 42 GB ; 100 m int16: 1.5 GB
FAA-handled flights x 1.8 h : 79,848 aircraft-h/day ; GA 79,452 ; piston 44,274
  raw 1 Hz  : all 4.59e+08 rows (18.4 GB) ; props 1.26e+08 rows (3.02 GB)
  5 s       : all 9.18e+07 rows ( 3.7 GB) ; props 2.52e+07 rows (0.60 GB)
prop track length/day: 7.560e+06 km ; lambda = 0.99 km/km^2/day ; R=10 km: 19.7 passes/day
  generous : p=2.15e-05 -> 4.11e+03 points (x K=18: 7.4e+04)
  realistic: p=2.58e-09 -> 0.495 points    (x K=18: 8.91)
  precise  : p=6.86e-12 -> 0.00131 points  (x K=18: 0.0237)
  sigma=1.72 K=18.3 generous-match points=75,124 | R_s=1km 34% | R_s=2km 23% | R_s=3km 17%
Monte-Carlo: 655 tracks, 250,000 points: brute-force hits = 501 ; analytic = 543
naive 10 km corridors: 3.78e+09 records, 60 GB, ~1.5e+12 FLOP ; speed+band: 1.65e+08 (2.64 GB), 23x fewer
forward model 35k cand: 2.0e+10 FLOP ; sweep 2.1e+11 gathers -> CPU 70 s, GPU 2.1 s, pure-Python 1.2 h
Ex.2: candidates @200 m naive 1.97e+07, speed+band 4.22e+05 ; @100 m 1.69e+06
      trace-line scoring 6.2e+11 FLOP -> 3 s ; brightness 5.1e+12 gathers -> CPU 28 min, GPU 51 s
      false single-event matches: generous 112,653, realistic 4,076, precise 338
```
`uv run scripts/05_gather_bench.py` → `6.00e+07 gathers in 0.170 s -> 3.52e+08 gathers/s single-thread numpy (8.5 ms per candidate-event)`
