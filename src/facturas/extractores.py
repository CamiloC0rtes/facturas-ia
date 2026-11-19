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
    """'$ 1.190.000,50' -> Decimal('1190000.50') (formato colombiano)."""
    if not s:
        return None
    limpio = re.sub(r"[^\d.,]", "", s)
    if "," in limpio:
        limpio = limpio.replace(".", "").replace(",", ".")
    else:
        limpio = limpio.replace(".", "")
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

    def extraer(self, texto: str) -> FacturaExtraida:
        datos: dict[str, Campo] = {}
        for campo, patron in _PATRONES.items():
            m = re.search(patron, texto, flags=re.IGNORECASE)
            if not m:
                datos[campo] = Campo()
                continue
            crudo, evidencia = m.group(1), m.group(0).strip()
            if campo in ("subtotal", "iva", "total"):
                valor = parse_monto(crudo)
            elif campo.startswith("fecha"):
                valor = parse_fecha(crudo)
            else:
                valor = crudo.strip()
            datos[campo] = Campo(valor=valor, evidencia=evidencia if valor is not None else None)
        return FacturaExtraida(**datos)


# ------------------------------------------------------------ extractor LLM

PROMPT_SISTEMA = """Eres un extractor de datos de facturas colombianas.
Reglas estrictas:
1. Extrae SOLO lo que aparece literalmente en el texto. No calcules, no deduzcas, no completes.
2. Si un valor no aparece o es ilegible, devuelve valor = null y evidencia = null.
3. Para cada valor, en `evidencia` copia EXACTAMENTE el fragmento del texto (máx. 80 caracteres)
   donde aparece. Si no puedes copiar un fragmento literal, el valor debe ser null.
4. Montos como string tal como aparecen (ej. "1.190.000"). Fechas en formato YYYY-MM-DD.
5. NIT con dígito de verificación, ej. "900.123.456-8"."""

_CAMPO_SCHEMA = {
    "type": "object",
    "properties": {
        "valor": {"type": ["string", "null"]},
        "evidencia": {"type": ["string", "null"]},
    },
    "required": ["valor", "evidencia"],
}
_CAMPOS = list(_PATRONES.keys())
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

    def __init__(self, modelo: str | None = None, client=None):
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.modelo = modelo or os.getenv("FACTURAS_MODELO", "claude-sonnet-5-5")

    def extraer(self, texto: str) -> FacturaExtraida:
        resp = self.client.messages.create(
            model=self.modelo,
            max_tokens=1500,
            temperature=0,
            system=PROMPT_SISTEMA,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": f"<factura>\n{texto}\n</factura>"}],
        )
        bloque = next(b for b in resp.content if b.type == "tool_use")
        return self._a_modelo(bloque.input)

    @staticmethod
    def _a_modelo(crudo: dict) -> FacturaExtraida:
        if isinstance(crudo, str):
            crudo = json.loads(crudo)
        datos = {}
        for campo in _CAMPOS:
            c = crudo.get(campo) or {}
            v = c.get("valor")
            if campo in ("subtotal", "iva", "total"):
                v = parse_monto(v)
            elif campo.startswith("fecha"):
                v = parse_fecha(v)
            datos[campo] = Campo(valor=v, evidencia=c.get("evidencia") if v is not None else None)
        return FacturaExtraida(**datos)
