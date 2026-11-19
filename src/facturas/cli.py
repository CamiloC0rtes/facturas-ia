"""Uso: python -m facturas.cli facturas/*.zip samples/*.pdf [--llm openai|claude]

Un .zip (PDF + XML DIAN) o un .xml usan el XML como fuente; un .pdf usa el extractor."""
from __future__ import annotations

import argparse

from .extractores import ExtractorReglas, extractor_llm
from .pipeline import Pipeline

ICONOS = {"automatico": "OK ", "revision": "REV", "rechazado": "X  "}


def main() -> None:
    ap = argparse.ArgumentParser(description="Procesa facturas PDF y genera cobros")
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--llm", choices=["openai", "claude"],
                    help="LLM como extractor principal (las reglas quedan como consenso)")
    ap.add_argument("--json", action="store_true", help="Imprimir el resultado completo")
    args = ap.parse_args()

    p = (Pipeline(extractor=extractor_llm(args.llm), segundo=ExtractorReglas())
         if args.llm else Pipeline())

    for ruta in args.pdfs:
        r = p.procesar_archivo(ruta)
        print(f"[{ICONOS[r.decision.value]}] {r.archivo}  ({r.fuente or r.metodo_texto}"
              + (f", nitidez={r.calidad:.0f}" if r.calidad is not None else "") + ")")
        for h in r.hallazgos:
            print(f"       - {h.severidad.value}: {h.mensaje}")
        if r.cobro:
            valor = f"{r.cobro.valor:,.0f}".replace(",", ".")
            print(f"       -> cobro {r.cobro.estado}: ${valor} "
                  f"a {r.cobro.nit_cliente} antes de {r.cobro.fecha_limite}")
        if args.json:
            print(r.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
