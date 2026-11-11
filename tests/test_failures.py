from datetime import datetime, timedelta, timezone

from xfcrawl.failures import Failure, RetryPolicy, parse_failures

T0 = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)


def failures(reason, count):
    return [Failure(T0, reason)] * count


def test_wait_doubles_from_one_hour_for_temporary_problems():
    policy = RetryPolicy()
    assert [policy.wait_after(failures("http-503", n)) for n in (1, 2, 3)] == [
        timedelta(hours=1), timedelta(hours=2), timedelta(hours=4)]


def test_wait_doubles_from_one_day_for_pages_that_look_gone():
    policy = RetryPolicy()
    assert policy.wait_after(failures("http-404", 1)) == timedelta(days=1)
    assert policy.wait_after(failures("redirect", 2)) == timedelta(days=2)


def test_wait_is_capped_and_never_overflows():
    assert RetryPolicy().wait_after(failures("http-503", 500)) == timedelta(days=30)


def test_next_try_counts_from_the_last_failure():
    history = [Failure(T0, "http-503"), Failure(T0 + timedelta(hours=5), "timeout")]
    assert RetryPolicy().next_try(history) == T0 + timedelta(hours=7)


def test_failures_round_trip_as_json_lines():
    written = [Failure(T0, "redirect", "http://x/threads/a.1/page-9", "http://x/threads/a.1/page-2"),
               Failure(T0, "http-404", "http://x/threads/b.2/")]
    assert parse_failures("".join(f.to_json_line() for f in written)) == written


def test_a_line_cut_short_by_a_crash_is_skipped():
    text = Failure(T0, "http-503").to_json_line() + '{"time": "2026-11-09T1'
    assert parse_failures(text) == [Failure(T0, "http-503")]
