#!/usr/bin/env python3
"""Check public Carleton course statuses once, then exit. Python 3.11+."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://central.carleton.ca/prod/"
PUBLIC_URL = BASE_URL + "bwysched.p_select_term?wsea_code=EXT"
CENTRAL_URL = BASE_URL + "env_util.p_central_main"
ROBOTS_URL = "https://central.carleton.ca/robots.txt"
POSTMARK_URL = "https://api.postmarkapp.com/email"
LOGGER = logging.getLogger("carleton-watch")
STATUS_LABELS = {
    "open": "Open",
    "waitlist open": "Waitlist Open",
    "full, no waitlist": "Full, No Waitlist",
    "waitlist closed": "Waitlist Closed",
    "waitlist full": "Waitlist Full",
    "closed": "Closed",
    "full": "Full",
    "cancelled": "Cancelled",
    "canceled": "Canceled",
}
ALERT_STATUSES = {"open": "Seat available", "waitlist open": "Waitlist available"}


class MonitorError(Exception):
    """An unsafe or unsuccessful check. Do not advance course statuses."""


class AlreadyRunning(MonitorError):
    pass


class PostmarkError(requests.HTTPError):
    """An email failure with a safe message and optional Retry-After response."""


@dataclass(frozen=True)
class Course:
    course: str
    section: str
    crn: str


@dataclass(frozen=True)
class Settings:
    term_code: str
    courses: tuple[Course, ...]
    automated_access_permitted: bool = False
    timeout_seconds: int = 30
    failure_alert_after: int = 3


@dataclass(frozen=True)
class Observation:
    course: Course
    status: str
    title: str


@dataclass
class State:
    version: int = 1
    statuses: dict[str, str] = field(default_factory=dict)
    consecutive_failures: int = 0
    failure_notified: bool = False
    next_check_at: float = 0.0
    last_success_at: str | None = None
    last_error: str | None = None


def utc_now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_settings(path: Path):
    with path.open("rb") as file:
        raw = tomllib.load(file)
    try:
        monitor = raw["monitor"]
        settings = Settings(
            term_code=monitor["term_code"],
            courses=tuple(Course(**course) for course in raw["courses"]),
            automated_access_permitted=monitor.get("automated_access_permitted", False),
            timeout_seconds=monitor.get("timeout_seconds", 30),
            failure_alert_after=monitor.get("failure_alert_after", 3),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise MonitorError("Invalid configuration. Use config.example.toml as a template.") from exc
    if not isinstance(settings.term_code, str) or not re.fullmatch(
        r"\d{4}(10|20|30)", settings.term_code
    ):
        raise MonitorError("term_code must be a six-digit string, such as '202630'.")
    if type(settings.automated_access_permitted) is not bool:
        raise MonitorError("automated_access_permitted must be true or false.")
    for name, maximum in (("timeout_seconds", 120), ("failure_alert_after", 100)):
        value = getattr(settings, name)
        if type(value) is not int or not 1 <= value <= maximum:
            raise MonitorError(f"{name} must be an integer between 1 and {maximum}.")
    if not settings.courses:
        raise MonitorError("Configure at least one course.")
    seen = set()
    for course in settings.courses:
        for name, pattern in (
            ("course", r"[A-Z]{2,5} \d{4}"),
            ("section", r"[A-Z0-9]{1,5}"),
            ("crn", r"\d{5}"),
        ):
            value = getattr(course, name)
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise MonitorError(f"Invalid {name} in course configuration: {value!r}.")
        if course.crn in seen:
            raise MonitorError(f"Duplicate CRN in configuration: {course.crn}.")
        seen.add(course.crn)
    return settings


def form_values(form):
    """Preserve repeated names, including Banner's hidden 'dummy' fields."""
    values = []
    for control in form.find_all(["input", "select", "textarea"]):
        name = control.get("name")
        if not name or control.has_attr("disabled"):
            continue
        if control.name == "select":
            options = [
                option for option in control.find_all("option") if not option.has_attr("disabled")
            ]
            selected = [option for option in options if option.has_attr("selected")]
            if not selected and not control.has_attr("multiple"):
                selected = options[:1]
            values.extend((name, option.get("value", option.get_text())) for option in selected)
        elif control.name == "textarea":
            values.append((name, control.get_text()))
        else:
            kind = control.get("type", "text").lower()
            if kind in {"submit", "button", "reset", "image", "file"}:
                continue
            if kind in {"checkbox", "radio"} and not control.has_attr("checked"):
                continue
            values.append(
                (name, control.get("value", "on" if kind in {"checkbox", "radio"} else ""))
            )
    return values


