"""Swiss QR-bill (Einzahlungsschein mit QR-Code) for the wage transfer.

`qrbill` validates the data and encodes the QR payload; the A4 PDF with the
payment part is drawn directly with pypdf, so no imaging libraries (Pillow,
lxml, reportlab) are needed.
"""

import re
from decimal import Decimal
from pathlib import Path

import qrcode
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from qrbill import QRBill
from qrbill.bill import format_amount, wrap_infos
from stdnum import iban

from .config import Config, ConfigError, Person

PT_PER_MM = 72 / 25.4
A4_WIDTH, A4_HEIGHT = 210, 297
BILL_HEIGHT = 105
RECEIPT_WIDTH = 62
MARGIN = 5
PAYMENT_LEFT = RECEIPT_WIDTH + MARGIN
DETAILS_LEFT = PAYMENT_LEFT + 46 + 5
QR_TOP, QR_SIZE = 17, 46
AMOUNT_TOP = 72

REGULAR, BOLD = "/F1", "/F2"
# "Annahmestelle" in Helvetica-Bold is 7.113 em wide; needed to right-align it.
ACCEPTANCE_POINT_WIDTH = 7.113 * 6 / PT_PER_MM


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


class _Canvas:
    """Minimal PDF content stream; coordinates in mm from the top of the bill."""

    def __init__(self) -> None:
        self.ops: list[bytes] = []

    @staticmethod
    def _xy(x: float, y: float) -> str:
        return f"{x * PT_PER_MM:.2f} {(BILL_HEIGHT - y) * PT_PER_MM:.2f}"

    def text(self, x: float, y: float, text: str, size: float, font: str = REGULAR) -> None:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.ops.append(
            f"BT {font} {size} Tf {self._xy(x, y)} Td (".encode()
            + escaped.encode("cp1252", errors="replace")
            + b") Tj ET"
        )

    def rect(self, x: float, y: float, w: float, h: float, gray: int = 0) -> None:
        self.ops.append(
            f"{gray} g {self._xy(x, y + h)} {w * PT_PER_MM:.3f} {h * PT_PER_MM:.3f} re f".encode()
        )

    def dashed_line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.ops.append(
            f"[2 2] 0 d 0.5 w {self._xy(x1, y1)} m {self._xy(x2, y2)} l S [] 0 d".encode()
        )

    def qr_code(self, x: float, y: float, size: float, data: str) -> None:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
        qr.add_data(data)
        matrix = qr.get_matrix()
        module = size / len(matrix)
        for row, cells in enumerate(matrix):
            col = 0
            while col < len(cells):  # draw runs of dark modules as one rectangle
                if cells[col]:
                    start = col
                    while col < len(cells) and cells[col]:
                        col += 1
                    self.rect(x + start * module, y + row * module, (col - start) * module, module)
                else:
                    col += 1
        # Swiss cross, 7 mm, in the centre (white margin, black square, white cross)
        u = 7 / 20
        cx, cy = x + size / 2 - 3.5, y + size / 2 - 3.5
        self.rect(cx, cy, 7, 7, gray=1)
        self.rect(cx + 0.7 * u, cy + 0.7 * u, 18.4 * u, 18.4 * u)
        self.rect(cx + 8.3 * u, cy + 4 * u, 3.3 * u, 11 * u, gray=1)
        self.rect(cx + 4.4 * u, cy + 7.9 * u, 11 * u, 3.3 * u, gray=1)
        self.ops.append(b"0 g")


def _draw_bill(bill: QRBill) -> _Canvas:
    c = _Canvas()
    c.dashed_line(0, 0, A4_WIDTH, 0)
    c.dashed_line(RECEIPT_WIDTH, 0, RECEIPT_WIDTH, BILL_HEIGHT)
    account = iban.format(bill.account)

    # Receipt: headings 6 pt, values 8 pt
    c.text(MARGIN, 10, bill.label("Receipt"), 11, BOLD)
    y = 15.0
    for heading, lines in [
        ("Account / Payable to", [account, *bill.creditor.as_paragraph(max_chars=38)]),
        ("Payable by", bill.debtor.as_paragraph(max_chars=38)),
    ]:
        c.text(MARGIN, y, bill.label(heading), 6, BOLD)
        for line in lines:
            y += 3.2
            c.text(MARGIN, y, line, 8)
        y += 4.5
    c.text(MARGIN, AMOUNT_TOP, bill.label("Currency"), 6, BOLD)
    c.text(MARGIN + 12, AMOUNT_TOP, bill.label("Amount"), 6, BOLD)
    c.text(MARGIN, AMOUNT_TOP + 4, bill.currency, 8)
    c.text(MARGIN + 12, AMOUNT_TOP + 4, format_amount(bill.amount), 8)
    acceptance_x = RECEIPT_WIDTH - MARGIN - ACCEPTANCE_POINT_WIDTH
    c.text(acceptance_x, 86, bill.label("Acceptance point"), 6, BOLD)

    # Payment part: headings 8 pt, values 10 pt
    c.text(PAYMENT_LEFT, 10, bill.label("Payment part"), 11, BOLD)
    c.qr_code(PAYMENT_LEFT, QR_TOP, QR_SIZE, bill.qr_data())
    c.text(PAYMENT_LEFT, AMOUNT_TOP, bill.label("Currency"), 8, BOLD)
    c.text(PAYMENT_LEFT + 15, AMOUNT_TOP, bill.label("Amount"), 8, BOLD)
    c.text(PAYMENT_LEFT, AMOUNT_TOP + 5, bill.currency, 10)
    c.text(PAYMENT_LEFT + 15, AMOUNT_TOP + 5, format_amount(bill.amount), 10)
    y = 10.0
    for heading, lines in [
        ("Account / Payable to", [account, *bill.creditor.as_paragraph()]),
        ("Additional information", wrap_infos([bill.additional_information])),
        ("Payable by", bill.debtor.as_paragraph()),
    ]:
        c.text(DETAILS_LEFT, y, bill.label(heading), 8, BOLD)
        for line in lines:
            y += 4
            c.text(DETAILS_LEFT, y, line, 10)
        y += 6
    return c


def write_qr_bill(cfg: Config, amount: Decimal, message: str, output: Path) -> None:
    """Write an A4 PDF with the QR-bill payment part at the bottom."""
    bill = build_qr_bill(cfg, amount, message)
    writer = PdfWriter()
    page = writer.add_blank_page(A4_WIDTH * PT_PER_MM, A4_HEIGHT * PT_PER_MM)
    fonts = DictionaryObject()
    for name, base in [(REGULAR, "/Helvetica"), (BOLD, "/Helvetica-Bold")]:
        fonts[NameObject(name)] = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject(base),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    content = DecodedStreamObject()
    content.set_data(b"\n".join(_draw_bill(bill).ops))
    page.replace_contents(content)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as f:
        writer.write(f)
