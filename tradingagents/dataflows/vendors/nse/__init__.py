"""NSE and NSE Archives: the transport, throttling and cache the India vendors share.

The package holds the exchange adapters that serve an NSE/BSE listing: the
equity master (``issuer``), and later the announcements, integrated filings,
XBRL and ownership feeds. ``client`` owns the one HTTP session, the per-bucket
spacing, the on-disk cache and the failure budget so every adapter behaves the
same way under throttling or a block.
"""
