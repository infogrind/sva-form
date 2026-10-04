"""Payroll calculations for the SVA Zürich hourly wage form (vereinfachtes Abrechnungsverfahren)."""

import calendar
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS_DE_PLURAL = [
    "Montage",
    "Dienstage",
    "Mittwoche",
    "Donnerstage",
    "Freitage",
    "Samstage",
    "Sonntage",
]

MONTHS_DE = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Rates:
    """Rates in percent (and the BVG threshold in CHF); defaults are valid for 2025/2026."""

    vacation: Decimal = Decimal("8.33")  # Ferienzuschlag, 4 weeks vacation
    ahv: Decimal = Decimal("5.3")  # AHV/IV/EO, employee share
    alv: Decimal = Decimal("1.1")  # ALV, employee share
    tax: Decimal = Decimal("5")  # Steuerabzug (Quellensteuer im vereinfachten Verfahren)
    employer_ahv: Decimal = Decimal("10.6")  # AHV/IV/EO billed: employer + employee share
    employer_alv: Decimal = Decimal("2.2")  # ALV billed: employer + employee share
    fak: Decimal = Decimal("1.025")  # Familienausgleichskasse, employer only
    admin: Decimal = Decimal("5")  # Verwaltungskosten, % of the AHV/IV/EO contributions
    bvg_threshold: Decimal = Decimal("22680")  # BVG Eintrittsschwelle, CHF per year

    def with_overrides(self, overrides: dict) -> "Rates":
        unknown = set(overrides) - set(self.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown rate(s) in config: {', '.join(sorted(unknown))}")
        return replace(self, **{k: Decimal(str(v)) for k, v in overrides.items()})


# Rates verified against SVA Zürich for each year (the 2025 bill matched them).
# When rates change, add the new year here.
KNOWN_RATES: dict[int, Rates] = {2025: Rates(), 2026: Rates()}


def known_rates(year: int) -> tuple[Rates, int]:
    """Verified rates for `year`, or those of the closest year that has some."""
    if year in KNOWN_RATES:
        return KNOWN_RATES[year], year
    earlier = [y for y in KNOWN_RATES if y < year]
    closest = max(earlier) if earlier else min(KNOWN_RATES)
    return KNOWN_RATES[closest], closest


def round_5rp(amount: Decimal) -> Decimal:
    """Round to 5 Rappen, like the form's Math.round(x*20)/20."""
    return (amount * 20).quantize(Decimal(1), rounding=ROUND_HALF_UP) / 20


def money(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def payroll_month(today: date, workday: int = 0) -> tuple[int, int]:
    """Month of the most recent `workday` (0 = Monday) on or before `today`."""
    last_workday = today - timedelta(days=(today.weekday() - workday) % 7)
    return last_workday.year, last_workday.month


def count_weekdays(year: int, month: int, weekday: int) -> int:
    """Number of e.g. Mondays (weekday 0) in a month."""
    first_weekday, days = calendar.monthrange(year, month)
    first = 1 + (weekday - first_weekday) % 7
    return (days - first) // 7 + 1


def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return last_day - timedelta(days=(last_day.weekday() - weekday) % 7)


def payment_date(year: int, month: int, workday: int = 0) -> date:
    """Transfer date for a month's wage.

    If the Wednesday after the last Monday is at least one day before the end
    of the month, the transfer goes out the next day (Thursday, still in the
    month); otherwise on the Tuesday after the last Monday, which falls into
    the next month only if that Monday is the last day of the month.
    `workday` shifts the rule for a workday other than Monday.
    """
    last_workday = last_weekday_of_month(year, month, workday)
    thursday = last_workday + timedelta(days=3)
    if thursday.month == month:
        return thursday
    return last_workday + timedelta(days=1)


def month_label(year: int, month: int) -> str:
    return f"{MONTHS_DE[month - 1]} {year}"


def parse_month_label(label: str) -> tuple[int, int]:
    """Parse e.g. 'September 2026' into (2026, 9)."""
    name, year = label.split()
    return int(year), MONTHS_DE.index(name) + 1


@dataclass(frozen=True)
class Payslip:
    """Mirrors the calculation script embedded in the SVA form."""

    hours: Decimal
    hourly_rate: Decimal
    rates: Rates = Rates()

    @property
    def gross(self) -> Decimal:
        return self.hours * self.hourly_rate

    @property
    def vacation(self) -> Decimal:
        return self.gross / 100 * self.rates.vacation

    @property
    def base(self) -> Decimal:
        """Beitragspflichtiger Lohn: Grundlohn + Ferienzuschlag."""
        return self.gross + self.vacation

    @property
    def ahv(self) -> Decimal:
        return self.base / 100 * self.rates.ahv

    @property
    def alv(self) -> Decimal:
        return self.base / 100 * self.rates.alv

    @property
    def tax(self) -> Decimal:
        return self.base / 100 * self.rates.tax

    @property
    def total_deductions(self) -> Decimal:
        return round_5rp(self.ahv + self.alv + self.tax)

    @property
    def net(self) -> Decimal:
        return round_5rp(self.base - self.total_deductions)

    @property
    def payout(self) -> Decimal:
        return self.net


@dataclass(frozen=True)
class EmployerBill:
    """What SVA Zürich bills at year end for a given Beitragspflichtiger Lohn.

    Like on the actual bill, every item is rounded to 5 Rappen; the
    Verwaltungskosten are computed from the rounded AHV/IV/EO contributions.
    """

    base: Decimal
    rates: Rates = Rates()

    @property
    def ahv(self) -> Decimal:
        return round_5rp(self.base / 100 * self.rates.employer_ahv)

    @property
    def alv(self) -> Decimal:
        return round_5rp(self.base / 100 * self.rates.employer_alv)

    @property
    def fak(self) -> Decimal:
        return round_5rp(self.base / 100 * self.rates.fak)

    @property
    def admin(self) -> Decimal:
        return round_5rp(self.ahv / 100 * self.rates.admin)

    @property
    def tax(self) -> Decimal:
        return round_5rp(self.base / 100 * self.rates.tax)

    @property
    def total(self) -> Decimal:
        return self.ahv + self.alv + self.fak + self.admin + self.tax
