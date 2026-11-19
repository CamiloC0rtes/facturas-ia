"""Lectura del XML de la factura electrónica DIAN (UBL 2.1).

El correo de una factura electrónica trae un ZIP con el PDF (representación gráfica)
y un XML `AttachedDocument` que envuelve la `Invoice` firmada. El XML es la fuente
de verdad: no necesita OCR ni IA. Se usa para:

1. Etiquetar automáticamente facturas reales para la evaluación (v0.3).
2. Como fuente principal cuando el XML viene adjunto (v0.4).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from defusedxml import ElementTree as ET

from .models import Campo, FacturaExtraida

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
INVOICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
IVA = "01"  # código DIAN del tributo IVA


def _invoice(xml: bytes):
    raiz = ET.fromstring(xml)
    if raiz.tag == f"{{{INVOICE_NS}}}Invoice":
        return raiz
    # AttachedDocument: la Invoice va como texto (CDATA) dentro de un Description
    for desc in raiz.iterfind(".//cac:Attachment/cac:ExternalReference/cbc:Description", NS):
        if desc.text and "<Invoice" in desc.text:
            return ET.fromstring(desc.text.strip().encode("utf-8"))
    raise ValueError("El XML no contiene una Invoice UBL")


def _txt(nodo, ruta: str) -> str | None:
    el = nodo.find(ruta, NS)
    return el.text.strip() if el is not None and el.text else None


def _nit(party) -> str | None:
    el = party.find(".//cac:PartyTaxScheme/cbc:CompanyID", NS)
    if el is None or not el.text:
        return None
    dv = el.get("schemeID")
    if el.get("schemeName") == "13":  # cédula de ciudadanía: no lleva DV
        return el.text.strip()
    return f"{el.text.strip()}-{dv}" if dv not in (None, "") else el.text.strip()


def leer_factura_xml(xml: bytes) -> FacturaExtraida:
    inv = _invoice(xml)
    total = inv.find("cac:LegalMonetaryTotal", NS)

    iva = Decimal("0")
    for tt in inv.findall("cac:TaxTotal", NS):
        if _txt(tt, ".//cac:TaxCategory/cac:TaxScheme/cbc:ID") == IVA:
            iva += Decimal(_txt(tt, "cbc:TaxAmount") or "0")

    def dec(ruta: str) -> Decimal | None:
        v = _txt(total, ruta)
        return Decimal(v) if v else None

    def fecha(ruta: str) -> date | None:
        v = _txt(inv, ruta)
        if not v or v.startswith("0001"):  # algunos proveedores usan 0001-01-01 como vacío
            return None
        return date.fromisoformat(v[:10])

    ven = fecha("cbc:DueDate") or fecha("cac:PaymentMeans/cbc:PaymentDueDate")
    fuente = "XML DIAN"
    c = lambda v: Campo(valor=v, evidencia=fuente if v is not None else None)  # noqa: E731
    return FacturaExtraida(
        numero=c(_txt(inv, "cbc:ID")),
        nit_emisor=c(_nit(inv.find("cac:AccountingSupplierParty", NS))),
        nit_cliente=c(_nit(inv.find("cac:AccountingCustomerParty", NS))),
        fecha_emision=c(fecha("cbc:IssueDate")),
        fecha_vencimiento=c(ven),
        # TaxExclusiveAmount = base gravable; LineExtensionAmount = suma de líneas
        subtotal=c(dec("cbc:LineExtensionAmount")),
        iva=c(iva),
        total=c(dec("cbc:PayableAmount")),
    )


def cufe(xml: bytes) -> str | None:
    return _txt(_invoice(xml), "cbc:UUID")
