"""Orquestación.

Dos caminos (v0.4):
- **XML DIAN** (factura electrónica): el XML firmado es la fuente de verdad. No hay OCR
  ni LLM; se valida la respuesta de la DIAN, la coherencia de los totales y que el PDF
  corresponda al XML (mismo CUFE).
- **PDF** (sin XML): texto -> extracción con LLM -> validación -> decisión.
En ambos casos el cobro lo calcula código, nunca el LLM.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from pathlib import Path

from .dian_xml import VALIDADO, cufe, leer_factura_xml, respuesta_dian
from .extractores import Extractor, ExtractorReglas
from .models import Cobro, Decision, FacturaExtraida, Hallazgo, Resultado, Severidad
from .paquete import PaqueteInvalido, leer_zip
from .texto import PDFProtegido, extraer_texto, imagenes_pdf
from .validacion import (
    comparar,
    verificar_fechas,
    verificar_grounding,
    verificar_reglas,
    verificar_roles,
    verificar_rotulos,
)

UMBRAL_NITIDEZ = 30.0  # por debajo: pedir reenvío. Calibrar con facturas reales.
UMBRAL_CONF_OCR = 60.0  # por debajo: siempre a revisión
PLAZO_DEFECTO = timedelta(days=30)


class Pipeline:
    def __init__(self, extractor: Extractor | None = None,
                 segundo: Extractor | None = None, hoy: date | None = None,
                 vision=None, vision_siempre: bool = False):
        self.extractor = extractor or ExtractorReglas()
        self.segundo = segundo  # opcional, para consenso
        self.hoy = hoy
        # v0.6: extractor multimodal para escaneados y fotos. Por defecto solo se usa cuando el
        # PDF no tiene texto (OCR); `vision_siempre` lo usa también en PDFs nativos (para comparar).
        self.vision = vision
        self.vision_siempre = vision_siempre
        self._vistos: set[str] = set()  # en producción: tabla con índice único
        self._facturas: set[tuple[str, str]] = set()  # (NIT emisor, número): una factura = un cobro

    # ------------------------------------------------------------------ entrada
    def procesar_zip(self, datos: bytes, nombre: str = "factura.zip") -> Resultado:
        """El ZIP tal como llega por correo (PDF + XML DIAN)."""
        try:
            paq = leer_zip(datos)
        except PaqueteInvalido as e:
            return self._rechazo(nombre, hashlib.sha256(datos).hexdigest(), "zip_invalido", str(e))
        if paq.xml_factura is None and paq.pdf is None:
            return self._rechazo(nombre, hashlib.sha256(datos).hexdigest(), "zip_vacio",
                                 "el ZIP no trae PDF ni XML de factura")
        return self.procesar(paq.pdf, nombre, xml=paq.xml_factura, otros_xml=paq.otros_xml)

    def procesar(self, pdf: bytes | None, nombre: str = "factura.pdf", xml: bytes | None = None,
                 otros_xml: list[bytes] | None = None) -> Resultado:
        sha = hashlib.sha256((xml or b"") + (pdf or b"")).hexdigest()
        if sha in self._vistos:
            return self._rechazo(nombre, sha, "duplicado", "archivo ya procesado")
        self._vistos.add(sha)

        if xml is not None:
            try:
                return self._por_xml(xml, pdf, nombre, sha, otros_xml or [])
            except Exception:  # noqa: BLE001 - XML ilegible: se cae al camino del PDF
                if pdf is None:
                    return self._rechazo(nombre, sha, "xml_invalido", "el XML no es una factura UBL legible")
        if pdf is None:
            return self._rechazo(nombre, sha, "sin_pdf", "no hay PDF ni XML de factura")
        return self._por_pdf(pdf, nombre, sha)

    def _rechazo(self, nombre: str, sha: str, regla: str, mensaje: str) -> Resultado:
        return Resultado(archivo=nombre, sha256=sha, metodo_texto="-", extractor=self.extractor.nombre,
                         decision=Decision.RECHAZADO,
                         hallazgos=[Hallazgo(regla=regla, severidad=Severidad.ERROR, mensaje=mensaje)])

    def _cerrar(self, base: dict, factura: FacturaExtraida, hallazgos: list[Hallazgo]) -> Resultado:
        """Decisión, deduplicación por (NIT emisor, número) y cobro."""
        clave = (re.sub(r"\D", "", (factura.nit_emisor.valor or "").split("-")[0]),
                 re.sub(r"[^0-9A-Z]", "", str(factura.numero.valor or "").upper()))
        if all(clave) and clave in self._facturas:
            hallazgos.append(Hallazgo(regla="factura_duplicada", severidad=Severidad.ERROR,
                                      mensaje=f"la factura {factura.numero.valor} de {factura.nit_emisor.valor} "
                                              "ya tiene cobro"))
        errores = [x for x in hallazgos if x.severidad == Severidad.ERROR]
        decision = Decision.REVISION if errores else Decision.AUTOMATICO
        if decision == Decision.AUTOMATICO and all(clave):
            self._facturas.add(clave)
        cobro = generar_cobro(factura) if decision == Decision.AUTOMATICO else None
        return Resultado(**base, factura=factura, hallazgos=hallazgos, decision=decision, cobro=cobro)

    # -------------------------------------------------------------- camino XML
    def _por_xml(self, xml: bytes, pdf: bytes | None, nombre: str, sha: str,
                 otros_xml: list[bytes]) -> Resultado:
        factura = leer_factura_xml(xml)
        hallazgos = verificar_reglas(factura, hoy=self.hoy)

        codigo = respuesta_dian(xml, *otros_xml)
        if codigo is None:
            hallazgos.append(Hallazgo(regla="sin_respuesta_dian", severidad=Severidad.ADVERTENCIA,
                                      mensaje="el paquete no trae la respuesta de validación de la DIAN"))
        elif codigo != VALIDADO:
            hallazgos.append(Hallazgo(regla="no_validada_dian", severidad=Severidad.ERROR,
                                      mensaje=f"la DIAN respondió código {codigo}, no 02 (validado)"))

        texto = None
        if pdf is not None:  # ¿el PDF es la representación gráfica de ESTE XML?
            try:
                texto = extraer_texto(pdf).texto
            except PDFProtegido:
                hallazgos.append(Hallazgo(regla="pdf_protegido", severidad=Severidad.ADVERTENCIA,
                                          mensaje="PDF con clave: se usó solo el XML"))
            if texto is not None:
                hallazgos += cruzar_cufe(cufe(xml), texto)

        base = dict(archivo=nombre, sha256=sha, metodo_texto="xml", fuente="xml_dian", texto=texto,
                    extractor="xml_dian")
        return self._cerrar(base, factura, hallazgos)

    # -------------------------------------------------------------- camino PDF
    def _por_pdf(self, pdf: bytes, nombre: str, sha: str) -> Resultado:
        try:
            doc = extraer_texto(pdf)
        except PDFProtegido as e:
            return self._rechazo(nombre, sha, "pdf_protegido", f"{e}: enviar el ZIP con el XML o el PDF sin clave")
        usar_vision = self.vision is not None and (doc.metodo == "ocr" or self.vision_siempre)
        extractor = self.vision if usar_vision else self.extractor
        base = dict(archivo=nombre, sha256=sha, metodo_texto=doc.metodo, calidad=doc.calidad,
                    fuente="pdf_vision" if usar_vision else "pdf_llm",
                    texto=doc.texto,
                    extractor=extractor.nombre,
                    modelo=getattr(extractor, "modelo", None),
                    prompt_version=getattr(extractor, "prompt_version", None))

        # 1. Gate de calidad: no adivinar sobre imágenes ilegibles
        if doc.metodo == "ocr" and doc.calidad is not None and doc.calidad < UMBRAL_NITIDEZ:
            return Resultado(**base, decision=Decision.RECHAZADO, hallazgos=[
                Hallazgo(regla="imagen_borrosa", severidad=Severidad.ERROR,
                         mensaje=f"nitidez {doc.calidad:.0f} < {UMBRAL_NITIDEZ:.0f}: solicitar reenvío")])

        # 2. Extracción (IA o reglas). Con visión, el modelo lee la imagen y el texto del
        #    OCR queda como lectura independiente: el grounding exige que cada número del
        #    modelo aparezca con los mismos dígitos en el OCR ("la visión propone, el OCR confirma").
        if usar_vision:
            factura = self.vision.extraer_imagenes(imagenes_pdf(pdf))
        else:
            factura = self.extractor.extraer(doc.texto)

        # 3. Validación determinística
        hallazgos = verificar_grounding(factura, doc.texto)
        hallazgos += verificar_reglas(factura, hoy=self.hoy)
        hallazgos += verificar_roles(factura, doc.texto)
        hallazgos += verificar_fechas(factura, doc.texto)
        hallazgos += verificar_rotulos(factura)
        if self.segundo:
            hallazgos += comparar(factura, self.segundo.extraer(doc.texto))
        if doc.metodo == "ocr" and (doc.confianza_ocr or 0) < UMBRAL_CONF_OCR:
            hallazgos.append(Hallazgo(regla="ocr_baja_confianza", severidad=Severidad.ERROR,
                                      mensaje=f"confianza OCR {doc.confianza_ocr:.0f}%"))

        # 4. Decisión y 5. cobro
        return self._cerrar(base, factura, hallazgos)

    def procesar_archivo(self, ruta: str | Path) -> Resultado:
        ruta = Path(ruta)
        if ruta.suffix.lower() == ".zip":
            return self.procesar_zip(ruta.read_bytes(), ruta.name)
        if ruta.suffix.lower() == ".xml":
            return self.procesar(None, ruta.name, xml=ruta.read_bytes())
        return self.procesar(ruta.read_bytes(), ruta.name)


_CUFE = re.compile(r"[0-9a-f]{96}")


def cruzar_cufe(cufe_xml: str | None, texto_pdf: str) -> list[Hallazgo]:
    """El CUFE identifica la factura ante la DIAN. Si el PDF trae otro CUFE, el PDF no es
    la representación de ese XML (error); si no se encuentra, se avisa."""
    if not cufe_xml:
        return []
    plano = re.sub(r"\s+", "", texto_pdf.lower())  # el CUFE suele venir partido en dos líneas
    if cufe_xml.lower() in plano:
        return []
    otros = set(_CUFE.findall(plano)) - {cufe_xml.lower()}
    if otros:
        return [Hallazgo(regla="pdf_xml_no_coinciden", severidad=Severidad.ERROR,
                         mensaje="el PDF trae un CUFE distinto al del XML")]
    return [Hallazgo(regla="cufe_no_en_pdf", severidad=Severidad.ADVERTENCIA,
                     mensaje="no se encontró el CUFE del XML en el PDF")]


def revisar(f: FacturaExtraida, hoy: date | None = None) -> tuple[list[Hallazgo], Cobro | None]:
    """Revisión humana: los valores corregidos por una persona vuelven a pasar
    por las reglas de negocio (no por grounding, porque los digitó un humano)."""
    hallazgos = verificar_reglas(f, hoy=hoy)
    faltantes = [c for c in f.CAMPOS_CRITICOS if getattr(f, c).valor is None]
    hallazgos += [Hallazgo(regla="campo_faltante", campo=c, severidad=Severidad.ERROR,
                           mensaje=f"{c}: sigue vacío") for c in faltantes]
    if any(h.severidad == Severidad.ERROR for h in hallazgos):
        return hallazgos, None
    cobro = generar_cobro(f)
    cobro.estado = "borrador (revisado)"
    return hallazgos, cobro


def generar_cobro(f: FacturaExtraida) -> Cobro:
    limite = f.fecha_vencimiento.valor or (f.fecha_emision.valor + PLAZO_DEFECTO)
    return Cobro(
        factura_numero=f.numero.valor,
        nit_cliente=f.nit_cliente.valor,
        valor=f.total.valor,
        fecha_limite=limite,
        concepto=f"Cobro factura {f.numero.valor}",
    )
