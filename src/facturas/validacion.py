"""Validaciones determinísticas. Aquí es donde se atrapan las alucinaciones."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from rapidfuzz import fuzz

from .models import FacturaExtraida, Hallazgo, Severidad

TOLERANCIA = Decimal("1.00")  # pesos: valor vs. su evidencia
TOLERANCIA_SUMA = Decimal("100")  # pesos: algunos emisores redondean el total (ETB: $2,63)
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


def _partes_numericas(texto: str) -> list[str]:
    """Números como secuencias de dígitos, partiendo por guion: '900.123.456-8' -> ['900123456', '8']."""
    return [re.sub(r"\D", "", p) for t in _NUMERO.findall(texto) for p in t.split("-") if re.search(r"\d", p)]


def _numeros_exactos(evidencia: str, fuente: str) -> bool:
    """La coincidencia difusa perdona un dígito cambiado (1.190.000 vs 1.290.000 da 96%).
    Por eso cada número de la evidencia debe existir con EXACTAMENTE los mismos dígitos
    en la fuente (ignorando solo separadores de miles: 900.123.456 == 900123456)."""
    en_fuente = set(_partes_numericas(fuente))
    return all(n in en_fuente for n in _partes_numericas(evidencia))


_MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _fecha_en_texto(valor: date, texto: str) -> bool:
    """Acepta 2026-08-16, 16/08/2026, 2026/08/16, 16-ago-2026, 16 de agosto de 2026, 16/08/26..."""
    t = texto.lower()
    nums = [int(n) for n in re.findall(r"\d+", t)]
    anio_ok = valor.year in nums or (valor.year % 100) in nums
    dia_ok = valor.day in nums
    mes_ok = valor.month in nums or _MESES[valor.month - 1] in t
    return anio_ok and dia_ok and mes_ok


def _valor_en_evidencia(valor, evidencia: str) -> bool:
    """El valor devuelto debe poder leerse dentro de su propia evidencia."""
    from .extractores import parse_monto

    if isinstance(valor, Decimal):  # tolera centavos: el PDF suele mostrar el valor redondeado
        return any((m := parse_monto(t)) is not None and abs(m - valor) <= TOLERANCIA
                   for t in _NUMERO.findall(evidencia))
    if isinstance(valor, date):
        return _fecha_en_texto(valor, evidencia)
    if re.fullmatch(r"[\d.\s-]+", str(valor)):  # NIT/cédula: comparar solo dígitos
        return re.sub(r"\D", "", str(valor)) in re.sub(r"\D", "", evidencia)
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
        # Lo que protege contra alucinaciones es que los NÚMEROS existan exactos en el
        # documento. El rótulo puede venir unido por el LLM desde columnas de una tabla
        # ("IVA 19% 119.827,73"), así que la similitud del texto solo se exige si la
        # evidencia no tiene números.
        tiene_numeros = bool(_NUMERO.search(evidencia))
        valido = _numeros_exactos(evidencia, fuente) if tiene_numeros else score >= UMBRAL_GROUNDING
        if not valido:
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
        extras = (f.otros_impuestos.valor or 0) + (f.cargos.valor or 0) - (f.descuentos.valor or 0)
        esperado = sub + (iva or 0) + extras
        if abs(esperado - tot) > TOLERANCIA_SUMA:
            partes = ["subtotal", "IVA" if iva is not None else "sin IVA"]
            partes += [n for n in ("otros_impuestos", "cargos") if getattr(f, n).valor]
            partes += ["- descuentos"] if f.descuentos.valor else []
            err("suma_total", f"{' + '.join(partes)} = {esperado} != total ({tot})", "total")
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

    nit = f.nit_emisor.valor
    if nit and not nit_valido(nit):
        err("dv_nit", f"nit_emisor {nit}: dígito de verificación inválido", "nit_emisor")
    nit = f.nit_cliente.valor
    if nit and not nit_valido(nit):
        # Los emisores imprimen con frecuencia mal el DV de la cédula del cliente
        # (visto en facturas reales): se avisa, pero no bloquea.
        warn("dv_nit_cliente", f"nit_cliente {nit}: dígito de verificación inválido", "nit_cliente")

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


# ---------------------------------------------------- fechas ambiguas (d/m vs m/d)
_FECHA_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b(\s+\d{1,2}:\d{2}(:\d{2})?\s*([AaPp]\.?\s?[Mm]\.?))?")


def _formato_fechas(texto: str) -> str | None:
    """Deduce si el documento usa d/m/a o m/d/a.

    Pistas: una fecha no ambigua (25/11 -> d/m; 11/25 -> m/d), o el formato de hora:
    'PM' en mayúsculas sin puntos es el formato en inglés de EE. UU. ('5/10/2026 4:47:53 PM'),
    mientras que 'p. m.' es el formato en español ('04/05/2022 07:28:37p. m.').
    """
    dm = md = 0
    for m in _FECHA_SLASH.finditer(texto):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 >= b:
            dm += 1
        elif b > 12 >= a:
            md += 1
        elif m.group(6):
            if re.fullmatch(r"[AP]M", m.group(6)):
                md += 1
            elif "." in m.group(6):
                dm += 1
    if dm and not md:
        return "dm"
    if md and not dm:
        return "md"
    return None


def verificar_fechas(f: FacturaExtraida, texto: str) -> list[Hallazgo]:
    """3/11/2026 puede ser 3 de noviembre o 11 de marzo. Visto en facturas reales (Puma):
    el LLM leyó '5/10/2026 4:47:53 PM' como 5 de octubre y era 10 de mayo."""
    h: list[Hallazgo] = []
    formato = _formato_fechas(texto)
    for campo in ("fecha_emision", "fecha_vencimiento"):
        c = getattr(f, campo)
        if c.valor is None or not c.evidencia:
            continue
        for m in _FECHA_SLASH.finditer(c.evidencia):
            a, b, anio = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a > 12 or b > 12 or a == b or anio != c.valor.year:
                continue
            if formato is None:
                # sin pistas: convención colombiana (día/mes), pero se avisa
                h.append(Hallazgo(regla="fecha_ambigua", campo=campo, severidad=Severidad.ADVERTENCIA,
                                  mensaje=f"{m.group(0)} es ambigua; se asume día/mes"))
            esperado = (b, a) if formato == "md" else (a, b)
            if (c.valor.day, c.valor.month) != esperado:
                h.append(Hallazgo(regla="fecha_mal_interpretada", campo=campo, severidad=Severidad.ERROR,
                                  mensaje=f"el documento usa formato {'mes/día' if formato == 'md' else 'día/mes'}: "
                                          f"{m.group(0)} no es {c.valor}"))
            break
    return h


# ------------------------------------------------ rótulo coherente con el campo
# Visto en facturas reales (Falabella): el LLM puso el IVA como "descuento" para que la
# suma cuadrara. Los campos que entran en la suma deben venir de un rótulo de su tipo.
_ROTULOS = {
    "descuentos": re.compile(r"desc", re.I),
    "otros_impuestos": re.compile(r"consumo|inc\b|bolsa|impuesto|imp\.", re.I),
    "cargos": re.compile(r"propina|servicio|cargo|flete|domicilio", re.I),
    "iva": re.compile(r"iva|impuesto|imp\.", re.I),
}
_ROTULO_IVA = re.compile(r"\biva\b|impuesto a las ventas", re.I)
_ROTULO_IMPUESTO = re.compile(r"\biva\b|impuesto|\bimp\.", re.I)
_ROTULO_DESCUENTO = re.compile(r"desc", re.I)


def verificar_rotulos(f: FacturaExtraida) -> list[Hallazgo]:
    h: list[Hallazgo] = []
    for campo, patron in _ROTULOS.items():
        c = getattr(f, campo)
        if not c.valor or not c.evidencia:  # None o 0
            continue
        if not patron.search(c.evidencia):
            h.append(Hallazgo(regla="rotulo_incoherente", campo=campo, severidad=Severidad.ERROR,
                              mensaje=f"{campo} {c.valor} viene de '{c.evidencia[:40]}', sin rótulo de {campo}"))
        elif campo != "iva" and _ROTULO_IVA.search(c.evidencia) and not patron.search(
                _ROTULO_IVA.sub("", c.evidencia)):
            h.append(Hallazgo(regla="rotulo_incoherente", campo=campo, severidad=Severidad.ERROR,
                              mensaje=f"{campo} {c.valor} parece ser el IVA ('{c.evidencia[:40]}')"))
        elif (campo == "descuentos" and _ROTULO_IMPUESTO.search(c.evidencia)) or (
                campo in ("iva", "otros_impuestos") and _ROTULO_DESCUENTO.search(c.evidencia)):
            # Encabezados de tabla juntos (Falabella: "DESCUENTOS ... IMPUESTO ... 95.782,00"):
            # no se puede saber a cuál columna pertenece el valor.
            h.append(Hallazgo(regla="rotulo_ambiguo", campo=campo, severidad=Severidad.ERROR,
                              mensaje=f"{campo} {c.valor}: la evidencia mezcla rótulos de descuento e impuesto"))
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
