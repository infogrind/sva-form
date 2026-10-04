"""Command line interface: `sva-form month|year|import|init`."""

import argparse
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import config as cfgmod
from .calc import (
    EmployerBill,
    Payslip,
    money,
    month_label,
    parse_month_label,
    payment_date,
    payroll_month,
)
from .config import Config, ConfigError
from .pdf import fill_form, read_form
from .qr import write_qr_bill

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


def qr_filename(year: int, month: int) -> str:
    return f"qr-zahlung-{year}-{month:02d}.pdf"


def qr_message(year: int, month: int, hours: Decimal) -> str:
    """E.g. 'Lohn Oktober 2026 (9.75h)'."""
    return f"Lohn {month_label(year, month)} ({hours.normalize():f}h)"


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


# Fields compared when a month is recorded again (filling date and PDF path
# are expected to change and are ignored).
COMPARED_FIELDS = [
    ("hours", "Stunden"),
    ("hourly_rate", "Stundenlohn"),
    ("gross", "Grundlohn"),
    ("vacation", "Ferienzuschlag"),
    ("base", "Beitragspfl. Lohn"),
    ("ahv", "AHV/IV/EO"),
    ("alv", "ALV"),
    ("tax", "Steuerabzug"),
    ("total_deductions", "Total Abzüge"),
    ("payout", "Auszahlung"),
    ("paid_on", "Überweisung am"),
]


def record_diff(old: dict, new: dict) -> list[tuple[str, str, str]]:
    """(label, stored, new) for each field that differs; amounts compared to the Rappen."""
    diff = []
    for key, label in COMPARED_FIELDS:
        a, b = old.get(key), new.get(key)
        if key == "paid_on":
            same = a == b
            shown = (a or "–", b or "–")
        else:
            a, b = Decimal(a or 0), Decimal(b or 0)
            same = money(a) == money(b)
            shown = (chf(a), chf(b))
        if not same:
            diff.append((label, *shown))
    return diff


def print_diff_table(diff: list[tuple[str, str, str]]) -> None:
    print(f"  {'':<20}{'gespeichert':>14}{'neu':>14}")
    for label, old, new in diff:
        print(f"  {label:<20}{old:>14}{new:>14}")


def confirm_replace(key: str, diff: list[tuple[str, str, str]], assume_yes: bool) -> bool:
    print(f"The stored record for {key} differs:")
    print_diff_table(diff)
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"Replace the record for {key}? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes", "j", "ja")


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

    def row(label: str, amount: Decimal) -> str:
        return f"{label:<50}CHF {chf(amount):>10}"

    def cell(amount: Decimal | None) -> str:
        return "–" if amount is None or money(amount) == 0 else chf(amount)

    def split_row(label: str, billed: Decimal, employee: Decimal | None = None) -> str:
        employer = billed - (employee or 0)
        return f"{label:<46}{cell(billed):>14}{cell(employee):>18}{cell(employer):>17}"

    # The employee shares are what was actually withheld on the monthly payslips.
    employee = {key: total(key) for key in ("ahv", "alv", "tax")}
    lines += [
        "",
        row("Bruttolohn (Grundlohn)", total("gross")),
        row("Ferienzuschlag", total("vacation")),
        row("Beitragspflichtiger Lohn", base),
        "",
        f"{'Erwartete Rechnung SVA Zürich':<46}{'Rechnung SVA':>14}"
        f"{'Arbeitnehmer/in':>18}{'Arbeitgeber/in':>17}",
        split_row(f"  Lohnbeiträge AHV/IV/EO {rates.employer_ahv}%", bill.ahv, employee["ahv"]),
        split_row(f"  Lohnbeiträge ALV {rates.employer_alv}%", bill.alv, employee["alv"]),
        split_row(f"  Lohnbeiträge FAK {rates.fak}%", bill.fak),
        split_row(f"  Verwaltungskosten {rates.admin}% der AHV/IV/EO", bill.admin),
        split_row(f"  Steuerabzug {rates.tax}%", bill.tax, employee["tax"]),
        "-" * 95,
        split_row("  Total", bill.total, sum(employee.values())),
        "",
        "  Arbeitnehmer/in: monatlich vom Lohn abgezogen und an die SVA weitergeleitet.",
        "  Arbeitgeber/in: eigene Beiträge zusätzlich zum Lohn.",
        "",
        row("Auszahlungen an Arbeitnehmer/in", total("payout")),
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
    year, month = args.month or payroll_month(filled_on, cfg.workday)
    paid_on = args.paid_on or payment_date(year, month, cfg.workday)
    slip = Payslip(args.hours, cfg.hourly_rate, cfg.rates)
    if args.dry_run:
        output_dir = args.output_dir or Path(".")
        output = output_dir / "test.pdf"
        qr_output = output_dir / "test-qr.pdf"
    else:
        output_dir = args.output_dir or cfg.output_dir
        output = output_dir / output_filename(year, month)
        qr_output = (cfg.qr_output_dir or output_dir) / qr_filename(year, month)
    key = f"{year}-{month:02d}"
    record = record_from_payslip(slip, filled_on, paid_on, output.resolve())
    records = cfgmod.load_records()
    stored = records.get(key)
    diff = record_diff(stored, record) if stored else []
    if diff and args.dry_run:
        print(f"Note: differs from the stored record for {key}:")
        print_diff_table(diff)
    elif diff and not confirm_replace(key, diff, args.yes):
        raise ConfigError(f"Kept the stored record for {key}; nothing written.")

    fill_form(form_values(cfg, slip, year, month, filled_on, paid_on), output)
    write_qr_bill(cfg, slip.payout, qr_message(year, month, slip.hours), qr_output)
    replaced = not args.dry_run and cfgmod.save_record(key, record, records=records)

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
    print(f"QR-Rechnung: {qr_output} (Überweisung am {paid_on:%d.%m.%Y})")
    if args.dry_run:
        print("Dry run: nothing recorded.")
    elif replaced:
        print(f"Replaced the existing record for {key}{'' if diff else ' (same values)'}.")


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
    p.add_argument(
        "--paid-on",
        type=date.fromisoformat,
        help="transfer date (default: payment rule, see README)",
    )
    p.add_argument(
        "-o", "--output-dir", type=Path, help="folder for the payslip (default: output_dir)"
    )
    p.add_argument(
        "-y", "--yes", action="store_true", help="replace a differing stored record without asking"
    )
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="write test.pdf and test-qr.pdf (current folder or -o) and don't record the month",
    )
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