def get_form(html: str, endpoint: str):
    soup = BeautifulSoup(html, "html.parser")
    expected = urljoin(BASE_URL, endpoint)
    for form in soup.find_all("form", action=True):
        if urljoin(BASE_URL, form["action"]) == expected:
            return form
    raise MonitorError(f"Public form {endpoint} is missing. The site may have changed.")


def submit_form(session, form, endpoint: str, replacements: dict[str, list[str]], timeout: int):
    values = [(name, value) for name, value in form_values(form) if name not in replacements]
    for name, replacements_for_name in replacements.items():
        values.extend((name, value) for value in replacements_for_name)
    method = form.get("method", "get").lower()
    if method not in {"get", "post"}:
        raise MonitorError(f"Unexpected form method: {method}.")
    # Only these known public search endpoints can receive form submissions.
    if endpoint not in {"bwysched.p_search_fields", "bwysched.p_course_search"}:
        raise MonitorError("Refusing an unexpected form endpoint.")
    kwargs = {"params" if method == "get" else "data": values}
    response = session.request(method, urljoin(BASE_URL, endpoint), timeout=(10, timeout), **kwargs)
    return response_html(response)


def response_html(response):
    response.raise_for_status()
    parsed = urlparse(response.url)
    if parsed.scheme != "https" or parsed.netloc != "central.carleton.ca":
        raise MonitorError("The public search redirected away from Carleton Central.")
    if "html" not in response.headers.get("Content-Type", "").lower():
        raise MonitorError("The public search did not return HTML.")
    return response.text


def parse_results(html: str, settings: Settings):
    soup = BeautifulSoup(html, "html.parser")
    terms = {
        control.get("value") for control in soup.find_all("input", attrs={"name": "term_code"})
    }
    if terms != {settings.term_code}:
        raise MonitorError("The results do not confirm the configured term.")
    rows = [
        [cell.get_text(" ", strip=True) for cell in row.find_all("td", recursive=False)]
        for row in soup.find_all("tr")
    ]
    required = {"Status", "CRN", "Subject", "Section", "Title"}
    header = next((row for row in rows if required.issubset(row)), None)
    if header is None:
        raise MonitorError("The course table header is missing or has changed.")
    columns = {name: header.index(name) for name in required}
    targets = {course.crn: course for course in settings.courses}
    found = {}
    for cells in rows:
        if len(cells) != len(header):
            continue
        crn = cells[columns["CRN"]]
        if crn not in targets:
            continue
        if crn in found:
            raise MonitorError(f"Duplicate result for CRN {crn}.")
        target = targets[crn]
        subject = " ".join(cells[columns["Subject"]].split())
        if subject != target.course or cells[columns["Section"]] != target.section:
            raise MonitorError(
                f"CRN {crn} does not match {target.course} section {target.section}."
            )
        status = " ".join(cells[columns["Status"]].split()).casefold()
        if status not in STATUS_LABELS:
            raise MonitorError(f"Unknown status for CRN {crn}: {status!r}.")
        found[crn] = Observation(target, status, cells[columns["Title"]])
    missing = targets.keys() - found.keys()
    if missing:
        raise MonitorError(f"Missing CRNs in search results: {', '.join(sorted(missing))}.")
    return [found[course.crn] for course in settings.courses]


def fetch_courses(settings: Settings):
    if not settings.automated_access_permitted:
        raise MonitorError(
            f"Automated access is disabled. {ROBOTS_URL} disallows crawling. "
            "Confirm that this use is permitted before setting "
            "monitor.automated_access_permitted = true."
        )
    with requests.Session() as session:
        session.headers.update(
            {"User-Agent": "CarletonCourseWatch/0.1 (personal course status monitor)"}
        )
        html = response_html(session.get(PUBLIC_URL, timeout=(10, settings.timeout_seconds)))
        form = get_form(html, "bwysched.p_search_fields")
        selector = form.find("select", attrs={"name": "term_code"})
        if selector is None or settings.term_code not in {
            option.get("value") for option in selector.find_all("option")
        }:
            raise MonitorError(
                f"Term {settings.term_code} is not available in the public selector."
            )
        html = submit_form(
            session,
            form,
            "bwysched.p_search_fields",
            {"term_code": [settings.term_code]},
            settings.timeout_seconds,
        )
        form = get_form(html, "bwysched.p_course_search")
        values = form_values(form)
        if ("term_code", settings.term_code) not in values or not any(
            name == "session_id" and value for name, value in values
        ):
            raise MonitorError("The search form has no valid anonymous session or term.")
        subjects = sorted({course.course.split()[0] for course in settings.courses})
        html = submit_form(
            session,
            form,
            "bwysched.p_course_search",
            {
                "sel_subj": ["dummy", *subjects],
                "sel_special": ["dummy", "N"],
                "sel_crn": [""],
                "sel_number": [""],
            },
            settings.timeout_seconds,
        )
        return parse_results(html, settings)


