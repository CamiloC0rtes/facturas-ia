"""Smoke test de la interfaz Streamlit."""
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
APP = str(Path(__file__).resolve().parent.parent / "app.py")
SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(scope="module", autouse=True)
def muestras():
    import generar_demo

    if not (SAMPLES / "factura_inconsistente.pdf").exists():
        generar_demo.main()


def _procesar(ejemplo: str):
    at = st_testing.AppTest.from_file(APP, default_timeout=60).run()
    at.selectbox[0].select(ejemplo).run()
    at.button[0].click().run()
    assert not at.exception
    return at


def test_ui_aprobada():
    at = _procesar("factura_nativa.pdf")
    assert "Aprobada" in at.success[0].value


def test_ui_revision_y_correccion():
    at = _procesar("factura_inconsistente.pdf")
    assert "revisión" in at.warning[0].value
    total = next(t for t in at.text_input if t.label.startswith("Total"))
    total.set_value("1190000")
    at.button[-1].click().run()  # botón del formulario
    assert any("Cobro generado" in s.value for s in at.success)


def test_api_acepta_zip(tmp_path):
    from fastapi.testclient import TestClient
    from test_v04_xml import PDF_OK, attached_con_respuesta, zip_de

    from facturas import api

    r = TestClient(api.app).post("/facturas", files={"archivo": (
        "f.zip", zip_de(f_pdf=PDF_OK, f_xml=attached_con_respuesta()), "application/zip")})
    assert r.status_code == 200
    assert r.json()["fuente"] == "xml_dian" and r.json()["decision"] == "automatico"
