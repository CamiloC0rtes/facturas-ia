"""Ubica en la imagen de la factura dónde está la evidencia de cada campo.

- PDF nativo: búsqueda exacta del texto de la evidencia (PyMuPDF).
- PDF escaneado: cajas de palabras del OCR que contienen los números de la evidencia.

Sirve para que el revisor humano vea de un vistazo de dónde salió cada valor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pymupdf as fitz
import pytesseract
from PIL import Image, ImageDraw

from .models import FacturaExtraida

DPI = 150
_NUM = re.compile(r"\d[\d.,/-]*\d|\d")


@dataclass
class Caja:
    campo: str
    x0: float
    y0: float
    x1: float
    y1: float


def _render(pagina: fitz.Page) -> Image.Image:
    pix = pagina.get_pixmap(dpi=DPI)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _cajas_nativo(pagina: fitz.Page, campo: str, evidencia: str) -> list[Caja]:
    escala = DPI / 72
    rects = pagina.search_for(evidencia)
    if not rects:  # la evidencia del LLM puede tener espacios distintos: buscar los números
        rects = [r for n in _NUM.findall(evidencia) for r in pagina.search_for(n)]
    return [Caja(campo, r.x0 * escala, r.y0 * escala, r.x1 * escala, r.y1 * escala) for r in rects]


def _palabras_ocr(img: Image.Image) -> list[tuple[str, int, int, int, int]]:
    gris = np.array(img.convert("L"))
    d = pytesseract.image_to_data(gris, lang="spa", config="--psm 6",
                                  output_type=pytesseract.Output.DICT)
    return [(t, l, tp, l + w, tp + h) for t, l, tp, w, h
            in zip(d["text"], d["left"], d["top"], d["width"], d["height"]) if t.strip()]


def _cajas_ocr(palabras, campo: str, evidencia: str) -> list[Caja]:
    # ignora números cortos (ej. el "19" de "IVA 19%") para no resaltar etiquetas
    numeros = {n for n in _NUM.findall(evidencia) if len(n) >= 3}
    return [Caja(campo, x0, y0, x1, y1) for t, x0, y0, x1, y1 in palabras
            if any(n in t for n in numeros)]


def imagen_con_evidencias(pdf: bytes, factura: FacturaExtraida | None, metodo: str,
                          campos_con_error: set[str] = frozenset()) -> Image.Image:
    """Primera página con un recuadro por campo: verde si pasó, rojo si tiene error."""
    pagina = fitz.open(stream=pdf, filetype="pdf")[0]
    img = _render(pagina)
    if factura is None:
        return img

    palabras = _palabras_ocr(img) if metodo == "ocr" else None
    cajas: list[Caja] = []
    for campo in factura.CAMPOS_CRITICOS + ("fecha_vencimiento",):
        c = getattr(factura, campo)
        if not c.evidencia:
            continue
        if metodo == "ocr":
            cajas += _cajas_ocr(palabras, campo, c.evidencia)
        else:
            cajas += _cajas_nativo(pagina, campo, c.evidencia)

    draw = ImageDraw.Draw(img, "RGBA")
    for cj in cajas:
        rojo = cj.campo in campos_con_error
        borde = (220, 50, 50, 255) if rojo else (20, 150, 100, 255)
        relleno = (220, 50, 50, 40) if rojo else (20, 150, 100, 35)
        draw.rectangle([cj.x0 - 3, cj.y0 - 3, cj.x1 + 3, cj.y1 + 3],
                       outline=borde, fill=relleno, width=2)
        draw.text((cj.x0, max(cj.y0 - 14, 0)), cj.campo, fill=borde)
    return img
