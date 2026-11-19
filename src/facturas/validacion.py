"""Validaciones determinísticas. Aquí es donde se atrapan las alucinaciones."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from rapidfuzz import fuzz

from .models import FacturaExtraida, Hallazgo, Severidad

TOLERANCIA = Decimal("1.00")  # pesos, por redondeos
TARIFAS_IVA = (Decimal("0.19"), Decimal("0.05"), Decimal("0"))
UMBRAL_GROUNDING = 90

_PESOS_DIAN = [3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71]


def digito_verificacion(nit: str) -> int:
    """Dígito de verificación del NIT según el algoritmo de la DIAN."""
    digitos = re.sub(r"\D", "", nit)
    total = sum(int(d) * p for d, p in zip(reversed(digitos), _PESOS_DIAN))
    r = total % 11
    return r if r in (0, 1) else 11 - r


def nit_valido(nit_con_dv: str) -> bool:
    if "-" not in nit_con_dv:
        return False
    base, dv = nit_con_dv.rsplit("-", 1)
    return dv.isdigit() and digito_verificacion(base) == int(dv)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


_NUMERO = re.compile(r"\d[\d.,/-]*\d|\d")


def _numeros_exactos(evidencia: str, fuente: str) -> bool:
    """La coincidencia difusa perdona un dígito cambiado (1.190.000 vs 1.290.000
    da 96%). Por eso cada número de la evidencia debe aparecer EXACTO en la fuente."""
    return all(n in fuente for n in _NUMERO.findall(evidencia))


def _valor_en_evidencia(valor, evidencia: str) -> bool:
    """El valor devuelto debe poder leerse dentro de su propia evidencia."""
    from .extractores import parse_fecha, parse_monto

    tokens = _NUMERO.findall(evidencia)
    if isinstance(valor, Decimal):
        return any(parse_monto(t) == valor for t in tokens)
    if isinstance(valor, date):
        return any(parse_fecha(t) == valor for t in tokens)
    return _norm(str(valor)) in _norm(evidencia)


def verificar_grounding(f: FacturaExtraida, texto: str) -> list[Hallazgo]:
    """Cada evidencia debe existir en el texto fuente y contener el valor."""
    hallazgos = []
    fuente = _norm(texto)
    for nombre in f.CAMPOS_CRITICOS:
        campo = getattr(f, nombre)
        if campo.valor is None:
            hallazgos.append(
                Hallazgo(regla="campo_faltante", campo=nombre, severidad=Severidad.ERROR,
                         mensaje=f"{nombre}: no se pudo leer")
            )
            continue
        if not campo.evidencia:
            hallazgos.append(
                Hallazgo(regla="sin_evidencia", campo=nombre, severidad=Severidad.ERROR,
                         mensaje=f"{nombre}: valor sin evidencia")
            )
            continue
        evidencia = _norm(campo.evidencia)
        score = fuzz.partial_ratio(evidencia, fuente)
        if score < UMBRAL_GROUNDING or not _numeros_exactos(evidencia, fuente):
            hallazgos.append(
                Hallazgo(regla="evidencia_inexistente", campo=nombre, severidad=Severidad.ERROR,
                         mensaje=f"{nombre}: la evidencia no aparece en el documento ({score:.0f}%)")
            )
        elif not _valor_en_evidencia(campo.valor, campo.evidencia):
            hallazgos.append(
                Hallazgo(regla="valor_no_coincide", campo=nombre, severidad=Severidad.ERROR,
                         mensaje=f"{nombre}: el valor '{campo.valor}' no está en su evidencia")
            )
    return hallazgos


def verificar_reglas(f: FacturaExtraida, hoy: date | None = None) -> list[Hallazgo]:
    hoy = hoy or date.today()
    h: list[Hallazgo] = []
    def err(regla: str, msg: str, campo: str | None = None) -> None:
        h.append(Hallazgo(regla=regla, campo=campo, severidad=Severidad.ERROR, mensaje=msg))

    def warn(regla: str, msg: str, campo: str | None = None) -> None:
        h.append(Hallazgo(regla=regla, campo=campo, severidad=Severidad.ADVERTENCIA, mensaje=msg))

    sub, iva, tot = f.subtotal.valor, f.iva.valor, f.total.valor
    if None not in (sub, iva, tot):
        if abs(sub + iva - tot) > TOLERANCIA:
            err("suma_total", f"subtotal + IVA ({sub + iva}) != total ({tot})", "total")
        if sub > 0 and not any(abs(sub * t - iva) <= TOLERANCIA for t in TARIFAS_IVA):
            err("tarifa_iva", f"IVA {iva} no corresponde a 19%, 5% ni 0% de {sub}", "iva")

    if f.lineas and sub is not None:
        suma = sum(l.total for l in f.lineas)
        if abs(suma - sub) > TOLERANCIA:
            err("suma_lineas", f"suma de líneas ({suma}) != subtotal ({sub})", "subtotal")

    for nombre in ("nit_emisor", "nit_cliente"):
        nit = getattr(f, nombre).valor
        if nit and not nit_valido(nit):
            err("dv_nit", f"{nombre} {nit}: dígito de verificación inválido", nombre)

    emi, ven = f.fecha_emision.valor, f.fecha_vencimiento.valor
    if emi and emi > hoy:
        err("fecha_futura", f"fecha de emisión {emi} está en el futuro", "fecha_emision")
    if emi and ven and ven < emi:
        err("vencimiento", "vencimiento anterior a la emisión", "fecha_vencimiento")
    if ven is None:
        warn("sin_vencimiento", "sin fecha de vencimiento: se usará el plazo por defecto",
             "fecha_vencimiento")
    return h


def comparar(a: FacturaExtraida, b: FacturaExtraida) -> list[Hallazgo]:
    """Consenso: dos extracciones independientes deben coincidir en lo crítico."""
    h = []
    for nombre in a.CAMPOS_CRITICOS:
        va, vb = getattr(a, nombre).valor, getattr(b, nombre).valor
        if va is not None and vb is not None and va != vb:
            h.append(Hallazgo(regla="sin_consenso", campo=nombre, severidad=Severidad.ERROR,
                              mensaje=f"{nombre}: '{va}' vs '{vb}'"))
    return h
