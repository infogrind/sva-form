"""Filling and reading SVA Zürich PDF forms (payslip and Lohndeklaration)."""

from importlib.resources import files
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject

TEMPLATE = files("sva_form") / "data" / "template.pdf"

# Grey "Hier ... eintragen" hints; the form's own scripts hide them once filled.
HINT_FIELDS = ["hintergrund", "hintergrund01", "hintergrund05", "hintergrund06", "hintergrund_001"]

PRINT_FLAG = 4
HIDDEN_FLAG = 2


def _field_name(annot: DictionaryObject) -> str | None:
    """Fully qualified field name of a widget, e.g. "AHVnr.0.0"."""
    parts = []
    node: DictionaryObject | None = annot
    while node is not None:
        if "/T" in node:
            parts.append(str(node["/T"]))
        node = node["/Parent"].get_object() if "/Parent" in node else None
    return ".".join(reversed(parts)) or None


def fill_form(values: dict[str, str], output: Path, template: Path | None = None) -> None:
    """Fill the form and make filled fields printable on a white background.

    The form's JavaScript does this in Acrobat when a value is typed in; it
    does not run when fields are filled programmatically.
    """
    writer = PdfWriter(clone_from=str(template or TEMPLATE))
    for page in writer.pages:
        for ref in page.get("/Annots", []):
            annot = ref.get_object()
            if annot.get("/Subtype") != "/Widget":
                continue
            name = _field_name(annot)
            if name in HINT_FIELDS:
                annot[NameObject("/F")] = NumberObject(HIDDEN_FLAG)
            elif values.get(name):
                annot[NameObject("/F")] = NumberObject(PRINT_FLAG)
                mk = annot.setdefault(NameObject("/MK"), DictionaryObject())
                mk[NameObject("/BG")] = ArrayObject([FloatObject(1)])
        writer.update_page_form_field_values(page, values, auto_regenerate=False)
    writer.set_need_appearances_writer(True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as f:
        writer.write(f)


def read_form(path: Path) -> dict[str, str]:
    """Field values of a filled form (empty fields are omitted)."""
    fields = PdfReader(path).get_fields() or {}
    return {name: str(f["/V"]) for name, f in fields.items() if f.get("/V") not in (None, "")}
