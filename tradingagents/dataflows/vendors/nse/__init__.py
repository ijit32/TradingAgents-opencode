"""NSE and NSE Archives: the transport, throttling and cache the India vendors share.

The package holds the exchange adapters that serve an NSE/BSE listing — the
equity master (``issuer``), corporate announcements and the integrated results
index (``filings``), SEBI PIT insider disclosures (``pit``) and quarterly
shareholding (``shareholding``) — plus the one HTTP transport they share.
``client`` owns the session, the per-bucket spacing, the on-disk cache and the
failure budget, so every adapter behaves the same way under throttling or a
block.
"""
