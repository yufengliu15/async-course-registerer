import json
import smtplib
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from bs4 import BeautifulSoup

import carleton_watch as watch

ROOT = Path(__file__).resolve().parents[1]
TERM_FORM = """
<form action="bwysched.p_search_fields" method="post">
<input type="hidden" name="wsea_code" value="EXT">
<input type="hidden" name="session_id" value="123456">
<select name="term_code">
<option value="202620" selected>Summer 2026</option>
<option value="202630">Fall 2026</option>
</select>
<input type="submit" value="Proceed to Search">
</form>
"""
SEARCH_FORM = """
<form action="bwysched.p_course_search" method="post">
<input type="hidden" name="wsea_code" value="EXT">
<input type="hidden" name="session_id" value="123456">
<input type="hidden" name="term_code" value="202630">
<input type="hidden" name="sel_subj" value="dummy">
<select name="sel_subj" multiple><option value="" selected>All Subjects</option></select>
<input type="hidden" name="sel_special" value="dummy">
<select name="sel_special" multiple><option value="N" selected>Show All</option></select>
<input type="hidden" name="sel_day" value="dummy">
<input type="checkbox" name="sel_day" value="m" checked>
<input type="checkbox" name="sel_day" value="t" checked>
<input name="sel_number" value="">
<input name="sel_crn" value="">
<input type="submit" name="time_table" value="View Worksheet">
<input type="submit" name="reset_button" value="Reset">
</form>
"""


@pytest.fixture
def settings():
    return replace(
        watch.load_settings(ROOT / "config.example.toml"), automated_access_permitted=True
    )


def result_html(settings, statuses=None):
    """Reduced public Banner table structure, including its extra closing tr tag."""
    header = [
        "Select",
        "Status",
        "CRN",
        "Subject",
        "Section",
        "Title",
        "Credits",
        "Schedule",
        "Restricts?",
        "Prereqs?",
        "Instructor",
    ]
    html = [
        f'<input type="hidden" name="term_code" value="{settings.term_code}">',
        "<table>",
        "<tr>",
    ]
    html.extend(f"<td><b>{name}</b></td>" for name in header)
    html.append("</tr>")
    for index, course in enumerate(settings.courses):
        status = statuses[index] if statuses else "Full, No Waitlist"
        cells = [
            "&nbsp;",
            f'<font color="red">{status}</font>',
            f'<a href="bwysched.p_display_course?crn={course.crn}">{course.crn}</a>',
            course.course,
            course.section,
            "A course title",
            ".5",
            "Lecture",
            "No",
            "No",
            "An instructor",
        ]
        html.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
        html.append('<tr><td>&nbsp;</td><td colspan="10">Meeting Date: Sep 09, 2026</td></tr></tr>')
    html.append("</table>")
    return "\n".join(html)


def observations(settings, status="full, no waitlist"):
    return [watch.Observation(course, status, "A course title") for course in settings.courses]


def response(html, url=watch.PUBLIC_URL):
    result = requests.Response()
    result.status_code = 200
    result.url = url
    result.headers["Content-Type"] = "text/html; charset=UTF-8"
    result._content = html.encode()
    return result


def install_fetch(monkeypatch, result):
    fetch = Mock(return_value=result)
    monkeypatch.setattr(watch, "fetch_courses", fetch)
    return fetch


def test_example_has_exact_watchlist_and_access_is_disabled():
    settings = watch.load_settings(ROOT / "config.example.toml")
    assert settings.term_code == "202630"
    assert not settings.automated_access_permitted
    assert [course.crn for course in settings.courses] == [
        "32301",
        "32309",
        "32315",
        "32338",
        "33780",
        "33788",
        "34130",
        "34139",
    ]
    assert settings.courses[-1].section == "B"


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace('crn = "32309"', 'crn = "32301"'),
        lambda text: text.replace('term_code = "202630"', "term_code = 202630"),
        lambda text: text.replace("timeout_seconds = 30", "timeout_seconds = 0"),
        lambda text: text.replace("failure_alert_after = 3", "failure_alert_after = true"),
        lambda text: text.replace(
            "automated_access_permitted = false", 'automated_access_permitted = "true"'
        ),
        lambda text: text.replace('section = "B"', 'section = ""'),
        lambda text: text.replace('course = "HIST 2003"', 'course = "HIST"'),
    ],
)
def test_rejects_bad_config(tmp_path, change):
    path = tmp_path / "config.toml"
    path.write_text(change((ROOT / "config.example.toml").read_text()))
    with pytest.raises(watch.MonitorError):
        watch.load_settings(path)


