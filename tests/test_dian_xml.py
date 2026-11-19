"""XML DIAN mínimo (ficticio) con la misma estructura que los reales."""
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape

from facturas.dian_xml import cufe, leer_factura_xml

INVOICE = """<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>FE1024</cbc:ID><cbc:UUID>abc123</cbc:UUID>
 <cbc:IssueDate>2026-11-03</cbc:IssueDate><cbc:DueDate>0001-01-01</cbc:DueDate>
 <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme>
  <cbc:CompanyID schemeID="8" schemeName="31">900123456</cbc:CompanyID>
 </cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>
 <cac:AccountingCustomerParty><cac:Party><cac:PartyTaxScheme>
  <cbc:CompanyID schemeID="0" schemeName="13">1012345678</cbc:CompanyID>
 </cac:PartyTaxScheme></cac:Party></cac:AccountingCustomerParty>
 <cac:TaxTotal><cbc:TaxAmount>190000.00</cbc:TaxAmount><cac:TaxSubtotal><cac:TaxCategory>
  <cac:TaxScheme><cbc:ID>01</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>
 <cac:LegalMonetaryTotal><cbc:LineExtensionAmount>1000000.00</cbc:LineExtensionAmount>
  <cbc:PayableAmount>1190000.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
</Invoice>"""

ATTACHED = f"""<AttachedDocument xmlns="urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cac:Attachment><cac:ExternalReference><cbc:Description>{escape(INVOICE)}</cbc:Description>
 </cac:ExternalReference></cac:Attachment></AttachedDocument>""".encode()


def test_lee_attached_document():
    f = leer_factura_xml(ATTACHED)
    assert f.numero.valor == "FE1024"
    assert f.nit_emisor.valor == "900123456-8"
    assert f.nit_cliente.valor == "1012345678"  # cédula: sin DV
    assert f.fecha_emision.valor == date(2026, 11, 3)
    assert f.fecha_vencimiento.valor is None  # 0001-01-01 = vacío
    assert f.subtotal.valor == Decimal("1000000.00")
    assert f.iva.valor == Decimal("190000.00")
    assert f.total.valor == Decimal("1190000.00")
    assert cufe(ATTACHED) == "abc123"


def test_comparacion_normalizada_eval():
    from facturas.eval import igual

    assert igual("numero", "AD116303005", "AD11-6303005")
    assert igual("nit_emisor", "900518732-2", "900.518.732-2")
    assert igual("nit_cliente", "1000491862", "1000491862-9")
    assert igual("total", Decimal("750500.00"), Decimal("750500"))
    assert igual("iva", Decimal("0"), None)
    assert not igual("total", Decimal("750500"), Decimal("705500"))


def test_eval_sobre_carpeta(tmp_path):
    """Caso completo: PDF generado + XML equivalente en una carpeta."""
    import generar_demo
    from facturas.eval import evaluar, resumen
    from facturas.pipeline import Pipeline

    caso = tmp_path / "f01"
    caso.mkdir()
    (caso / "f.pdf").write_bytes(generar_demo.factura_pdf())
    xml = ATTACHED.replace(b"1012345678", b"800987654").replace(b'schemeName="13"', b'schemeName="31"')
    xml = xml.replace(b'schemeID="0"', b'schemeID="4"')
    (caso / "f.xml").write_bytes(xml)
    filas = evaluar(tmp_path, lambda: Pipeline(hoy=date(2026, 11, 18)))
    r = resumen(filas)
    assert r["facturas"] == 1
    assert r["exactitud_por_campo"]["total"] == 1.0
    assert r["exactitud_por_campo"]["numero"] == 1.0  # FE-1024 == FE1024 normalizado
    assert r["falsos_aprobados"] == 0


def test_eval_reusar_extracciones(tmp_path):
    import json

    import generar_demo
    from facturas.eval import cargar_cache, evaluar
    from facturas.pipeline import Pipeline

    caso = tmp_path / "datos" / "f01"
    caso.mkdir(parents=True)
    (caso / "f.pdf").write_bytes(generar_demo.factura_pdf())
    (caso / "f.xml").write_bytes(ATTACHED)
    filas = evaluar(tmp_path / "datos", lambda: Pipeline(hoy=date(2026, 11, 18)))
    ruta = tmp_path / "r.json"
    ruta.write_text(json.dumps({"resumen": {"config": {"modelo": "x"}},
                                "casos": [{"caso": f.caso, "extraccion": f.extraccion} for f in filas]}))
    cache, cfg = cargar_cache(ruta)

    def no_llamar():
        raise AssertionError("no debería crear un extractor nuevo")

    filas2 = evaluar(tmp_path / "datos", no_llamar, cache, cfg)
    assert filas2[0].correctos == filas[0].correctos
