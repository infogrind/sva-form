"""Configuration (XDG config dir) and the per-month payroll records (XDG data dir)."""

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .calc import WEEKDAYS, Rates, known_rates

APP = "sva-form"

SAMPLE_CONFIG = """\
# sva-form configuration

# Place used for "Ort, Datum" on the form.
place = "Zürich"
# Weekday she normally works; the payroll month is the month of the most
# recent such day.
workday = "monday"
# Where the generated PDFs are written (default: current directory).
# output_dir = "~/Documents/Lohnabrechnungen"
# Where the QR-bill for e-banking is written (default: output_dir).
# qr_output_dir = "~/Downloads"
# Open the payslip after `month` (macOS, interactive runs only).
# open_payslip = true

[employer]
name = "Vorname Nachname"
address = ["Strasse 1", "8000 Zürich"]

[employee]
name = "Vorname Nachname"
address = ["Strasse 1", "8000 Zürich"]
ahv_number = "756.0000.0000.00"
iban = "CH00 0000 0000 0000 0000 0"
# bank = "Bank, Ort"   # optional, printed below the IBAN
# birth_date = "31.12.1980"   # for the year-end Lohndeklaration

[wage]
hourly_rate = 30.00

# For the year-end Lohndeklaration (`sva-form declaration`); all optional,
# missing values are left blank on the form.
# [declaration]
# account_number = "000.000"          # Abrechnungs-Nr. of SVA Zürich
# contact_phone = "044 000 00 00"
# contact_email = "name@example.com"
# refund_iban = "CH00 0000 0000 0000 0000 0"   # for refunds, account of the employer
# accident_insurance = "Versicherung"  # obligatory accident insurance (UVG)

# Optional overrides of the built-in rates (all in percent). sva-form warns
# when it runs for a year whose rates it doesn't know; check them at
# svazurich.ch and confirm with a [rates.YYYY] section (empty if unchanged):
# [rates.2027]
# fak = 1.0
#
# Overrides for all years:
# [rates]
# vacation = 8.33      # Ferienzuschlag
# ahv = 5.3            # AHV/IV/EO employee share
# alv = 1.1            # ALV employee share
# tax = 5              # Steuerabzug
# employer_ahv = 10.6  # AHV/IV/EO billed (employer + employee)
# employer_alv = 2.2   # ALV billed (employer + employee)
# fak = 1.025          # Familienausgleichskasse
# admin = 5            # Verwaltungskosten, % of AHV/IV/EO contributions
"""


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP


def config_path() -> Path:
    return config_dir() / "config.toml"


def records_path() -> Path:
    return data_dir() / "records.json"


@dataclass(frozen=True)
class Person:
    name: str
    address: list[str]

    @property
    def block(self) -> str:
        return "\n".join([self.name, *self.address])

    @property
    def first_name(self) -> str:
        return self.name.rsplit(" ", 1)[0]

    @property
    def last_name(self) -> str:
        return self.name.rsplit(" ", 1)[-1]


@dataclass(frozen=True)
class Declaration:
    """Settings only needed for the year-end Lohndeklaration."""

    account_number: str = ""
    contact_phone: str = ""
    contact_email: str = ""
    refund_iban: str = ""
    accident_insurance: str = ""


@dataclass(frozen=True)
class Config:
    employer: Person
    employee: Person
    ahv_number: str
    iban: str
    hourly_rate: Decimal
    bank: str = ""
    place: str = "Zürich"
    workday: int = 0
    output_dir: Path = Path(".")
    qr_output_dir: Path | None = None
    open_payslip: bool = True
    birth_date: str = ""
    declaration: Declaration = field(default_factory=Declaration)
    rate_overrides: dict = field(default_factory=dict)  # [rates]: all years
    year_rate_overrides: dict[int, dict] = field(default_factory=dict)  # [rates.YYYY]

    def rates_for(self, year: int) -> tuple[Rates, str | None]:
        """Rates for `year` and a warning if they have not been verified for it.

        A [rates.YYYY] section in the config (empty if nothing changed) counts
        as verification by the user.
        """
        base, base_year = known_rates(year)
        rates = base.with_overrides(self.rate_overrides)
        rates = rates.with_overrides(self.year_rate_overrides.get(year, {}))
        if base_year == year or year in self.year_rate_overrides:
            return rates, None
        return rates, (
            f"The contribution rates for {year} have not been verified; using those of "
            f"{base_year}. Check them at svazurich.ch and confirm them with a [rates.{year}] "
            f"section in {config_path()} (empty if unchanged)."
        )


