"""Black-Scholes pricing and implied-vol solver.

Pure-Python (math + numpy). Used by iv_backfill to recover historical
ATM IV for individual stocks from Databento OPRA daily settlement prices.

Conventions:
  S      = spot
  K      = strike
  T      = time to expiry in YEARS (e.g. 30 days = 30/365)
  r      = continuously-compounded risk-free rate (decimal, e.g. 0.05)
  q      = continuous dividend yield (decimal). Default 0.
  sigma  = annualized volatility (decimal, 0.18 = 18% IV)

All functions return decimals; callers convert to display units.
"""
import math


SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_price(S, K, T, r, sigma, option_type="call", q=0.0):
    """Black-Scholes-Merton price (handles continuous dividend yield)."""
    if T <= 0:
        intrinsic = max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
        return intrinsic
    if sigma <= 0 or S <= 0 or K <= 0:
        return float("nan")
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if option_type == "call":
        return S * disc_q * _norm_cdf(d1) - K * disc_r * _norm_cdf(d2)
    return K * disc_r * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)


def bs_vega(S, K, T, r, sigma, q=0.0):
    """dPrice/dSigma. Same for calls and puts."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    return S * math.exp(-q * T) * _norm_pdf(d1) * sqrtT


def implied_vol(price, S, K, T, r=0.05, option_type="call", q=0.0,
                tol=1e-6, max_iter=60):
    """
    Solve for sigma given an option mid price. Newton-Raphson with a
    bisection bracket fallback for hard cases (deep ITM/OTM, near expiry).

    Returns sigma as a decimal in (0.01, 5.0) or None when no clean root.
    """
    if price is None or price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None

    # Arbitrage bounds. If price is below the no-arb floor or above the
    # ceiling there is no real IV to recover; bail out cleanly.
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if option_type == "call":
        lower = max(S * disc_q - K * disc_r, 0.0)
        upper = S * disc_q
    else:
        lower = max(K * disc_r - S * disc_q, 0.0)
        upper = K * disc_r
    if price < lower - 1e-6 or price > upper + 1e-6:
        return None

    # ---- Newton-Raphson from a Brenner-Subrahmanyam ATM seed ----
    sigma = math.sqrt(2.0 * math.pi / T) * (price / S) if S > 0 else 0.3
    sigma = max(0.05, min(2.0, sigma))

    for _ in range(max_iter):
        p = bs_price(S, K, T, r, sigma, option_type, q)
        v = bs_vega(S, K, T, r, sigma, q)
        diff = p - price
        if abs(diff) < tol:
            return sigma if 0.01 < sigma < 5.0 else None
        if v < 1e-8:
            break  # vega vanished -- fall through to bisection
        step = diff / v
        sigma_new = sigma - step
        if sigma_new <= 0 or sigma_new > 5.0:
            sigma_new = sigma * 0.5 if step > 0 else sigma * 1.5
        sigma = sigma_new

    # ---- Bisection fallback (always converges if a root exists) ----
    lo, hi = 1e-4, 5.0
    p_lo = bs_price(S, K, T, r, lo, option_type, q) - price
    p_hi = bs_price(S, K, T, r, hi, option_type, q) - price
    if p_lo * p_hi > 0:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        p_mid = bs_price(S, K, T, r, mid, option_type, q) - price
        if abs(p_mid) < tol:
            return mid if 0.01 < mid < 5.0 else None
        if p_mid * p_lo < 0:
            hi, p_hi = mid, p_mid
        else:
            lo, p_lo = mid, p_mid
    return 0.5 * (lo + hi)
