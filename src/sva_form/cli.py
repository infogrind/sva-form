"""Command line interface: `sva-form month|year|import|init`."""

import argparse
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import config as cfgmod
from .calc import EmployerBill, Payslip, money, month_label, parse_month_label, payroll_month
from .config import Config, ConfigError
from .pdf import fill_form, read_form

AMOUNT_KEYS = [
    "gross",
    "vacation",
    "base",
    "ahv",
    "alv",
    "tax",
    "total_deductions",
    "net",
    "payout",
]


def chf(amount: Decimal) -> str:
    return f"{money(amount):,.2f}".replace(",", "'")


def fmt2(amount: Decimal) -> str:
    """Number as the form displays it: two decimals, no thousands separator."""
    return f"{money(amount):.2f}"


def output_filename(year: int, month: int) -> str:
    return f"ahv-formular-stundenlohnabrechnung-{year}-{month:02d}.pdf"


def form_values(
    cfg: Config, slip: Payslip, year: int, month: int, filled_on: date, paid_on: date
) -> dict[str, str]:
    return {
        "Arbeitgeber1": cfg.employer.block,
        "Arbeitnehmer1": cfg.employee.block,
        "Lohnabrechnung": month_label(year, month),
        "AHVNummer": cfg.ahv_number,
        "1Datum": f"{cfg.place}, {filled_on:%d.%m.%Y}",
        "Anzahl_Stunden": fmt2(slip.hours),
        "Stundenlohn": fmt2(slip.hourly_rate),
        "Grundlohn": fmt2(slip.gross),
        "Anzahl_Prozent_Ferienzuschlag": fmt2(slip.rates.vacation),
        "Ferienzuschlag": fmt2(slip.vacation),
        "Grundlohn_Ferienzuschlag": fmt2(slip.base),
        "AHV": fmt2(slip.ahv),
        "ALV": fmt2(slip.alv),
        "Anzahl_Quellensteuer_NBU": fmt2(slip.rates.tax),
        "Quellensteuer": fmt2(slip.tax),
        "Total_Abzuege": fmt2(slip.total_deductions),
        "Nettolohn": fmt2(slip.net),
        "Total_Auszahlung": fmt2(slip.payout),
        "Ueberweisung_am": f"{paid_on:%d.%m.%Y}",
        "Konto_Nr": cfg.iban,
        "Bank_Ort": cfg.bank,
    }


def record_from_payslip(slip: Payslip, filled_on: date, paid_on: date, pdf: Path) -> dict:
    return {
        "hours": str(slip.hours),
        "hourly_rate": str(slip.hourly_rate),
        **{key: str(getattr(slip, key)) for key in AMOUNT_KEYS},
        "rates": {
            "vacation": str(slip.rates.vacation),
            "ahv": str(slip.rates.ahv),
            "alv": str(slip.rates.alv),
            "tax": str(slip.rates.tax),
        },
        "filled_on": filled_on.isoformat(),
        "paid_on": paid_on.isoformat(),
        "source": str(pdf),
    }


def _parse_swiss_date(text: str) -> str | None:
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    return date(int(m[3]), int(m[2]), int(m[1])).isoformat() if m else None


def record_from_form(fields: dict[str, str], pdf: Path) -> tuple[str, dict]:
    """Rebuild a month's record from a form filled earlier (by hand or by this tool)."""
    year, month = parse_month_label(fields["Lohnabrechnung"])
    num = {
        "hours": "Anzahl_Stunden", "hourly_rate": "Stundenlohn", "gross": "Grundlohn",
        "vacation": "Ferienzuschlag", "base": "Grundlohn_Ferienzuschlag", "ahv": "AHV",
        "alv": "ALV", "tax": "Quellensteuer", "total_deductions": "Total_Abzuege",
        "net": "Nettolohn", "payout": "Total_Auszahlung",
    }  # fmt: skip
    record = {key: str(Decimal(fields.get(name, "0"))) for key, name in num.items()}
    record["filled_on"] = _parse_swiss_date(fields.get("1Datum", ""))
    record["paid_on"] = _parse_swiss_date(fields.get("Ueberweisung_am", ""))
    record["source"] = str(pdf)
    return f"{year}-{month:02d}", record


