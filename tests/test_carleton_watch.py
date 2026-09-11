import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import carleton_watch as watch

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings():
    return replace(
        watch.load_settings(ROOT / "config.example.toml"), automated_access_permitted=True
    )


@pytest.fixture
def postmark(monkeypatch):
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "example-server-token")
    monkeypatch.setenv("POSTMARK_FROM_EMAIL", "alerts@example.com")
    monkeypatch.setenv("ALERT_EMAIL", "recipient@gmail.com")
    post = Mock(
        return_value=http_response(json.dumps({"ErrorCode": 0, "MessageID": "test-message-id"}))
    )
    monkeypatch.setattr(watch.requests, "post", post)
    return post


def http_response(body, status=200):
    response = requests.Response()
    response.status_code = status
    response.url = watch.PUBLIC_URL
    response.headers["Content-Type"] = "text/html; charset=UTF-8"
    response._content = body.encode()
    return response


def result_html(settings):
    header = "<tr><td>Status</td><td>CRN</td><td>Subject</td><td>Section</td><td>Title</td></tr>"
    rows = "".join(
        f'<tr><td><font color="red">Full, No Waitlist</font></td><td>{course.crn}</td>'
        f"<td>{course.course}</td><td>{course.section}</td><td>Course title</td></tr>"
        for course in settings.courses
    )
    return f'<input name="term_code" value="202630"><table>{header}{rows}</table>'


def open_courses(settings):
    return [watch.Observation(course, "open", "Course title") for course in settings.courses]


def test_watchlist_and_parser(settings):
    assert settings.term_code == "202630"
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
    html = result_html(settings)
    results = watch.parse_results(html, settings)
    assert len(results) == 8
    assert results[-1].course.section == "B"
    assert all(item.status == "full, no waitlist" for item in results)
    with pytest.raises(watch.MonitorError, match="Missing CRNs"):
        watch.parse_results(html.replace("32301", "99999"), settings)


def test_public_search_uses_anonymous_forms(settings, monkeypatch):
    term_form = """<form action="bwysched.p_search_fields" method="post">
      <input name="wsea_code" value="EXT"><input name="session_id" value="123456">
      <select name="term_code"><option value="202630">Fall 2026</option></select></form>"""
    search_form = """<form action="bwysched.p_course_search" method="post">
      <input name="wsea_code" value="EXT"><input name="session_id" value="123456">
      <input name="term_code" value="202630"><input name="sel_subj" value="dummy">
      <select name="sel_subj" multiple><option value="" selected>All</option></select>
      <input name="sel_day" value="dummy"><input type="checkbox" name="sel_day" value="m" checked>
      <input type="submit" name="time_table" value="View Worksheet"></form>"""
    session = Mock()
    session.get.return_value = http_response(term_form)
    session.request.side_effect = [http_response(search_form), http_response(result_html(settings))]
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=session)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(watch.requests, "Session", factory)
    assert len(watch.fetch_courses(settings)) == 8
    assert session.get.call_count == 1
    assert session.request.call_count == 2
    payload = session.request.call_args.kwargs["data"]
    assert ("session_id", "123456") in payload
    assert [value for name, value in payload if name == "sel_subj"] == [
        "dummy",
        "HIST",
        "PHIL",
        "RELI",
    ]
    assert [value for name, value in payload if name == "sel_day"] == ["dummy", "m"]
    assert not any(name == "time_table" for name, _ in payload)


def test_alerts_distinguish_waitlist_and_seat_changes(settings):
    course = settings.courses[0]
    key = watch.status_key(settings, course)
    state = watch.State()
    waiting = watch.Observation(course, "waitlist open", "Course title")
    opened = watch.Observation(course, "open", "Course title")
    assert watch.new_alerts(settings, [waiting], state) == [waiting]
    assert "Waitlist available" in watch.alert_message(settings, [waiting], False)[0]
    state.statuses[key] = "waitlist open"
    assert watch.new_alerts(settings, [waiting], state) == []
    assert watch.new_alerts(settings, [opened], state) == [opened]
    assert "Seat available" in watch.alert_message(settings, [opened], False)[0]
    state.statuses[key] = "open"
    assert watch.new_alerts(settings, [opened], state) == []