def load_state(path: Path):
    if not path.exists():
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(asdict(State())):
            raise ValueError("unexpected state fields")
        state = State(**data)
        if type(state.version) is not int or state.version != 1:
            raise ValueError("unsupported state version")
        if not isinstance(state.statuses, dict) or any(
            not re.fullmatch(r"\d{6}:\d{5}", key) or value not in STATUS_LABELS
            for key, value in state.statuses.items()
        ):
            raise ValueError("invalid saved course statuses")
        if type(state.consecutive_failures) is not int or state.consecutive_failures < 0:
            raise ValueError("invalid failure count")
        if type(state.failure_notified) is not bool:
            raise ValueError("invalid failure notification flag")
        if type(state.next_check_at) not in {int, float} or not 0 <= state.next_check_at < float(
            "inf"
        ):
            raise ValueError("invalid retry time")
        if any(
            value is not None and not isinstance(value, str)
            for value in (state.last_success_at, state.last_error)
        ):
            raise ValueError("invalid saved timestamps or error")
        return state
    except (ValueError, TypeError) as exc:
        raise MonitorError(
            f"Invalid state file {path}. Inspect it before a reset; it was not overwritten."
        ) from exc


def save_state(path: Path, state: State):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".state-", delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(asdict(state), file, indent=2, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("Another check holds the state lock; skipped this run.") from exc
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def postmark_sender():
    token = os.environ.get("POSTMARK_SERVER_TOKEN", "").strip()
    sender = os.environ.get("POSTMARK_FROM_EMAIL", "").strip()
    recipient = os.environ.get("ALERT_EMAIL", "").strip()
    for name, value in (("POSTMARK_FROM_EMAIL", sender), ("ALERT_EMAIL", recipient)):
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
            raise MonitorError(f"Set {name} to one email address.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise MonitorError("Set POSTMARK_SERVER_TOKEN to your Postmark server API token.")
    if token.casefold() == "postmark_api_test":
        raise MonitorError(
            "POSTMARK_API_TEST does not deliver email. Use a live Postmark server token."
        )

    def send(subject: str, body: str):
        try:
            response = requests.post(
                POSTMARK_URL,
                headers={
                    "X-Postmark-Server-Token": token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "From": sender,
                    "To": recipient,
                    "Subject": subject,
                    "TextBody": body,
                    "MessageStream": "outbound",
                    "TrackOpens": False,
                    "TrackLinks": "None",
                },
                timeout=(10, 30),
                # A redirect must never forward the server token to another host.
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PostmarkError(
                "Postmark API request failed. Check connectivity and the Postmark service.",
                response=exc.response,
            ) from exc
        try:
            result = response.json()
        except ValueError:
            result = None
        error_code = result.get("ErrorCode") if isinstance(result, dict) else None
        if response.status_code != 200 or type(error_code) is not int or error_code != 0:
            code = error_code if type(error_code) is int else "unknown"
            # Do not log response bodies or request headers: they can contain secrets.
            raise PostmarkError(
                f"Postmark rejected the email (HTTP {response.status_code}, ErrorCode {code}). "
                "Check the server token, sender verification, and Postmark Activity.",
                response=response,
            )
        if not isinstance(result.get("MessageID"), str) or not result["MessageID"].strip():
            raise PostmarkError("Postmark returned no valid MessageID.", response=response)

    return send


def status_key(settings: Settings, course: Course):
    return f"{settings.term_code}:{course.crn}"


def new_alerts(settings: Settings, observations: list[Observation], state: State):
    return [
        observation
        for observation in observations
        if observation.status in ALERT_STATUSES
        and state.statuses.get(status_key(settings, observation.course)) != observation.status
    ]


def alert_message(settings: Settings, alerts: list[Observation], recovered: bool):
    if len(alerts) == 1:
        item = alerts[0]
        subject = f"[Carleton] {ALERT_STATUSES[item.status]}: {item.course.course} {item.course.section} ({item.course.crn})"
    elif alerts:
        subject = f"[Carleton] {len(alerts)} course availability updates"
    else:
        subject = "[Carleton] Course monitor recovered"
    lines = [f"Term: {settings.term_code}", f"Checked at: {utc_now()} (UTC)", ""]
    if recovered:
        lines.extend(["The monitor recovered after its reported failure.", ""])
    for item in alerts:
        course = item.course
        lines.extend(
            [
                f"{ALERT_STATUSES[item.status]}: {course.course} {course.section} (CRN {course.crn})",
                f"Title: {item.title}",
                f"Public status: {STATUS_LABELS[item.status]}",
                "",
            ]
        )
    lines.extend(
        [
            "An open waitlist is not an available seat.",
            "Public status does not confirm your eligibility. A place can disappear before you register.",
            f"Register manually: {CENTRAL_URL}",
            f"Public timetable: {PUBLIC_URL}",
        ]
    )
    return subject, "\n".join(lines)


def retry_delay(error: Exception, failures: int):
    delay = min(300 * 2 ** min(failures - 1, 4), 3600)
    if isinstance(error, requests.HTTPError) and error.response is not None:
        header = error.response.headers.get("Retry-After", "")
        try:
            seconds = (
                int(header)
                if header.isdigit()
                else parsedate_to_datetime(header).timestamp() - time.time()
            )
            delay = max(delay, seconds)
        except (ValueError, TypeError, OverflowError):
            pass
    return delay


def print_observations(observations: list[Observation]):
    for item in observations:
        print(
            f"{item.course.crn}  {item.course.course} {item.course.section:<3}  {STATUS_LABELS[item.status]}",
            flush=True,
        )


def monitor_once(settings: Settings, path: Path, send):
    with state_lock(path):
        state = load_state(path)
        if state.next_check_at > time.time():
            LOGGER.info("Retry backoff is active; skipped this run.")
            return 0
        try:
            observations = fetch_courses(settings)
            print_observations(observations)
            alerts = new_alerts(settings, observations, state)
            if alerts or state.failure_notified:
                send(*alert_message(settings, alerts, state.failure_notified))
                LOGGER.info("Postmark accepted the notification.")
        except (MonitorError, requests.RequestException, OSError) as exc:
            state.consecutive_failures += 1
            state.last_error = str(exc)[:2000]
            state.next_check_at = time.time() + retry_delay(exc, state.consecutive_failures)
            LOGGER.error("Check failed (%s): %s", state.consecutive_failures, exc)
            if (
                state.consecutive_failures >= settings.failure_alert_after
                and not state.failure_notified
                and not isinstance(exc, PostmarkError)
            ):
                try:
                    send(
                        "[Carleton] Course monitor needs attention",
                        f"The monitor failed {state.consecutive_failures} consecutive checks.\n"
                        f"Last successful check: {state.last_success_at or 'none'}\n"
                        f"Error: {state.last_error}\n\n"
                        "Course availability is unknown. Check the server logs.\n"
                        "The monitor will retry automatically on later timer runs.\n",
                    )
                    state.failure_notified = True
                except (MonitorError, requests.RequestException, OSError) as mail_error:
                    state.next_check_at = max(
                        state.next_check_at,
                        time.time() + retry_delay(mail_error, state.consecutive_failures),
                    )
                    LOGGER.error("Could not send the failure notification: %s", mail_error)
            save_state(path, state)
            return 1
        # Do not advance statuses until Postmark accepts any required notification.
        state.statuses.update(
            {status_key(settings, item.course): item.status for item in observations}
        )
        state.consecutive_failures = 0
        state.failure_notified = False
        state.next_check_at = 0.0
        state.last_error = None
        state.last_success_at = utc_now()
        save_state(path, state)
        LOGGER.info(
            "Checked %s courses; %s new availability alerts.", len(observations), len(alerts)
        )
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument(
        "--state", type=Path, default=Path.home() / ".local/state/carleton-watch/state.json"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print statuses, without email or state writes.",
    )
    modes.add_argument(
        "--test-email",
        action="store_true",
        help="Send a Postmark test email; do not contact Carleton or change state.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.test_email:
            postmark_sender()(
                "[Carleton] Test email",
                "Your Postmark test email arrived.\nNo course check or registration took place.\n",
            )
            LOGGER.info("Postmark accepted the test email. Check your inbox and Postmark Activity.")
            return 0
        settings = load_settings(args.config)
        if not settings.automated_access_permitted:
            raise MonitorError(
                f"Automated access is disabled: {ROBOTS_URL} disallows crawling. "
                "Confirm permission before setting monitor.automated_access_permitted = true."
            )
        if args.dry_run:
            print_observations(fetch_courses(settings))
            return 0
        return monitor_once(settings, args.state, postmark_sender())
    except AlreadyRunning as exc:
        LOGGER.info("%s", exc)
        return 0
    except (
        MonitorError,
        OSError,
        ValueError,
        requests.RequestException,
    ) as exc:
        LOGGER.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
