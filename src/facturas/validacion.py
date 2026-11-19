"""Validaciones determinísticas. Aquí es donde se atrapan las alucinaciones."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from rapidfuzz import fuzz

from .models import FacturaExtraida, Hallazgo, Severidad

TOLERANCIA = Decimal("1.00")  # pesos, por redondeos
TARIFAS_IVA = (Decimal("0.19"), Decimal("0.05"), Decimal("0"))
MAX_TARIFA_IVA = Decimal("0.19")
UMBRAL_GROUNDING = 90

_PESOS_DIAN = [3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71]


def digito_verificacion(nit: str) -> int:
    """Dígito de verificación del NIT según el algoritmo de la DIAN."""
    digitos = re.sub(r"\D", "", nit)
    total = sum(int(d) * p for d, p in zip(reversed(digitos), _PESOS_DIAN))
    r = total % 11
    return r if r in (0, 1) else 11 - r


def nit_valido(nit_con_dv: str) -> bool:
    """Valida el DV si viene. Una cédula (persona natural) no lleva DV: se acepta."""
    if "-" not in nit_con_dv:
        return bool(re.fullmatch(r"[\d.]{5,15}", nit_con_dv))
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
    for nombre in f.CAMPOS:
        campo = getattr(f, nombre)
        if campo.valor is None:
            if nombre not in f.CAMPOS_CRITICOS:
                continue
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
    if sub is not None and tot is not None:
        esperado = sub + (iva or 0)
        if abs(esperado - tot) > TOLERANCIA:
            detalle = f"subtotal + IVA ({esperado})" if iva is not None else f"subtotal ({sub}) sin IVA"
            err("suma_total", f"{detalle} != total ({tot})", "total")
    if sub and iva is not None:
        if iva > sub * MAX_TARIFA_IVA + TOLERANCIA:
            err("tarifa_iva", f"IVA {iva} supera el 19% de {sub}", "iva")
        elif not any(abs(sub * t - iva) <= TOLERANCIA for t in TARIFAS_IVA):
            # posible con ítems mixtos (gravados + excluidos): no bloquea
            warn("tarifa_iva_mixta", f"IVA {iva} no es 19/5/0% exacto de {sub}: ítems mixtos?", "iva")

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


# --------------------------------------------------------------- reglas de rol
# El grounding confirma que el valor EXISTE en el documento, no que esté en el campo
# correcto. Estos errores aparecieron en la evaluación con facturas reales (v0.3):
# el LLM tomó el NIT del software de facturación como emisor y un celular como cliente.

# NITs de proveedores tecnológicos / software de facturación vistos en facturas reales.
NITS_PROVEEDORES_TECNOLOGICOS = {
    "900559088",  # Alegra
    "900390126",  # The Factory HKA
    "900306823",  # F&M Technology
    "901223648",  # Dataico
    "900984424",  # Esdinamico
    "901380553",  # Ecosistemas Digitales
    "900248304",  # Mahalo POS
}
_CONTEXTO_PROVEEDOR = re.compile(r"proveedor\s+tecnol|software|fabricad[oa]\s+por|generad[oa]\s+por", re.I)
_CONTEXTO_TELEFONO = re.compile(r"tel[eé]?f?|cel(ular)?|m[oó]vil|whatsapp|contacto", re.I)
_ETIQUETA_ID = re.compile(r"nit|c\.?\s?c\.?|c[eé]dula|documento|identificaci[oó]n", re.I)
_CONTEXTO_RESOLUCION = re.compile(r"resoluci[oó]n|autorizaci[oó]n|rango|habilitaci[oó]n", re.I)


def _base_nit(nit: str) -> str:
    return re.sub(r"\D", "", nit.split("-")[0])


def _contextos(texto: str, valor: str, ventana: int = 60) -> list[str]:
    """Texto que precede a cada aparición del valor (con o sin puntos)."""
    digitos = re.sub(r"\D", "", valor.split("-")[0])
    if not digitos:
        return []
    patron = r"\.?".join(digitos)  # 900559088 también encuentra 900.559.088
    plano = re.sub(r"\s+", " ", texto)
    return [plano[max(0, m.start() - ventana):m.start()] for m in re.finditer(patron, plano)]


def _rotulo_telefono(contexto: str) -> bool:
    """¿El rótulo más cercano antes del valor es de teléfono? ("TELÉFONO: 744 7285 Nit:" -> no)."""
    cola = contexto[-25:]
    tel = list(_CONTEXTO_TELEFONO.finditer(cola))
    return bool(tel) and not _ETIQUETA_ID.search(cola[tel[-1].end():])


def verificar_roles(f: FacturaExtraida, texto: str) -> list[Hallazgo]:
    h: list[Hallazgo] = []

    def err(regla: str, campo: str, msg: str) -> None:
        h.append(Hallazgo(regla=regla, campo=campo, severidad=Severidad.ERROR, mensaje=msg))

    for campo in ("nit_emisor", "nit_cliente"):
        nit = getattr(f, campo).valor
        if not nit:
            continue
        base = _base_nit(nit)
        ctx = _contextos(texto, nit)
        if base in NITS_PROVEEDORES_TECNOLOGICOS or (ctx and all(_CONTEXTO_PROVEEDOR.search(c[-40:]) for c in ctx)):
            err("nit_proveedor_tecnologico", campo,
                f"{campo} {nit} es el NIT del software/proveedor de facturación, no de la factura")
        if len(base) == 10 and base.startswith("3"):
            err("nit_es_celular", campo, f"{campo} {nit} parece un número de celular")
        elif ctx and all(_rotulo_telefono(c) for c in ctx):
            err("nit_es_telefono", campo, f"{campo} {nit} aparece como teléfono en el documento")

    emi, cli = f.nit_emisor.valor, f.nit_cliente.valor
    if emi and cli and _base_nit(emi) == _base_nit(cli):
        err("emisor_igual_cliente", "nit_cliente", "el NIT del cliente es igual al del emisor")

    num = f.numero.valor
    if num:
        solo_digitos = re.sub(r"\D", "", num)
        ctx = _contextos(texto, num) if solo_digitos == num else []
        if re.fullmatch(r"18\d{12}", solo_digitos) or (ctx and all(_CONTEXTO_RESOLUCION.search(c[-40:]) for c in ctx)):
            err("numero_es_resolucion", "numero", f"{num} parece el número de resolución DIAN, no el de la factura")
    return h


def comparar(a: FacturaExtraida, b: FacturaExtraida) -> list[Hallazgo]:
    """Consenso: dos extracciones independientes deben coincidir en lo crítico."""
    h = []
    for nombre in a.CAMPOS:
        va, vb = getattr(a, nombre).valor, getattr(b, nombre).valor
        if va is not None and vb is not None and va != vb:
            h.append(Hallazgo(regla="sin_consenso", campo=nombre, severidad=Severidad.ERROR,
                              mensaje=f"{nombre}: '{va}' vs '{vb}'"))
    return h