def year_report(records: dict[str, dict], year: int, cfg: Config) -> str:
    months = {k: v for k, v in sorted(records.items()) if k.startswith(f"{year}-")}
    if not months:
        return f"No records for {year}."

    def total(key: str) -> Decimal:
        return sum((Decimal(r[key]) for r in months.values()), Decimal(0))

    cols = [("Stunden", "hours"), ("Grundlohn", "gross"), ("Ferienzus.", "vacation"),
            ("Beitragspfl.", "base"), ("AHV/IV/EO", "ahv"), ("ALV", "alv"), ("Steuer", "tax"),
            ("Abzüge", "total_deductions"), ("Auszahlung", "payout")]  # fmt: skip
    lines = [f"Jahresübersicht {year} – {cfg.employee.name}", ""]
    lines.append(f"{'Monat':<9}" + "".join(f"{title:>13}" for title, _ in cols))
    for key, r in months.items():
        lines.append(f"{key:<9}" + "".join(f"{chf(Decimal(r[c])):>13}" for _, c in cols))
    lines.append("-" * (9 + 13 * len(cols)))
    lines.append(f"{'Total':<9}" + "".join(f"{chf(total(c)):>13}" for _, c in cols))

    base = total("base")
    bill = EmployerBill(base, cfg.rates)
    rates = cfg.rates
    withheld = total("total_deductions")

    def row(label: str, amount: Decimal) -> str:
        return f"{label:<54}CHF {chf(amount):>10}"

    lines += [
        "",
        row("Bruttolohn (Grundlohn)", total("gross")),
        row("Ferienzuschlag", total("vacation")),
        row("Beitragspflichtiger Lohn", base),
        "",
        "Erwartete Rechnung SVA Zürich:",
        row(f"  Lohnbeiträge AHV/IV/EO {rates.employer_ahv}%", bill.ahv),
        row(f"  Lohnbeiträge ALV {rates.employer_alv}%", bill.alv),
        row(f"  Lohnbeiträge FAK {rates.fak}%", bill.fak),
        row(f"  Verwaltungskosten {rates.admin}% der AHV/IV/EO-Beiträge", bill.admin),
        row(f"  Steuerabzug {rates.tax}%", bill.tax),
        row("  Total", bill.total),
        "",
        row("  davon bereits vom Lohn abgezogen", -withheld),
        row("  Effektive Kosten Arbeitgeber (zusätzlich zum Lohn)", bill.total - withheld),
        "",
        row(f"Gesamtkosten {year} (Auszahlungen + Rechnung SVA)", total("payout") + bill.total),
    ]
    return "\n".join(lines)


def _parse_decimal(text: str) -> Decimal:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None


def _parse_month(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if not m or not 1 <= int(m[2]) <= 12:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {text!r}")
    return int(m[1]), int(m[2])


def cmd_month(args: argparse.Namespace) -> None:
    cfg = cfgmod.load_config()
    filled_on = args.date or date.today()
    paid_on = args.paid_on or filled_on
    year, month = args.month or payroll_month(filled_on, cfg.workday)
    slip = Payslip(args.hours, cfg.hourly_rate, cfg.rates)
    output = args.output or cfg.output_dir / output_filename(year, month)
    fill_form(form_values(cfg, slip, year, month, filled_on, paid_on), output)
    key = f"{year}-{month:02d}"
    replaced = cfgmod.save_record(
        key, record_from_payslip(slip, filled_on, paid_on, output.resolve())
    )

    print(f"Lohnabrechnung {month_label(year, month)} – {cfg.employee.name}")
    print(f"  {slip.hours} Stunden à CHF {chf(slip.hourly_rate)}")
    for label, value in [
        ("Grundlohn", slip.gross),
        (f"Ferienzuschlag {slip.rates.vacation}%", slip.vacation),
        ("Beitragspflichtiger Lohn", slip.base),
        (f"AHV/IV/EO {slip.rates.ahv}%", -slip.ahv),
        (f"ALV {slip.rates.alv}%", -slip.alv),
        (f"Steuerabzug {slip.rates.tax}%", -slip.tax),
        ("Total Abzüge", -slip.total_deductions),
        ("Auszahlung", slip.payout),
    ]:
        print(f"  {label:<28}{chf(value):>10}")
    print(f"PDF: {output}")
    if replaced:
        print(f"Replaced the existing record for {key}.")


def cmd_year(args: argparse.Namespace) -> None:
    cfg = cfgmod.load_config()
    records = cfgmod.load_records()
    if not records:
        raise ConfigError(f"No records in {cfgmod.records_path()}.")
    year = args.year or max(int(k[:4]) for k in records)
    print(year_report(records, year, cfg))


def cmd_import(args: argparse.Namespace) -> None:
    for pdf in args.pdfs:
        key, record = record_from_form(read_form(pdf), pdf.resolve())
        replaced = cfgmod.save_record(key, record)
        print(
            f"{key}: Auszahlung CHF {chf(Decimal(record['payout']))}"
            f"{' (replaced)' if replaced else ''}  <- {pdf}"
        )


def cmd_init(args: argparse.Namespace) -> None:
    path = cfgmod.init_config()
    print(f"Created {path} – please fill in the details.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sva-form",
        description="Monthly SVA Zürich hourly payslips and year-end summary.",
        epilog=f"Config: {cfgmod.config_path()}\nRecords: {cfgmod.records_path()}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("month", help="create the payslip PDF for a month and record it")
    p.add_argument("hours", type=_parse_decimal, help="total hours worked in the month")
    p.add_argument(
        "--month", type=_parse_month, help="YYYY-MM (default: month of the last workday)"
    )
    p.add_argument(
        "--date", type=date.fromisoformat, help="date the form is filled (default: today)"
    )
    p.add_argument("--paid-on", type=date.fromisoformat, help="transfer date (default: --date)")
    p.add_argument("-o", "--output", type=Path, help="output PDF path")
    p.set_defaults(func=cmd_month)

    p = sub.add_parser("year", help="year-end summary incl. expected SVA bill")
    p.add_argument("year", type=int, nargs="?", help="default: latest year with records")
    p.set_defaults(func=cmd_year)

    p = sub.add_parser("import", help="record months from previously filled PDF forms")
    p.add_argument("pdfs", type=Path, nargs="+")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("init", help="create a sample config file")
    p.set_defaults(func=cmd_init)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ConfigError as e:
        sys.exit(f"sva-form: {e}")
