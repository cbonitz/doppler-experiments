"""Shared physics for the scripts. Imported by sibling scripts (they add this folder to sys.path).

Conventions: local ENU metres, seconds. Observer stationary on the ground.
Radial velocity v_r > 0 means the source is moving away from the observer.
"""
import numpy as np


def emission_time(t_r, pos_fn, obs, c=343.0, wind=(0.0, 0.0, 0.0), iters=12):
    """Solve for emission time t_e such that a wavefront emitted at the source position
    x_s(t_e) reaches the observer at t_r.

    Physics (first principles): in the air frame a wavefront is a sphere of radius
    c*(t - t_e) centred on the emission point, and the whole sphere drifts with the wind w.
    So reception happens when |obs - x_s(t_e) - w*(t_r - t_e)| = c*(t_r - t_e).
    With w = 0 this is the screenshot's  t_r = t_e + r(t_e)/c.
    Solved by fixed-point iteration on the delay D = t_r - t_e (a contraction because the
    source is subsonic: |dD/dt_e| <= |v|/c < 1).
    """
    obs = np.asarray(obs, float)
    w = np.asarray(wind, float)
    t_r = np.asarray(t_r, float)
    D = np.linalg.norm(pos_fn(t_r) - obs[..., :], axis=-1) / c
    for _ in range(iters):
        x = pos_fn(t_r - D)
        D = np.linalg.norm(obs - x - w * D[..., None], axis=-1) / c
    return t_r - D


def radial_velocity(t, pos_fn, vel_fn, obs):
    d = pos_fn(t) - np.asarray(obs, float)
    return np.sum(vel_fn(t) * d, axis=-1) / np.linalg.norm(d, axis=-1)


def doppler(f0, v_r, c=343.0):
    """Moving source, stationary observer, still air: f = f0 * c / (c + v_r)."""
    return f0 * c / (c + v_r)


def straight_line_curve(t_r, f0, v, d, t0, c=343.0):
    """Closed form received frequency for a straight, constant-speed pass.
    Source at s=v*(t_e - t0) along track, perpendicular (3D) miss distance d.
    Retarded-time condition c*(T - s_t) = sqrt(v^2 s_t^2 + d^2), with T = t_r - t0, s_t = t_e - t0,
    is a quadratic in s_t; take the causal root."""
    T = np.asarray(t_r, float) - t0
    disc = c**2 * v**2 * T**2 + (c**2 - v**2) * d**2
    s = (c**2 * T - np.sqrt(disc)) / (c**2 - v**2)
    v_r = v**2 * s / np.sqrt(v**2 * s**2 + d**2)
    return f0 * c / (c + v_r)
