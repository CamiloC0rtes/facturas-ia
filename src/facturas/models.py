"""Modelos de datos. Cada campo extraído lleva su evidencia textual."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Campo(BaseModel, Generic[T]):
    """Un valor extraído + el fragmento literal del documento de donde salió.

    Si el extractor no puede leer el valor, debe devolver `valor=None`
    en lugar de inventarlo.
    """

    valor: Optional[T] = None
    evidencia: Optional[str] = Field(
        default=None, description="Texto literal del documento que respalda el valor"
    )


class LineaFactura(BaseModel):
    descripcion: str
    cantidad: Decimal
    valor_unitario: Decimal
    total: Decimal


class FacturaExtraida(BaseModel):
    numero: Campo[str] = Campo()
    nit_emisor: Campo[str] = Campo()
    nit_cliente: Campo[str] = Campo()
    fecha_emision: Campo[date] = Campo()
    fecha_vencimiento: Campo[date] = Campo()
    subtotal: Campo[Decimal] = Campo()
    iva: Campo[Decimal] = Campo()
    total: Campo[Decimal] = Campo()
    lineas: list[LineaFactura] = []

    CAMPOS_CRITICOS: ClassVar[tuple[str, ...]] = (
        "numero",
        "nit_emisor",
        "nit_cliente",
        "fecha_emision",
        "subtotal",
        "iva",
        "total",
    )


class Severidad(str, Enum):
    ERROR = "error"
    ADVERTENCIA = "advertencia"


class Hallazgo(BaseModel):
    regla: str
    severidad: Severidad
    mensaje: str


class Decision(str, Enum):
    AUTOMATICO = "automatico"  # pasa todo: se genera el cobro
    REVISION = "revision"  # va a revisión humana
    RECHAZADO = "rechazado"  # imagen ilegible / duplicado: pedir reenvío


class Cobro(BaseModel):
    factura_numero: str
    nit_cliente: str
    valor: Decimal
    fecha_limite: date
    estado: str = "borrador"
    concepto: str


class Resultado(BaseModel):
    archivo: str
    sha256: str
    metodo_texto: str  # "nativo" | "ocr"
    calidad: Optional[float] = None
    factura: Optional[FacturaExtraida] = None
    hallazgos: list[Hallazgo] = []
    decision: Decision
    cobro: Optional[Cobro] = None
