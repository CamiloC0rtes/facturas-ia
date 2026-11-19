"""Interfaz visual: streamlit run app.py"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import streamlit as st

from facturas.extractores import (
    PROMPT_VERSION,
    PROMPTS,
    ExtractorClaude,
    ExtractorOpenAI,
    ExtractorReglas,
    extractor_vision,
)
from facturas.models import Campo, Decision, FacturaExtraida, Severidad
from facturas.paquete import PaqueteInvalido, leer_zip
from facturas.pipeline import Pipeline, revisar
from facturas.resaltado import imagen_con_evidencias

SAMPLES = Path(__file__).parent / "samples"
ETIQUETAS = {
    "numero": "Número",
    "nit_emisor": "NIT emisor",
    "nit_cliente": "NIT cliente",
    "fecha_emision": "Fecha emisión",
    "fecha_vencimiento": "Fecha vencimiento",
    "subtotal": "Subtotal",
    "iva": "IVA",
    "total": "Total",
}

st.set_page_config(page_title="facturas-ia", layout="wide")


def pesos(v) -> str:
    return "$ " + f"{v:,.0f}".replace(",", ".") if v is not None else "—"


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Configuración")
    proveedor = st.radio("Extractor", ["Reglas (sin IA)", "OpenAI", "Claude"],
                         help="Las reglas no usan IA: sirven de demo y de segunda opinión.")
    modelo, consenso = None, False
    if proveedor == "OpenAI":
        if not os.getenv("OPENAI_API_KEY"):
            key = st.text_input("OPENAI_API_KEY", type="password")
            if key:
                os.environ["OPENAI_API_KEY"] = key
        modelo = st.text_input("Modelo", os.getenv("FACTURAS_MODELO_OPENAI", "gpt-4.1-mini"))
    elif proveedor == "Claude":
        if not os.getenv("ANTHROPIC_API_KEY"):
            key = st.text_input("ANTHROPIC_API_KEY", type="password")
            if key:
                os.environ["ANTHROPIC_API_KEY"] = key
        modelo = st.text_input("Modelo", os.getenv("FACTURAS_MODELO", "claude-sonnet-5-5"))
    prompt_v = PROMPT_VERSION
    usar_vision = False
    if proveedor != "Reglas (sin IA)":
        versiones = list(PROMPTS)
        prompt_v = st.selectbox("Versión de prompt", versiones, index=versiones.index(PROMPT_VERSION))
        usar_vision = st.checkbox("Visión para escaneados y fotos", value=True,
                                  help="Si el PDF no tiene texto, el modelo lee la imagen y el OCR "
                                       "confirma cada número (dos lecturas independientes).")
        consenso = st.checkbox("Consenso con extractor de reglas", value=False,
                               help="Con facturas reales el extractor de reglas falla seguido: "
                                    "activarlo manda casi todo a revisión.")
        with st.expander(f"Ver prompt {prompt_v}"):
            st.code(PROMPTS[prompt_v], language="markdown")


def crear_pipeline() -> Pipeline:
    clave = {"OpenAI": "openai", "Claude": "claude"}.get(proveedor)
    vision = extractor_vision(clave, modelo=modelo, prompt_version=prompt_v) if usar_vision else None
    if proveedor == "OpenAI":
        return Pipeline(extractor=ExtractorOpenAI(modelo=modelo, prompt_version=prompt_v),
                        segundo=ExtractorReglas() if consenso else None, vision=vision)
    if proveedor == "Claude":
        return Pipeline(extractor=ExtractorClaude(modelo=modelo, prompt_version=prompt_v),
                        segundo=ExtractorReglas() if consenso else None, vision=vision)
    return Pipeline()


# ------------------------------------------------------------------ entrada
st.title("Lector de facturas")
st.caption("La IA extrae, el código verifica y el cobro lo calcula una regla, nunca el LLM.")

c1, c2 = st.columns([2, 1])
subido = c1.file_uploader("Sube el ZIP de la factura electrónica (PDF + XML) o solo el PDF",
                          type=["zip", "pdf"],
                          help="Con el ZIP se usa el XML de la DIAN como fuente de verdad (sin IA). "
                               "Con solo el PDF se usa el extractor elegido.")
ejemplos = sorted(p.name for p in [*SAMPLES.glob("*.zip"), *SAMPLES.glob("*.pdf")]) if SAMPLES.exists() else []
ejemplo = c2.selectbox("…o usa un ejemplo", ["—"] + ejemplos)

pdf, nombre, es_zip = None, None, False
if subido:
    pdf, nombre, es_zip = subido.getvalue(), subido.name, subido.name.lower().endswith(".zip")
elif ejemplo != "—":
    pdf, nombre, es_zip = (SAMPLES / ejemplo).read_bytes(), ejemplo, ejemplo.endswith(".zip")

if pdf and st.button("Procesar", type="primary"):
    with st.spinner("Leyendo, extrayendo y validando…"):
        try:
            if es_zip:
                st.session_state.resultado = crear_pipeline().procesar_zip(pdf, nombre)
                try:
                    st.session_state.pdf = leer_zip(pdf).pdf
                except PaqueteInvalido:
                    st.session_state.pdf = None
            else:
                st.session_state.resultado = crear_pipeline().procesar(pdf, nombre)
                st.session_state.pdf = pdf
        except Exception as e:  # p. ej. API key inválida
            st.error(f"No se pudo procesar: {e}")

r = st.session_state.get("resultado")
if not r:
    st.info("Elige una factura y presiona **Procesar**.")
    st.stop()

# ------------------------------------------------------------------ resultado
if r.decision == Decision.AUTOMATICO:
    st.success("Aprobada automáticamente: se generó el cobro en borrador.")
elif r.decision == Decision.REVISION:
    st.warning("Requiere revisión humana: hay validaciones que no pasaron.")
else:
    st.error("Rechazada: " + "; ".join(h.mensaje for h in r.hallazgos))

m = st.columns(5)
m[0].metric("Fuente", {"xml_dian": "XML DIAN", "pdf_llm": "PDF (texto)",
                       "pdf_vision": "PDF (visión)"}.get(r.fuente, "—"))
m[1].metric("Lectura", r.metodo_texto)
m[2].metric("Extractor", r.extractor or "—")
m[3].metric("Modelo", r.modelo or "—")
m[4].metric("Prompt", r.prompt_version or "—")
if r.fuente == "xml_dian":
    st.caption("Los datos salen del XML firmado de la DIAN: no se usó OCR ni IA. "
               "El PDF solo se usa para verificar que corresponde al XML (mismo CUFE).")

errores_por_campo: dict[str, list[str]] = {}
for h in r.hallazgos:
    if h.campo and h.severidad == Severidad.ERROR:
        errores_por_campo.setdefault(h.campo, []).append(h.mensaje)

izq, der = st.columns([1, 1])
with izq:
    st.subheader("Documento")
    if st.session_state.get("pdf") and r.fuente != "xml_dian":
        st.image(imagen_con_evidencias(st.session_state.pdf, r.factura, r.metodo_texto,
                                       set(errores_por_campo)),
                 caption="Verde: evidencia verificada · Rojo: campo con error",
                 use_container_width=True)
    elif st.session_state.get("pdf"):
        try:
            st.image(imagen_con_evidencias(st.session_state.pdf, None, "nativo"),
                     caption="Representación gráfica (PDF)", use_container_width=True)
        except Exception:  # noqa: BLE001 - PDF con clave: el XML basta
            st.info("El PDF está protegido con clave; se usó solo el XML.")
    else:
        st.info("El paquete no trae PDF; se usó solo el XML.")

with der:
    if r.factura:
        st.subheader("Campos extraídos")
        filas = []
        for campo, etiqueta in ETIQUETAS.items():
            c = getattr(r.factura, campo)
            v = c.valor
            valor = pesos(v) if isinstance(v, Decimal) else (str(v) if v is not None else "—")
            estado = "❌" if campo in errores_por_campo else ("✅" if v is not None else "⚪")
            filas.append({"": estado, "Campo": etiqueta, "Valor": valor,
                          "Evidencia": c.evidencia or "—"})
        st.dataframe(pd.DataFrame(filas), hide_index=True, use_container_width=True)

    if r.hallazgos:
        st.subheader("Hallazgos")
        for h in r.hallazgos:
            icono = "🔴" if h.severidad == Severidad.ERROR else "🟡"
            st.markdown(f"{icono} `{h.regla}` {h.mensaje}")

    if r.cobro:
        st.subheader("Cobro")
        st.markdown(
            f"**{pesos(r.cobro.valor)}** a NIT `{r.cobro.nit_cliente}` "
            f"antes del **{r.cobro.fecha_limite}** · estado: `{r.cobro.estado}`"
        )

# ------------------------------------------------------------------ revisión humana
if r.decision == Decision.REVISION and r.factura:
    st.divider()
    st.subheader("Revisión humana")
    st.caption("Corrige los campos marcados. Los valores vuelven a pasar por las reglas de negocio.")
    f = r.factura
    with st.form("form_revision"):
        cols = st.columns(4)
        entradas = {}
        for i, (campo, etiqueta) in enumerate(ETIQUETAS.items()):
            v = getattr(f, campo).valor
            marca = " ❌" if campo in errores_por_campo else ""
            entradas[campo] = cols[i % 4].text_input(etiqueta + marca,
                                                     "" if v is None else str(v))
        enviado = st.form_submit_button("Validar y generar cobro")

    if enviado:
        datos, invalidos = {}, []
        for campo, texto in entradas.items():
            texto = texto.strip()
            try:
                if not texto:
                    valor = None
                elif campo in ("subtotal", "iva", "total"):
                    valor = Decimal(texto.replace("$", "").replace(".", "").replace(",", ".")
                                    if "," in texto or texto.count(".") > 1 else texto)
                elif campo.startswith("fecha"):
                    valor = date.fromisoformat(texto)
                else:
                    valor = texto
            except (InvalidOperation, ValueError):
                invalidos.append(ETIQUETAS[campo])
                continue
            datos[campo] = Campo(valor=valor, evidencia="corrección manual" if valor else None)
        if invalidos:
            st.error("Formato inválido en: " + ", ".join(invalidos))
        else:
            hallazgos, cobro = revisar(FacturaExtraida(**datos))
            if cobro:
                st.success(f"Cobro generado: {pesos(cobro.valor)} a {cobro.nit_cliente} "
                           f"antes del {cobro.fecha_limite} · `{cobro.estado}`")
            else:
                for h in hallazgos:
                    if h.severidad == Severidad.ERROR:
                        st.error(h.mensaje)

with st.expander("Texto fuente"):
    st.text(r.texto or "")
with st.expander("JSON del resultado"):
    st.json(r.model_dump(mode="json", exclude={"texto"}))
