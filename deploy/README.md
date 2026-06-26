# Deploying the Lab Stock Manager (Debian)

```
browser (lab PC) ──HTTPS──► nginx ──HTTP(localhost)──► gunicorn ──► Flask app ──► stock.db
```

One Debian server; lab PCs use it through a browser over the internal network.
Standard [Flask deployment](https://flask.palletsprojects.com/en/stable/deploying/).

Substitute throughout: `SERVER_HOSTNAME` (the name the TLS cert is issued for)
and `DEPLOY_USER` (the SSH account).

## Components

- **Flask app** — the code; `wsgi.py` exposes it as `app`.
- **gunicorn** — runs the app; listens on `127.0.0.1:8000` only.
- **nginx** — terminates HTTPS, serves `/static/`, proxies the rest to gunicorn.
- **systemd** — keeps gunicorn running 24/7 and restarts it on crash/reboot.
- **SQLite** (`instance/stock.db`) — the entire data store; one file, no DB server.

## Access tiers

1. **Server admin** (shell/root) — files, code, DB, upkeep, backups. Unaudited; restrict access.
2. **In-app admin** (`role='admin'`) — orders, corrections, reconcile, log, users. Audited; no shell needed.
3. **In-app user** (`role='user'`) — day-to-day moves and consumption.

## Prerequisites

`sudo` for `DEPLOY_USER`; a TLS cert for `SERVER_HOSTNAME` (self-signed ok for
testing — step 6); firewall open inbound **443** + **80**; DNS `SERVER_HOSTNAME`
→ this server.

## Install

**1. Packages**
```bash
sudo apt update && sudo apt install -y python3 python3-venv git nginx
```

**2. Code** — the `deploy/` configs assume `/opt/stock-manager`.
```bash
sudo mkdir -p /opt/stock-manager && sudo chown "$USER" /opt/stock-manager
git clone https://github.com/AndreaCacioppo/stock-manager-ecoli-lab.git /opt/stock-manager
cd /opt/stock-manager
```

**3. Python env + deps**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**4. Database + first admin** — `create-admin` refuses if an admin already exists.
```bash
.venv/bin/python manage.py init-db                        # creates stock.db (WAL) + secret_key (chmod 600)
.venv/bin/python manage.py create-admin --username alice  # prompts for a password
```

**5. Dedicated service user** — the app runs unprivileged, able to touch only its own data.
```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin stock
sudo chown -R stock:stock /opt/stock-manager/instance /opt/stock-manager/labels
sudo chmod 700 /opt/stock-manager/instance
sudo chmod 600 /opt/stock-manager/instance/stock.db /opt/stock-manager/instance/secret_key
```
(To run as the login user instead, skip `useradd` and set `User=`/`Group=` to `DEPLOY_USER` in the unit.)

**6. TLS certificate**
```bash
sudo mkdir -p /etc/ssl/stock-manager
```
- **Institutional cert (preferred):** copy to `/etc/ssl/stock-manager/server.crt` and `server.key`.
- **Self-signed (testing only):**
  ```bash
  sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout /etc/ssl/stock-manager/server.key \
    -out    /etc/ssl/stock-manager/server.crt -subj "/CN=SERVER_HOSTNAME"
  sudo chmod 600 /etc/ssl/stock-manager/server.key
  ```

**7. gunicorn under systemd**
```bash
sudo cp deploy/stock-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stock-manager
sudo systemctl status stock-manager            # expect: active (running)
journalctl -u stock-manager -n 30 --no-pager
```
The unit sets `COOKIE_SECURE=1`, so login only works once HTTPS is live (steps 6 + 8).

**8. nginx reverse proxy**
```bash
sudo cp deploy/nginx-stock-manager.conf /etc/nginx/sites-available/stock-manager
sudo sed -i 's/SERVER_HOSTNAME/<the real hostname>/g' /etc/nginx/sites-available/stock-manager
sudo ln -sf /etc/nginx/sites-available/stock-manager /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

**9. Verify**
```bash
curl -kI https://SERVER_HOSTNAME/          # expect: 302 -> /login
```
Open `https://SERVER_HOSTNAME/`, log in as the bootstrap admin, and create the
staff `user` accounts on the **Users** page.

## Update to a new version
```bash
cd /opt/stock-manager && git pull
.venv/bin/pip install -r requirements.txt        # only if dependencies changed
sudo systemctl restart stock-manager
```

## Backups

`deploy/backup.sh` snapshots `instance/stock.db` with SQLite's online backup API
(atomic, consistent, taken while the app runs) to `/var/backups/stock-manager`, and
prunes snapshots older than 30 days. Never `cp` a live DB or its `-wal`/`-shm` files.
```bash
sudo cp deploy/stock-backup.{service,timer} /etc/systemd/system/
sudo install -d -o stock -g stock /var/backups/stock-manager
sudo systemctl daemon-reload
sudo systemctl enable --now stock-backup.timer    # nightly ~02:15
```

## Hardening

- **Firewall (ufw)** — allow SSH **before** enabling, or you lock yourself out:
  ```bash
  sudo apt install -y ufw
  sudo ufw default deny incoming && sudo ufw default allow outgoing
  sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
  sudo ufw enable
  ```
- **Auto security updates:** `sudo apt install -y unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades`.
- **Nightly reconcile** — conservation check; ends FAILED on a mismatch (no web page):
  ```bash
  sudo cp deploy/stock-reconcile.{service,timer} /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now stock-reconcile.timer
  ```
- **ProxyFix** is already applied in `create_app()`; no action needed.

## Checking reconcile & backups

Both jobs run nightly and record their result; check them on the server.

**Did any reconcile fail?** Each run prints its verdict — scan for `MISMATCH`:
```bash
journalctl -u stock-reconcile.service --no-pager | grep -E 'OK:|MISMATCH'
systemctl status stock-reconcile          # last run: active (exited) = OK, failed = mismatch
```
A `MISMATCH` line names the batch that doesn't balance; no output means it hasn't run yet.

**Are backups running?** The newest snapshot should be from last night:
```bash
ls -lt /var/backups/stock-manager/ | head
systemctl list-timers stock-backup.timer                                  # last + next run
journalctl -u stock-backup.service --no-pager | grep -iE 'Failed|error|not found'   # any failures