import arrow


def test_humanize_uses_real_calendar_months_not_naive_month_count():
    # Jan 31 -> May 1 is 90 raw days, but only 3 full calendar months have
    # elapsed (Jan 31 -> Apr 30 = 3 months, plus 1 leftover day). Naively
    # subtracting (year*12 + month) integers overcounts this as 4 months
    # because it ignores day-of-month alignment. humanize() should report
    # the correct calendar-aware value.
    earlier = arrow.Arrow(2000, 1, 31)
    later = arrow.Arrow(2000, 5, 1)
    assert earlier.humanize(later) == "3 months ago"
    assert later.humanize(earlier) == "in 3 months"


def test_humanize_month_count_not_inflated_across_short_months():
    # Another case where the naive (year*12+month) subtraction overcounts:
    # Nov 30 -> Feb 1 is really 2 months and 2 days (Nov 30 -> Jan 30 = 2
    # months, + 2 days to Feb 1), not 3 months.
    earlier = arrow.Arrow(2000, 11, 30)
    later = arrow.Arrow(2001, 2, 1)
    assert earlier.humanize(later) == "2 months ago"
    assert later.humanize(earlier) == "in 2 months"


if __name__ == "__main__":
    test_humanize_uses_real_calendar_months_not_naive_month_count()
    test_humanize_month_count_not_inflated_across_short_months()
    print("ok")
