from datetime import date
from decimal import Decimal

import pytest

from facturas.extractores import parse_monto
from facturas.models import Campo, FacturaExtraida
from facturas.validacion import (
    comparar,
    digito_verificacion,
    nit_valido,
    verificar_grounding,
    verificar_reglas,
)

TEXTO = """NIT Emisor: 900.123.456-8
Factura electrónica de venta No. FE-1024
Fecha de emisión: 2026-11-03
NIT Cliente: 800.987.654-4
Subtotal: $ 1.000.000
IVA 19%: $ 190.000
Total a pagar: $ 1.190.000"""


def factura(**cambios) -> FacturaExtraida:
    base = dict(
        numero=Campo(valor="FE-1024", evidencia="No. FE-1024"),
        nit_emisor=Campo(valor="900.123.456-8", evidencia="NIT Emisor: 900.123.456-8"),
        nit_cliente=Campo(valor="800.987.654-4", evidencia="NIT Cliente: 800.987.654-4"),
        fecha_emision=Campo(valor=date(2026, 11, 3), evidencia="Fecha de emisión: 2026-11-03"),
        subtotal=Campo(valor=Decimal("1000000"), evidencia="Subtotal: $ 1.000.000"),
        iva=Campo(valor=Decimal("190000"), evidencia="IVA 19%: $ 190.000"),
        total=Campo(valor=Decimal("1190000"), evidencia="Total a pagar: $ 1.190.000"),
    )
    base.update(cambios)
    return FacturaExtraida(**base)


HOY = date(2026, 11, 18)


def test_digito_verificacion_dian():
    assert digito_verificacion("900123456") == 8
    assert nit_valido("800.987.654-4")
    assert not nit_valido("800.987.654-1")


def test_factura_correcta_sin_errores():
    f = factura()
    assert verificar_grounding(f, TEXTO) == []
    errores = [h for h in verificar_reglas(f, hoy=HOY) if h.severidad.value == "error"]
    assert errores == []


def test_detecta_total_alucinado_por_evidencia_inexistente():
    """El LLM 'lee' 1.290.000 y se inventa la evidencia: el grounding lo atrapa."""
    f = factura(total=Campo(valor=Decimal("1290000"), evidencia="Total a pagar: $ 1.290.000"))
    reglas = {h.regla for h in verificar_grounding(f, TEXTO) + verificar_reglas(f, hoy=HOY)}
    assert "evidencia_inexistente" in reglas
    assert "suma_total" in reglas


def test_valor_sin_evidencia_es_error():
    f = factura(numero=Campo(valor="FE-9999", evidencia=None))
    assert any(h.regla == "sin_evidencia" for h in verificar_grounding(f, TEXTO))


def test_campo_ilegible_va_a_revision():
    f = factura(total=Campo())
    assert any(h.regla == "campo_faltante" for h in verificar_grounding(f, TEXTO))


def test_iva_imposible_es_error_y_mixto_es_advertencia():
    imposible = factura(iva=Campo(valor=Decimal("250000"), evidencia="x"),
                        total=Campo(valor=Decimal("1250000"), evidencia="x"))
    assert any(h.regla == "tarifa_iva" for h in verificar_reglas(imposible, hoy=HOY))
    # facturas reales con ítems gravados y excluidos (p. ej. cine): IVA "raro" pero válido
    mixto = factura(iva=Campo(valor=Decimal("510.92"), evidencia="x"),
                    subtotal=Campo(valor=Decimal("52689.08"), evidencia="x"),
                    total=Campo(valor=Decimal("53200"), evidencia="x"))
    h = verificar_reglas(mixto, hoy=HOY)
    assert [x.regla for x in h if x.severidad.value == "error"] == []
    assert any(x.regla == "tarifa_iva_mixta" for x in h)


def test_factura_sin_iva():
    f = factura(iva=Campo(), subtotal=Campo(valor=Decimal("114559"), evidencia="x"),
                total=Campo(valor=Decimal("114559"), evidencia="x"))
    assert [x for x in verificar_reglas(f, hoy=HOY) if x.severidad.value == "error"] == []


def test_cedula_sin_dv_es_valida():
    assert nit_valido("1000491862")


def test_nit_con_dv_incorrecto():
    f = factura(nit_cliente=Campo(valor="800.987.654-1", evidencia="x"))
    assert any(h.regla == "dv_nit" for h in verificar_reglas(f, hoy=HOY))


def test_fecha_futura():
    f = factura(fecha_emision=Campo(valor=date(2027, 1, 1), evidencia="x"))
    assert any(h.regla == "fecha_futura" for h in verificar_reglas(f, hoy=HOY))


def test_consenso_detecta_desacuerdo():
    a = factura()
    b = factura(total=Campo(valor=Decimal("1190001"), evidencia="x"))
    assert [h.regla for h in comparar(a, b)] == ["sin_consenso"]


@pytest.mark.parametrize("texto,esperado", [
    ("$ 1.190.000", "1190000"),
    ("630.672,27", "630672.27"),
    ("750,500.00", "750500.00"),
    ("114,559", "114559"),
    ("52689.08", "52689.08"),
    ("18739,50", "18739.50"),
    ("$369950", "369950"),
])
def test_parse_monto_formatos_reales(texto, esperado):
    assert parse_monto(texto) == Decimal(esperado)
