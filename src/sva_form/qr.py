"""Swiss QR-bill (Einzahlungsschein mit QR-Code) for the wage transfer."""

import io
import re
from decimal import Decimal
from pathlib import Path

from qrbill import QRBill
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg

from .config import Config, ConfigError, Person


def structured_address(person: Person) -> dict[str, str]:
    """Split ["Strasse 12a", "8000 Zürich"] into the structured QR-bill fields."""
    if len(person.address) != 2:
        raise ConfigError(f"Address of {person.name} must be [street + number, postcode + town]")
    street_line, town_line = person.address
    street = re.fullmatch(r"(.+?)\s+(\d+\s?[a-zA-Z]?)", street_line.strip())
    town = re.fullmatch(r"(\d{4})\s+(.+)", town_line.strip())
    if not town:
        raise ConfigError(f"Cannot read postcode and town from {town_line!r}")
    return {
        "name": person.name,
        "street": street[1] if street else street_line.strip(),
        "house_num": street[2] if street else "",
        "pcode": town[1],
        "city": town[2],
        "country": "CH",
    }


def build_qr_bill(cfg: Config, amount: Decimal, message: str) -> QRBill:
    return QRBill(
        account=cfg.iban,
        creditor=structured_address(cfg.employee),
        debtor=structured_address(cfg.employer),
        amount=f"{amount:.2f}",
        additional_information=message,
        language="de",
    )


def write_qr_bill(cfg: Config, amount: Decimal, message: str, output: Path) -> None:
    bill = build_qr_bill(cfg, amount, message)
    svg = io.StringIO()
    bill.as_svg(svg, full_page=True)
    drawing = svg2rlg(io.BytesIO(svg.getvalue().encode()))
    output.parent.mkdir(parents=True, exist_ok=True)
    renderPDF.drawToFile(drawing, str(output))
