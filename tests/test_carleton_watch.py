from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

import carleton_watch as watch

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_COURSES = {
    course.crn: course
    for course in (
        watch.Course("HIST 2003", "A", "32301"),
        watch.Course("HIST 2401", "A", "32309"),
        watch.Course("HIST 2710", "A", "32315"),
        watch.Course("HIST 3909", "A", "32338"),
        watch.Course("PHIL 2301", "A", "33780"),
        watch.Course("PHIL 2901", "A", "33788"),
        watch.Course("RELI 2110", "A", "34130"),
        watch.Course("RELI 3101", "B", "34139"),
    )
}


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("CRNS", ",".join(SAMPLE_COURSES))
    return watch.load_settings(ROOT / "config.example.toml")


@pytest.fixture
def ntfy(monkeypatch):
    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.sh")
    monkeypatch.setenv("NTFY_TOPIC", "test-course-topic")
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    post = Mock(return_value=http_response('{"event":"message"}'))
    monkeypatch.setattr(watch.requests, "post", post)
    return post


def http_response(body, status=200):
    response = requests.Response()
    response.status_code = status
    response.url = watch.PUBLIC_URL
    response.headers["Content-Type"] = "text/html; charset=UTF-8"
    response._content = body.encode()
    return response


def result_html(crns):
    header = "<tr><td>Status</td><td>CRN</td><td>Subject</td><td>Section</td><td>Title</td></tr>"
    rows = "".join(
        f'<tr><td><font color="red">Full, No Waitlist</font></td><td>{course.crn}</td>'
        f"<td>{course.course}</td><td>{course.section}</td><td>Course title</td></tr>"
        for course in (SAMPLE_COURSES[crn] for crn in crns)
    )
    return f'<input name="term_code" value="202630"><table>{header}{rows}</table>'


def open_courses(settings):
    return [watch.Observation(SAMPLE_COURSES[crn], "open", "Course title") for crn in settings.crns]


def test_environment_watchlist_and_parser(settings, monkeypatch):
    assert settings.term_code == "202630"
    assert list(settings.crns) == [
        "32301",
        "32309",
        "32315",
        "32338",
        "33780",
        "33788",
        "34130",
        "34139",
    ]
    html = result_html(settings.crns)
    results = watch.parse_results(html, settings.term_code, settings.crns)
    assert len(results) == 8
    assert results[-1].course.course == "RELI 3101"
    assert results[-1].course.section == "B"
    assert all(item.status == "full, no waitlist" for item in results)
    with pytest.raises(watch.MonitorError, match="Missing CRNs"):
        watch.parse_results(html.replace("32301", "99999"), settings.term_code, settings.crns)

    monkeypatch.setenv("CRNS", "99999, 34139")
    updated = watch.load_settings(ROOT / "config.example.toml")
    assert updated.crns == ("99999", "34139")
    results = watch.parse_results(html.replace("32301", "99999"), updated.term_code, updated.crns)
    assert [item.course.crn for item in results] == ["99999", "34139"]


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
    session.request.side_effect = [http_response(search_form)] + [
        http_response(result_html((crn,))) for crn in settings.crns
    ]
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=session)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(watch.requests, "Session", factory)
    assert len(watch.fetch_courses(settings)) == 8
    assert session.get.call_count == 1
    assert session.request.call_count == 1 + len(settings.crns)
    searches = session.request.call_args_list[1:]
    assert [dict(call.kwargs["data"])["sel_crn"] for call in searches] == list(settings.crns)
    payload = searches[-1].kwargs["data"]
    assert ("session_id", "123456") in payload
    assert [value for name, value in payload if name == "sel_subj"] == ["dummy", ""]
    assert [value for name, value in payload if name == "sel_day"] == ["dummy", "m"]
    assert not any(name == "time_table" for name, _ in payload)


def test_alerts_distinguish_waitlist_and_seat_changes(settings):
    course = SAMPLE_COURSES[settings.crns[0]]
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


