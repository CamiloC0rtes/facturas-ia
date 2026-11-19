"""Lectura segura del ZIP que llega por correo con cada factura electrónica.

El ZIP es un archivo externo: se lee en memoria con límites de cantidad y tamaño
(evita zip bombs) y nunca se extrae a disco ni se ejecuta nada de su contenido.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

from .dian_xml import es_factura_xml

MAX_ARCHIVOS = 20
MAX_BYTES = 20 * 1024 * 1024  # por archivo, descomprimido


class PaqueteInvalido(Exception):
    pass


@dataclass
class Paquete:
    pdf: bytes | None = None
    xml_factura: bytes | None = None
    otros_xml: list[bytes] = field(default_factory=list)  # p. ej. respuesta DIAN aparte


def leer_zip(datos: bytes, profundidad: int = 0) -> Paquete:
    try:
        zf = zipfile.ZipFile(io.BytesIO(datos))
    except zipfile.BadZipFile as e:
        raise PaqueteInvalido("no es un ZIP válido") from e
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ARCHIVOS:
        raise PaqueteInvalido(f"el ZIP trae {len(infos)} archivos (máx. {MAX_ARCHIVOS})")

    p = Paquete()
    for info in infos:
        if info.file_size > MAX_BYTES:
            raise PaqueteInvalido(f"{info.filename} es demasiado grande")
        contenido = zf.read(info)
        nombre = info.filename.lower()
        if contenido[:4] == b"%PDF":
            p.pdf = p.pdf or contenido
        elif nombre.endswith(".zip") and profundidad == 0:  # algunos proveedores anidan un ZIP
            interno = leer_zip(contenido, profundidad + 1)
            p.pdf = p.pdf or interno.pdf
            p.xml_factura = p.xml_factura or interno.xml_factura
            p.otros_xml += interno.otros_xml
        elif contenido.lstrip(b"\xef\xbb\xbf \r\n\t")[:1] == b"<":  # XML con cualquier extensión (.xml, .fe, .dian)
            if p.xml_factura is None and es_factura_xml(contenido):
                p.xml_factura = contenido
            else:
                p.otros_xml.append(contenido)
    return p