def test_form_preserves_dummy_fields_and_does_not_submit_worksheet():
    form = watch.get_form(SEARCH_FORM, "bwysched.p_course_search")
    values = watch.form_values(form)
    assert [value for name, value in values if name == "sel_subj"] == ["dummy", ""]
    assert [value for name, value in values if name == "sel_day"] == ["dummy", "m", "t"]
    assert not any(name in {"time_table", "reset_button"} for name, _ in values)


def test_form_uses_browser_selection_defaults():
    form = BeautifulSoup(
        """<form>
      <select name="one"><option value="0">Zero</option><option value="1">One</option></select>
      <select name="many" multiple><option value="x">X</option></select>
      <input type="checkbox" name="unchecked" value="x">
      <input name="disabled" value="x" disabled>
      <textarea name="notes">test</textarea>
    </form>""",
        "html.parser",
    ).form
    assert watch.form_values(form) == [("one", "0"), ("notes", "test")]


def test_fetch_uses_fresh_anonymous_forms_and_one_combined_search(settings, monkeypatch):
    session = Mock()
    session.get.return_value = response(TERM_FORM)
    session.request.side_effect = [response(SEARCH_FORM), response(result_html(settings))]
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=session)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(watch.requests, "Session", factory)
    result = watch.fetch_courses(settings)
    assert len(result) == 8
    session.get.assert_called_once_with(watch.PUBLIC_URL, timeout=(10, 30))
    assert session.request.call_count == 2
    first, second = session.request.call_args_list
    assert first.args == ("post", watch.BASE_URL + "bwysched.p_search_fields")
    assert ("term_code", "202630") in first.kwargs["data"]
    assert second.args == ("post", watch.BASE_URL + "bwysched.p_course_search")
    payload = second.kwargs["data"]
    assert ("session_id", "123456") in payload
    assert [value for name, value in payload if name == "sel_subj"] == [
        "dummy",
        "HIST",
        "PHIL",
        "RELI",
    ]
    assert [value for name, value in payload if name == "sel_special"] == ["dummy", "N"]
    assert not any(name in {"time_table", "reset_button"} for name, _ in payload)


def test_disabled_access_makes_no_network_requests(settings, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(watch.requests, "Session", factory)
    with pytest.raises(watch.MonitorError, match="disabled"):
        watch.fetch_courses(replace(settings, automated_access_permitted=False))
    factory.assert_not_called()


def test_rejects_external_form_action():
    html = '<form action="https://example.com/bwysched.p_search_fields"></form>'
    with pytest.raises(watch.MonitorError, match="missing"):
        watch.get_form(html, "bwysched.p_search_fields")


def test_rejects_external_redirect():
    with pytest.raises(watch.MonitorError, match="redirected"):
        watch.response_html(response("login", "https://example.com/login"))


def test_parse_realistic_statuses(settings):
    statuses = ["Full, No Waitlist"] * 4 + ["Waitlist Closed"] * 2 + ["Full, No Waitlist"] * 2
    result = watch.parse_results(result_html(settings, statuses), settings)
    assert [item.course for item in result] == list(settings.courses)
    assert result[4].status == "waitlist closed"
    assert result[7].course.section == "B"


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda html: html.replace('value="202630"', 'value="202710"'), "term"),
        (lambda html: html.replace("<b>Status</b>", "<b>Availability</b>"), "header"),
        (lambda html: html.replace("32301", "99999"), "Missing CRNs"),
        (lambda html: html.replace("HIST 2003", "HIST 2004"), "does not match"),
        (lambda html: html.replace("<td>A</td>", "<td>Z</td>", 1), "does not match"),
        (lambda html: html.replace("Full, No Waitlist", "Unknown result", 1), "Unknown status"),
    ],
)
def test_parser_rejects_incomplete_or_changed_results(settings, change, message):
    with pytest.raises(watch.MonitorError, match=message):
        watch.parse_results(change(result_html(settings)), settings)


def test_parser_rejects_login_html(settings):
    with pytest.raises(watch.MonitorError):
        watch.parse_results('<html><form action="login">Sign in</form></html>', settings)


def test_parser_rejects_duplicate_crns(settings):
    html = result_html(settings)
    soup = BeautifulSoup(html, "html.parser")
    course_row = soup.find("a").find_parent("tr")
    with pytest.raises(watch.MonitorError, match="Duplicate"):
        watch.parse_results(html + str(course_row), settings)


