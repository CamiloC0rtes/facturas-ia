"""Extractores: convierten texto en una FacturaExtraida.

- `ExtractorReglas`: regex, determinístico, sin red. Sirve para la demo,
  para tests y como segunda opinión (consenso).
- `ExtractorClaude`: LLM con salida forzada a JSON (tool use) y
  evidencia obligatoria por campo.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .models import Campo, FacturaExtraida


class Extractor(Protocol):
    nombre: str

    def extraer(self, texto: str) -> FacturaExtraida: ...


# ---------------------------------------------------------------- utilidades


def parse_monto(s: str | None) -> Decimal | None:
    """Convierte montos en formato colombiano o estadounidense a Decimal.

    '$ 1.190.000' -> 1190000      '630.672,27' -> 630672.27
    '750,500.00'  -> 750500.00    '114,559'    -> 114559      '52689.08' -> 52689.08
    """
    if s is None:
        return None
    limpio = re.sub(r"[^\d.,]", "", str(s))
    if not limpio or not limpio[0].isdigit():
        return None
    if "," in limpio and "." in limpio:  # el último separador es el decimal
        dec, mil = (",", ".") if limpio.rfind(",") > limpio.rfind(".") else (".", ",")
        limpio = limpio.replace(mil, "").replace(dec, ".")
    elif "," in limpio or "." in limpio:
        sep = "," if "," in limpio else "."
        partes = limpio.split(sep)
        if len(partes) > 2 or len(partes[-1]) == 3:  # 1.190.000 / 114,559 -> miles
            limpio = "".join(partes)
        else:  # 18739,50 / 52689.08 -> decimales
            limpio = ".".join(partes)
    try:
        return Decimal(limpio)
    except InvalidOperation:
        return None


def parse_fecha(s: str | None):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ------------------------------------------------------------ extractor regex

_MONTO = r"\$?\s*([\d][\d.,]*)"
_PATRONES = {
    "numero": r"(?:factura[^\n:]*?(?:n[o°º.]*|n[uú]mero)\s*:?\s*)([A-Z]{0,5}-?\d+)",
    "nit_emisor": r"nit\s*(?:emisor|vendedor|proveedor)?\s*:?\s*([\d.]{6,15}-\d)",
    "nit_cliente": r"(?:nit|c\.?c\.?)\s*(?:cliente|adquiriente|comprador)\s*:?\s*([\d.]{6,15}-\d)",
    "fecha_emision": r"fecha\s*(?:de\s*)?emisi[oó]n\s*:?\s*([\d/-]{8,10})",
    "fecha_vencimiento": r"fecha\s*(?:de\s*)?vencimiento\s*:?\s*([\d/-]{8,10})",
    "subtotal": r"subtotal\s*:?\s*" + _MONTO,
    "iva": r"iva(?:\s*\d{1,2}\s*%)?\s*:?\s*" + _MONTO,
    "total": r"total\s*(?:a\s*pagar)\s*:?\s*" + _MONTO,
}


class ExtractorReglas:
    nombre = "reglas"
    modelo = None
    prompt_version = None

    def extraer(self, texto: str) -> FacturaExtraida:
        datos: dict[str, Campo] = {}
        for campo, patron in _PATRONES.items():
            m = re.search(patron, texto, flags=re.IGNORECASE)
            if not m:
                datos[campo] = Campo()
                continue
            crudo, evidencia = m.group(1), m.group(0).strip()
            if campo in FacturaExtraida.MONTOS:
                valor = parse_monto(crudo)
            elif campo.startswith("fecha"):
                valor = parse_fecha(crudo)
            else:
                valor = crudo.strip()
            datos[campo] = Campo(valor=valor, evidencia=evidencia if valor is not None else None)
        return FacturaExtraida(**datos)


# ------------------------------------------------------------ extractor LLM

# Versionado de prompts: cada cambio crea una versión nueva y se compara contra la
# anterior con `python -m facturas.eval` (ver ROADMAP.md, v0.3).
PROMPTS = {
    "v1": """Eres un extractor de datos de facturas colombianas.
Reglas estrictas:
1. Extrae SOLO lo que aparece literalmente en el texto. No calcules, no deduzcas, no completes.
2. Si un valor no aparece o es ilegible, devuelve valor = null y evidencia = null.
3. Para cada valor, en `evidencia` copia EXACTAMENTE el fragmento del texto (máx. 80 caracteres)
   donde aparece. Si no puedes copiar un fragmento literal, el valor debe ser null.
