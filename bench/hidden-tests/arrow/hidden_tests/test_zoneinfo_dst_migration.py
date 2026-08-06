import arrow


def test_imaginary_local_time_resolves_to_pre_transition_offset():
    # America/Anchorage spring-forward on 2016-03-13: 02:00 -> 03:00 local,
    # so 02:01 never actually occurred that day (it's an "imaginary" time).
    #
    # Constructing an Arrow directly at that imaginary wall-clock time must
    # resolve using the UTC offset that was in effect immediately *before*
    # the gap (standard "fold=0" gap semantics), matching the moment right
    # before the transition, not the moment after it.
    before = arrow.Arrow(2016, 3, 13, 1, 59, tzinfo="America/Anchorage")
    imaginary = arrow.Arrow(2016, 3, 13, 2, 1, tzinfo="America/Anchorage")

    assert imaginary.utcoffset() == before.utcoffset()


def test_post_transition_time_has_different_offset_than_before():
    before = arrow.Arrow(2016, 3, 13, 1, 59, tzinfo="America/Anchorage")
    after = arrow.Arrow(2016, 3, 13, 3, 1, tzinfo="America/Anchorage")

    assert before.utcoffset() != after.utcoffset()


def test_timezone_string_parsing_still_works_for_common_names():
    # Basic regression guard: common IANA zone names must still resolve.
    a = arrow.now("America/New_York")
    assert a.tzinfo is not None
    b = arrow.now("Europe/London")
    assert b.tzinfo is not None


if __name__ == "__main__":
    test_imaginary_local_time_resolves_to_pre_transition_offset()
    test_post_transition_time_has_different_offset_than_before()
    test_timezone_string_parsing_still_works_for_common_names()
    print("ok")
