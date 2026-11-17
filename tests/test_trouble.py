"""The trouble ladder: what changes as pages keep failing, and when the crawl stops."""

from xfcrawl.trouble import LADDER, LONGEST_PAUSE, TOP_STEP_TRIES, Step, Trouble, is_trouble


def pause_after(trouble: Trouble, reason: str, retry_after: float | None = None) -> float:
    step = trouble.failed(reason, now=0, retry_after=retry_after)
    assert step is not None
    return step.pause


def climb(trouble: Trouble, pages: int, reason: str = "refused") -> list[Step | None]:
    return [trouble.failed(reason, now=60.0 * n) for n in range(pages)]


def test_the_ladder_as_agreed():
    assert [(s.pause, s.new_server) for s in LADDER] == [
        (30, False), (60, True), (120, True), (300, True), (600, True), (900, True)]
    assert LONGEST_PAUSE == 900 and TOP_STEP_TRIES == 3


def test_each_page_failing_in_a_row_climbs_a_step_and_the_top_is_tried_three_times():
    steps = climb(Trouble(), 9)
    assert [s.pause for s in steps[:8] if s] == [30, 60, 120, 300, 600, 900, 900, 900]
    assert steps[8] is None                                      # the ninth: stop


def test_what_was_tried_is_counted_for_the_stop_message():
    trouble = Trouble()
    climb(trouble, 9, "refused")
    assert trouble.servers == 7
    message = trouble.why_stop(now=60.0 * 8 + 3600)
    assert message.startswith("no page has come through for 68 min, 9 pages in a row, despite pauses and "
                              "7 new servers and visitors; the answers: refused x9.")


def test_one_good_page_goes_back_to_the_bottom():
    trouble = Trouble()
    climb(trouble, 5)
    trouble.recovered()
    assert (trouble.in_a_row, trouble.servers) == (0, 0)
    assert pause_after(trouble, "refused") == 30


def test_a_site_asking_for_longer_gets_it_up_to_the_longest_pause():
    assert pause_after(Trouble(), "http-429", retry_after=200) == 200
    assert pause_after(Trouble(), "http-429", retry_after=7200) == 900
    assert pause_after(Trouble(), "http-429", retry_after=5) == 30


def test_the_longest_pause_caps_every_step():
    assert {s.pause for s in climb(Trouble(longest_pause=45), 8) if s} == {30, 45}
    assert {s.pause for s in climb(Trouble(longest_pause=0), 8) if s} == {0}


def test_normal_answers_are_not_trouble():
    for reason in ("redirect", "http-404", "http-410", "http-403", "robots"):
        assert not is_trouble(reason)
    for reason in ("refused", "http-429", "http-418", "http-503", "timeout", "network", "error-page"):
        assert is_trouble(reason)
