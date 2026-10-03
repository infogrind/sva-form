"""Filling and reading the SVA Zürich "Stundenlohnabrechnung" PDF form."""

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
    if "/T" in annot:
        return annot["/T"]
    if "/Parent" in annot:
        return annot["/Parent"].get_object().get("/T")
    return None


def fill_form(values: dict[str, str], output: Path, template: Path | None = None) -> None:
    """Fill the form and make filled fields printable on a white background.

    The form's JavaScript does this in Acrobat when a value is typed in; it
    does not run when fields are filled programmatically.
    """
    writer = PdfWriter(clone_from=str(template or TEMPLATE))
    page = writer.pages[0]
    for ref in page["/Annots"]:
        annot = ref.get_object()
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
