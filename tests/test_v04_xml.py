"""v0.4: el XML DIAN como fuente principal."""
import io
import zipfile
from xml.sax.saxutils import escape

import pymupdf
import pytest

from facturas.models import Decision
from facturas.paquete import MAX_ARCHIVOS, PaqueteInvalido, leer_zip
from facturas.pipeline import Pipeline
from test_dian_xml import ATTACHED, INVOICE  # XML ficticio con estructura real

CUFE = "abc123"
RESPUESTA = """<ApplicationResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cac:DocumentResponse><cac:Response><cbc:ResponseCode>{codigo}</cbc:ResponseCode>
 <cbc:Description>Documento validado por la DIAN</cbc:Description></cac:Response></cac:DocumentResponse>
</ApplicationResponse>"""


def attached_con_respuesta(codigo: str = "02") -> bytes:
    return f"""<AttachedDocument xmlns="urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cac:Attachment><cac:ExternalReference><cbc:Description>{escape(INVOICE)}</cbc:Description>
 </cac:ExternalReference></cac:Attachment>
 <cac:ParentDocumentLineReference><cac:DocumentReference><cac:Attachment><cac:ExternalReference>
 <cbc:Description>{escape(RESPUESTA.format(codigo=codigo))}</cbc:Description>
 </cac:ExternalReference></cac:Attachment></cac:DocumentReference></cac:ParentDocumentLineReference>
</AttachedDocument>""".encode()


def pdf_con(texto: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 72), texto, fontsize=9)
    return doc.tobytes()


def zip_de(**archivos: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for nombre, datos in archivos.items():
            z.writestr(nombre.replace("_", "."), datos)
    return buf.getvalue()


PDF_OK = pdf_con(f"Factura FE1024 Total 1.190.000 CUFE: {CUFE}" + "0" * 0)


def test_zip_con_xml_validado_se_aprueba_sin_llm():
    r = Pipeline().procesar_zip(zip_de(f_pdf=PDF_OK, f_xml=attached_con_respuesta("02")))
    assert r.fuente == "xml_dian" and r.decision == Decision.AUTOMATICO
    assert r.cobro.valor == 1_190_000
    assert r.cobro.nit_cliente == "1012345678"


def test_rechazo_de_la_dian_bloquea():
    r = Pipeline().procesar_zip(zip_de(f_pdf=PDF_OK, f_xml=attached_con_respuesta("04")))
    assert r.decision == Decision.REVISION
    assert any(h.regla == "no_validada_dian" for h in r.hallazgos)


def test_respuesta_dian_en_archivo_aparte():
    # Smart Fit: factura en .fe y respuesta de la DIAN en .dian
    r = Pipeline().procesar_zip(zip_de(f_pdf=PDF_OK, f_fe=INVOICE.encode(),
                                       f_dian=RESPUESTA.format(codigo="02").encode()))
    assert r.decision == Decision.AUTOMATICO
    assert not any(h.regla == "sin_respuesta_dian" for h in r.hallazgos)


def test_pdf_de_otra_factura_no_coincide():
    otro = pdf_con("CUFE: " + "f" * 96)
    r = Pipeline().procesar(otro, xml=attached_con_respuesta())
    assert any(h.regla == "pdf_xml_no_coinciden" for h in r.hallazgos)
    assert r.decision == Decision.REVISION


def test_solo_xml_y_pdf_protegido():
    assert Pipeline().procesar(None, xml=ATTACHED).decision == Decision.AUTOMATICO
    protegido = pymupdf.open(stream=PDF_OK, filetype="pdf").tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="1012345678", owner_pw="x")
    r = Pipeline().procesar(protegido, xml=ATTACHED)
    assert r.decision == Decision.AUTOMATICO  # el XML basta
    assert any(h.regla == "pdf_protegido" and h.severidad.value == "advertencia" for h in r.hallazgos)


def test_xml_ilegible_cae_al_camino_pdf():
    import generar_demo

    r = Pipeline().procesar(generar_demo.factura_pdf(), xml=b"<no-es-factura/>")
    assert r.fuente == "pdf_llm"


def test_misma_factura_dos_veces_un_solo_cobro():
    p = Pipeline()
    assert p.procesar(None, "a.xml", xml=ATTACHED).cobro is not None
    # mismo XML con otros bytes (p. ej. reenviado y re-firmado): misma factura
    r = p.procesar(None, "b.xml", xml=ATTACHED + b"\n")
    assert r.cobro is None
    assert any(h.regla == "factura_duplicada" for h in r.hallazgos)


def test_zip_malicioso_o_invalido():
    with pytest.raises(PaqueteInvalido):
        leer_zip(b"esto no es un zip")
    demasiados = zip_de(**{f"a{i}_txt": b"x" for i in range(MAX_ARCHIVOS + 1)})
    with pytest.raises(PaqueteInvalido):
        leer_zip(demasiados)
    assert Pipeline().procesar_zip(b"nada").decision == Decision.RECHAZADO