def test_daily_reports_and_alert_deduplication(settings, tmp_path, monkeypatch, capsys):
    path = tmp_path / "state.json"
    clock = [1000.0]
    monkeypatch.setattr(watch.time, "time", lambda: clock[0])
    fetch = Mock(return_value=open_courses(settings))
    monkeypatch.setattr(watch, "fetch_courses", fetch)
    send = Mock()
    assert watch.monitor_once(settings, path, send) == 0
    assert capsys.readouterr().out.strip() in send.call_args.args[1]
    assert watch.load_state(path).last_daily_report_at == clock[0]
    assert watch.monitor_once(settings, path, send) == 0
    send.assert_called_once()
    assert len(watch.load_state(path).statuses) == 8
    assert path.stat().st_mode & 0o777 == 0o600

    clock[0] += 86400
    assert watch.monitor_once(settings, path, send) == 0
    assert send.call_count == 2
    assert send.call_args.args[0] == "[Carleton] Daily status"
    assert "Checked 8 courses; 0 new availability alerts." in send.call_args.args[1]

    clock[0] += 86400
    fetch.side_effect = watch.MonitorError("site unavailable")
    assert watch.monitor_once(settings, path, send) == 1
    assert send.call_count == 3
    assert send.call_args.args[0] == "[Carleton] Daily status: check failed"
    assert "site unavailable" in send.call_args.args[1]
    assert watch.load_state(path).last_daily_report_at == clock[0]


def test_ntfy_api_submission_and_rejection(ntfy):
    send = watch.ntfy_sender()
    send("Test", "Message body")
    args = ntfy.call_args
    assert args.args == ("https://ntfy.sh/",)
    assert args.kwargs["headers"] == {}
    assert args.kwargs["allow_redirects"] is False
    assert args.kwargs["timeout"] == (10, 30)
    assert args.kwargs["json"] == {
        "topic": "test-course-topic",
        "title": "Test",
        "message": "Message body",
        "priority": 4,
        "click": watch.CENTRAL_URL,
    }
    ntfy.return_value = http_response("topic is reserved", 403)
    with pytest.raises(watch.NotificationError, match="topic is reserved"):
        send("Test", "Message body")


def test_ntfy_failure_retains_status_and_respects_retry_after(
    settings, tmp_path, monkeypatch, ntfy
):
    path = tmp_path / "state.json"
    initial = watch.State(
        consecutive_failures=2,
        statuses={f"{settings.term_code}:{crn}": "closed" for crn in settings.crns},
    )
    watch.save_state(path, initial)
    monkeypatch.setattr(watch.time, "time", lambda: 1000.0)
    monkeypatch.setattr(watch, "fetch_courses", Mock(return_value=open_courses(settings)))
    ntfy.return_value = http_response("rate limit exceeded", 429)
    ntfy.return_value.headers["Retry-After"] = "7200"
    assert watch.monitor_once(settings, path, watch.ntfy_sender()) == 1
    state = watch.load_state(path)
    assert state.statuses == initial.statuses
    assert state.next_check_at == 8200.0
    assert state.last_daily_report_at is None
    # Do not make a second notification request to report the ntfy rate limit.
    ntfy.assert_called_once()
    assert watch.monitor_once(settings, path, watch.ntfy_sender()) == 0
    ntfy.assert_called_once()
    monkeypatch.setattr(watch.time, "time", lambda: 8201.0)
    ntfy.return_value = http_response('{"event":"message"}')
    assert watch.monitor_once(settings, path, watch.ntfy_sender()) == 0
    assert set(watch.load_state(path).statuses.values()) == {"open"}
    assert watch.load_state(path).last_daily_report_at == 8201.0


def test_test_notification_does_not_contact_carleton(monkeypatch, ntfy):
    fetch = Mock(side_effect=AssertionError("must not contact Carleton"))
    monkeypatch.setattr(watch, "fetch_courses", fetch)
    assert watch.main(["--test-notification", "--config", "/nonexistent/config.toml"]) == 0
    ntfy.assert_called_once()
    fetch.assert_not_called()
    monkeypatch.setenv("NTFY_TOKEN", "example-access-token")
    watch.ntfy_sender()("Test", "Message body")
    assert ntfy.call_args.kwargs["headers"]["Authorization"] == "Bearer example-access-token"


def test_dry_run_does_not_send_notifications_or_write_state(settings, tmp_path, monkeypatch):
    sender = Mock(side_effect=AssertionError("must not load notification settings"))
    fetch = Mock(return_value=open_courses(settings))
    monkeypatch.setattr(watch, "ntfy_sender", sender)
    monkeypatch.setattr(watch, "fetch_courses", fetch)
    path = tmp_path / "state.json"
    assert (
        watch.main(
            ["--dry-run", "--config", str(ROOT / "config.example.toml"), "--state", str(path)]
        )
        == 0
    )
    fetch.assert_called_once()
    sender.assert_not_called()
    assert not list(tmp_path.iterdir())