class ConfigError(Exception):
    pass


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(f"No config found at {path}. Run `sva-form init` to create one.")
    try:
        raw = tomllib.loads(path.read_text())
        employee = raw["employee"]
        workday = raw.get("workday", "monday").lower()
        if workday not in WEEKDAYS:
            raise ConfigError(f"Invalid workday {workday!r} in {path}")
        return Config(
            employer=Person(raw["employer"]["name"], raw["employer"].get("address", [])),
            employee=Person(employee["name"], employee.get("address", [])),
            ahv_number=employee["ahv_number"],
            iban=employee["iban"],
            bank=employee.get("bank", ""),
            hourly_rate=Decimal(str(raw["wage"]["hourly_rate"])),
            place=raw.get("place", "Zürich"),
            workday=WEEKDAYS.index(workday),
            output_dir=Path(raw.get("output_dir", ".")).expanduser(),
            qr_output_dir=Path(raw["qr_output_dir"]).expanduser()
            if "qr_output_dir" in raw
            else None,
            open_payslip=bool(raw.get("open_payslip", True)),
            birth_date=_birth_date(employee.get("birth_date", "")),
            declaration=_declaration(raw.get("declaration", {})),
            **_rate_overrides(raw.get("rates", {})),
        )
    except KeyError as e:
        raise ConfigError(f"Missing setting {e} in {path}") from e
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise ConfigError(f"Invalid config {path}: {e}") from e


def _birth_date(text: str) -> str:
    if text and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", text):
        raise ValueError(f"birth_date must be DD.MM.YYYY, got {text!r}")
    if text:
        datetime.strptime(text, "%d.%m.%Y")  # raises ValueError for e.g. 31.02.
    return text


def _declaration(table: dict) -> Declaration:
    unknown = set(table) - set(Declaration.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown setting(s) in [declaration]: {', '.join(sorted(unknown))}")
    return Declaration(**{k: str(v) for k, v in table.items()})


def _rate_overrides(table: dict) -> dict:
    """Split [rates] into overrides for all years and [rates.YYYY] sections."""
    general = {k: v for k, v in table.items() if not isinstance(v, dict)}
    years = {}
    for key, value in table.items():
        if isinstance(value, dict):
            if not key.isdigit():
                raise ValueError(f"Invalid section [rates.{key}], expected a year")
            years[int(key)] = value
    for overrides in [general, *years.values()]:
        Rates().with_overrides(overrides)  # validate the names early
    return {"rate_overrides": general, "year_rate_overrides": years}


def init_config(path: Path | None = None) -> Path:
    path = path or config_path()
    if path.exists():
        raise ConfigError(f"Config already exists at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SAMPLE_CONFIG)
    return path


def load_records(path: Path | None = None) -> dict[str, dict]:
    """Records keyed by 'YYYY-MM'."""
    path = path or records_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_record(
    key: str, record: dict, path: Path | None = None, records: dict[str, dict] | None = None
) -> bool:
    """Store a month's record; returns True if an existing record was replaced.

    Pass `records` if they were already loaded, to avoid reading the file again.
    """
    path = path or records_path()
    records = dict(load_records(path) if records is None else records)
    replaced = key in records
    records[key] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(records.items())), indent=2, ensure_ascii=False) + "\n")
    return replaced
