import datetime
import pytest

from app.fines import (
    FINE_BLOCK_AT,
    LOAN_DAYS,
    MAX_ACTIVE_LOANS,
    MAX_FINE,
    MAX_RENEWALS,
    RENEWAL_DAYS,
    LoanError,
    as_date,
    calendar_days_late,
    can_borrow,
    can_renew,
    chargeable_days,
    due_date,
    fine_for,
    is_free_day,
    member_summary,
    outstanding,
    overdue,
    projected_fine,
    renewed_due_date,
)


class TestAsDate:
    def test_as_date_from_datetime(self):
        dt = datetime.datetime(2026, 10, 2, 15, 30, 0)
        assert as_date(dt) == datetime.date(2026, 10, 2)

    def test_as_date_from_date(self):
        d = datetime.date(2026, 10, 2)
        assert as_date(d) == d

    def test_as_date_from_iso_string(self):
        assert as_date("2026-10-02") == datetime.date(2026, 10, 2)
        assert as_date("2026-10-02T18:00:00Z") == datetime.date(2026, 10, 2)
        assert as_date("  2026-10-02  ") == datetime.date(2026, 10, 2)

    def test_as_date_invalid_string(self):
        with pytest.raises(LoanError, match="is not a date"):
            as_date("invalid-date")

    def test_as_date_invalid_types(self):
        with pytest.raises(LoanError, match="is not a date"):
            as_date(12345)
        with pytest.raises(LoanError, match="is not a date"):
            as_date(None)
        with pytest.raises(LoanError, match="is not a date"):
            as_date(["2026-10-02"])


class TestFreeDay:
    def test_weekend_days(self):
        # 2026-10-03 is Saturday, 2026-10-04 is Sunday
        assert is_free_day(datetime.date(2026, 10, 3)) is True
        assert is_free_day("2026-10-04") is True

    def test_weekdays(self):
        # 2026-10-02 is Friday, 2026-10-05 is Monday
        assert is_free_day(datetime.date(2026, 10, 2)) is False
        assert is_free_day("2026-10-05") is False
        assert is_free_day(datetime.date(2026, 10, 7)) is False  # Wednesday


class TestDueDate:
    def test_default_loan_days(self):
        issued = datetime.date(2026, 10, 1)
        expected = datetime.date(2026, 10, 1) + datetime.timedelta(days=LOAN_DAYS)
        assert due_date(issued) == expected

    def test_custom_days(self):
        issued = datetime.date(2026, 10, 1)
        assert due_date(issued, days=7) == datetime.date(2026, 10, 8)

    def test_invalid_days(self):
        with pytest.raises(LoanError, match="at least one day long"):
            due_date(datetime.date(2026, 10, 1), days=0)
        with pytest.raises(LoanError, match="at least one day long"):
            due_date(datetime.date(2026, 10, 1), days=-5)


class TestCalendarDaysLate:
    def test_returned_on_or_before_due_date_never_negative(self):
        due = datetime.date(2026, 10, 10)
        assert calendar_days_late(due, datetime.date(2026, 10, 5)) == 0
        assert calendar_days_late(due, datetime.date(2026, 10, 10)) == 0

    def test_returned_after_due_date(self):
        due = datetime.date(2026, 10, 10)
        assert calendar_days_late(due, datetime.date(2026, 10, 15)) == 5


class TestChargeableDays:
    def test_friday_due_tuesday_return_readme_specification(self):
        """Due Friday, returned following Tuesday with 2-day grace charges 2 days.

        Paper calculation:
        - Due date: Friday 2026-10-02
        - Returned: Tuesday 2026-10-06 (4 calendar days late)
        - Grace period: 2 days (Saturday 2026-10-03, Sunday 2026-10-04)
        - Remaining days: Monday 2026-10-05 and Tuesday 2026-10-06
        - Both are weekdays, so chargeable count is 2 (Monday and Tuesday).
        """
        due_friday = datetime.date(2026, 10, 2)
        returned_tuesday = datetime.date(2026, 10, 6)
        assert chargeable_days(due_friday, returned_tuesday) == 2

    def test_on_or_before_due_date(self):
        due = datetime.date(2026, 10, 10)
        assert chargeable_days(due, datetime.date(2026, 10, 9)) == 0
        assert chargeable_days(due, datetime.date(2026, 10, 10)) == 0

    def test_inside_grace_period_charges_nothing(self):
        # Grace = 2 days
        due = datetime.date(2026, 10, 5)  # Monday
        assert chargeable_days(due, datetime.date(2026, 10, 6)) == 0  # 1 day late <= grace
        assert chargeable_days(due, datetime.date(2026, 10, 7)) == 0  # 2 days late == grace

    def test_first_chargeable_day_is_grace_plus_one(self):
        due = datetime.date(2026, 10, 5)  # Monday
        # 3 days late: Thursday (grace ate Tue and Wed)
        assert chargeable_days(due, datetime.date(2026, 10, 8)) == 1

    def test_negative_grace_raises_error(self):
        with pytest.raises(LoanError, match="cannot be negative"):
            chargeable_days("2026-10-01", "2026-10-05", grace=-1)

    def test_skip_weekends_false_charges_weekends(self):
        """Due Wednesday, returned following Monday.
        Grace (2 days) covers Thursday and Friday.
        Remaining days: Saturday, Sunday, Monday.
        - skip_weekends=True: only Monday is charged -> 1 day
        - skip_weekends=False: Saturday, Sunday, Monday charged -> 3 days
        """
        due_wed = datetime.date(2026, 10, 7)
        returned_mon = datetime.date(2026, 10, 12)
        assert chargeable_days(due_wed, returned_mon, skip_weekends=True) == 1
        assert chargeable_days(due_wed, returned_mon, skip_weekends=False) == 3