@pytest.mark.parametrize(
    "previous, current, expected",
    [
        (None, "full, no waitlist", False),
        (None, "open", True),
        (None, "waitlist open", True),
        ("full, no waitlist", "open", True),
        ("waitlist closed", "waitlist open", True),
        ("waitlist open", "open", True),
        ("open", "waitlist open", True),
        ("open", "open", False),
        ("waitlist open", "waitlist open", False),
        ("open", "closed", False),
    ],
)
def test_alert_transitions(settings, previous, current, expected):
    course = settings.courses[0]
    state = watch.State()
    if previous is not None:
        state.statuses[watch.status_key(settings, course)] = previous
    alerts = watch.new_alerts(settings, [watch.Observation(course, current, "Title")], state)
    assert bool(alerts) == expected


def test_term_is_part_of_state_key(settings):
    course = settings.courses[0]
    state = watch.State(statuses={f"202710:{course.crn}": "open"})
    assert watch.new_alerts(settings, [watch.Observation(course, "open", "Title")], state)


def test_atomic_state_round_trip_and_permissions(tmp_path):
    path = tmp_path / "private/state.json"
    state = watch.State(statuses={"202630:32301": "open"}, last_success_at=watch.utc_now())
    watch.save_state(path, state)
    assert watch.load_state(path) == state
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".state-*"))


@pytest.mark.parametrize("content", ["not json", "{}", "[]", '{"version":2}'])
def test_corrupt_state_is_not_reset_or_overwritten(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content)
    with pytest.raises(watch.MonitorError, match="not overwritten"):
        watch.load_state(path)
    assert path.read_text() == content


def test_invalid_state_status_is_rejected(tmp_path):
    path = tmp_path / "state.json"
    watch.save_state(path, watch.State())
    data = json.loads(path.read_text())
    data["statuses"] = {"202630:32301": ["open"]}
    path.write_text(json.dumps(data))
    with pytest.raises(watch.MonitorError):
        watch.load_state(path)


def test_state_lock_prevents_overlap(tmp_path):
    path = tmp_path / "state.json"
    with watch.state_lock(path), pytest.raises(watch.AlreadyRunning), watch.state_lock(path):
        pass
    with watch.state_lock(path):
        pass


