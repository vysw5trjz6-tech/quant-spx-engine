# vol1d — same-day expected-volatility signal derived from a VIX1D proxy.
#
# Spec: docs/vix1d_module_spec.md (the source of truth for this package).
#
# The proxy is computed in-house from the SPX/SPXW option chain; it is NOT
# the official Cboe VIX1D print. Treat the level as relative, not absolute:
# regime and relative change are what the engine consumes, reconciled daily
# against the official close (vol1d.qa).
#
# Import discipline: this package must stay importable without network,
# Databento, or Flask. Heavy work happens in functions, not at import time.
