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
