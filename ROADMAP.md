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

## v0.3 Evaluación (en curso)
- ✅ `python -m facturas.eval <carpeta> [--llm openai] [--prompt v1|v2] [--modelo X]`: exactitud por campo, falsos aprobados, tasa de automatización y latencia
- ✅ Etiquetado automático con el **XML DIAN** que viene en el ZIP de cada factura electrónica (`dian_xml.py`): no hay que etiquetar a mano
- ✅ Set privado inicial: 21 facturas reales de 17 emisores (retail, gimnasios, cine, Cámara de Comercio…), fuera del repo
- ✅ Prompts versionados (`v1`, `v2`) para comparar
- ✅ Primera corrida con OpenAI (`gpt-4.1-mini`, prompt `v1`): **4 falsos aprobados** → reglas de rol
- ✅ Re-corrida con reglas de rol: `v1` 0 falsos aprobados / 5 % automatización; `v2` **0 falsos aprobados / 29 % automatización**, NIT cliente 24 % → 95 % (tabla en el README)
- ✅ Diagnóstico de revisiones innecesarias (v2: 9 facturas con todo correcto frenadas). Causas: formato, no contenido — NIT con espacio o puntos en la evidencia, fechas tipo `16-ago-2026`, rótulo y valor unidos desde una tabla, DV de la cédula del cliente mal impreso por el emisor
- ✅ Ajuste: el grounding exige que los **números** existan con los mismos dígitos en el documento (la similitud del rótulo pasa a ser secundaria); fechas en formatos flexibles; DV del cliente pasa a advertencia. El total alucinado de una tabla sigue bloqueado (test)
- ✅ `--reusar`: re-valida extracciones guardadas sin volver a llamar al LLM (iterar reglas cuesta $0)
- ✅ Medido: automatización 33 % → **62 %**, revisiones innecesarias 9 → 2, **0 falsos aprobados**
- ✅ Tolerancia de $1 entre el valor y su evidencia (el PDF muestra montos redondeados)
- ⏳ Prompt `v3`: número de factura con prefijo y nunca el de resolución; subtotal antes de IVA. Medir contra `v2`
- ⏳ Ampliar el set a 50+ facturas para que las métricas sean más estables
- ⏳ Próximo prompt `v3`: prefijo del número de factura (`TDCR28625`, no `28625`) y subtotal antes de IVA en facturas con precios IVA incluido

**Línea base (extractor de reglas, sin IA, 21 facturas reales):** exactitud por campo entre 0 % y 57 %, 0 % de automatización y **0 falsos aprobados**. Las reglas no sirven para extraer de formatos reales, pero las validaciones cumplen su función: nada mal leído llega a cobro.

**Primera corrida con LLM (gpt-4.1-mini, prompt v1, sin reglas de rol):**

| Métrica | Resultado |
|---|---|
| Exactitud total / fecha de emisión | 100 % / 100 % |
| Exactitud NIT emisor / NIT cliente | 86 % / 24 % |
| Tasa de automatización | 19 % |
| **Falsos aprobados** | **4 de 21** ❌ |

Los 4 falsos aprobados **no eran alucinaciones**: el modelo tomó valores que sí existen en la factura, pero en el campo equivocado. En 3 casos usó el NIT del software de facturación (Alegra) como emisor o como cliente; en 1, un número de celular impreso en la factura como NIT del cliente. El grounding no lo detecta porque el valor sí aparece en el documento.

**Lección:** el grounding verifica *que el valor exista*, no *que esté en el rol correcto*. Se agregaron **reglas de rol** (`verificar_roles`): NIT de proveedores tecnológicos (lista + contexto "Software:", "Proveedor tecnológico"), celulares como NIT, NIT emisor igual al del cliente, y número de resolución DIAN como número de factura. Re-aplicadas sobre los 4 casos: los 4 se detectan; sobre las 21 etiquetas correctas: 0 falsas alarmas.

**Lo que enseñaron las facturas reales** (y no habrían mostrado las sintéticas):
| Hallazgo | Cambio |
|---|---|
| Montos en formato estadounidense (`750,500.00`) se leían mal | `parse_monto` soporta ambos formatos |
| El cliente persona natural tiene cédula sin dígito de verificación | El DV solo se valida si viene |
| Un tercio de las facturas no tienen IVA | El IVA deja de ser campo crítico; si falta, se exige subtotal = total |
| IVA mixto (ítems gravados y excluidos, p. ej. cine) | Tarifa no estándar es advertencia; solo es error si supera el 19 % |
| Se confunde el número de resolución o el NIT del proveedor tecnológico con los de la factura | Prompt `v2` con definición explícita de cada campo |
| Fecha de vencimiento `0001-01-01` como valor vacío en el XML | Se trata como vacío |

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
