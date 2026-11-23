"""Curves and key-rate bump interpolation (spec Stage F, Stage H).

All zero rates are continuously compounded and in percent, used exactly as published.
"""
from __future__ import annotations

import numpy as np
from scipy.interpolate import CubicSpline, PchipInterpolator

KEYS = np.array([2.0, 5.0, 10.0, 30.0])


class NSCurve:
    """Nelson-Siegel: r(t) = b0 + b1*f + b2*(f - e^(-t/tau)), f = (1 - e^(-t/tau)) / (t/tau)."""

    def __init__(self, b0: float, b1: float, b2: float, tau: float, label: str = ""):
        self.b0, self.b1, self.b2, self.tau, self.label = b0, b1, b2, tau, label

    def zero(self, t):
        t = np.asarray(t, dtype=float)
        x = t / self.tau
        e = np.exp(-x)
        safe = np.where(x > 1e-12, x, 1.0)
        f = np.where(x > 1e-12, (1.0 - e) / safe, 1.0)
        return self.b0 + self.b1 * f + self.b2 * (f - e)


def _loadings(t, tau):
    x = t / tau
    e = np.exp(-x)
    safe = np.where(x > 1e-12, x, 1.0)
    f = np.where(x > 1e-12, (1.0 - e) / safe, 1.0)
    return f, f - e


class SvenssonCurve:
    """r(t) = b0 + b1 f(t/tau1) + b2 [f(t/tau1) - e^(-t/tau1)] + b3 [f(t/tau2) - e^(-t/tau2)],
    f(x) = (1 - e^-x)/x. Zero rates in %, continuously compounded. b3 = 0 gives Nelson-Siegel."""

    NAMES = ("b0", "b1", "b2", "b3", "tau1", "tau2")

    def __init__(self, b0, b1, b2, b3, tau1, tau2, label: str = ""):
        self.b0, self.b1, self.b2, self.b3 = float(b0), float(b1), float(b2), float(b3)
        self.tau1, self.tau2, self.label = float(tau1), float(tau2), label

    def zero(self, t):
        t = np.asarray(t, dtype=float)
        f1, h1 = _loadings(t, self.tau1)
        _, h2 = _loadings(t, self.tau2)
        return self.b0 + self.b1 * f1 + self.b2 * h1 + self.b3 * h2

    @property
    def params(self) -> dict:
        return {k: getattr(self, k) for k in self.NAMES}



class KeyRateCurve:
    """Test-mode curve: linear interpolation of key rates, flat outside [2Y, 30Y]."""

    def __init__(self, rates, keys=KEYS, label: str = "test-mode"):
        self.keys, self.rates, self.label = np.asarray(keys, float), np.asarray(rates, float), label

    def zero(self, t):
        return np.interp(np.asarray(t, float), self.keys, self.rates)


class BumpInterpolator:
    """Spreads a vector of key-rate moves across maturities. Flat outside [first key, last key]."""

    METHODS = ("linear", "cubic", "pchip")

    def __init__(self, method: str = "linear", keys=KEYS):
        if method not in self.METHODS:
            raise ValueError(f"interpolation must be one of {self.METHODS}")
        self.method, self.keys = method, np.asarray(keys, float)

    def spread(self, t, v_bp):
        t = np.clip(np.asarray(t, float), self.keys[0], self.keys[-1])
        v = np.asarray(v_bp, float)
        if not np.any(v):
            return np.zeros_like(t)
        if self.method == "linear":
            return np.interp(t, self.keys, v)
        if self.method == "cubic":
            return CubicSpline(self.keys, v, bc_type="natural")(t)
        return PchipInterpolator(self.keys, v)(t)


class ShiftedCurve:
    """base curve + interpolated key-rate shift (v in bp)."""

    def __init__(self, base, v_bp, interp: BumpInterpolator):
        self.base, self.v, self.interp = base, np.asarray(v_bp, float), interp

    def zero(self, t):
        return self.base.zero(t) + self.interp.spread(t, self.v) / 100.0


class ParShift:
    """Zero-rate shift implied by bumping semi-annual PAR yields at the key tenors and re-bootstrapping.

    Par yields come from the base curve's discount factors on a semi-annual grid (0.5Y .. max_t).
    The key-rate bump is spread over the par grid with the chosen interpolation, the discount
    factors are re-bootstrapped, and the change in zero rate is applied on top of the base zero
    curve (so unbumped prices stay exactly on the base curve). Below the first grid point the
    shift is held flat (money-market end); beyond max_t it is held flat.
    """

    def __init__(self, base, interp: BumpInterpolator, step: float = 0.5, max_t: float = 40.0):
        self.interp, self.step = interp, step
        self.grid = np.arange(step, max_t + step / 2, step)
        self.D0 = np.exp(-base.zero(self.grid) / 100.0 * self.grid)
        self.par0 = (1.0 - self.D0) / (step * np.cumsum(self.D0))      # decimal, annualised

    def par_yields(self, t=None) -> np.ndarray:
        """Base semi-annual par yields (%) on the grid, or interpolated at t."""
        p = self.par0 * 100.0
        return p if t is None else np.interp(np.asarray(t, float), self.grid, p)

    def bootstrap(self, par: np.ndarray) -> np.ndarray:
        D = np.empty_like(par)
        acc = 0.0
        for n, c in enumerate(par):
            D[n] = (1.0 - c * self.step * acc) / (1.0 + c * self.step)
            acc += D[n]
        return D

    def shift_bp(self, t, v_bp) -> np.ndarray:
        """Zero-rate change (bp) at times t for par-yield key moves v (bp)."""
        t = np.asarray(t, float)
        v = np.asarray(v_bp, float)
        if not np.any(v):
            return np.zeros_like(t)
        D1 = self.bootstrap(self.par0 + self.interp.spread(self.grid, v) / 1e4)
        if np.any(D1 <= 0):
            raise ValueError("par-yield shock too large: bootstrapped discount factor <= 0")
        dz = -np.log(D1 / self.D0) / self.grid * 1e4
        return np.interp(t, self.grid, dz)


SPACES = ("spot", "par")


def key_vector(i: int, size_bp: float, n: int = len(KEYS)) -> np.ndarray:
    v = np.zeros(n)
    v[i] = size_bp
    return v
