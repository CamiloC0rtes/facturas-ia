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
    emisor = factura(nit_emisor=Campo(valor="900.123.456-1", evidencia="x"))
    assert any(h.regla == "dv_nit" for h in verificar_reglas(emisor, hoy=HOY))
    # DV del cliente mal impreso por el emisor: advertencia, no bloquea
    cliente = factura(nit_cliente=Campo(valor="800.987.654-1", evidencia="x"))
    h = verificar_reglas(cliente, hoy=HOY)
    assert not [x for x in h if x.severidad.value == "error"]
    assert any(x.regla == "dv_nit_cliente" for x in h)


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


# ------------------------------------------------- reglas de rol (casos reales v0.3)
from facturas.validacion import verificar_roles  # noqa: E402

TEXTO_REAL = """STARK GYM S.A.S. NIT 900965267-5
Cliente: JUAN CORTES  C.C. 1000491862
Pedido ACO9AA27665-1 3203653336
Resolución de facturación No. 18764113926324 del 2026-01-01
Factura de venta FEH119328
Software: Alegra - NIT 900.559.088-2"""


def roles(**cambios):
    return {h.regla for h in verificar_roles(factura(**cambios), TEXTO_REAL)}


def test_nit_del_software_como_emisor():
    assert "nit_proveedor_tecnologico" in roles(nit_emisor=Campo(valor="900.559.088-2", evidencia="x"))


def test_celular_como_nit_cliente():
    assert "nit_es_celular" in roles(nit_cliente=Campo(valor="3203653336", evidencia="x"))


def test_emisor_igual_cliente():
    assert "emisor_igual_cliente" in roles(nit_emisor=Campo(valor="900965267-5", evidencia="x"),
                                           nit_cliente=Campo(valor="900965267-5", evidencia="x"))


def test_resolucion_como_numero():
    assert "numero_es_resolucion" in roles(numero=Campo(valor="18764113926324", evidencia="x"))


def test_roles_correctos_sin_errores():
    assert roles(nit_emisor=Campo(valor="900965267-5", evidencia="x"),
                 nit_cliente=Campo(valor="1000491862", evidencia="x"),
                 numero=Campo(valor="FEH119328", evidencia="x")) == set()


def test_telefono_antes_del_rotulo_nit_no_es_falsa_alarma():
    texto = "COMODIN S.A.S TELÉFONO: 744 7285 Nit: 800069933-6"
    f = factura(nit_emisor=Campo(valor="800069933-6", evidencia="x"))
    assert not [h for h in verificar_roles(f, texto) if h.campo == "nit_emisor"]


def test_normalizar_nit():
    from facturas.extractores import normalizar_nit

    assert normalizar_nit("800069933 6") == "800069933-6"
    assert normalizar_nit("900.123.456 - 8") == "900.123.456-8"
    assert normalizar_nit("1000491862") == "1000491862"


# ------------------------------------- variantes de formato vistas con gpt-4.1-mini (v0.3)
TEXTO_TABLA = """NIT: 800069933 6
Fecha: 16-ago-2026
IVA
Base   Tarifa   Valor
420.000,00   19%   79.800,00
TOTAL
499.800,00"""


def test_formatos_reales_no_bloquean():
    f = factura(
        nit_emisor=Campo(valor="800069933-6", evidencia="NIT: 800069933 6"),
        fecha_emision=Campo(valor=date(2026, 8, 16), evidencia="Fecha: 16-ago-2026"),
        iva=Campo(valor=Decimal("79800"), evidencia="IVA 19% 79.800,00"),  # rótulo unido de tabla
        total=Campo(valor=Decimal("499800"), evidencia="TOTAL 499.800,00"),
    )
    regl = {h.regla + ":" + (h.campo or "") for h in verificar_grounding(f, TEXTO_TABLA)}
    for campo in ("nit_emisor", "fecha_emision", "iva", "total"):
        assert not any(r.endswith(":" + campo) for r in regl), regl


def test_tabla_con_numero_alucinado_sigue_bloqueando():
    f = factura(total=Campo(valor=Decimal("498800"), evidencia="TOTAL 498.800,00"))
    assert any(h.regla == "evidencia_inexistente" and h.campo == "total"
               for h in verificar_grounding(f, TEXTO_TABLA))


def test_monto_redondeado_en_pdf_no_bloquea():
    texto = "Subtotal $ 205.529"
    f = factura(subtotal=Campo(valor=Decimal("205529.41"), evidencia="Subtotal $ 205.529"))
    assert not [h for h in verificar_grounding(f, texto) if h.campo == "subtotal"]


