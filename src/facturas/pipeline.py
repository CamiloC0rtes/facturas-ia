"""Orquestación: texto -> extracción -> validación -> decisión -> cobro."""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

from .extractores import Extractor, ExtractorReglas
from .models import Cobro, Decision, FacturaExtraida, Hallazgo, Resultado, Severidad
from .texto import extraer_texto
from .validacion import comparar, verificar_grounding, verificar_reglas

UMBRAL_NITIDEZ = 30.0  # por debajo: pedir reenvío. Calibrar con facturas reales.
UMBRAL_CONF_OCR = 60.0  # por debajo: siempre a revisión
PLAZO_DEFECTO = timedelta(days=30)


class Pipeline:
    def __init__(self, extractor: Extractor | None = None,
                 segundo: Extractor | None = None, hoy: date | None = None):
        self.extractor = extractor or ExtractorReglas()
        self.segundo = segundo  # opcional, para consenso
        self.hoy = hoy
        self._vistos: set[str] = set()  # en producción: tabla con índice único

    def procesar(self, pdf: bytes, nombre: str = "factura.pdf") -> Resultado:
        sha = hashlib.sha256(pdf).hexdigest()
        if sha in self._vistos:
            return Resultado(archivo=nombre, sha256=sha, metodo_texto="-",
                             decision=Decision.RECHAZADO,
                             hallazgos=[Hallazgo(regla="duplicado", severidad=Severidad.ERROR,
                                                 mensaje="archivo ya procesado")])
        self._vistos.add(sha)

        doc = extraer_texto(pdf)
        base = dict(archivo=nombre, sha256=sha, metodo_texto=doc.metodo, calidad=doc.calidad)

        # 1. Gate de calidad: no adivinar sobre imágenes ilegibles
        if doc.metodo == "ocr" and doc.calidad is not None and doc.calidad < UMBRAL_NITIDEZ:
            return Resultado(**base, decision=Decision.RECHAZADO, hallazgos=[
                Hallazgo(regla="imagen_borrosa", severidad=Severidad.ERROR,
                         mensaje=f"nitidez {doc.calidad:.0f} < {UMBRAL_NITIDEZ:.0f}: solicitar reenvío")])

        # 2. Extracción (IA o reglas)
        factura = self.extractor.extraer(doc.texto)

        # 3. Validación determinística
        hallazgos = verificar_grounding(factura, doc.texto)
        hallazgos += verificar_reglas(factura, hoy=self.hoy)
        if self.segundo:
            hallazgos += comparar(factura, self.segundo.extraer(doc.texto))
        if doc.metodo == "ocr" and (doc.confianza_ocr or 0) < UMBRAL_CONF_OCR:
            hallazgos.append(Hallazgo(regla="ocr_baja_confianza", severidad=Severidad.ERROR,
                                      mensaje=f"confianza OCR {doc.confianza_ocr:.0f}%"))

        # 4. Decisión
        errores = [x for x in hallazgos if x.severidad == Severidad.ERROR]
        decision = Decision.REVISION if errores else Decision.AUTOMATICO

        # 5. Cobro: lo calcula código, nunca el LLM
        cobro = generar_cobro(factura) if decision == Decision.AUTOMATICO else None
        return Resultado(**base, factura=factura, hallazgos=hallazgos,
                         decision=decision, cobro=cobro)

    def procesar_archivo(self, ruta: str | Path) -> Resultado:
        ruta = Path(ruta)
        return self.procesar(ruta.read_bytes(), ruta.name)


def generar_cobro(f: FacturaExtraida) -> Cobro:
    limite = f.fecha_vencimiento.valor or (f.fecha_emision.valor + PLAZO_DEFECTO)
    return Cobro(
        factura_numero=f.numero.valor,
        nit_cliente=f.nit_cliente.valor,
        valor=f.total.valor,
        fecha_limite=limite,
        concepto=f"Cobro factura {f.numero.valor}",
    )
