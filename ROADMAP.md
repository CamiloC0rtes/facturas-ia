# Roadmap

## Salida objetivo

Por cada factura, el sistema debe entregar un **resultado auditable**. No basta con "los datos de la factura".

| Bloque | Contenido | Estado |
|---|---|---|
| Factura canónica | número, NIT emisor y cliente, fechas, subtotal, IVA, total (+ líneas, retenciones, CUFE, moneda) | parcial (faltan líneas, retenciones, CUFE) |
| Por cada campo | `valor`, `evidencia` literal, ubicación (página + caja), `fuente` (xml / qr / ocr+llm / humano) | valor + evidencia |
| Validaciones | resultado de cada regla (grounding, sumas, IVA, DV NIT, fechas, consenso) | ✅ |
| Decisión | `automatico` / `revision` / `rechazado`, con el motivo | ✅ |
| Cobro | borrador idempotente (una factura = un cobro), solo si no hay errores | ✅ en memoria |
| Trazabilidad | hash del archivo, extractor, modelo, versión de prompt, costo y latencia | parcial (faltan costo y latencia) |

### Métricas de éxito (medidas sobre un set etiquetado)

| Métrica | Meta | Por qué |
|---|---|---|
| **Falsos aprobados** (cobro automático con algún campo crítico errado) | ≤ 0,1 % | Es el error caro: cobrar mal. Es la métrica principal. |
| Tasa de automatización (sobre facturas legibles) | ≥ 80 % | Si todo va a revisión, el sistema no ahorra trabajo |
| Exactitud por campo en facturas aprobadas | ≥ 99,5 % | |
| Tiempo de revisión humana | < 30 s por factura | La UI resalta la evidencia y el campo con error |
| Costo y latencia p95 por factura | se definen con datos reales | Para elegir el modelo |

Regla de diseño: **es preferible enviar 20 % a revisión que cobrar mal el 1 %.**

---

## v0.1 Pipeline base ✅
PDF nativo vs OCR, gate de calidad, extractor de reglas, extractores OpenAI y Claude con esquema estricto, grounding, reglas fiscales, consenso, cobro en borrador, API y CLI.

## v0.2 Interfaz visual ✅
Streamlit con el documento y la evidencia resaltada (verde/rojo), tabla de campos, hallazgos, cobro, **revisión humana** con revalidación, y trazabilidad de extractor, modelo y versión de prompt.

## v0.3 Evaluación (siguiente, y base de todo lo demás)
- `eval/`: 50–100 facturas reales anonimizadas + `etiquetas.json` con los valores correctos
- `python -m facturas.eval --llm openai --modelo X`: exactitud por campo, falsos aprobados, tasa de automatización, costo y latencia
- Tabla comparativa de modelos (p. ej. gpt-4.1-mini vs gpt-5-mini vs Claude) y de versiones de prompt
- Sin esto, cada cambio de prompt o de modelo es una opinión, no una mejora medida

## v0.4 Factura electrónica DIAN
- Leer el XML UBL 2.1 (AttachedDocument) cuando venga: es la fuente de verdad y no necesita IA
- Decodificar el QR (CUFE, NIT, total) y cruzarlo con lo extraído
- Campo `fuente` por valor

## v0.5 Prompt engineering v2
- Few-shot con 2–3 formatos de factura distintos (solo si la evaluación muestra errores de formato)
- Ampliar el esquema: líneas, retenciones (ReteFuente, ReteIVA, ReteICA), CUFE, moneda
- Validación de líneas contra el subtotal
- Subir `PROMPT_VERSION` y comparar contra v1 en la evaluación

## v0.6 Visión
- Segunda extracción con un modelo multimodal sobre la imagen (no sobre el texto OCR)
- Consenso entre OCR+LLM y visión para escaneados y fotos de celular
- Ubicación de la evidencia (cajas) para PDFs escaneados más precisa

## v0.7 Persistencia y procesamiento asíncrono
- PostgreSQL con índice único `(nit_emisor, numero)` para deduplicar
- Originales en S3/MinIO, cola Redis + worker, API que responde `202` y se consulta por id

## v0.8 La revisión humana como dataset
- Guardar cada corrección (antes/después, quién, cuándo)
- Las correcciones alimentan el set de evaluación
- Métricas por proveedor: qué formatos fallan más

## v0.9 Cobros reales
- Plantillas de cobro, envío por correo, aprobación, estados (borrador → enviado → pagado)
- Idempotencia y log de auditoría

## v1.0 Producción
- Autenticación, observabilidad (logs estructurados, costo por factura, alertas de deriva), despliegue en contenedor
