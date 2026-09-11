# Carleton course alerts

A Python 3.11+ program for a headless Linux server. It checks public course statuses and sends alerts through Postmark. It does **not** register you for courses.

## Access restriction

**Carleton's [`robots.txt`](https://central.carleton.ca/robots.txt) disallows automated crawling.** The public pages do not require login, but public access does not establish permission for automated use.

The example configuration sets `automated_access_permitted = false`. The program makes no Carleton requests until you change this setting. Confirm that Carleton permits your use before you enable it. The program does not bypass login, MFA, rate limits, or other access controls.

No browser, browser cookies, Carleton password, or Carleton MFA is required for the public search flow. Each check obtains a new anonymous session from the public term selector. This is an HTML interface, not a documented API.

## Included course list

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

Edit `config.toml` to change the list. The program verifies the term, CRN, course name, and section before it accepts a result.

## Alert rules

- `Open`: **Seat available**.
- `Waitlist Open`: **Waitlist available**. This is not an available seat.
- A change from `Waitlist Open` to `Open` sends another alert.
- An unchanged status does not send another alert. A section that closes and opens again does.
- The first successful check also alerts you about any section that is already available.

One email contains all new availability alerts from a check. It includes the course, section, CRN, title, status, UTC check time, and links to Carleton Central and the public timetable.

Public availability does not establish your eligibility. Restrictions, prerequisites, linked sections, and registration deadlines can prevent registration. A place can disappear before you receive the email or register.

## Local setup

Use Python 3.11 or later. The program uses `requests` and `beautifulsoup4`; it needs no browser or display server.

### 1. Install

Run these commands from this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
cp config.example.toml config.toml
cp postmark.env.example postmark.env
chmod 600 postmark.env
```

Alternatively, with `uv`:

```sh
uv sync --locked
```

### 2. Configure Postmark

No Gmail password is required. You can receive alerts in Gmail or another mailbox.

1. Create or select a **Live** server in [Postmark](https://account.postmarkapp.com/).
2. Verify your sender address or domain under **Sender Signatures**. Use a domain that you control, not a `gmail.com` sender.
3. Open your server's **API Tokens** tab. Copy a **Server API token**, not an Account API token.
4. Enter the values directly in `postmark.env` on your machine or server. Do not send the token to an assistant.

```dotenv
POSTMARK_SERVER_TOKEN=your-server-api-token
POSTMARK_FROM_EMAIL=alerts@your-domain.com
ALERT_EMAIL=your-account@gmail.com
```

The program uses the default transactional message stream, `outbound`. Postmark may require account approval before it permits delivery to arbitrary recipients. See its [sender verification guide](https://postmarkapp.com/support/article/adding-sender-signatures) and [private-domain requirement](https://postmarkapp.com/blog/why-cant-i-use-gmail-address).

Load the file into your shell environment:

```sh
set -a
. ./postmark.env
set +a
```

Treat the server token as a secret. Do not commit the real environment file or paste its contents into a chat. `.gitignore` excludes `postmark.env`, `.env`, and `config.toml`. It still excludes the old `gmail.env` to protect any previous credentials.

### 3. Test email separately

This command sends one email. It does not contact Carleton or change monitor state:

```sh
.venv/bin/python carleton_watch.py --test-email
```

The server needs outbound HTTPS access to `api.postmarkapp.com` on TCP port **443**. No SMTP port is required. Check your inbox, spam folder, and Postmark Activity for delivery. API acceptance does not guarantee inbox delivery.

Use a Live server for actual alerts. A Sandbox server does not deliver email. The program rejects the special `POSTMARK_API_TEST` token because that token only validates requests and could otherwise silently discard alerts.

### 4. Check course access

**First confirm permission for automated use.** Then set this value under `[monitor]` in `config.toml`:

```toml
automated_access_permitted = true
```

Run a single check without email or state writes:

```sh
.venv/bin/python carleton_watch.py --config config.toml --dry-run
```

This command makes real Carleton requests. It needs no Postmark credentials. It prints the current statuses, not a saved result. Compare the statuses with Carleton Central before you enable recurring checks.

### 5. Run one normal check

With the Postmark environment variables loaded:

```sh
.venv/bin/python carleton_watch.py --config config.toml
```

The program checks once and exits. It does not run continuously. The default state file is `~/.local/state/carleton-watch/state.json`. Use `--state /path/to/state.json` to change it.

## Headless Linux deployment

These first-install instructions target Debian or Ubuntu with **systemd** and Python 3.11+. Ubuntu 24.04 provides a suitable Python version. Install `python3-venv` if it is absent.

Do not repeat the configuration-copy commands over an existing installation: they replace the destination files. No deployment command below has run automatically.

### 1. Install the program and create a service account

Run these commands from the project directory on the server:

```sh
sudo useradd --system --user-group --home-dir /var/lib/carleton-watch \
  --no-create-home --shell /usr/sbin/nologin carleton-watch
