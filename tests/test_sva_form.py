import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sva_form import cli
from sva_form.calc import (
    EmployerBill,
    Payslip,
    Rates,
    month_label,
    parse_month_label,
    payroll_month,
    round_5rp,
)
from sva_form.config import ConfigError, init_config, load_config, load_records
from sva_form.pdf import read_form

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


EXAMPLE = Path.home() / "tmp" / "ahv-formular-stundenlohnabrechnung-2026-09.pdf"


@pytest.mark.skipif(not EXAMPLE.exists(), reason="local example form not available")
def test_import_hand_filled_form():
    key, record = cli.record_from_form(read_form(EXAMPLE), EXAMPLE)
    assert key == "2026-09"
    assert D(record["base"]) == D("105.62175")
    assert record["payout"] == "93.6"
    assert record["paid_on"] == "2026-09-29"


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
    assert f"{bill.total:.2f}" in out
