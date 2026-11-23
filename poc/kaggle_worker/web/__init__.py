"""Kite's web back end (P3 of the web + provider design): one owner account with a password and 2FA,
a JSON API and WebSockets for every tab of the web page, and an audit log.

``kite web admin`` creates the account; ``kite web serve`` runs the server (FastAPI + uvicorn) on
loopback; Caddy publishes it over HTTPS. Everything Kite already does (tasks, workers, journal,
bench, memory, doctor, update) is reused, not rewritten: the routes call ``KiteOps`` and the gateway.
"""
