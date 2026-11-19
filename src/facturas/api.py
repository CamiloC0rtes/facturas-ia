"""API mínima. En producción: guardar en S3, encolar en Redis y responder 202."""
from __future__ import annotations

import os

from fastapi import FastAPI, File, HTTPException, UploadFile

from .extractores import ExtractorClaude, ExtractorReglas
from .models import Resultado
from .pipeline import Pipeline

app = FastAPI(title="facturas-ia", version="0.1.0")


def _crear_pipeline() -> Pipeline:
    if os.getenv("ANTHROPIC_API_KEY"):
        # LLM como extractor principal y reglas como segunda opinión (consenso)
        return Pipeline(extractor=ExtractorClaude(), segundo=ExtractorReglas())
    return Pipeline(extractor=ExtractorReglas())


pipeline = _crear_pipeline()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "extractor": pipeline.extractor.nombre}


@app.post("/facturas", response_model=Resultado)
async def subir_factura(archivo: UploadFile = File(...)) -> Resultado:
    if not (archivo.filename or "").lower().endswith(".pdf"):
        raise HTTPException(415, "Solo se aceptan PDF")
    return pipeline.procesar(await archivo.read(), archivo.filename)
