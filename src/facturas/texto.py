"""Obtención de texto: PDF nativo primero, OCR solo si hace falta.

Incluye el *gate de calidad*: si la imagen está demasiado borrosa
no se intenta adivinar, se rechaza y se pide reenvío.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import pymupdf as fitz
import numpy as np
import pytesseract

# MuPDF imprime avisos de fuentes embebidas que no afectan la extracción
# (p. ej. "FT_New_Memory_Face(Calibri): unknown file format").
fitz.TOOLS.mupdf_display_errors(False)

MIN_CHARS_NATIVO = 50
DPI_RENDER = 300


@dataclass
class TextoDocumento:
    texto: str
    metodo: str  # "nativo" | "ocr"
    calidad: float | None = None  # varianza del Laplaciano (solo OCR)
    confianza_ocr: float | None = None  # promedio 0-100 (solo OCR)


def nitidez(gris: np.ndarray) -> float:
    """Varianza del Laplaciano: valores bajos = imagen borrosa."""
    return float(cv2.Laplacian(gris, cv2.CV_64F).var())


def preprocesar(gris: np.ndarray) -> np.ndarray:
    """Limpieza básica para OCR: denoise + binarización adaptativa + deskew."""
    limpio = cv2.fastNlMeansDenoising(gris, h=10)
    binaria = cv2.adaptiveThreshold(
        limpio, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
    )
    return _enderezar(binaria)


def _enderezar(binaria: np.ndarray) -> np.ndarray:
    coords = np.column_stack(np.where(binaria < 128))
    if len(coords) < 100:
        return binaria
    angulo = cv2.minAreaRect(coords.astype(np.float32))[-1]
    if angulo > 45:
        angulo -= 90
    if abs(angulo) < 0.3 or abs(angulo) > 15:
        return binaria
    h, w = binaria.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angulo, 1.0)
    return cv2.warpAffine(binaria, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def _pagina_a_gris(pagina: fitz.Page) -> np.ndarray:
    pix = pagina.get_pixmap(dpi=DPI_RENDER, colorspace=fitz.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


class PDFProtegido(Exception):
    """El PDF pide contraseña (p. ej. facturas de telecomunicaciones con la cédula)."""


def _texto_de_datos(datos: dict) -> str:
    """Reconstruye el texto por líneas a partir de image_to_data (evita un segundo OCR)."""
    lineas: dict[tuple, list[str]] = {}
    for i, palabra in enumerate(datos["text"]):
        if palabra.strip():
            clave = (datos["block_num"][i], datos["par_num"][i], datos["line_num"][i])
            lineas.setdefault(clave, []).append(palabra)
    return "\n".join(" ".join(p) for _, p in sorted(lineas.items()))


def extraer_texto(pdf_bytes: bytes, lang: str = "spa", password: str | None = None) -> TextoDocumento:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(password or ""):
        raise PDFProtegido("el PDF está protegido con contraseña")

    nativo = "\n".join(p.get_text() for p in doc)
    if len(nativo.strip()) >= MIN_CHARS_NATIVO:
        return TextoDocumento(texto=nativo, metodo="nativo")

    textos, calidades, confs = [], [], []
    for pagina in doc:
        gris = _pagina_a_gris(pagina)
        calidades.append(nitidez(gris))
        img = preprocesar(gris)
        # una sola pasada de Tesseract: texto y confianza salen del mismo resultado
        datos = pytesseract.image_to_data(
            img, lang=lang, config="--psm 6", output_type=pytesseract.Output.DICT
        )
        confs += [float(c) for c in datos["conf"] if float(c) >= 0]
        textos.append(_texto_de_datos(datos))

    return TextoDocumento(
        texto="\n".join(textos),
        metodo="ocr",
        calidad=min(calidades) if calidades else 0.0,
        confianza_ocr=sum(confs) / len(confs) if confs else 0.0,
    )


def imagenes_pdf(pdf_bytes: bytes, dpi: int = 200, max_paginas: int = 3) -> list[bytes]:
    """Páginas como PNG para un modelo con visión (máx. `max_paginas`, para acotar costo)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(""):
        raise PDFProtegido("el PDF está protegido con contraseña")
    return [doc[i].get_pixmap(dpi=dpi).tobytes("png") for i in range(min(len(doc), max_paginas))]


def ocr_alternativo(pdf_bytes: bytes, lang: str = "spa") -> str:
    """Tercera lectura (v0.6): Tesseract sobre la imagen en gris SIN binarizar y con otros
    modos de segmentación. Se usa solo cuando el OCR principal no confirma un número que
    leyó el modelo con visión. Medido en el set «foto»: confirma 40 de 44 campos correctos
    (antes 5) y ninguno de los valores de una factura alucinada."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    textos = []
    for pagina in doc:
        gris = _pagina_a_gris(pagina)
        for cfg in ("--psm 11", "--psm 6", "--psm 4"):
            datos = pytesseract.image_to_data(gris, lang=lang, config=cfg, output_type=pytesseract.Output.DICT)
            textos.append(_texto_de_datos(datos))
    return "\n".join(textos)
