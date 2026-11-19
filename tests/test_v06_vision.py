"""v0.6: visión para escaneados. La visión propone, el OCR confirma."""
import json
from datetime import date
from types import SimpleNamespace

import generar_demo
import pytest

from facturas.extractores import ExtractorVisionOpenAI
from facturas.models import Decision
from facturas.pipeline import Pipeline

HOY = date(2026, 11, 18)
BIEN = {
    "numero": {"valor": "FE-1024", "evidencia": "No. FE-1024"},
    "nit_emisor": {"valor": "900.123.456-8", "evidencia": "NIT Emisor: 900.123.456-8"},
    "nit_cliente": {"valor": "800.987.654-4", "evidencia": "NIT Cliente: 800.987.654-4"},
    "fecha_emision": {"valor": "2026-11-03", "evidencia": "Fecha de emisión: 2026-11-03"},
    "fecha_vencimiento": {"valor": "2026-12-03", "evidencia": "Fecha de vencimiento: 2026-12-03"},
    "subtotal": {"valor": "1.000.000", "evidencia": "Subtotal: $ 1.000.000"},
    "iva": {"valor": "190.000", "evidencia": "IVA 19%: $ 190.000"},
    "total": {"valor": "1.190.000", "evidencia": "Total a pagar: $ 1.190.000"},
    "descuentos": {"valor": None, "evidencia": None},
    "otros_impuestos": {"valor": None, "evidencia": None},
    "cargos": {"valor": None, "evidencia": None},
}


class FakeVision:
    def __init__(self, respuesta):
        self._json, self.kwargs = json.dumps(respuesta), None
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.kwargs = kw
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._json))])


@pytest.fixture(scope="module")
def escaneada():
    return generar_demo.escanear(generar_demo.factura_pdf())


def test_vision_se_usa_solo_en_escaneados(escaneada):
    fake = FakeVision(BIEN)
    p = Pipeline(hoy=HOY, vision=ExtractorVisionOpenAI(client=fake, modelo="fake"))
    r = p.procesar(escaneada)
    assert r.fuente == "pdf_vision" and r.extractor == "openai_vision"
    contenido = fake.kwargs["messages"][1]["content"]
    assert any(c["type"] == "image_url" for c in contenido)  # se envió la imagen
    assert r.decision == Decision.AUTOMATICO, r.hallazgos

    nativa = Pipeline(hoy=HOY, vision=ExtractorVisionOpenAI(client=FakeVision(BIEN), modelo="fake"))
    assert nativa.procesar(generar_demo.factura_pdf()).fuente == "pdf_llm"


def test_numero_que_el_ocr_no_confirma_va_a_revision(escaneada):
    malo = {**BIEN, "total": {"valor": "1.180.000", "evidencia": "Total a pagar: $ 1.180.000"}}
    p = Pipeline(hoy=HOY, vision=ExtractorVisionOpenAI(client=FakeVision(malo), modelo="fake"))
    r = p.procesar(escaneada)
    assert r.decision == Decision.REVISION
    assert any(h.regla == "evidencia_inexistente" and h.campo == "total" for h in r.hallazgos)


def test_tercera_lectura_confirma_pero_exige_digitos_exactos(escaneada, monkeypatch):
    from facturas import pipeline as pl

    llamadas = []

    def ocr_alt(pdf):
        llamadas.append(1)
        return "Total a pagar: $ 1.185.000"  # la tercera lectura vio otro número

    monkeypatch.setattr(pl, "ocr_alternativo", ocr_alt)
    # el modelo dice 1.185.000; el OCR principal ve 1.190.000; la tercera lectura ve 1.185.000
    raro = {**BIEN, "total": {"valor": "1.185.000", "evidencia": "Total a pagar: $ 1.185.000"}}
    r = Pipeline(hoy=HOY, vision=ExtractorVisionOpenAI(client=FakeVision(raro), modelo="fake")).procesar(escaneada)
    assert llamadas  # solo se llama cuando hace falta
    assert not any(h.regla == "evidencia_inexistente" for h in r.hallazgos)  # confirmado exacto
    assert any(h.regla == "suma_total" for h in r.hallazgos)  # pero la suma no cuadra: revisión
    assert r.decision == Decision.REVISION

    llamadas.clear()
    Pipeline(hoy=HOY, vision=ExtractorVisionOpenAI(client=FakeVision(BIEN), modelo="fake")).procesar(escaneada)
    assert not llamadas  # el OCR principal ya confirmó todo: no hay tercera lectura


def test_reusar_replica_el_camino_de_vision(tmp_path, escaneada):
    """--reusar de una corrida con visión debe volver a pasar por la visión (y la tercera lectura)."""
    import json

    from test_dian_xml import ATTACHED

    from facturas.eval import cargar_cache, evaluar

    caso = tmp_path / "datos" / "f01"
    caso.mkdir(parents=True)
    (caso / "f.pdf").write_bytes(escaneada)
    (caso / "f.xml").write_bytes(ATTACHED)
    original = evaluar(tmp_path / "datos", lambda: Pipeline(
        hoy=HOY, vision=ExtractorVisionOpenAI(client=FakeVision(BIEN), modelo="fake")))
    ruta = tmp_path / "r.json"
    ruta.write_text(json.dumps({"resumen": {"config": {"vision": "openai_vision", "modelo": "fake"}},
                                "casos": [{"caso": f.caso, "extraccion": f.extraccion} for f in original]}))
    cache, cfg = cargar_cache(ruta)
    repetida = evaluar(tmp_path / "datos", lambda: None, cache, cfg)
    assert original[0].fuente == repetida[0].fuente == "pdf_vision"
