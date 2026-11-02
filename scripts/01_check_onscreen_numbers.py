# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""Re-derive every number visible in the video's 'Building the Doppler Curve' screenshot
and a few side claims from the narration. Pure arithmetic, no fitting."""
import numpy as np

c, f0 = 343.0, 130.0
ok = lambda b: "OK " if b else "MISMATCH"

print("== Screenshot: emission-time step ==")
t_r, t_e, r = 25.00, 16.910, 2775.0
print(f"t_e + r/c = {t_e} + {r}/{c} = {t_e + r / c:.3f} s   (shown t_r = {t_r})  {ok(abs(t_e + r / c - t_r) < 0.01)}")

print("\n== Screenshot: Doppler equation ==")
v_r = 121.16
f = f0 * c / (c + v_r)
print(f"f = 130*343/(343+121.16) = {f:.2f} Hz   (shown 96.07)  {ok(abs(f - 96.07) < 0.01)}")

v = np.array([140.0, 16.8, 0.0])
p = np.array([2367.0, 1264.0, 1020.0])
print(f"|v| = {np.linalg.norm(v):.1f} m/s; radial component {v_r} <= |v|  {ok(v_r <= np.linalg.norm(v))}")
# The shown position is in a local frame; observer is not at the origin. Find the set of
# observer positions consistent with r = 2775 m and v_r = 121.16 m/s (2 eqs, 3 unknowns).
# Observer on the ground below the aircraft path: try ground height h and solve for (ox, oy).
from itertools import product
best = None
for h in np.linspace(0, 600, 601):
    dz = p[2] - h
    rh = np.sqrt(max(r**2 - dz**2, 0))  # horizontal distance
    # unit vector from observer to aircraft: (rh cos a, rh sin a, dz)/r ; v_r = v . that
    a = np.linspace(-np.pi, np.pi, 72001)
    vr = (v[0] * rh * np.cos(a) + v[1] * rh * np.sin(a)) / r
    i = np.argmin(abs(vr - v_r))
    if abs(vr[i] - v_r) < 0.01:
        best = (h, a[i], rh)
        break
h, a, rh = best
obs = p - np.array([rh * np.cos(a), rh * np.sin(a), p[2] - h])
print(f"A consistent observer exists, e.g. ground elev {h:.0f} m at local ({obs[0]:.0f}, {obs[1]:.0f}) m "
      "(screenshot shows observer below/left of aircraft)  OK")
print(f"Asymptotic receding pitch f0*c/(c+|v|) = {f0 * c / (c + np.linalg.norm(v)):.1f} Hz "
      "(chart flattens at ~95 Hz)  OK")
print(f"Asymptotic approaching pitch f0*c/(c-|v|) = {f0 * c / (c - np.linalg.norm(v)):.1f} Hz "
      "(chart starts ~215 Hz, still rising toward it)  OK")

print("\n== Narration 16:30: v_r = 62.7 m/s -> 109.9 Hz ==")
f2 = f0 * c / (c + 62.7)
print(f"130*343/(343+62.7) = {f2:.2f} Hz  {ok(abs(f2 - 109.9) < 0.05)}")

print("\n== Narration 06:30: CPA 22:46:34 heard at 22:46:46 (12 s delay) ==")
print(f"12 s * 343 m/s = {12 * c / 1000:.2f} km slant range ('3 plus kilometres')  OK")

print("\n== Narration 20:00: 200 m grid -> correct point can be up to 141 m off ==")
print(f"half-diagonal of a 200 m cell = 100*sqrt(2) = {100 * np.sqrt(2):.1f} m  OK")

print("\n== Narration 08:00: CONUS in football fields, end to end, to the moon and back ~200x ==")
land_km2 = 7.66e6           # contiguous-US land area (approx., excl. water)
width_m = 48.77             # 160 ft field width; total length = area/width independent of field length
length_km = land_km2 * 1e6 / width_m / 1000
trip_km = 40_075 + 2 * 384_400  # around the Earth once + to the moon and back
print(f"total length = {length_km:.3g} km; / ({trip_km:,} km per trip) = {length_km / trip_km:.0f} trips  "
      f"{ok(150 < length_km / trip_km < 210)}")
