"""SYNTHETIC factory proforma PDF for tests — not a real supplier document.

The same products as factory_fixture.ROWS, laid out the way factory proformas usually are: a title line, a ruled
table (photo, article, description, size, quantity, unit price, line total, the customers it is for), a total, the
factory's commission percentage and the grand total. Prices are written the European way («2 000,00»).
"""
from __future__ import annotations

import io
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet

from factory_fixture import ROWS, png


def eu(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def build(path: Path, rate: float = 0.30, rows=ROWS) -> Path:
    styles = getSampleStyleSheet()
    data = [["Photo", "Article", "Description", "Size", "Qty", "Unit price", "Total", "Client"]]
    for n, (name, article, size, qty, price, alloc, _colour) in enumerate(rows):
        photo = Image(io.BytesIO(png((40 * n % 255, 90, 160))), width=12 * mm, height=12 * mm)
        data.append([photo, article, name, str(size), str(qty), eu(price), eu(price * qty), alloc])
    base = sum(qty * price for _, _, _, qty, price, _, _ in rows)
    table = Table(data, colWidths=[18 * mm, 30 * mm, 40 * mm, 20 * mm, 14 * mm, 26 * mm, 28 * mm, 40 * mm],
                  rowHeights=[8 * mm] + [15 * mm] * len(rows))
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    story = [Paragraph("Dior cruise SS27 — Proforma 2026/118", styles["Title"]), Spacer(1, 4 * mm), table,
             Spacer(1, 6 * mm), Paragraph(f"Total {eu(base)}", styles["Normal"]),
             Paragraph(f"Commission {int(rate * 100)}% {eu(base * rate)}", styles["Normal"]),
             Paragraph(f"Grand total {eu(base * (1 + rate))}", styles["Normal"])]
    SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm).build(story)
    return path
