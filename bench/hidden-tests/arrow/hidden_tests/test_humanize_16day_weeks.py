import arrow


def test_16_day_same_month_difference_is_weeks_not_a_month():
    # Reported case: "today" Jan 9 2026, an event on Jan 25 (16 days later)
    # showed as "in a month" even though it is clearly closer to two weeks
    # away and does not cross a calendar month boundary before it.
    today = arrow.Arrow(2026, 1, 9)
    sixteen_days_later = arrow.Arrow(2026, 1, 25)
    assert today.humanize(sixteen_days_later) == "in 2 weeks"
    assert sixteen_days_later.humanize(today) == "2 weeks ago"


def test_15_day_same_month_difference_is_also_weeks():
    today = arrow.Arrow(2026, 1, 9)
    fifteen_days_later = arrow.Arrow(2026, 1, 24)
    assert today.humanize(fifteen_days_later) == "in 2 weeks"
    assert fifteen_days_later.humanize(today) == "2 weeks ago"


def test_actual_calendar_month_boundary_still_reports_a_month():
    # A genuine calendar-month difference (Feb 8 -> Mar 8, 28 days, crosses
    # a real month boundary) must still humanize as "a month". Fixing the
    # 16-day mislabeling must not break this case.
    earlier = arrow.Arrow(2026, 2, 8)
    later = arrow.Arrow(2026, 3, 8)
    assert earlier.humanize(later) == "a month ago"


if __name__ == "__main__":
    test_16_day_same_month_difference_is_weeks_not_a_month()
    test_15_day_same_month_difference_is_also_weeks()
    test_actual_calendar_month_boundary_still_reports_a_month()
    print("ok")
