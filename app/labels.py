"""Generate a small PDF label for a batch.

A single make_label() function keeps the rest of the app independent of the
label engine. It uses fpdf2 (pure Python, no system packages). The label is a
three-field table (type, batch, expiring date) and the function returns the
path to the written PDF.

Callers generate labels after the stock transaction commits.
"""

import os

from fpdf import FPDF


TYPE_IT = {"microbiology": "microbiologia", "primers": "primer", "other": "altro"}


def make_label(batch, labels_dir):
    """Write a label PDF for `batch` (a dict-like row) and return its path.

    `batch` must provide: supply_number, type, batch_code, expiring_date,
    product_name. `labels_dir` is created if missing.
    """
    os.makedirs(labels_dir, exist_ok=True)

    pdf = FPDF(orientation="L", unit="mm", format=(60, 90))
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.set_margins(5, 5, 5)

    pdf.set_font("Helvetica", "B", 12)
    title = (batch["product_name"] or "").strip()
    pdf.cell(0, 8, _ascii(title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    rows = [
        ("ID lotto", batch["supply_number"]),
        ("Tipo", TYPE_IT.get(batch["type"], batch["type"])),
        ("Lotto", batch["batch_code"] or "-"),
        ("Scadenza", batch["expiring_date"] or "nessuna"),
    ]
    pdf.set_font("Helvetica", "", 11)
    for label, value in rows:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(28, 8, _ascii(label), border=1)
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 8, _ascii(str(value)), border=1, new_x="LMARGIN", new_y="NEXT")

    path = os.path.join(labels_dir, f"label_{batch['supply_number']}.pdf")
    pdf.output(path)
    return path


def _ascii(text):
    """fpdf2's core fonts are Latin-1 only; drops characters they cannot encode
    so a Unicode glyph does not crash label generation."""
    return text.encode("latin-1", "replace").decode("latin-1")
