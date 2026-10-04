import re
from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader

from sva_form import cli
from sva_form.calc import (
    EmployerBill,
    Payslip,
    Rates,
    money,
    month_label,
    parse_month_label,
    payment_date,
    payroll_month,
    round_5rp,
)
from sva_form.config import ConfigError, Person, init_config, load_config, load_records
from sva_form.pdf import read_form
from sva_form.qr import build_qr_bill, structured_address

D = Decimal

CONFIG = """\
place = "Zürich"
workday = "monday"
output_dir = "{out}"

[employer]
name = "Erika Muster"
address = ["Musterweg 1", "8000 Zürich"]

[employee]
name = "Anna Beispiel"
address = ["Beispielstrasse 2", "8047 Zürich"]
ahv_number = "756.1234.5678.97"
iban = "CH93 0076 2011 6238 5295 7"

[wage]
hourly_rate = 30
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = tmp_path / "config" / "sva-form" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(CONFIG.format(out=tmp_path / "out"))
    return tmp_path


# --- calculations -----------------------------------------------------------


def test_payslip_matches_filled_form_with_old_ahv_rate():
    # September 2026 example, filled with form version 01.2020 (AHV 5.275%).
    slip = Payslip(D("3.25"), D("30"), Rates(ahv=D("5.275")))
    assert slip.gross == D("97.50")
    assert slip.vacation == D("8.12175")
    assert slip.base == D("105.62175")
    assert slip.ahv == D("5.5715473125")
    assert slip.alv == D("1.16183925")
    assert slip.tax == D("5.2810875")
    assert slip.total_deductions == D("12.00")
    assert slip.net == D("93.60")
    assert slip.payout == D("93.60")


def test_payslip_current_rates():
    slip = Payslip(D("3.25"), D("30"))
    assert slip.total_deductions == D("12.05")
    assert slip.net == D("93.55")


def test_net_is_rounded_after_deductions():
    slip = Payslip(D("13.5"), D("30"))
    assert slip.base == D("438.7365")
    assert slip.total_deductions == D("50.00")  # 50.0157...
    assert slip.net == D("388.75")  # 388.7365


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("12.0145", "12.00"), ("12.025", "12.05"), ("12.07", "12.05"), ("12.075", "12.10")],
)
def test_round_5rp(amount, expected):
    assert round_5rp(D(amount)) == D(expected)


def test_employer_bill():
    bill = EmployerBill(D("1000"))
    assert bill.ahv == D("106")
    assert bill.alv == D("22")
    assert bill.fak == D("10.25")
    assert bill.admin == D("5.3")
    assert bill.tax == D("50")
    assert bill.total == D("193.55")


def test_employer_bill_items_rounded_to_5rp():
    bill = EmployerBill(D("714.978"))
    assert bill.ahv == D("75.80")  # 75.7877
    assert bill.alv == D("15.75")  # 15.7295
    assert bill.fak == D("7.35")  # 7.3285
    assert bill.admin == D("3.80")  # 5% of 75.80 = 3.79
    assert bill.tax == D("35.75")  # 35.7489
    assert bill.total == D("138.45")


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 9, 28), (2026, 9)),  # Monday itself
        (date(2026, 9, 30), (2026, 9)),
        (date(2026, 10, 3), (2026, 9)),  # last Monday was 28 Sept
        (date(2026, 10, 5), (2026, 10)),
        (date(2026, 8, 31), (2026, 8)),  # Monday on the last day of the month
        (date(2026, 9, 2), (2026, 8)),  # ... paid a few days later
    ],
)
def test_payroll_month(today, expected):
    assert payroll_month(today) == expected


def test_payroll_month_other_workday():
    assert payroll_month(date(2026, 10, 3), workday=3) == (2026, 10)  # Thursday 1 Oct


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (2026, 10, date(2026, 10, 29)),  # Mon 26, Wed 28 -> Thursday 29
        (2026, 9, date(2026, 9, 29)),  # Mon 28, Wed 30 is the last day -> Tuesday 29
        (2026, 3, date(2026, 3, 31)),  # Mon 30 -> Tuesday 31
        (2026, 8, date(2026, 9, 1)),  # Mon 31 -> Tuesday in next month
        (2025, 12, date(2025, 12, 30)),  # Mon 29, Wed 31 is the last day -> Tuesday 30
        (2026, 2, date(2026, 2, 26)),  # Mon 23, Wed 25, month ends Sat 28 -> Thursday 26
    ],
)
def test_payment_date(year, month, expected):
    assert payment_date(year, month) == expected


def test_month_labels():
    assert month_label(2026, 3) == "März 2026"
    assert parse_month_label("September 2026") == (2026, 9)


def test_unknown_rate_rejected():
    with pytest.raises(ValueError, match="ahvv"):
        Rates.from_mapping({"ahvv": 5})


# --- config -------------------------------------------------------------------


def test_load_config(env):
    cfg = load_config()
    assert cfg.employee.block == "Anna Beispiel\nBeispielstrasse 2\n8047 Zürich"
    assert cfg.hourly_rate == D("30")
    assert cfg.workday == 0
    assert cfg.rates == Rates()


def test_rate_overrides(env):
    path = env / "config" / "sva-form" / "config.toml"
    path.write_text(path.read_text() + "\n[rates]\nahv = 5.275\n")
    assert load_config().rates.ahv == D("5.275")


def test_missing_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="sva-form init"):
        load_config()


def test_init_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = init_config()
    assert path == tmp_path / "sva-form" / "config.toml"
    load_config()  # sample config is valid
    with pytest.raises(ConfigError):
        init_config()


# --- end to end ---------------------------------------------------------------


def test_month_creates_pdf_and_record(env, capsys):
    cli.main(["month", "3,25", "--date", "2026-10-02", "--paid-on", "2026-10-03"])
    pdf = env / "out" / "ahv-formular-stundenlohnabrechnung-2026-09.pdf"
    fields = read_form(pdf)
    assert fields["Lohnabrechnung"] == "September 2026"
    assert fields["1Datum"] == "Zürich, 02.10.2026"
    assert fields["Ueberweisung_am"] == "03.10.2026"
    assert fields["Arbeitnehmer1"] == "Anna Beispiel\nBeispielstrasse 2\n8047 Zürich"
    assert fields["Grundlohn_Ferienzuschlag"] == "105.62"
    assert fields["Total_Abzuege"] == "12.05"
    assert fields["Total_Auszahlung"] == "93.55"
    assert fields["Konto_Nr"] == "CH93 0076 2011 6238 5295 7"

    record = load_records()["2026-09"]
    assert record["base"] == "105.62175"
    assert record["payout"] == "93.55"
    assert "Auszahlung" in capsys.readouterr().out

    cli.main(["month", "4", "--month", "2026-09", "--date", "2026-10-02"])
    assert load_records()["2026-09"]["hours"] == "4"
    assert "Replaced" in capsys.readouterr().out


def test_import_roundtrip(env):
    cli.main(["month", "13.5", "--date", "2026-10-26"])
    pdf = env / "out" / "ahv-formular-stundenlohnabrechnung-2026-10.pdf"
    key, record = cli.record_from_form(read_form(pdf), pdf)
    assert key == "2026-10"
    assert record["payout"] == "388.75"
    assert record["filled_on"] == "2026-10-26"


def test_year_report(env, capsys):
    cli.main(["month", "10", "--month", "2026-01", "--date", "2026-01-26"])
    cli.main(["month", "12", "--month", "2026-02", "--date", "2026-02-23"])
    cli.main(["month", "8", "--month", "2025-12", "--date", "2025-12-29"])
    capsys.readouterr()
    cli.main(["year", "2026"])
    out = capsys.readouterr().out
    # base = 22 h * 30 * 1.0833 = 714.978
    assert re.search(r"Beitragspflichtiger Lohn +CHF +714\.98\n", out)
    assert "2025-12" not in out
    bill = EmployerBill(D("714.978"))
    employee = D("714.978") * D("0.114")  # 5.3% + 1.1% + 5% withheld
    total_row = re.search(r"  Total +(\S+) +(\S+) +(\S+)\n", out)
    assert total_row.groups() == tuple(
        f"{money(x):.2f}" for x in (bill.total, employee, bill.total - employee)
    )
    assert re.search(r"Lohnbeiträge FAK 1\.025% +7\.35 +– +7\.35\n", out)
    assert re.search(r"Steuerabzug 5% +35\.75 +35\.75 +–\n", out)


def test_dry_run_writes_test_pdf_and_records_nothing(env, monkeypatch, capsys):
    monkeypatch.chdir(env)
    cli.main(["month", "12", "--dry-run", "--date", "2026-10-26"])
    assert read_form(env / "test.pdf")["Lohnabrechnung"] == "Oktober 2026"
    assert not (env / "out").exists()
    assert load_records() == {}
    assert "Dry run" in capsys.readouterr().out


def test_month_default_payment_date(env):
    cli.main(["month", "12", "--month", "2026-08", "--date", "2026-08-26"])
    assert load_records()["2026-08"]["paid_on"] == "2026-09-01"


# --- QR-bill --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected"),
    [("9.75", "Lohn Oktober 2026 (9.75h)"), ("13.50", "Lohn Oktober 2026 (13.5h)"),
     ("10", "Lohn Oktober 2026 (10h)")],
)  # fmt: skip
def test_qr_message(hours, expected):
    assert cli.qr_message(2026, 10, D(hours)) == expected


@pytest.mark.parametrize(
    ("street_line", "street", "house_num"),
    [("Seeweg 5", "Seeweg", "5"), ("Am Bach 12a", "Am Bach", "12a"),
     ("Dorfplatz", "Dorfplatz", "")],
)  # fmt: skip
def test_structured_address(street_line, street, house_num):
    address = structured_address(Person("A B", [street_line, "8047 Zürich"]))
    assert address == {
        "name": "A B", "street": street, "house_num": house_num,
        "pcode": "8047", "city": "Zürich", "country": "CH",
    }  # fmt: skip


@pytest.mark.parametrize("address", [["Strasse 1"], ["Strasse 1", "Zürich"]])
def test_structured_address_invalid(address):
    with pytest.raises(ConfigError):
        structured_address(Person("A B", address))


def test_qr_bill_data(env):
    data = build_qr_bill(load_config(), D("93.55"), "Lohn September 2026").qr_data()
    lines = data.split("\r\n")
    assert lines[:4] == ["SPC", "0200", "1", "CH9300762011623852957"]
    assert lines[4:11] == ["S", "Anna Beispiel", "Beispielstrasse", "2", "8047", "Zürich", "CH"]
    assert "93.55" in lines and "Lohn September 2026" in lines and "NON" in lines


def test_output_dir_option(env, monkeypatch):
    cli.main(["month", "12", "--date", "2026-10-26", "-o", str(env / "elsewhere")])
    assert (env / "elsewhere" / "ahv-formular-stundenlohnabrechnung-2026-10.pdf").exists()
    assert (env / "elsewhere" / "qr-zahlung-2026-10.pdf").exists()
    assert not (env / "out").exists()
    cli.main(["month", "12", "--dry-run", "--date", "2026-10-26", "-o", str(env / "dry")])
    assert (env / "dry" / "test.pdf").exists() and (env / "dry" / "test-qr.pdf").exists()


def test_qr_output_dir(env):
    path = env / "config" / "sva-form" / "config.toml"
    path.write_text(f'qr_output_dir = "{env / "qr"}"\n' + path.read_text())
    cli.main(["month", "12", "--date", "2026-10-26"])
    assert (env / "qr" / "qr-zahlung-2026-10.pdf").exists()
    assert (env / "out" / "ahv-formular-stundenlohnabrechnung-2026-10.pdf").exists()
    assert not (env / "out" / "qr-zahlung-2026-10.pdf").exists()


def test_month_writes_qr_bill(env, monkeypatch):
    cli.main(["month", "12", "--date", "2026-10-26"])
    qr = env / "out" / "qr-zahlung-2026-10.pdf"
    text = PdfReader(qr).pages[0].extract_text()
    for expected in [
        "Zahlteil",
        "CH93 0076 2011 6238 5295 7",
        "Anna Beispiel",
        "Lohn Oktober 2026",
    ]:
        assert expected in text
    monkeypatch.chdir(env)
    cli.main(["month", "12", "--dry-run", "--date", "2026-10-26"])
    assert (env / "test-qr.pdf").exists()
