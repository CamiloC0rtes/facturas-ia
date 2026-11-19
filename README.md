# facturas-ia

Lectura de facturas PDF (nativas o escaneadas) con IA y generación de cobros, diseñada para **no alucinar**.

> El LLM solo **extrae** y está obligado a citar de dónde sacó cada valor.
> El código **verifica** cada valor. Y el cobro lo calcula una lógica determinística, **nunca el LLM**.

```
$ python -m facturas.cli samples/*.pdf

[X  ] factura_borrosa.pdf  (ocr, nitidez=1)
       - error: nitidez 1 < 30: solicitar reenvío
[OK ] factura_escaneada.pdf  (ocr, nitidez=75)
       -> cobro borrador: $1.190.000 a 800.987.654-4 antes de 2026-12-03
[REV] factura_inconsistente.pdf  (nativo)
       - error: subtotal + IVA (1190000) != total (1350000)
[OK ] factura_nativa.pdf  (nativo)
       -> cobro borrador: $1.190.000 a 800.987.654-4 antes de 2026-12-03
```

## Interfaz visual

```bash
streamlit run app.py
```

Sube un PDF o elige un ejemplo y elige el extractor (reglas, OpenAI o Claude). La interfaz muestra:

- la factura con la evidencia de cada campo resaltada: **verde** si está verificada, **rojo** si tiene error
- los campos extraídos, los hallazgos, la decisión y el cobro
- el extractor, el modelo y la versión de prompt que se usaron
- un formulario de **revisión humana**: los valores corregidos vuelven a pasar por las reglas antes de generar el cobro

## Modelo y prompt engineering

| | Actual (`PROMPT_VERSION = v1`) |
|---|---|
| Modelo | OpenAI `gpt-4.1-mini` por defecto (`FACTURAS_MODELO_OPENAI`) o Claude (`FACTURAS_MODELO`). Se elegirá con la evaluación de v0.3 |
| Entrada | Solo texto (nativo u OCR) delimitado con `<factura>…</factura>`. Todavía no se envía la imagen |
| Salida | Esquema JSON estricto: Structured Outputs (`strict: true`) en OpenAI, *tool use* forzado en Claude |
| Instrucciones | Extraer solo lo literal; no calcular ni deducir; `null` si no es legible; evidencia literal obligatoria; formatos normalizados (fechas ISO, NIT con DV) |
| Determinismo | `temperature=0` |
| Few-shot | No (zero-shot). Se agregará solo si la evaluación lo justifica |
| Defensa | El prompt **no** es la defensa principal: lo son el grounding, las reglas y el consenso |

Ver [ROADMAP.md](ROADMAP.md) para la salida objetivo, las métricas de éxito y los evolutivos.

## Arquitectura

```mermaid
flowchart TD
    A[PDF: nativo / escaneado / foto] --> B[API FastAPI]
    B --> C{¿Tiene texto?}
    C -- sí --> D[Texto nativo<br/>PyMuPDF]
    C -- no --> E{Gate de calidad<br/>varianza Laplaciano}
    E -- borrosa --> R[RECHAZADO<br/>pedir reenvío]
    E -- ok --> F[Preproceso OpenCV<br/>denoise · binarizar · deskew]
    F --> G[OCR Tesseract]
    D --> H[Extractor<br/>OpenAI / Claude con schema estricto · o reglas]
    G --> H
    H --> I[Validación determinística<br/>grounding · sumas · IVA · DV NIT · fechas · consenso]
    I -- errores --> V[REVISIÓN humana]
    I -- todo ok --> K[Motor de cobros<br/>borrador]
```

## Cómo evita alucinaciones

| Capa | Qué hace | Archivo |
|---|---|---|
| Gate de calidad | Si la imagen está muy borrosa, rechaza en vez de adivinar | `texto.py` |
| Salida forzada | El LLM responde con un esquema JSON estricto (Structured Outputs de OpenAI o *tool use* de Claude), `temperature=0`, y puede devolver `null` | `extractores.py` |
| Evidencia obligatoria | Cada campo trae el fragmento literal de donde salió | `models.py` |
| Grounding | La evidencia debe existir en el texto fuente (fuzzy) **y** sus números deben aparecer exactos; además, el valor debe leerse dentro de su propia evidencia | `validacion.py` |
| Reglas fiscales | subtotal + IVA = total, tarifa IVA válida (19 / 5 / 0 %), dígito de verificación del NIT (algoritmo DIAN), fechas coherentes | `validacion.py` |
| Consenso | Un segundo extractor independiente (regex) debe coincidir en los campos críticos | `validacion.py` |
| Confianza OCR | Si la confianza promedio de Tesseract es baja, va a revisión | `pipeline.py` |
| Cobro determinístico | Solo se genera si no hay errores, y queda en estado `borrador` | `pipeline.py` |

**Dato interesante encontrado al construirlo:** la coincidencia difusa sola **no basta**. Un total alucinado `1.290.000` frente al real `1.190.000` tiene un 96 % de similitud y pasaría el filtro. Por eso los números de la evidencia se comparan de forma exacta (ver `test_detecta_total_alucinado_por_evidencia_inexistente`).

## Uso

```bash
# requisitos del sistema: tesseract + idioma español
sudo apt install tesseract-ocr tesseract-ocr-spa   # macOS: brew install tesseract tesseract-lang

pip install -e ".[dev]"
python scripts/generar_demo.py          # crea facturas ficticias en samples/
python -m facturas.cli samples/*.pdf    # extractor de reglas (sin API key)

export OPENAI_API_KEY=...                    # o ANTHROPIC_API_KEY
python -m facturas.cli samples/*.pdf --llm openai   # LLM + reglas como consenso
# modelo configurable: FACTURAS_MODELO_OPENAI / FACTURAS_MODELO

uvicorn facturas.api:app --reload       # POST /facturas con un PDF
pytest -q
```

Con Docker:

```bash
docker build -t facturas-ia .
docker run -p 8000:8000 -e OPENAI_API_KEY facturas-ia
curl -F "archivo=@samples/factura_nativa.pdf" localhost:8000/facturas
```

## Estructura

```
src/facturas/
  models.py       # Campo(valor, evidencia), FacturaExtraida, Cobro, Resultado
  texto.py        # PDF nativo vs OCR, gate de calidad, preprocesamiento
  extractores.py  # ExtractorReglas (regex), ExtractorOpenAI y ExtractorClaude
  validacion.py   # grounding, reglas fiscales, DV NIT, consenso
  pipeline.py     # orquestación y decisión automatico / revision / rechazado
  resaltado.py    # ubica la evidencia en la imagen (PDF nativo u OCR)
  api.py          # FastAPI
  cli.py
app.py                    # interfaz Streamlit
scripts/generar_demo.py   # facturas ficticias: nativa, escaneada, borrosa, inconsistente
tests/                    # incluye un LLM simulado que alucina y es atrapado
```

## Próximos pasos

Ver [ROADMAP.md](ROADMAP.md). El siguiente es **v0.3: evaluación** con facturas reales etiquetadas.

## Licencia

MIT. Las facturas de `samples/` son ficticias.
