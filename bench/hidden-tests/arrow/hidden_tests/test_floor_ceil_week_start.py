import arrow


def test_floor_accepts_week_start():
    # 2021-03-17 is a Wednesday. With week_start=7 (Sunday), the floor of
    # the week should land on Sunday 2021-03-14.
    a = arrow.Arrow(2021, 3, 17)
    floored = a.floor("week", week_start=7)
    assert floored == arrow.Arrow(2021, 3, 14, 0, 0, 0)


def test_ceil_accepts_week_start():
    a = arrow.Arrow(2021, 3, 17)
    ceiled = a.ceil("week", week_start=7)
    assert ceiled.date() == arrow.Arrow(2021, 3, 20).date()


def test_floor_week_start_matches_span_workaround():
    # floor('week', week_start=7) must agree with the documented
    # span('week', week_start=7)[0] workaround.
    a = arrow.Arrow(2021, 3, 17)
    assert a.floor("week", week_start=7) == a.span("week", week_start=7)[0]


def test_floor_default_week_start_unchanged():
    # Default (Monday start) behavior must still work without week_start.
    a = arrow.Arrow(2021, 3, 17)
    assert a.floor("week") == arrow.Arrow(2021, 3, 15, 0, 0, 0)


if __name__ == "__main__":
    test_floor_accepts_week_start()
    test_ceil_accepts_week_start()
    test_floor_week_start_matches_span_workaround()
    test_floor_default_week_start_unchanged()
    print("ok")
