"""Uso: python -m facturas.cli samples/*.pdf [--llm]"""
from __future__ import annotations

import argparse

from .extractores import ExtractorClaude, ExtractorReglas
from .pipeline import Pipeline

ICONOS = {"automatico": "OK ", "revision": "REV", "rechazado": "X  "}


def main() -> None:
    ap = argparse.ArgumentParser(description="Procesa facturas PDF y genera cobros")
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--llm", action="store_true", help="Usar Claude como extractor principal")
    ap.add_argument("--json", action="store_true", help="Imprimir el resultado completo")
    args = ap.parse_args()

    p = (Pipeline(extractor=ExtractorClaude(), segundo=ExtractorReglas())
         if args.llm else Pipeline())

    for ruta in args.pdfs:
        r = p.procesar_archivo(ruta)
        print(f"[{ICONOS[r.decision.value]}] {r.archivo}  ({r.metodo_texto}"
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