4. Montos como string tal como aparecen (ej. "1.190.000"). Fechas en formato YYYY-MM-DD.
5. NIT con dígito de verificación, ej. "900.123.456-8".""",
    # v2: definiciones de campo a partir de errores vistos en facturas reales
    # (número de resolución confundido con número de factura, NIT del proveedor
    # tecnológico, formatos de monto con coma decimal, facturas sin IVA).
    "v2": """Eres un extractor de datos de facturas electrónicas colombianas (DIAN).

Reglas estrictas:
1. Extrae SOLO lo que aparece literalmente en el texto. No calcules, no sumes, no deduzcas.
2. Si un valor no aparece o es ilegible, devuelve valor = null y evidencia = null.
3. En `evidencia` copia EXACTAMENTE el fragmento del texto (máx. 80 caracteres) que contiene
   el valor. Si no puedes copiar un fragmento literal que lo contenga, el valor debe ser null.

Definición de cada campo:
- numero: número de la factura con su prefijo (ej. "SETP990001234", "FE-1024").
  NO es el número de resolución de facturación, ni el CUFE, ni un número de pedido.
- nit_emisor: NIT de quien vende y emite la factura.
- nit_cliente: NIT o cédula del adquiriente (comprador). Puede no tener dígito de verificación.
  NO confundas ninguno de los dos con el NIT del proveedor tecnológico de facturación.
- fecha_emision: fecha de expedición de la factura. NO la de la resolución ni la de validación.
- fecha_vencimiento: fecha de vencimiento o límite de pago. null si no aparece.
- subtotal: valor antes de impuestos (subtotal, valor bruto, base), después de descuentos.
- iva: valor total del IVA. null si la factura no muestra IVA.
- total: valor total a pagar.

Formato:
- Montos como string exactamente como aparecen (ej. "750,500.00", "1.190.000", "630.672,27").
- Fechas en formato YYYY-MM-DD.
- NIT tal como aparece, con dígito de verificación si lo tiene (ej. "900.123.456-8").""",
    # v3: a partir de la evaluación con 54 facturas (33 emisores): número con prefijo,
    # subtotal e IVA intercambiados, y totales con descuentos, INC, bolsas o propina.
    "v3": """Eres un extractor de datos de facturas electrónicas colombianas (DIAN).

Reglas estrictas:
1. Extrae SOLO lo que aparece literalmente en el texto. No calcules, no sumes, no deduzcas.
2. Si un valor no aparece o es ilegible, devuelve valor = null y evidencia = null.
3. En `evidencia` copia EXACTAMENTE el fragmento del texto (máx. 80 caracteres) que contiene
   el valor. Si no puedes copiar un fragmento literal que lo contenga, el valor debe ser null.

Definición de cada campo:
- numero: número de la factura INCLUYENDO su prefijo, tal como aparece (ej. "FE-1024",
  "TDCR 28625", "SETP990001234"). NUNCA es el número de resolución de facturación (suele tener
  14 dígitos y empezar por 187), ni el CUFE, ni un número de pedido, orden o caja.
- nit_emisor: NIT de quien vende y emite la factura.
- nit_cliente: NIT o cédula del adquiriente (comprador). Puede no tener dígito de verificación.
  NO confundas ninguno de los dos con el NIT del software o proveedor tecnológico de
  facturación, ni con un teléfono.
- fecha_emision: fecha de expedición de la factura. NO la de la resolución ni la de validación.
- fecha_vencimiento: fecha de vencimiento o límite de pago. null si no aparece.
- subtotal: suma de los ítems ANTES de impuestos y ANTES de descuentos globales
  (subtotal, valor bruto, total base). Es mayor que el IVA: no los intercambies.
- iva: valor total del IVA (no la tarifa en %). null si la factura no muestra IVA.
- descuentos: descuentos globales del documento. null si no hay.
- otros_impuestos: impuestos distintos al IVA que suman al total: impuesto al consumo (INC),
  impuesto a las bolsas plásticas, etc. Si hay varios, usa solo un total que ya aparezca
  impreso; no los sumes tú. null si no hay.
- cargos: cargos que suman al total: propina, servicio voluntario, fletes. null si no hay.
- total: valor total a pagar.

