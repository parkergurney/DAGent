import arrow


def test_format_rfc3339_strict_constant_exists():
    assert hasattr(arrow, "FORMAT_RFC3339_STRICT")


def test_format_rfc3339_strict_uses_t_separator():
    dt = arrow.Arrow(2021, 3, 15, 10, 30, 45)
    strict = dt.format(arrow.FORMAT_RFC3339_STRICT)
    assert strict == "2021-03-15T10:30:45+00:00"


def test_format_rfc3339_regular_still_uses_space():
    # The existing FORMAT_RFC3339 (space separator) must be unaffected.
    dt = arrow.Arrow(2021, 3, 15, 10, 30, 45)
    assert dt.format(arrow.FORMAT_RFC3339) == "2021-03-15 10:30:45+00:00"


if __name__ == "__main__":
    test_format_rfc3339_strict_constant_exists()
    test_format_rfc3339_strict_uses_t_separator()
    test_format_rfc3339_regular_still_uses_space()
    print("ok")
