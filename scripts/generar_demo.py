"""Genera facturas de ejemplo (ficticias) en samples/:

1. factura_nativa.pdf       -> PDF con texto, todo correcto
2. factura_escaneada.pdf    -> imagen con ruido y leve rotación (OCR)
3. factura_borrosa.pdf      -> imagen muy borrosa (debe rechazarse)
4. factura_inconsistente.pdf-> total mal sumado (debe ir a revisión)
"""
from __future__ import annotations

import io
from pathlib import Path

import cv2
import pymupdf as fitz
import numpy as np
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"

BASE = dict(
    numero="FE-1024",
    emisor="Servicios Andinos SAS",
    nit_emisor="900.123.456-8",
    cliente="Comercializadora El Roble Ltda",
    nit_cliente="800.987.654-4",
    emision="2026-11-03",
    vencimiento="2026-12-03",
    lineas=[("Consultoría analítica", 10, 85000), ("Licencia anual", 1, 150000)],
)


def _fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", ".")


def factura_pdf(total_override: float | None = None, **cambios) -> bytes:
    d = {**BASE, **cambios}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 740
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, y, d["emisor"]); y -= 22
    c.setFont("Helvetica", 11)
    for linea in (
        f"NIT Emisor: {d['nit_emisor']}",
        f"Factura electrónica de venta No. {d['numero']}",
        f"Fecha de emisión: {d['emision']}",
        f"Fecha de vencimiento: {d['vencimiento']}",
        "",
        f"Cliente: {d['cliente']}",
        f"NIT Cliente: {d['nit_cliente']}",
        "",
        "Descripción                     Cant.    Vr. unitario        Total",
    ):
        c.drawString(60, y, linea); y -= 18
    subtotal = 0
    for desc, cant, vu in d["lineas"]:
        t = cant * vu
        subtotal += t
        c.drawString(60, y, f"{desc:<30} {cant:>5}   {_fmt(vu):>12}   {_fmt(t):>12}")
        y -= 18
    iva = round(subtotal * 0.19)
    total = total_override if total_override is not None else subtotal + iva
    y -= 10
    for linea in (f"Subtotal: $ {_fmt(subtotal)}", f"IVA 19%: $ {_fmt(iva)}",
                  f"Total a pagar: $ {_fmt(total)}"):
        c.drawString(330, y, linea); y -= 18
    c.setFont("Helvetica", 8)
    c.drawString(60, 60, "Documento ficticio generado para pruebas.")
    c.save()
    return buf.getvalue()


def escanear(pdf: bytes, blur: int = 0, ruido: float = 8, angulo: float = 1.2) -> bytes:
    """Simula un escaneo: rasteriza, rota, agrega ruido y (opcional) desenfoque."""
    pagina = fitz.open(stream=pdf, filetype="pdf")[0]
    pix = pagina.get_pixmap(dpi=200, colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    h, w = img.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angulo, 1.0)
    img = cv2.warpAffine(img, m, (w, h), borderValue=255)
    if blur:
        img = cv2.GaussianBlur(img, (blur, blur), 0)
    rng = np.random.default_rng(42)
    img = np.clip(img + rng.normal(0, ruido, img.shape), 0, 255).astype(np.uint8)
    ok, png = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
    out = fitz.open()
    p = out.new_page(width=pagina.rect.width, height=pagina.rect.height)
    p.insert_image(p.rect, stream=png.tobytes())
    return out.tobytes()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    limpia = factura_pdf()
    archivos = {
        "factura_nativa.pdf": limpia,
        "factura_escaneada.pdf": escanear(limpia),
        "factura_borrosa.pdf": escanear(limpia, blur=21, ruido=3),
        "factura_inconsistente.pdf": factura_pdf(total_override=1_350_000, numero="FE-1025"),
    }
    for nombre, data in archivos.items():
        (OUT / nombre).write_bytes(data)
        print("generado", OUT / nombre)


if __name__ == "__main__":
    main()