Formato:
- Montos como string exactamente como aparecen (ej. "750,500.00", "1.190.000", "630.672,27").
- Fechas en formato YYYY-MM-DD.
- NIT tal como aparece, con dígito de verificación si lo tiene (ej. "900.123.456-8").""",
}
PROMPT_VERSION = os.getenv("FACTURAS_PROMPT", "v2")
PROMPT_SISTEMA = PROMPTS[PROMPT_VERSION]

_CAMPO_SCHEMA = {
    "type": "object",
    "properties": {
        "valor": {"type": ["string", "null"]},
        "evidencia": {"type": ["string", "null"]},
    },
    "required": ["valor", "evidencia"],
}
_CAMPOS = list(FacturaExtraida.CAMPOS)
TOOL = {
    "name": "registrar_factura",
    "description": "Registra los campos extraídos de la factura con su evidencia literal.",
    "input_schema": {
        "type": "object",
        "properties": {c: _CAMPO_SCHEMA for c in _CAMPOS},
        "required": _CAMPOS,
    },
}


class ExtractorClaude:
    nombre = "claude"

    def __init__(self, modelo: str | None = None, client=None, prompt_version: str | None = None):
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.modelo = modelo or os.getenv("FACTURAS_MODELO", "claude-sonnet-5-5")
        self.prompt_version = prompt_version or PROMPT_VERSION

    def extraer(self, texto: str) -> FacturaExtraida:
        resp = self.client.messages.create(
            model=self.modelo,
            max_tokens=1500,
            temperature=0,
            system=PROMPTS[self.prompt_version],
            tools=[TOOL],
            tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": f"<factura>\n{texto}\n</factura>"}],
        )
        bloque = next(b for b in resp.content if b.type == "tool_use")
        return self._a_modelo(bloque.input)

    @staticmethod
    def _a_modelo(crudo: dict) -> FacturaExtraida:
        return a_modelo(crudo)


class ExtractorOpenAI:
    """Mismo contrato que ExtractorClaude, usando Structured Outputs (schema estricto)."""

    nombre = "openai"

    def __init__(self, modelo: str | None = None, client=None, prompt_version: str | None = None):
        self.prompt_version = prompt_version or PROMPT_VERSION
        if client is None:
            from openai import OpenAI

            client = OpenAI()
        self.client = client
        self.modelo = modelo or os.getenv("FACTURAS_MODELO_OPENAI", "gpt-4.1-mini")

    def extraer(self, texto: str) -> FacturaExtraida:
        resp = self.client.chat.completions.create(
            model=self.modelo,
            temperature=0,
            messages=[
                {"role": "system", "content": PROMPTS[self.prompt_version]},
                {"role": "user", "content": f"<factura>\n{texto}\n</factura>"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "factura", "strict": True, "schema": SCHEMA_ESTRICTO},
            },
        )
        return a_modelo(json.loads(resp.choices[0].message.content))


SCHEMA_ESTRICTO = {
    "type": "object",
    "additionalProperties": False,
    "properties": {c: {**_CAMPO_SCHEMA, "additionalProperties": False} for c in _CAMPOS},
    "required": _CAMPOS,
}


def extractor_llm(proveedor: str | None = None, modelo: str | None = None,
                  prompt_version: str | None = None):
    """Elige el LLM según el argumento o las API keys disponibles."""
    proveedor = proveedor or ("openai" if os.getenv("OPENAI_API_KEY") else
                              "claude" if os.getenv("ANTHROPIC_API_KEY") else None)
    if proveedor == "openai":
        return ExtractorOpenAI(modelo=modelo, prompt_version=prompt_version)
    if proveedor == "claude":
        return ExtractorClaude(modelo=modelo, prompt_version=prompt_version)
    return None


def normalizar_nit(v: str | None) -> str | None:
    """'800069933 6' / '800069933 - 6' -> '800069933-6' (formato, no contenido)."""
    if not v:
        return v
    v = v.strip()
    m = re.fullmatch(r"([\d.]{5,15})\s*[-\s]\s*(\d)", v)
    return f"{m.group(1)}-{m.group(2)}" if m else v


def a_modelo(crudo: dict) -> FacturaExtraida:
    if isinstance(crudo, str):
        crudo = json.loads(crudo)
    datos = {}
    for campo in _CAMPOS:
        c = crudo.get(campo) or {}
        v = c.get("valor")
        if campo in FacturaExtraida.MONTOS:
            v = parse_monto(v)
        elif campo.startswith("fecha"):
            v = parse_fecha(v)
        elif campo.startswith("nit"):
            v = normalizar_nit(v)
        datos[campo] = Campo(valor=v, evidencia=c.get("evidencia") if v is not None else None)
    return FacturaExtraida(**datos)
