"""Year-end Lohndeklaration (SVA Zürich form "Lohndeklaration für Hausangestellte").

SVA Zürich publishes a new form every year, so the user provides it as a
template; `validate_template` checks that it is the expected form for the
expected year before it is filled.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .calc import Rates, round_5rp
from .config import Config, ConfigError

# Fields the tool fills; a template without them is not the expected form.
REQUIRED_FIELDS = [
    "abrechnungsnummer",
    "firmenadresse",
    "Lohnauszahlung1",
    "AHVnr.0.0",
    "Geburtsdatum.0.0",
    "Name.0.0",
    "Vorname.0.0",
    "Von.0.0",
    "Bis.0.0",
    "Pflichtig.0.0",
    "TotalPflichtig.0",
    "TotalFAK.0",
    "TotalALV.0",
    "ort_datum",
]
# Checkboxes and the "on" state the template must offer for them.
CHECKBOXES = {"Lohnauszahlung1": "/ja", "keine BVG-Anschlusspflicht": "/ja"}

YEAR_PATTERN = re.compile(r"Lohndeklaration\b[^\n\d]{0,60}?(20\d{2})")


class TemplateError(ConfigError):
    pass


def template_year(reader: PdfReader) -> int | None:
    """The year the form is for, from its title or page text."""
    texts = [str((reader.metadata or {}).get("/Title", ""))]
    texts += [page.extract_text() or "" for page in reader.pages]
    for text in texts:
        if m := YEAR_PATTERN.search(text):
            return int(m[1])
    return None


def validate_template(path: Path, year: int) -> None:
    """Make sure `path` is the SVA Lohndeklaration for Hausangestellte for `year`."""
    hint = (
        f"Download the 'Lohndeklaration für Hausangestellte und Hauswartung {year}' "
        "from svazurich.ch and pass it with --template."
    )
    try:
        reader = PdfReader(path)
        fields = reader.get_fields() or {}
    except (PdfReadError, OSError, ValueError) as e:
        raise TemplateError(f"Cannot read {path} as a PDF form: {e}. {hint}") from e
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    if missing:
        raise TemplateError(
            f"{path} is not the expected form (missing fields: {', '.join(missing)}). {hint}"
        )
    for name, state in CHECKBOXES.items():
        states = fields[name].get("/_States_", []) if name in fields else [state]
        if state not in states:
            raise TemplateError(f"{path}: checkbox {name!r} has no {state} state. {hint}")
    found = template_year(reader)
    if found is None:
        raise TemplateError(f"Cannot tell which year {path} is for. {hint}")
    if found != year:
        raise TemplateError(f"{path} is the form for {found}, not {year}. {hint}")


@dataclass(frozen=True)
class DeclarationForm:
    values: dict[str, str]
    missing: list[str]  # settings that are left blank on the form


def _amount(value: Decimal) -> str:
    """Number as the form expects it (its scripts parse plain numbers)."""
    return f"{value:.2f}"


def declaration_values(
    cfg: Config, records: dict[str, dict], year: int, rates: Rates, today: date
) -> DeclarationForm:
    months = sorted(k for k in records if k.startswith(f"{year}-"))
    if not months:
        raise ConfigError(f"No records for {year}; nothing to declare.")
    first, last = int(months[0][5:]), int(months[-1][5:])
    exact_wage = sum((Decimal(records[k]["base"]) for k in months), Decimal(0))
    wage = exact_wage.quantize(Decimal(1), rounding=ROUND_HALF_UP)  # whole francs
    total = round_5rp(wage)  # like the form: sum of the rows, rounded to 5 Rappen
    employer, employee, settings = cfg.employer, cfg.employee, cfg.declaration
    refund_iban = settings.refund_iban.replace(" ", "")
    values = {
        "abrechnungsnummer": settings.account_number,
        "firmenadresse": ", ".join(
            [f"{employer.last_name} {employer.first_name}", *employer.address]
        ),
        "Lohnauszahlung1": "/ja",
        "Kontakt_Name_Vorname": f"{employer.last_name} {employer.first_name}",
        "Kontakt_Email_Adresse": settings.contact_email,
        "Kontakt_Telefon": settings.contact_phone,
        "Zahlungsverbindung_Rückzahlung": employer.name if refund_iban else "",
        # the form prints "CH" in front of the field
        "IBAN_Nr": refund_iban.removeprefix("CH"),
        "name der unfallversicherung": settings.accident_insurance,
        "AHVnr.0.0": cfg.ahv_number,
        "Geburtsdatum.0.0": cfg.birth_date,
        "Name.0.0": employee.last_name,
        "Vorname.0.0": employee.first_name,
        "Von.0.0": f"01.{first:02d}.",
        "Bis.0.0": f"{calendar.monthrange(year, last)[1]:02d}.{last:02d}.",
        "Pflichtig.0.0": _amount(wage),
        "TotalPflichtig.0": _amount(total),
        "TotalFAK.0": _amount(total),
        "TotalALV.0": _amount(total),  # below the ALV ceiling, no retirees
        # expected wage sum next year: assume the same
        "feld1": _amount(total),
        "feld2": _amount(total),
        "feld3": _amount(total),
        "ort_datum": f"{cfg.place}, {today:%d.%m.%Y}",
    }
    if wage < rates.bvg_threshold:
        values["keine BVG-Anschlusspflicht"] = "/ja"
        values["vorsorgeeinrichtung"] = "Lohn unter der BVG-Eintrittsschwelle"
    missing = [
        label
        for label, value in [
            ("Abrechnungs-Nr. ([declaration] account_number)", settings.account_number),
            ("Geburtsdatum ([employee] birth_date)", cfg.birth_date),
            ("Unfallversicherung ([declaration] accident_insurance)", settings.accident_insurance),
        ]
        if not value
    ]
    if wage >= rates.bvg_threshold:
        missing.append("Berufliche Vorsorge (BVG): wage above the BVG threshold")
    return DeclarationForm(values, missing)


def declaration_filename(year: int) -> str:
    return f"lohndeklaration-{year}.pdf"
