# gunicorn.conf.py: production WSGI server config for the stock manager.
#
# Run:  gunicorn -c deploy/gunicorn.conf.py wsgi:app
# (wsgi.py exposes `app = create_app()`.)

# Bind to localhost only. nginx (reverse proxy) is the public face and
# forwards requests here. gunicorn not exposed directly to the network.
bind = "127.0.0.1:8000"

# A small, always-on internal tool needs only a few sync workers (3 here).
workers = 3
worker_class = "sync"

# Recycle workers periodically to bound any slow memory growth.
max_requests = 1000
max_requests_jitter = 100

# Must comfortably exceed busy_timeout (5s) * retries so a slow write under
# contention is never killed mid-transaction.
timeout = 60

# Log to stdout/stderr, which systemd captures in the journal.
# View with:  journalctl -u stock-manager -f
accesslog = "-"
errorlog = "-"
loglevel = "info"
