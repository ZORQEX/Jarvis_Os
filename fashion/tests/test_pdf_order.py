"""Factory PDFs (proformas) create orders like Excel files: items, photos, sizes, prices, customers and the factory's
percentage; a scan without a table stays a reference document; the factory's stated total is checked.
Synthetic proforma only (factory_pdf_fixture), no network."""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas
from sqlalchemy import func, select

import factory_fixture
import factory_pdf_fixture
from test_copilot import client, wait  # noqa: F401
from test_factory_settlement import db  # noqa: F401
from app import factory_import as fi, pdf_order
from app.models import OrderItem

KNOWN = {"AA", "IDS", "SS", "ALINA"}


def summary(analysis):
    return [(i.article, i.quantity, i.unit_price, i.sizes, [(u["customer"], u["size"]) for u in i.units], i.confident)
            for s in analysis.sheets for i in s.items]


def test_a_proforma_reads_like_the_excel_file(tmp_path):
    pdf = factory_pdf_fixture.build(tmp_path / "Dior cruise SS27 proforma.pdf")
    meta = pdf_order.pdf_to_workbook(pdf, tmp_path / "from-pdf.xlsx")
    assert meta["rows"] == len(factory_fixture.ROWS) and meta["photos"] == len(factory_fixture.ROWS)
    assert meta["warnings"] == []                                   # the stated total matches the rows
    from_pdf = fi.analyze_workbook(tmp_path / "from-pdf.xlsx", pdf.name, KNOWN)
    from_xlsx = fi.analyze_workbook(factory_fixture.build(tmp_path / "f.xlsx"), "f.xlsx", KNOWN)
    assert summary(from_pdf) == summary(from_xlsx)                  # the same items, allocations and doubts
    sheet = next(s for s in from_pdf.sheets if s.kind == "order")
    assert sheet.brand.startswith("Dior cruise") and [x.rate for x in sheet.sections] == ["0.3"]
    assert D(sheet.computed_base) == D(factory_fixture.BASE_TOTAL) and from_pdf.season == "SS27"
    assert all(i.image_part for i in sheet.items)


def test_upload_review_and_save_with_photos(client, db, tmp_path):  # noqa: F811
    pdf = factory_pdf_fixture.build(tmp_path / "Dior cruise SS27 proforma.pdf")
    with pdf.open("rb") as fh:
        r = client.post("/factory-import", files={"files": (pdf.name, fh, "application/pdf")},
                        headers={"accept": "application/json"})
    token = r.json()["tokens"][0]
    assert wait(client, token) == "ready"
    status = client.get(f"/factory-import/{token}/status").json()
    assert status["items"] == 6 and status["unclear"] == 1
    page = client.get(f"/factory-import/{token}").text
    assert "TEST-COAT-01" in page and "Прочитано из PDF" in page
    part = next(p for p in __import__("re").findall(r"image\?part=([^\"&]+)", page))
    assert client.get(f"/factory-import/{token}/image", params={"part": part}).headers["content-type"] == "image/png"
    r = client.post(f"/factory-import/{token}/commit", data={"season": "SS27", "rates_confirmed": "on"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert db.scalar(select(func.count(OrderItem.id))) >= 6
    assert db.scalar(select(func.count(OrderItem.id)).where(OrderItem.image_path.is_not(None))) >= 6


def test_a_scan_without_a_table_stays_a_reference(client, tmp_path):  # noqa: F811
    path = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(72, 750, "Order confirmation 118 - goods will be ready in March")
    c.save()
    with path.open("rb") as fh:
        token = client.post("/factory-import", files={"files": (path.name, fh, "application/pdf")},
                            headers={"accept": "application/json"}).json()["tokens"][0]
    assert wait(client, token) == "reference"
    assert "не нашлось таблицы" in client.get(f"/factory-import/{token}/status").json()["message"]


def test_the_factorys_own_total_is_checked(tmp_path, monkeypatch):
    pdf = factory_pdf_fixture.build(tmp_path / "p.pdf")
    groups = pdf_order.read_pdf(pdf)
    groups[0]["tail"][0] = "Total 8 000,00"                          # the factory's sum is 100 less than its rows
    warnings = pdf_order.stated_totals(groups)
    assert warnings and "8000" in warnings[0] and "8100" in warnings[0]


@pytest.mark.parametrize("text,value", [("2 000,00", D("2000.00")), ("2,000.00", D("2000.00")), ("€ 1.500", D("1500")),
                                        ("1.250,50", D("1250.50")), ("0,3", D("0.3")), ("38, 40", None), ("UNI", None),
                                        ("711R07", None)])
def test_numbers_as_factories_write_them(text, value):
    assert pdf_order.number(text) == value