def test_success_sends_once_across_restarts(settings, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    install_fetch(monkeypatch, observations(settings, "open"))
    send = Mock()
    assert watch.monitor_once(settings, path, send) == 0
    assert watch.monitor_once(settings, path, send) == 0
    assert send.call_count == 1
    assert "8 course availability updates" in send.call_args.args[0]
    assert "Seat available" in send.call_args.args[1]
    assert watch.load_state(path).last_success_at


def test_reopening_sends_again(settings, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    fetch = install_fetch(monkeypatch, observations(settings, "open"))
    send = Mock()
    watch.monitor_once(settings, path, send)
    fetch.return_value = observations(settings)
    watch.monitor_once(settings, path, send)
    fetch.return_value = observations(settings, "open")
    watch.monitor_once(settings, path, send)
    assert send.call_count == 2


def test_smtp_failure_retains_previous_statuses_for_retry(settings, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    initial = watch.State(
        statuses={watch.status_key(settings, course): "closed" for course in settings.courses}
    )
    watch.save_state(path, initial)
    install_fetch(monkeypatch, observations(settings, "open"))
    send = Mock(side_effect=smtplib.SMTPException("SMTP unavailable"))
    monkeypatch.setattr(watch.time, "time", lambda: 1000.0)
    assert watch.monitor_once(settings, path, send) == 1
    state = watch.load_state(path)
    assert state.statuses == initial.statuses
    assert state.consecutive_failures == 1
    assert state.next_check_at == 1300.0
    monkeypatch.setattr(watch.time, "time", lambda: 1301.0)
    send.side_effect = None
    assert watch.monitor_once(settings, path, send) == 0
    assert set(watch.load_state(path).statuses.values()) == {"open"}


def test_failure_notice_is_deduplicated_and_recovery_is_reported(settings, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    fetch = install_fetch(monkeypatch, None)
    fetch.side_effect = watch.MonitorError("Missing CRNs")
    send = Mock()
    clock = [1000.0]
    monkeypatch.setattr(watch.time, "time", lambda: clock[0])
    for number in range(4):
        clock[0] += 4000
        assert watch.monitor_once(settings, path, send) == 1
        assert send.call_count == (0 if number < 2 else 1)
    state = watch.load_state(path)
    assert state.statuses == {}
    assert state.failure_notified
    fetch.side_effect = None
    fetch.return_value = observations(settings)
    clock[0] += 4000
    assert watch.monitor_once(settings, path, send) == 0
    assert send.call_count == 2
    assert "recovered" in send.call_args.args[0]
    assert watch.load_state(path).consecutive_failures == 0


def test_failed_failure_email_is_retried(settings, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    watch.save_state(path, watch.State(consecutive_failures=2))
    fetch = install_fetch(monkeypatch, None)
    fetch.side_effect = watch.MonitorError("site offline")
    send = Mock(side_effect=smtplib.SMTPException("mail offline"))
    assert watch.monitor_once(settings, path, send) == 1
    assert not watch.load_state(path).failure_notified
    assert send.call_count == 1


def test_backoff_skips_network_and_email(settings, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    watch.save_state(path, watch.State(next_check_at=2000.0))
    monkeypatch.setattr(watch.time, "time", lambda: 1000.0)
    fetch = install_fetch(monkeypatch, observations(settings))
    send = Mock()
    assert watch.monitor_once(settings, path, send) == 0
    fetch.assert_not_called()
    send.assert_not_called()


@pytest.mark.parametrize("retry_after", ["7200", "Thu, 01 Jan 1970 02:16:40 GMT"])
def test_retry_after_is_respected(monkeypatch, retry_after):
    monkeypatch.setattr(watch.time, "time", lambda: 1000.0)
    result = response("")
    result.status_code = 429
    result.headers["Retry-After"] = retry_after
    assert watch.retry_delay(requests.HTTPError(response=result), 1) == 7200


def test_retry_backoff_is_bounded():
    assert watch.retry_delay(watch.MonitorError("offline"), 1) == 300
    assert watch.retry_delay(watch.MonitorError("offline"), 100) == 3600


@pytest.mark.parametrize(
    "address",
    [
        "",
        "not-an-email",
        "a,b@example.com",
        "a@example.com,b@example.com",
        "Name <a@example.com>",
        "a@example.com\nBcc: b@example.com",
    ],
)
def test_gmail_rejects_invalid_or_multiple_addresses(monkeypatch, address):
    monkeypatch.setenv("GMAIL_USERNAME", "sender@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("ALERT_EMAIL", address)
    with pytest.raises(watch.MonitorError, match="one email address"):
        watch.gmail_sender()


def test_gmail_requires_an_app_password(monkeypatch):
    monkeypatch.setenv("GMAIL_USERNAME", "sender@gmail.com")
    monkeypatch.setenv("ALERT_EMAIL", "recipient@example.com")
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(watch.MonitorError, match="GMAIL_APP_PASSWORD"):
        watch.gmail_sender()


def test_failed_state_replace_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = watch.State(statuses={"202630:32301": "closed"})
    watch.save_state(path, original)
    monkeypatch.setattr(watch.os, "replace", Mock(side_effect=OSError("disk error")))
    with pytest.raises(OSError, match="disk error"):
        watch.save_state(path, watch.State(statuses={"202630:32301": "open"}))
    assert watch.load_state(path) == original
    assert not list(tmp_path.glob(".state-*"))


def test_gmail_uses_starttls_and_app_password(monkeypatch):
    monkeypatch.setenv("GMAIL_USERNAME", "sender@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("ALERT_EMAIL", "recipient@example.com")
    smtp = Mock()
    smtp.send_message.return_value = {}
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=smtp)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(watch.smtplib, "SMTP", factory)
    watch.gmail_sender()("Test", "Body")
    factory.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("sender@gmail.com", "abcdefghijklmnop")
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "recipient@example.com"
    assert message["Date"]
    assert message["Message-ID"]
    assert "abcdefghijklmnop" not in message.as_string()
    methods = [call[0] for call in smtp.method_calls]
    assert methods.index("starttls") < methods.index("login") < methods.index("send_message")


def test_no_credentials_are_required_for_dry_run(settings, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(watch, "load_settings", lambda _: settings)
    fetch = install_fetch(monkeypatch, observations(settings))
    sender = Mock(side_effect=AssertionError("must not load email credentials"))
    monkeypatch.setattr(watch, "gmail_sender", sender)
    assert watch.main(["--dry-run", "--state", str(path)]) == 0
    fetch.assert_called_once()
    sender.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_disabled_cli_does_not_send_email_or_write_state(tmp_path, monkeypatch):
    sender = Mock()
    fetch = install_fetch(monkeypatch, [])
    monkeypatch.setattr(watch, "gmail_sender", sender)
    assert (
        watch.main(
            ["--config", str(ROOT / "config.example.toml"), "--state", str(tmp_path / "state.json")]
        )
        == 2
    )
    sender.assert_not_called()
    fetch.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_test_email_does_not_read_config_or_contact_carleton(monkeypatch):
    send = Mock()
    monkeypatch.setattr(watch, "gmail_sender", lambda: send)
    fetch = install_fetch(monkeypatch, [])
    assert watch.main(["--test-email", "--config", "/nonexistent/config.toml"]) == 0
    send.assert_called_once()
    fetch.assert_not_called()
