"""
Business-day arithmetic for settlement timing.

T+2 does not mean two calendar days. It means two clearing days, skipping
weekends and bank holidays. A reconciler that treats it as calendar days will
flag every post-weekend settlement as late, generating a queue of false
exceptions that trains the operator to ignore the queue -- which is the worst
possible failure mode for this class of tool.

The holiday list is illustrative and national-only. Real Indian bank holidays
vary by state and by clearing system (NEFT/RTGS/cheque calendars differ). That
gap is recorded in the project's threats-to-validity section rather than
papered over.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable


class BusinessCalendar:
    def __init__(self, holidays: Iterable[date] | None = None) -> None:
        self.holidays: frozenset[date] = frozenset(holidays or ())

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def add_business_days(self, start: date, n: int) -> date:
        """Advance `n` clearing days from `start`.

        n=0 rolls forward to the next business day if `start` is not one.
        """
        d = start
        while not self.is_business_day(d):
            d += timedelta(days=1)
        remaining = n
        while remaining > 0:
            d += timedelta(days=1)
            if self.is_business_day(d):
                remaining -= 1
        return d

    def next_business_day(self, d: date) -> date:
        return self.add_business_days(d + timedelta(days=1), 0)

    def business_days_between(self, a: date, b: date) -> int:
        """Signed count of clearing days from a to b."""
        if b < a:
            return -self.business_days_between(b, a)
        n, d = 0, a
        while d < b:
            d += timedelta(days=1)
            if self.is_business_day(d):
                n += 1
        return n
