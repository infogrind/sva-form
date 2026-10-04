# sva-form

Creates the monthly SVA Zürich *Stundenlohnabrechnung* (hourly payslip for
household help, vereinfachtes Abrechnungsverfahren) as a filled PDF, keeps a
record of every month, and prints a year-end summary including the expected
SVA bill.

## Usage

```sh
uv run sva-form init              # create ~/.config/sva-form/config.toml
uv run sva-form month             # asks for the hours (suggestion from last month)
uv run sva-form month 13.5        # payslip + QR-bill for the month of the last Monday
uv run sva-form month 13.5 --dry-run   # writes ./test.pdf and ./test-qr.pdf, records nothing
uv run sva-form month 12 --month 2026-08 --date 2026-09-01 --paid-on 2026-09-02
uv run sva-form import ~/old/ahv-formular-*.pdf   # record earlier, hand-filled forms
uv run sva-form year 2026         # year-end summary
```

The payroll month is the month of the most recent workday (default: Monday)
on or before the filling date. Re-running `month` for the same month replaces
its record. Without hours, `month` asks for them and suggests the previous
month's hours per workday times this month's number of workdays. On macOS the
payslip is opened afterwards (`--no-open` or `open_payslip = false` to turn
this off).

The transfer date ("Überweisung am") defaults to the Thursday after the last
Monday of the month if that Thursday is still in the month; otherwise to the
Tuesday after the last Monday (in the next month only if that Monday is the
last day of the month). If that date is already past, `month` asks whether to
use tomorrow instead (without asking with `--yes` or without a terminal).
Override it with `--paid-on`.

## Files

- Config: `$XDG_CONFIG_HOME/sva-form/config.toml` (default `~/.config/...`):
  employer, employee details, hourly rate, optional rate overrides.
- Records: `$XDG_DATA_HOME/sva-form/records.json` (default
  `~/.local/share/...`): one entry per month with exact amounts.
- PDFs: `output_dir` from the config, default the current directory:
  `ahv-formular-stundenlohnabrechnung-YYYY-MM.pdf` (payslip) and
  `qr-zahlung-YYYY-MM.pdf` (Swiss QR-bill for the payout, to upload in
  e-banking; the transfer date has to be set there). Set `qr_output_dir` to
  write the QR-bill elsewhere, e.g. `~/Downloads`; in dry run it is always
  written next to `test.pdf`.

## Calculation

Mirrors the script embedded in the SVA form (version 01.2021):

- Grundlohn = hours × hourly rate
- Ferienzuschlag = 8.33 % of Grundlohn
- Beitragspflichtiger Lohn = Grundlohn + Ferienzuschlag
- Deductions: AHV/IV/EO 5.3 %, ALV 1.1 %, Steuerabzug 5 % of
  Beitragspflichtiger Lohn; their total is rounded to 5 Rappen
- Nettolohn = Beitragspflichtiger Lohn − deductions, rounded to 5 Rappen

Year end, SVA bills on the Beitragspflichtiger Lohn: AHV/IV/EO 10.6 %,
ALV 2.2 % (employer and employee share), FAK 1.025 %, Steuerabzug 5 %, and
Verwaltungskosten of 5 % of the AHV/IV/EO contributions. The employee shares
and the tax were already withheld from the wage, so the employer's own extra
cost is the bill minus the withheld deductions.

## Rates

The contribution rates are built in for each year verified against SVA Zürich
(currently 2025 and 2026, see `KNOWN_RATES` in `calc.py`). For any other
year, `month` and `year` warn and use the closest known year's rates. After
checking the rates, confirm them in the config with a `[rates.YYYY]` section,
empty if nothing changed, or with the changed values:

```toml
[rates.2027]
fak = 1.0
```

## Development

```sh
uv run pytest
uv run ruff check .
```