class TestFineFor:
    def test_returned_on_time(self):
        result = fine_for("2026-10-10", "2026-10-10")
        assert result == {
            "days_late": 0,
            "chargeable_days": 0,
            "uncapped": 0.0,
            "amount": 0.0,
            "capped": False,
        }

    def test_returned_within_grace(self):
        result = fine_for("2026-10-10", "2026-10-12")
        assert result["days_late"] == 2
        assert result["chargeable_days"] == 0
        assert result["amount"] == 0.0
        assert result["capped"] is False

    def test_friday_due_tuesday_return_calculation(self):
        # 2 chargeable days * 5.0 = 10.0
        result = fine_for("2026-10-02", "2026-10-06")
        assert result["days_late"] == 4
        assert result["chargeable_days"] == 2
        assert result["uncapped"] == 10.0
        assert result["amount"] == 10.0
        assert result["capped"] is False

    def test_fine_capped_at_max_fine(self):
        # 50 chargeable days * 5.0 = 250.0 > MAX_FINE (200.0)
        due = datetime.date(2026, 1, 1)
        returned = datetime.date(2026, 4, 1)
        result = fine_for(due, returned, cap=MAX_FINE)
        assert result["uncapped"] > MAX_FINE
        assert result["amount"] == MAX_FINE
        assert result["capped"] is True

    def test_negative_rate_raises(self):
        with pytest.raises(LoanError, match="cannot be negative"):
            fine_for("2026-10-01", "2026-10-05", rate=-2.0)


class TestOutstanding:
    def test_empty_fines(self):
        assert outstanding([]) == 0.0

    def test_mixed_paid_and_unpaid(self):
        fines = [
            {"amount": 25.50, "paid": False},
            {"amount": 10.00, "paid": True},
            {"amount": "14.50", "paid": False},
        ]
        assert outstanding(fines) == 40.0

    def test_all_paid(self):
        fines = [
            {"amount": 50.0, "paid": True},
            {"amount": 30.0, "paid": True},
        ]
        assert outstanding(fines) == 0.0


class TestOverdue:
    def test_overdue_filtering(self):
        today = datetime.date(2026, 10, 15)
        loans = [
            {"id": 1, "due_on": "2026-10-10", "returned_on": None},       # overdue
            {"id": 2, "due_on": "2026-10-10", "returned_on": "2026-10-12"}, # returned
            {"id": 3, "due_on": "2026-10-15", "returned_on": None},       # due today (not overdue)
            {"id": 4, "due_on": "2026-10-20", "returned_on": None},       # future
        ]
        res = overdue(loans, today)
        assert len(res) == 1
        assert res[0]["id"] == 1


class TestCanBorrow:
    def test_allowed(self):
        ok, reason = can_borrow(active_loans=1, unpaid_total=0.0, copies_available=3)
        assert ok is True
        assert reason == "ok"

    def test_reference_only(self):
        ok, reason = can_borrow(active_loans=0, unpaid_total=0.0, copies_available=5, reference_only=True)
        assert ok is False
        assert reason == "reference_only"

    def test_no_copies_available(self):
        ok, reason = can_borrow(active_loans=0, unpaid_total=0.0, copies_available=0)
        assert ok is False
        assert reason == "no_copies_available"

    def test_loan_limit_reached(self):
        ok, reason = can_borrow(active_loans=MAX_ACTIVE_LOANS, unpaid_total=0.0, copies_available=2)
        assert ok is False
        assert reason == "loan_limit_reached"

    def test_unpaid_fines_blocked(self):
        ok, reason = can_borrow(active_loans=0, unpaid_total=FINE_BLOCK_AT, copies_available=2)
        assert ok is False
        assert reason == "unpaid_fines"

    def test_has_overdue_book(self):
        ok, reason = can_borrow(active_loans=1, unpaid_total=0.0, copies_available=2, has_overdue=True)
        assert ok is False
        assert reason == "overdue_book_held"