def test_total_con_descuento_inc_y_propina():
    # restaurante: subtotal + INC + propina; tienda: subtotal - descuento global
    rest = factura(iva=Campo(), subtotal=Campo(valor=Decimal("141204"), evidencia="x"),
                   otros_impuestos=Campo(valor=Decimal("11296"), evidencia="x"),
                   cargos=Campo(valor=Decimal("14100"), evidencia="x"),
                   total=Campo(valor=Decimal("166600"), evidencia="x"))
    tienda = factura(iva=Campo(), subtotal=Campo(valor=Decimal("64759"), evidencia="x"),
                     descuentos=Campo(valor=Decimal("40706"), evidencia="x"),
                     total=Campo(valor=Decimal("24053"), evidencia="x"))
    redondeo = factura(subtotal=Campo(valor=Decimal("94023"), evidencia="x"),
                       iva=Campo(valor=Decimal("17864.37"), evidencia="x"),
                       total=Campo(valor=Decimal("111890"), evidencia="x"))
    for f in (rest, tienda, redondeo):
        assert not [h for h in verificar_reglas(f, hoy=HOY) if h.regla == "suma_total"]
    # sin declarar el descuento, sí debe bloquear
    sin = factura(iva=Campo(), subtotal=Campo(valor=Decimal("64759"), evidencia="x"),
                  total=Campo(valor=Decimal("24053"), evidencia="x"))
    assert any(h.regla == "suma_total" for h in verificar_reglas(sin, hoy=HOY))


# ------------------------------------------ fechas ambiguas y rótulos (casos reales, 54 facturas)
from facturas.validacion import verificar_fechas, verificar_rotulos  # noqa: E402

TEXTO_PUMA = ("Fecha de Emisión 5/10/2026 4:47:53 PM Fecha de Validación 5/10/2026 4:47:54 PM "
              "Fecha de Vencimiento 1/1/0001 12:00:00 AM")


def test_fecha_formato_estadounidense_mal_leida():
    mal = factura(fecha_emision=Campo(valor=date(2026, 10, 5), evidencia="Fecha de Emisión 5/10/2026 4:47:53 PM"))
    bien = factura(fecha_emision=Campo(valor=date(2026, 5, 10), evidencia="Fecha de Emisión 5/10/2026 4:47:53 PM"))
    assert [h.regla for h in verificar_fechas(mal, TEXTO_PUMA)] == ["fecha_mal_interpretada"]
    assert verificar_fechas(bien, TEXTO_PUMA) == []


def test_fecha_ambigua_asume_dia_mes():
    f = factura(fecha_emision=Campo(valor=date(2026, 11, 3), evidencia="Fecha: 03/11/2026"))
    h = verificar_fechas(f, "Fecha: 03/11/2026")
    assert [(x.regla, x.severidad.value) for x in h] == [("fecha_ambigua", "advertencia")]
    texto_dm = "Fecha: 03/11/2026  Vence: 25/11/2026"  # 25/11 prueba que es día/mes
    assert verificar_fechas(f, texto_dm) == []
    mal = factura(fecha_emision=Campo(valor=date(2026, 3, 11), evidencia="Fecha: 03/11/2026"))
    assert [x.regla for x in verificar_fechas(mal, texto_dm)] == ["fecha_mal_interpretada"]


def test_p_m_en_espanol_es_dia_mes():
    texto = "Fecha: 04/05/2022  07:28:37p. m."
    f = factura(fecha_emision=Campo(valor=date(2022, 5, 4), evidencia="04/05/2022  07:28:37p. m."))
    assert [x for x in verificar_fechas(f, texto) if x.severidad.value == "error"] == []


def test_iva_disfrazado_de_descuento():
    f = factura(descuentos=Campo(valor=Decimal("54380"), evidencia="IVA $ 54.380,00"))
    assert [h.regla for h in verificar_rotulos(f)] == ["rotulo_incoherente"]
    ok = factura(descuentos=Campo(valor=Decimal("20994"), evidencia="Descuento 20.994,00"))
    assert verificar_rotulos(ok) == []


def test_encabezados_de_tabla_mezclados_son_ambiguos():
    ev = "CANT. DESCRIPCIÓN DESCUENTOS UNID. PRECIO VTA. TOTAL PORCENTAJE IMPUESTO ITEM 95.782,00"
    f = factura(descuentos=Campo(valor=Decimal("95782"), evidencia=ev))
    assert [h.regla for h in verificar_rotulos(f)] == ["rotulo_ambiguo"]
