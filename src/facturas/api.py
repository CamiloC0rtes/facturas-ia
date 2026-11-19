"""API mínima. En producción: guardar en S3, encolar en Redis y responder 202."""
from __future__ import annotations

import os

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

from .extractores import ExtractorReglas, extractor_llm
from .models import Resultado
from .pipeline import Pipeline

app = FastAPI(title="facturas-ia", version="0.6.0")


def _crear_pipeline() -> Pipeline:
    # FACTURAS_LLM=openai|claude; si no se define, se elige según la API key disponible
    llm = extractor_llm(os.getenv("FACTURAS_LLM"))
    if llm:
        # LLM como extractor principal y reglas como segunda opinión (consenso)
        return Pipeline(extractor=llm, segundo=ExtractorReglas())
    return Pipeline(extractor=ExtractorReglas())


pipeline = _crear_pipeline()


@app.get("/", include_in_schema=False)
def raiz() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "extractor": pipeline.extractor.nombre}


@app.post("/facturas", response_model=Resultado, response_model_exclude={"texto"})
async def subir_factura(archivo: UploadFile = File(...)) -> Resultado:
    """Acepta el ZIP de la factura electrónica (PDF + XML DIAN), solo el XML o solo el PDF."""
    nombre = (archivo.filename or "").lower()
    datos = await archivo.read()
    if nombre.endswith(".zip"):
        return pipeline.procesar_zip(datos, archivo.filename)
    if nombre.endswith(".xml"):
        return pipeline.procesar(None, archivo.filename, xml=datos)
    if nombre.endswith(".pdf"):
        return pipeline.procesar(datos, archivo.filename)
    raise HTTPException(415, "Se acepta .zip (PDF + XML DIAN), .xml o .pdf")
