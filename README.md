# Carleton course alerts via ntfy

The Raspberry Pi runs the course checker. **ntfy.sh delivers push notifications to your iPhone.**

```text
Pi: Python checker → ntfy.sh → ntfy iPhone app
```

No email account, sender verification, incoming port, or ntfy server installation is required. The Pi needs Python 3.11+ and outbound HTTPS access.

## Quick setup

### 1. Subscribe on your iPhone

Install [ntfy from the App Store](https://apps.apple.com/us/app/ntfy/id1625396347). Allow notifications. Subscribe to a topic on `https://ntfy.sh`.

Choose a long, random topic name. You can generate one locally:

```sh
python3 -c "import secrets; print('carleton-' + secrets.token_hex(12))"
```

Use this same topic name in the phone app and the Pi configuration.

**Public topics are not private by default.** Anyone who knows an unprotected topic name can read messages and publish to it. A random name reduces discovery but is not access control. An authenticated, access-protected topic is optional if you need privacy.

### 2. Install on the Pi

Use Raspberry Pi OS Bookworm or later, or another Linux distribution with Python 3.11+.

Run these commands from the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.toml config.toml
cp ntfy.env.example ntfy.env
chmod 600 ntfy.env
```

If Python cannot create the virtual environment, install `python3-venv` with your package manager.

### 3. Set your topic

Edit `ntfy.env`:

```dotenv
NTFY_SERVER=https://ntfy.sh
NTFY_TOPIC=your-long-random-topic
NTFY_TOKEN=
```

Leave `NTFY_TOKEN` empty for an anonymous topic. If you use a protected topic, put its access token here and configure access in the phone app too.

Load the settings into your shell:

```sh
set -a
. ./ntfy.env
set +a
```

The script trusts your settings. It does not add local URL, topic, or token validators. ntfy reports actual request failures.

### 4. Send a test notification

```sh
.venv/bin/python carleton_watch.py --test-notification
```

This sends one notification. It does not contact Carleton or change saved course state. Confirm that it appears in the ntfy app. The service accepting a request does not guarantee that your phone displayed it.

For a course check without a notification or state update:

```sh
.venv/bin/python carleton_watch.py --config config.toml --dry-run
```

For one normal check:

```sh
.venv/bin/python carleton_watch.py --config config.toml
```

### 5. Enable the Linux timer

The program checks once and exits. The supplied systemd timer runs it approximately every five minutes and starts it after reboot. See the installation commands below.

## Watched courses

Term: **Fall 2026**, code `202630`.

| Course | Section | CRN |
| --- | --- | --- |
| HIST 2003 | A | 32301 |
| HIST 2401 | A | 32309 |
| HIST 2710 | A | 32315 |
| HIST 3909 | A | 32338 |
| PHIL 2301 | A | 33780 |
| PHIL 2901 | A | 33788 |
| RELI 2110 | A | 34130 |
| RELI 3101 | B | 34139 |

Edit `config.toml` to change the list. The checker uses Carleton's public timetable, not your browser cookies or student login.

## Notification behavior

- `Open`: **Seat available**.
- `Waitlist Open`: **Waitlist available**, not an available seat.
- A change from an open waitlist to an available seat sends another notification.
- An unchanged status does not send another notification. A section that closes and opens again does.
- The first check also reports sections that are already available.

A notification includes the course, section, CRN, title, status, UTC check time, and a registration link. Multiple new alerts from one check share one notification.

Public availability does not establish your personal eligibility. The program never registers you for a course. A place can disappear between checks or before you register.

## systemd installation

These commands are for a **first installation** on the Pi. Do not copy the example files over an existing configuration.

### Install the program

Run from the project directory:

```sh
sudo useradd --system --user-group --home-dir /var/lib/carleton-watch \
  --no-create-home --shell /usr/sbin/nologin carleton-watch
sudo install -d -m 755 /opt/carleton-watch
sudo install -m 644 carleton_watch.py pyproject.toml /opt/carleton-watch/
sudo python3 -m venv /opt/carleton-watch/.venv
sudo /opt/carleton-watch/.venv/bin/python -m pip install /opt/carleton-watch
sudo install -m 644 config.toml /etc/carleton-watch.toml
sudo install -m 600 ntfy.env /etc/carleton-watch.env
sudo install -m 644 deploy/carleton-watch.service deploy/carleton-watch.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
```

### Start the timer

```sh
sudo systemctl start carleton-watch.service
sudo journalctl -u carleton-watch.service -n 30 --no-pager
sudo systemctl enable --now carleton-watch.timer
```

The service reads `/etc/carleton-watch.env`. It writes state under `/var/lib/carleton-watch` as a dedicated service user.

View logs or stop future checks:

```sh
sudo journalctl -u carleton-watch.service -f
sudo systemctl disable --now carleton-watch.timer
```

Stop the timer when you no longer need the courses. There is no automatic term-end cutoff.

### Switch an existing installation from Postmark

Update the script and replace the old email variables in `/etc/carleton-watch.env` with the three ntfy variables. Keep the existing course configuration and state file.

```sh
sudo install -m 644 carleton_watch.py /opt/carleton-watch/carleton_watch.py
sudoedit /etc/carleton-watch.env
sudo systemctl start carleton-watch.service
```

The existing timer does not need a change. The old `--test-email` command is now `--test-notification`.

## Failure handling

The checker retains its last successful course state if a page request or notification fails. It rejects incomplete course results rather than report a false opening.

Retries wait 5, 10, 20, 40, then at most 60 minutes after consecutive failures. A longer HTTP `Retry-After` value takes priority. The timer skips requests until the retry delay ends.

After three consecutive course-check failures, the program attempts one failure notification. It reports recovery after a subsequent successful check. If ntfy itself fails, the program logs the failure rather than immediately send another notification through the failed service.

State uses an atomic file replacement and a lock to prevent overlapping runs. It survives a Pi restart. A crash or timeout after ntfy accepts a notification can still cause a duplicate on retry.

## Notes

Carleton's [`robots.txt`](https://central.carleton.ca/robots.txt) disallows automated crawling. This is an informational notice, not a program gate.

The program does not read or use email credentials. Local `ntfy.env`, `postmark.env`, and `gmail.env` files remain excluded from Git.

The Pi only makes outbound requests. With ntfy.sh, iPhone push delivery needs no self-hosted upstream configuration. See [ntfy's documentation](https://docs.ntfy.sh/) for service details and limits.

## Development

```sh
uv sync --locked --extra dev
make check
```

Alternatively, install development dependencies with `.venv/bin/python -m pip install -e '.[dev]'`.

The eight core tests use mocked HTTP. They do not contact Carleton or ntfy. `make check` also runs lint, format, and compilation checks. No live notification was sent during this change.
