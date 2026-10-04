"""Configuration (XDG config dir) and the per-month payroll records (XDG data dir)."""

import json
import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .calc import WEEKDAYS, Rates

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

[employer]
name = "Vorname Nachname"
address = ["Strasse 1", "8000 Zürich"]

[employee]
name = "Vorname Nachname"
address = ["Strasse 1", "8000 Zürich"]
ahv_number = "756.0000.0000.00"
iban = "CH00 0000 0000 0000 0000 0"
# bank = "Bank, Ort"   # optional, printed below the IBAN

[wage]
hourly_rate = 30.00

# Optional overrides of the built-in 2026 rates (all in percent):
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
    rates: Rates = field(default_factory=Rates)


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
            rates=Rates.from_mapping(raw.get("rates", {})),
        )
    except KeyError as e:
        raise ConfigError(f"Missing setting {e} in {path}") from e
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise ConfigError(f"Invalid config {path}: {e}") from e


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
