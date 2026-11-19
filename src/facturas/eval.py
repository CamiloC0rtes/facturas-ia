"""Evaluación contra facturas reales etiquetadas por su XML DIAN.

Estructura esperada (una carpeta por factura, tal como viene el ZIP del correo):

    eval_privado/
      f01/ factura.pdf  factura.xml
      f02/ ...

Uso:
    python -m facturas.eval eval_privado                       # extractor de reglas
    python -m facturas.eval eval_privado --llm openai          # prompt por defecto
    python -m facturas.eval eval_privado --llm openai --prompt v1 --modelo gpt-4.1-mini
    python -m facturas.eval eval_privado --reusar resultados.json   # re-valida sin llamar al LLM

Las facturas son datos personales: la carpeta va en .gitignore y solo se publican métricas.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .dian_xml import leer_factura_xml
from .extractores import ExtractorReglas, extractor_llm
from .models import Decision, FacturaExtraida
from .pipeline import Pipeline

TOL = Decimal("1")


def _norm_id(v) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(v).upper())


def _norm_nit(v) -> str:
    """Compara solo la base del NIT/cédula (sin puntos ni DV)."""
    return re.sub(r"\D", "", str(v).split("-")[0])


def igual(campo: str, esperado, obtenido) -> bool:
    if campo in ("iva", "descuentos", "otros_impuestos", "cargos") and obtenido is None and esperado == 0:
        return True  # no mostrarlo equivale a 0
    if esperado is None:
        return obtenido is None
    if obtenido is None:
        return False
    if campo in FacturaExtraida.MONTOS:
        return abs(Decimal(esperado) - Decimal(obtenido)) <= TOL
    if campo.startswith("nit"):
        return _norm_nit(esperado) == _norm_nit(obtenido)
    if campo == "numero":
        return _norm_id(esperado) == _norm_id(obtenido)
    return esperado == obtenido


@dataclass
class Fila:
    caso: str
    decision: str
    segundos: float
    correctos: dict[str, bool] = field(default_factory=dict)
    errores: list[str] = field(default_factory=list)
    hallazgos: list[str] = field(default_factory=list)
    extraccion: dict | None = None  # lo que devolvió el extractor (para --reusar)
    evidencias: dict[str, str] = field(default_factory=dict)  # valor <- evidencia de campos frenados

    @property
    def criticos_ok(self) -> bool:
        return all(self.correctos[c] for c in FacturaExtraida.CAMPOS_CRITICOS)


class _ExtraccionGuardada:
    """Extractor que devuelve una extracción ya hecha: permite cambiar reglas y
    re-evaluar sin volver a pagar llamadas al LLM."""

    nombre = "cache"

    def __init__(self, factura: FacturaExtraida, modelo=None, prompt_version=None):
        self.factura, self.modelo, self.prompt_version = factura, modelo, prompt_version

    def extraer(self, texto: str) -> FacturaExtraida:
        return self.factura


def cargar_cache(ruta: Path) -> tuple[dict[str, FacturaExtraida], dict]:
    datos = json.loads(ruta.read_text())
    cache = {c["caso"]: FacturaExtraida.model_validate(c["extraccion"])
             for c in datos["casos"] if c.get("extraccion")}
    return cache, datos["resumen"].get("config", {})


def evaluar(carpeta: Path, pipeline_factory, cache: dict[str, FacturaExtraida] | None = None,
            config_cache: dict | None = None) -> list[Fila]:
    filas = []
    for caso in sorted(p for p in carpeta.iterdir() if p.is_dir()):
        pdfs, xmls = list(caso.glob("*.pdf")), list(caso.glob("*.xml"))
        if not pdfs or not xmls:
            continue
        etiqueta = leer_factura_xml(xmls[0].read_bytes())
        t0 = time.perf_counter()
        if cache is not None and caso.name in cache:
            cfg = config_cache or {}
            pipe = Pipeline(extractor=_ExtraccionGuardada(cache[caso.name], cfg.get("modelo"),
                                                          cfg.get("prompt")), hoy=date.today())
        else:
            pipe = pipeline_factory()
        r = pipe.procesar(pdfs[0].read_bytes(), caso.name)
        seg = time.perf_counter() - t0

        fila = Fila(caso.name, r.decision.value, round(seg, 2),
                    hallazgos=[f"{h.regla}:{h.campo or ''}" for h in r.hallazgos
                               if h.severidad.value == "error"],
                    evidencias={h.campo: f"{getattr(r.factura, h.campo).valor} <- "
                                         f"'{getattr(r.factura, h.campo).evidencia}'"
                                for h in r.hallazgos if h.campo and r.factura
                                and h.severidad.value == "error"},
                    extraccion=r.factura.model_dump(mode="json") if r.factura else None)
        for c in FacturaExtraida.CAMPOS:
            esp = getattr(etiqueta, c).valor
            obt = getattr(r.factura, c).valor if r.factura else None
            ok = igual(c, esp, obt)
            fila.correctos[c] = ok
            if not ok:
                fila.errores.append(f"{c}: esperado={esp} obtenido={obt}")
        filas.append(fila)
    return filas


def resumen(filas: list[Fila]) -> dict:
    n = len(filas)
    auto = [f for f in filas if f.decision == Decision.AUTOMATICO.value]
    falsos = [f.caso for f in auto if not f.criticos_ok]
    # revisión innecesaria: todo lo crítico estaba bien, pero una regla la frenó
    innecesarias = [f for f in filas if f.criticos_ok and f.decision != Decision.AUTOMATICO.value]
    por_campo = {c: round(sum(f.correctos[c] for f in filas) / n, 3) for c in FacturaExtraida.CAMPOS}
    return {
        "facturas": n,
        "exactitud_por_campo": por_campo,
        "facturas_100%_criticos": round(sum(f.criticos_ok for f in filas) / n, 3),
        "tasa_automatizacion": round(len(auto) / n, 3),
        "falsos_aprobados": len(falsos),
        "falsos_aprobados_casos": falsos,
        "revisiones_innecesarias": len(innecesarias),
        "reglas_que_frenan_revisiones_innecesarias": dict(
            Counter(h for f in innecesarias for h in f.hallazgos).most_common()),
        "segundos_promedio": round(sum(f.segundos for f in filas) / n, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Evalúa el pipeline contra facturas con XML DIAN")
    ap.add_argument("carpeta", type=Path)
    ap.add_argument("--llm", choices=["openai", "claude"])
    ap.add_argument("--modelo")
    ap.add_argument("--prompt", help="versión de prompt (v1, v2, ...)")
    ap.add_argument("--consenso", action="store_true", help="usar reglas como segunda opinión")
    ap.add_argument("--salida", type=Path, help="guardar detalle y extracciones en JSON")
    ap.add_argument("--reusar", type=Path,
                    help="JSON de una corrida anterior: re-valida sus extracciones sin llamar al LLM")
    args = ap.parse_args()

    def factory() -> Pipeline:
        hoy = date.today()
        if args.llm:
            llm = extractor_llm(args.llm, modelo=args.modelo, prompt_version=args.prompt)
            return Pipeline(extractor=llm, segundo=ExtractorReglas() if args.consenso else None, hoy=hoy)
        return Pipeline(hoy=hoy)

    if not args.carpeta.is_dir():
        raise SystemExit(f"No existe la carpeta '{args.carpeta}'. Descomprime ahí las facturas "
                         "(una subcarpeta por factura con su .pdf y su .xml).")
    cache, config_cache = cargar_cache(args.reusar) if args.reusar else (None, None)
    filas = evaluar(args.carpeta, factory, cache, config_cache)
    if not filas:
        raise SystemExit(f"No hay casos (carpetas con .pdf y .xml) en {args.carpeta}")

    for f in filas:
        marca = {"automatico": "OK ", "revision": "REV", "rechazado": "X  "}[f.decision]
        alerta = "  <-- FALSO APROBADO" if f.decision == "automatico" and not f.criticos_ok else ""
        print(f"[{marca}] {f.caso}  {f.segundos}s{alerta}")
        for e in f.errores:
            print(f"        x {e}")
        if f.decision != "automatico":
            print(f"        > frenada por: {', '.join(f.hallazgos) or '-'}")
            for campo, ev in f.evidencias.items():
                print(f"          {campo}: {ev}")

    res = resumen(filas)
    if config_cache:
        res["config"] = {**config_cache, "reusado_de": str(args.reusar)}
    else:
        p = factory()
        res["config"] = {"extractor": p.extractor.nombre,
                         "modelo": getattr(p.extractor, "modelo", None),
                         "prompt": getattr(p.extractor, "prompt_version", None),
                         "consenso": args.consenso}
    print("\n" + json.dumps(res, indent=2, ensure_ascii=False))
    if args.salida:
        args.salida.write_text(json.dumps({"resumen": res, "casos": [asdict(f) for f in filas]},
                                          indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