sudo install -d -m 755 /opt/carleton-watch
sudo install -m 644 carleton_watch.py pyproject.toml /opt/carleton-watch/
sudo python3 -m venv /opt/carleton-watch/.venv
sudo /opt/carleton-watch/.venv/bin/python -m pip install /opt/carleton-watch
sudo install -m 644 config.example.toml /etc/carleton-watch.toml
sudo install -m 600 postmark.env.example /etc/carleton-watch.env
```

### 2. Set the configuration and credentials

```sh
sudoedit /etc/carleton-watch.toml
sudoedit /etc/carleton-watch.env
```

Set `POSTMARK_SERVER_TOKEN`, `POSTMARK_FROM_EMAIL`, and `ALERT_EMAIL`. Confirm permission before you change `automated_access_permitted` to `true`.

Check public access from the server before you enable the timer:

```sh
/opt/carleton-watch/.venv/bin/python /opt/carleton-watch/carleton_watch.py \
  --config /etc/carleton-watch.toml --dry-run
```

### 3. Install and test the service

```sh
sudo install -m 644 deploy/carleton-watch.service deploy/carleton-watch.timer \
  /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/carleton-watch.service \
  /etc/systemd/system/carleton-watch.timer
sudo systemctl daemon-reload
sudo systemctl start carleton-watch.service
sudo journalctl -u carleton-watch.service -n 50 --no-pager
```

The service reads the root-owned environment file, then runs as `carleton-watch`. It writes state only under `/var/lib/carleton-watch`. Its system files are read-only, and it cannot access user home directories.

The first run can send availability alerts. No test email is sent automatically.

### 4. Enable recurring checks

After the manual service check succeeds:

```sh
sudo systemctl enable --now carleton-watch.timer
systemctl list-timers carleton-watch.timer
```

The timer checks approximately every **five minutes**, measured from the end of the previous check. It adds a short random delay. It also starts after a reboot. No interactive login or desktop is required.

### 5. View logs or stop the timer

```sh
sudo journalctl -u carleton-watch.service -f
sudo systemctl disable --now carleton-watch.timer
```

The second command stops future checks. To stop an active check too, use `sudo systemctl stop carleton-watch.service`.

Stop the timer when you no longer need the courses. There is no automatic term-end cutoff.

## Reliability and limits

### State and duplicate alerts

The program saves state with a private file mode (`600`) and an atomic file replacement. A file lock prevents concurrent processes from using the same state file. The state survives server restarts.

If an email fails, the program retains the previous course statuses so that a later check can retry the alert. An email API and a local state file cannot guarantee exactly-once delivery. A crash after Postmark accepts an email but before the state save can cause a duplicate. A network timeout after Postmark accepts a request can also cause a duplicate on retry.

Do not delete the state file to fix a routine error. Deletion resets alert history. If the file is corrupt, the program stops without overwriting it. Inspect the file and server logs before you reset it.

### Errors and retry delays

A missing CRN, an unexpected course or section, an unknown status, a changed table, or a wrong term makes the **whole check fail**. The program does not treat an error or a missing row as an available seat. It retains the last successful course statuses.

After a failed check, it delays retries by 5, 10, 20, 40, then at most 60 minutes. A longer HTTP `Retry-After` value takes priority. The timer still runs, but the program skips network requests until that delay ends. It does not make rapid retry requests.

After **three consecutive failed checks**, it attempts one failure email. It sends a recovery email after a subsequent successful check. You can change the threshold with `failure_alert_after`.

If Postmark rejects an email or its API is unavailable, the program does not immediately send another request to report that failure. It logs the failure and retries on later checks. This also protects Postmark's rate limits. Configuration errors and corrupt state files appear in the logs; they do not generate failure emails.

The normal check uses three HTTP requests: public term selector, search form, and a combined subject search. The program then selects the configured CRNs from the result. The five-minute interval can miss an opening that disappears between checks.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Successful check, dry run, test email, or intentional skip due to lock or retry delay |
| `1` | Check or notification failed; the program saved failure state |
| `2` | Setup, access opt-in, state, or command error; inspect the log |

## Development checks

The tests use mocked HTTP for both Carleton and Postmark. They do not contact either service or send email.

```sh
uv sync --locked --extra dev
make check
```

Without `uv`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check
```

`make check` runs Ruff lint, pytest, the format check, and Python compilation. Use `make format` to format the Python files.

The public endpoint was inspected during the initial discussion. The completed monitor has not made a live course check or sent a real Postmark message. Deployment still requires the permission check and the server-side tests above.