def test_state_prevents_duplicate_alerts_after_restart(settings, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(watch, "fetch_courses", Mock(return_value=open_courses(settings)))
    send = Mock()
    assert watch.monitor_once(settings, path, send) == 0
    assert watch.monitor_once(settings, path, send) == 0
    send.assert_called_once()
    assert len(watch.load_state(path).statuses) == 8
    assert path.stat().st_mode & 0o777 == 0o600


def test_postmark_api_submission_and_rejection(postmark):
    send = watch.postmark_sender()
    send("Test", "Message body")
    args = postmark.call_args
    assert args.args == (watch.POSTMARK_URL,)
    assert args.kwargs["headers"]["X-Postmark-Server-Token"] == "example-server-token"
    assert args.kwargs["allow_redirects"] is False
    assert args.kwargs["timeout"] == (10, 30)
    assert args.kwargs["json"] == {
        "From": "alerts@example.com",
        "To": "recipient@gmail.com",
        "Subject": "Test",
        "TextBody": "Message body",
        "MessageStream": "outbound",
        "TrackOpens": False,
        "TrackLinks": "None",
    }
    postmark.return_value = http_response(
        json.dumps({"ErrorCode": 400, "Message": "example-server-token"}), 422
    )
    with pytest.raises(watch.PostmarkError, match="ErrorCode 400") as error:
        send("Test", "Message body")
    assert "example-server-token" not in str(error.value)
    postmark.return_value = http_response(json.dumps({"ErrorCode": 0}))
    with pytest.raises(watch.PostmarkError, match="MessageID"):
        send("Test", "Message body")


def test_postmark_failure_retains_status_and_respects_retry_after(
    settings, tmp_path, monkeypatch, postmark
):
    path = tmp_path / "state.json"
    initial = watch.State(
        consecutive_failures=2,
        statuses={watch.status_key(settings, course): "closed" for course in settings.courses},
    )
    watch.save_state(path, initial)
    monkeypatch.setattr(watch.time, "time", lambda: 1000.0)
    monkeypatch.setattr(watch, "fetch_courses", Mock(return_value=open_courses(settings)))
    postmark.return_value = http_response(json.dumps({"ErrorCode": 429}), 429)
    postmark.return_value.headers["Retry-After"] = "7200"
    assert watch.monitor_once(settings, path, watch.postmark_sender()) == 1
    state = watch.load_state(path)
    assert state.statuses == initial.statuses
    assert state.next_check_at == 8200.0
    # Do not make a second email request to report the Postmark rate limit.
    postmark.assert_called_once()
    assert watch.monitor_once(settings, path, watch.postmark_sender()) == 0
    postmark.assert_called_once()
    monkeypatch.setattr(watch.time, "time", lambda: 8201.0)
    postmark.return_value = http_response(json.dumps({"ErrorCode": 0, "MessageID": "retry-id"}))
    assert watch.monitor_once(settings, path, watch.postmark_sender()) == 0
    assert set(watch.load_state(path).statuses.values()) == {"open"}


def test_test_email_does_not_contact_carleton(monkeypatch, postmark):
    fetch = Mock(side_effect=AssertionError("must not contact Carleton"))
    monkeypatch.setattr(watch, "fetch_courses", fetch)
    assert watch.main(["--test-email", "--config", "/nonexistent/config.toml"]) == 0
    postmark.assert_called_once()
    fetch.assert_not_called()
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "POSTMARK_API_TEST")
    with pytest.raises(watch.MonitorError, match="does not deliver"):
        watch.postmark_sender()


def test_disabled_access_and_dry_run_do_not_send_email(settings, tmp_path, monkeypatch):
    sender = Mock(side_effect=AssertionError("must not load email credentials"))
    fetch = Mock(return_value=open_courses(settings))
    monkeypatch.setattr(watch, "postmark_sender", sender)
    monkeypatch.setattr(watch, "fetch_courses", fetch)
    path = tmp_path / "state.json"
    assert watch.main(["--config", str(ROOT / "config.example.toml"), "--state", str(path)]) == 2
    fetch.assert_not_called()
    monkeypatch.setattr(watch, "load_settings", lambda _: settings)
    assert watch.main(["--dry-run", "--state", str(path)]) == 0
    fetch.assert_called_once()
    sender.assert_not_called()
    assert not list(tmp_path.iterdir())
