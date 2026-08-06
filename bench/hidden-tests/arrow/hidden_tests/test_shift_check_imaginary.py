import arrow


def test_shift_accepts_check_imaginary_kwarg():
    # America/New_York spring-forward on 2018-03-11: 02:00 -> 03:00.
    # Shifting into the gap should not raise just because check_imaginary
    # was passed.
    start = arrow.Arrow(2018, 3, 11, 1, 30, tzinfo="America/New_York")
    shifted = start.shift(hours=1, check_imaginary=False)
    assert shifted is not None


def test_check_imaginary_false_keeps_raw_imaginary_time():
    start = arrow.Arrow(2018, 3, 11, 1, 30, tzinfo="America/New_York")
    raw = start.shift(hours=1, check_imaginary=False)
    # The naive wall-clock result of adding 1 hour is 02:30, which never
    # occurred in America/New_York on this date. With the check disabled,
    # arrow should leave it unresolved rather than normalizing it.
    assert raw.hour == 2
    assert raw.minute == 30


def test_check_imaginary_true_is_the_default_and_resolves_gap():
    start = arrow.Arrow(2018, 3, 11, 1, 30, tzinfo="America/New_York")
    default_result = start.shift(hours=1)
    explicit_true = start.shift(hours=1, check_imaginary=True)
    # Default behavior (and explicit True) should resolve the imaginary
    # 02:30 forward past the DST gap to 03:30.
    assert default_result.hour == 3
    assert default_result.minute == 30
    assert explicit_true.hour == 3
    assert explicit_true.minute == 30


if __name__ == "__main__":
    test_shift_accepts_check_imaginary_kwarg()
    test_check_imaginary_false_keeps_raw_imaginary_time()
    test_check_imaginary_true_is_the_default_and_resolves_gap()
    print("ok")
