# sva-form

Creates the monthly SVA Zürich *Stundenlohnabrechnung* (hourly payslip for
household help, vereinfachtes Abrechnungsverfahren) as a filled PDF, keeps a
record of every month, and prints a year-end summary including the expected
SVA bill.

## Usage

```sh
uv run sva-form init              # create ~/.config/sva-form/config.toml
uv run sva-form month 13.5        # payslip for the month of the last Monday
uv run sva-form month 12 --month 2026-08 --date 2026-09-01 --paid-on 2026-09-02
uv run sva-form import ~/old/ahv-formular-*.pdf   # record earlier, hand-filled forms
uv run sva-form year 2026         # year-end summary
```

The payroll month is the month of the most recent workday (default: Monday)
on or before the filling date. Re-running `month` for the same month replaces
its record.

## Files

- Config: `$XDG_CONFIG_HOME/sva-form/config.toml` (default `~/.config/...`):
  employer, employee details, hourly rate, optional rate overrides.
- Records: `$XDG_DATA_HOME/sva-form/records.json` (default
  `~/.local/share/...`): one entry per month with exact amounts.
- PDFs: `output_dir` from the config, default the current directory.

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

## Development

```sh
uv run pytest
uv run ruff check .
```