class TestCanRenew:
    def test_eligible_renewal(self):
        ok, reason = can_renew(times_renewed=0, due_on="2026-10-20", today="2026-10-15")
        assert ok is True
        assert reason == "ok"

    def test_reserved_by_others(self):
        ok, reason = can_renew(times_renewed=0, due_on="2026-10-20", today="2026-10-15", reserved_by_others=True)
        assert ok is False
        assert reason == "reserved_by_someone_else"

    def test_renewal_limit_reached(self):
        ok, reason = can_renew(times_renewed=MAX_RENEWALS, due_on="2026-10-20", today="2026-10-15")
        assert ok is False
        assert reason == "renewal_limit_reached"

    def test_unpaid_fines_block_renewal(self):
        ok, reason = can_renew(times_renewed=0, due_on="2026-10-20", today="2026-10-15", unpaid_total=FINE_BLOCK_AT)
        assert ok is False
        assert reason == "unpaid_fines"

    def test_already_overdue_cannot_renew(self):
        ok, reason = can_renew(times_renewed=0, due_on="2026-10-10", today="2026-10-15")
        assert ok is False
        assert reason == "already_overdue"


class TestRenewedDueDate:
    def test_renew_before_due_date_extends_from_due_date(self):
        # Cannot bank unused days by renewing early
        due_on = datetime.date(2026, 10, 20)
        today = datetime.date(2026, 10, 15)
        expected = due_on + datetime.timedelta(days=RENEWAL_DAYS)
        assert renewed_due_date(due_on, today) == expected

    def test_renew_on_due_date(self):
        due_on = datetime.date(2026, 10, 20)
        today = datetime.date(2026, 10, 20)
        expected = due_on + datetime.timedelta(days=RENEWAL_DAYS)
        assert renewed_due_date(due_on, today) == expected

    def test_renew_past_due_date_extends_from_today(self):
        due_on = datetime.date(2026, 10, 10)
        today = datetime.date(2026, 10, 15)
        expected = today + datetime.timedelta(days=RENEWAL_DAYS)
        assert renewed_due_date(due_on, today) == expected


class TestMemberSummary:
    def test_summary_calculation(self):
        today = datetime.date(2026, 10, 15)
        loans = [
            {"id": 1, "due_on": "2026-10-20", "returned_on": None},
            {"id": 2, "due_on": "2026-10-10", "returned_on": None},  # overdue
            {"id": 3, "due_on": "2026-10-05", "returned_on": "2026-10-08"},
        ]
        fines = [
            {"id": 1, "amount": 20.0, "paid": False},
            {"id": 2, "amount": 10.0, "paid": True},
        ]
        summary = member_summary(loans, fines, today)
        assert summary["active_loans"] == 2
        assert summary["overdue_loans"] == 1
        assert summary["slots_left"] == MAX_ACTIVE_LOANS - 2
        assert summary["unpaid_fines"] == 20.0
        assert summary["blocked"] is True  # due to overdue loan

    def test_summary_blocked_by_fines(self):
        today = datetime.date(2026, 10, 15)
        loans = [{"id": 1, "due_on": "2026-10-20", "returned_on": None}]
        fines = [{"id": 1, "amount": 120.0, "paid": False}]
        summary = member_summary(loans, fines, today)
        assert summary["blocked"] is True

    def test_summary_slots_left_never_negative(self):
        today = datetime.date(2026, 10, 15)
        loans = [{"id": i, "due_on": "2026-10-20", "returned_on": None} for i in range(6)]
        summary = member_summary(loans, [], today, max_loans=4)
        assert summary["slots_left"] == 0


class TestProjectedFine:
    def test_projected_fine_not_overdue(self):
        res = projected_fine("2026-10-20", "2026-10-15")
        assert res == {
            "days_late": 0,
            "chargeable_days": 0,
            "uncapped": 0.0,
            "amount": 0.0,
            "capped": False,
        }

    def test_projected_fine_overdue(self):
        # Due Friday 2026-10-02, today Tuesday 2026-10-06
        res = projected_fine("2026-10-02", "2026-10-06")
        assert res["chargeable_days"] == 2
        assert res["amount"] == 10.0
