"""Convierte un set de evaluación (carpetas con factura.pdf + factura.xml) en su versión
"escaneada" o "fotografiada": el PDF pasa a ser una imagen degradada y el XML se conserva,
así que las etiquetas siguen siendo exactas.

    python scripts/escanear_set.py eval_privado eval_escaneado --nivel escaneo
    python scripts/escanear_set.py eval_privado eval_foto --nivel foto

Niveles:
  escaneo  200 dpi, gris, leve rotación y ruido, JPEG (oficina con escáner)
  foto     perspectiva, iluminación desigual, leve desenfoque, JPEG fuerte (celular)
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import cv2
import numpy as np
import pymupdf


def _a_pdf(imagenes: list[np.ndarray], calidad: int, tam: list[pymupdf.Rect]) -> bytes:
    out = pymupdf.open()
    for img, rect in zip(imagenes, tam):
        _, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, calidad])
        pagina = out.new_page(width=rect.width, height=rect.height)
        pagina.insert_image(pagina.rect, stream=jpg.tobytes())
    return out.tobytes()


def _paginas_gris(pdf: bytes, dpi: int) -> tuple[list[np.ndarray], list[pymupdf.Rect]]:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    imgs, rects = [], []
    for p in doc:
        pix = p.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        imgs.append(np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy())
        rects.append(p.rect)
    return imgs, rects


def escaneo(pdf: bytes, semilla: int = 0) -> bytes:
    rng = np.random.default_rng(semilla)
    imgs, rects = _paginas_gris(pdf, 200)
    salida = []
    for img in imgs:
        h, w = img.shape
        m = cv2.getRotationMatrix2D((w / 2, h / 2), rng.uniform(-1.5, 1.5), 1.0)
        img = cv2.warpAffine(img, m, (w, h), borderValue=255)
        img = np.clip(img + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
        salida.append(img)
    return _a_pdf(salida, 70, rects)


def foto(pdf: bytes, semilla: int = 0) -> bytes:
    rng = np.random.default_rng(semilla)
    imgs, rects = _paginas_gris(pdf, 170)
    salida = []
    for img in imgs:
        h, w = img.shape
        d = 0.04  # perspectiva: esquinas desplazadas hasta 4 %
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        dst = np.float32([[rng.uniform(0, d) * w, rng.uniform(0, d) * h],
                          [w - rng.uniform(0, d) * w, rng.uniform(0, d) * h],
                          [w - rng.uniform(0, d) * w, h - rng.uniform(0, d) * h],
                          [rng.uniform(0, d) * w, h - rng.uniform(0, d) * h]])
        img = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=200)
        # iluminación desigual: gradiente diagonal (sombra de la mano / lámpara)
        yy, xx = np.mgrid[0:h, 0:w]
        luz = 0.75 + 0.25 * ((xx / w + yy / h) / 2)
        img = np.clip(img * luz, 0, 255).astype(np.uint8)
        img = cv2.GaussianBlur(img, (3, 3), 0)
        img = np.clip(img + rng.normal(0, 8, img.shape), 0, 255).astype(np.uint8)
        salida.append(img)
    return _a_pdf(salida, 55, rects)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entrada", type=Path)
    ap.add_argument("salida", type=Path)
    ap.add_argument("--nivel", choices=["escaneo", "foto"], default="escaneo")
    args = ap.parse_args()
    degradar = escaneo if args.nivel == "escaneo" else foto
    hechos = omitidos = 0
    for i, caso in enumerate(sorted(p for p in args.entrada.iterdir() if p.is_dir())):
        pdfs, xmls = list(caso.glob("*.pdf")), list(caso.glob("*.xml"))
        if not pdfs or not xmls:
            continue
        try:
            nuevo = degradar(pdfs[0].read_bytes(), semilla=i)
        except Exception as e:  # noqa: BLE001 - p. ej. PDF con clave
            print(f"  omitido {caso.name}: {e}")
            omitidos += 1
            continue
        destino = args.salida / caso.name
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "factura.pdf").write_bytes(nuevo)
        shutil.copy(xmls[0], destino / "factura.xml")
        hechos += 1
    print(f"{hechos} facturas en {args.salida} (nivel {args.nivel}); {omitidos} omitidas")


if __name__ == "__main__":
    main()
