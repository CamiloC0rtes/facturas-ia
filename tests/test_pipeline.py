from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import generar_demo as demo
from facturas.extractores import ExtractorClaude, ExtractorReglas
from facturas.models import Decision
from facturas.pipeline import Pipeline

HOY = date(2026, 11, 18)


@pytest.fixture(scope="module")
def pdfs():
    limpia = demo.factura_pdf()
    return {
        "nativa": limpia,
        "escaneada": demo.escanear(limpia),
        "borrosa": demo.escanear(limpia, blur=21, ruido=3),
        "inconsistente": demo.factura_pdf(total_override=1_350_000, numero="FE-1025"),
    }


def test_nativa_genera_cobro(pdfs):
    r = Pipeline(hoy=HOY).procesar(pdfs["nativa"])
    assert r.metodo_texto == "nativo"
    assert r.decision == Decision.AUTOMATICO
    assert r.cobro.valor == 1_190_000
    assert r.cobro.estado == "borrador"


def test_escaneada_se_lee_con_ocr(pdfs):
    r = Pipeline(hoy=HOY).procesar(pdfs["escaneada"])
    assert r.metodo_texto == "ocr"
    assert r.factura.total.valor == 1_190_000


def test_borrosa_se_rechaza_sin_adivinar(pdfs):
    r = Pipeline(hoy=HOY).procesar(pdfs["borrosa"])
    assert r.decision == Decision.RECHAZADO
    assert r.factura is None
    assert r.hallazgos[0].regla == "imagen_borrosa"


def test_inconsistente_va_a_revision(pdfs):
    r = Pipeline(hoy=HOY).procesar(pdfs["inconsistente"])
    assert r.decision == Decision.REVISION
    assert r.cobro is None


def test_duplicado(pdfs):
    p = Pipeline(hoy=HOY)
    p.procesar(pdfs["nativa"])
    assert p.procesar(pdfs["nativa"]).hallazgos[0].regla == "duplicado"


class _FakeAnthropic:
    """Simula a Claude devolviendo un total alucinado."""

    def __init__(self, respuesta: dict):
        self._r = respuesta
        self.messages = self

    def create(self, **_):
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self._r)])


def test_llm_que_alucina_no_genera_cobro(pdfs):
    alucinado = {c: {"valor": None, "evidencia": None} for c in
                 ["numero", "nit_emisor", "nit_cliente", "fecha_emision",
                  "fecha_vencimiento", "subtotal", "iva", "total"]}
    alucinado.update({
        "numero": {"valor": "FE-1024", "evidencia": "No. FE-1024"},
        "nit_emisor": {"valor": "900.123.456-8", "evidencia": "NIT Emisor: 900.123.456-8"},
        "nit_cliente": {"valor": "800.987.654-4", "evidencia": "NIT Cliente: 800.987.654-4"},
        "fecha_emision": {"valor": "2026-11-03", "evidencia": "Fecha de emisión: 2026-11-03"},
        "subtotal": {"valor": "1.000.000", "evidencia": "Subtotal: $ 1.000.000"},
        "iva": {"valor": "190.000", "evidencia": "IVA 19%: $ 190.000"},
        "total": {"valor": "1.900.000", "evidencia": "Total a pagar: $ 1.900.000"},
    })
    llm = ExtractorClaude(client=_FakeAnthropic(alucinado), modelo="fake")
    r = Pipeline(extractor=llm, segundo=ExtractorReglas(), hoy=HOY).procesar(pdfs["nativa"])
    reglas = {h.regla for h in r.hallazgos}
    assert r.decision == Decision.REVISION
    assert {"evidencia_inexistente", "suma_total", "sin_consenso"} <= reglas
    assert r.cobro is None


class _FakeOpenAI:
    """Simula la respuesta de chat.completions con Structured Outputs."""

    def __init__(self, respuesta: dict):
        import json

        self._json = json.dumps(respuesta)
        self.chat = SimpleNamespace(completions=self)
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        msg = SimpleNamespace(content=self._json)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_openai_correcto_genera_cobro(pdfs):
    from facturas.extractores import ExtractorOpenAI

    bueno = {
        "numero": {"valor": "FE-1024", "evidencia": "No. FE-1024"},
        "nit_emisor": {"valor": "900.123.456-8", "evidencia": "NIT Emisor: 900.123.456-8"},
        "nit_cliente": {"valor": "800.987.654-4", "evidencia": "NIT Cliente: 800.987.654-4"},
        "fecha_emision": {"valor": "2026-11-03", "evidencia": "Fecha de emisión: 2026-11-03"},
        "fecha_vencimiento": {"valor": "2026-12-03", "evidencia": "Fecha de vencimiento: 2026-12-03"},
        "subtotal": {"valor": "1.000.000", "evidencia": "Subtotal: $ 1.000.000"},
        "iva": {"valor": "190.000", "evidencia": "IVA 19%: $ 190.000"},
        "total": {"valor": "1.190.000", "evidencia": "Total a pagar: $ 1.190.000"},
    }
    fake = _FakeOpenAI(bueno)
    llm = ExtractorOpenAI(client=fake, modelo="fake")
    r = Pipeline(extractor=llm, segundo=ExtractorReglas(), hoy=HOY).procesar(pdfs["nativa"])
    assert r.decision == Decision.AUTOMATICO, r.hallazgos
    assert r.cobro.valor == 1_190_000
    assert fake.kwargs["response_format"]["json_schema"]["strict"] is True


def test_openai_alucinado_va_a_revision(pdfs):
    from facturas.extractores import ExtractorOpenAI

    alucinado = {c: {"valor": None, "evidencia": None} for c in
                 ["numero", "nit_emisor", "nit_cliente", "fecha_emision",
                  "fecha_vencimiento", "subtotal", "iva", "total"]}
    alucinado["total"] = {"valor": "1.290.000", "evidencia": "Total a pagar: $ 1.290.000"}
    llm = ExtractorOpenAI(client=_FakeOpenAI(alucinado), modelo="fake")
    r = Pipeline(extractor=llm, hoy=HOY).procesar(pdfs["nativa"])
    assert r.decision == Decision.REVISION
    assert any(h.regla == "evidencia_inexistente" for h in r.hallazgos)


def test_api(pdfs, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from facturas import api

    cliente = TestClient(api.app)
    assert cliente.get("/health").json()["ok"]
    r = cliente.post("/facturas", files={"archivo": ("f.pdf", pdfs["inconsistente"], "application/pdf")})
    assert r.status_code == 200
    assert r.json()["decision"] == "revision"


def test_pdf_protegido_se_rechaza_sin_caerse(pdfs):
    import pymupdf

    doc = pymupdf.open(stream=pdfs["nativa"], filetype="pdf")
    protegido = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="1000491862", owner_pw="x")
    r = Pipeline(hoy=HOY).procesar(protegido)
    assert r.decision == Decision.RECHAZADO
    assert r.hallazgos[0].regla == "pdf_protegido"
