"""Modelos SQLAlchemy — CrumbPOS.

Estructura multi-tenant:
  Empresa → Sucursal → Caja
  Empresa → Familia → Artículo
  Sucursal ←→ Artículo (precio y disponibilidad por sucursal)
  Sucursal → Venta → VentaItem / Pago
  Sucursal → SesionCaja → Venta
"""
import uuid
from datetime import datetime, date

from sqlalchemy import (
    String, Integer, BigInteger, Boolean, Float, Text, Date, DateTime,
    ForeignKey, UniqueConstraint, Index, JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════
# TENANT: Empresa + Sucursal
# ═══════════════════════════════════════════════════════════════

class Empresa(Base):
    __tablename__ = "empresa"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    rut: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    razon_social: Mapped[str] = mapped_column(String(100), nullable=False)
    nombre_fantasia: Mapped[str | None] = mapped_column(String(100))
    giro: Mapped[str] = mapped_column(String(80), nullable=False)
    acteco: Mapped[int | None] = mapped_column(Integer)
    direccion: Mapped[str] = mapped_column(String(120), nullable=False)
    comuna: Mapped[str] = mapped_column(String(50), nullable=False)
    ciudad: Mapped[str] = mapped_column(String(50), nullable=False)
    logo_url: Mapped[str | None] = mapped_column(String(255))
    # SII
    ambiente_sii: Mapped[str] = mapped_column(String(15), default="certificacion")
    fecha_resolucion: Mapped[str | None] = mapped_column(String(10))
    numero_resolucion: Mapped[int] = mapped_column(Integer, default=0)
    # Certificado digital
    cert_path: Mapped[str | None] = mapped_column(String(255))  # path al .pfx
    cert_data: Mapped[str | None] = mapped_column(Text)  # .pfx en base64 (para cloud)
    cert_password: Mapped[str | None] = mapped_column(String(100))  # TODO: encriptar en prod
    cert_rut_firmante: Mapped[str | None] = mapped_column(String(12))
    # Config
    tasa_iva: Mapped[int] = mapped_column(Integer, default=19)
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relaciones
    sucursales: Mapped[list["Sucursal"]] = relationship(back_populates="empresa")
    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="empresa")
    familias: Mapped[list["Familia"]] = relationship(back_populates="empresa")
    articulos: Mapped[list["Articulo"]] = relationship(back_populates="empresa")


class Sucursal(Base):
    __tablename__ = "sucursal"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    codigo: Mapped[str | None] = mapped_column(String(10))
    direccion: Mapped[str] = mapped_column(String(120), nullable=False)
    comuna: Mapped[str] = mapped_column(String(50), nullable=False)
    ciudad: Mapped[str] = mapped_column(String(50), nullable=False)
    sii_sucursal: Mapped[str] = mapped_column(String(50), default="SANTIAGO ORIENTE")
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relaciones
    empresa: Mapped["Empresa"] = relationship(back_populates="sucursales")
    cajas: Mapped[list["Caja"]] = relationship(back_populates="sucursal")
    ventas: Mapped[list["Venta"]] = relationship(back_populates="sucursal")

    __table_args__ = (
        UniqueConstraint("empresa_id", "codigo", name="uq_sucursal_codigo"),
    )


class Caja(Base):
    __tablename__ = "caja"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sucursal_id: Mapped[str] = mapped_column(ForeignKey("sucursal.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(30), nullable=False)  # "Caja 1"
    activa: Mapped[bool] = mapped_column(Boolean, default=True)

    sucursal: Mapped["Sucursal"] = relationship(back_populates="cajas")


# ═══════════════════════════════════════════════════════════════
# USUARIOS
# ═══════════════════════════════════════════════════════════════

class Usuario(Base):
    __tablename__ = "usuario"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[str] = mapped_column(String(20), nullable=False)
    # Roles: super_admin, admin_empresa, admin_sucursal, cajero
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    empresa: Mapped["Empresa"] = relationship(back_populates="usuarios")
    sucursales_acceso: Mapped[list["UsuarioSucursal"]] = relationship(
        back_populates="usuario"
    )


class UsuarioSucursal(Base):
    """Acceso de un usuario a una o más sucursales."""
    __tablename__ = "usuario_sucursal"

    usuario_id: Mapped[str] = mapped_column(
        ForeignKey("usuario.id"), primary_key=True
    )
    sucursal_id: Mapped[str] = mapped_column(
        ForeignKey("sucursal.id"), primary_key=True
    )

    usuario: Mapped["Usuario"] = relationship(back_populates="sucursales_acceso")
    sucursal: Mapped["Sucursal"] = relationship()


# ═══════════════════════════════════════════════════════════════
# CLIENTES (maestro por empresa)
# ═══════════════════════════════════════════════════════════════

class Cliente(Base):
    """Cliente/receptor frecuente de una empresa."""
    __tablename__ = "cliente"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    rut: Mapped[str] = mapped_column(String(12), nullable=False)
    razon_social: Mapped[str] = mapped_column(String(120), nullable=False)
    nombre_fantasia: Mapped[str | None] = mapped_column(String(120))
    giro: Mapped[str] = mapped_column(String(80), nullable=False)
    direccion: Mapped[str] = mapped_column(String(150), nullable=False)
    comuna: Mapped[str] = mapped_column(String(50), nullable=False)
    ciudad: Mapped[str | None] = mapped_column(String(50))
    contacto_nombre: Mapped[str | None] = mapped_column(String(80))
    contacto_email: Mapped[str | None] = mapped_column(String(120))
    contacto_telefono: Mapped[str | None] = mapped_column(String(20))
    condicion_pago: Mapped[int | None] = mapped_column(Integer)  # dias: 0=contado, 15, 30, 60
    notas: Mapped[str | None] = mapped_column(Text)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    empresa: Mapped["Empresa"] = relationship()

    __table_args__ = (
        UniqueConstraint("empresa_id", "rut", name="uq_cliente_empresa_rut"),
        Index("ix_cliente_empresa_rut", "empresa_id", "rut"),
    )


# ═══════════════════════════════════════════════════════════════
# CATÁLOGO: Familias + Artículos (maestro por empresa)
# ═══════════════════════════════════════════════════════════════

class Familia(Base):
    __tablename__ = "familia"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(60), nullable=False)
    codigo: Mapped[str | None] = mapped_column(String(10))
    color: Mapped[str | None] = mapped_column(String(7))  # hex "#FF5733"
    icono: Mapped[str | None] = mapped_column(String(30))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("familia.id"))
    orden: Mapped[int] = mapped_column(Integer, default=0)
    activa: Mapped[bool] = mapped_column(Boolean, default=True)

    empresa: Mapped["Empresa"] = relationship(back_populates="familias")
    parent: Mapped["Familia | None"] = relationship(remote_side="Familia.id")
    articulos: Mapped[list["Articulo"]] = relationship(back_populates="familia")
    sucursales: Mapped[list["FamiliaSucursal"]] = relationship(
        back_populates="familia"
    )

    __table_args__ = (
        UniqueConstraint("empresa_id", "codigo", name="uq_familia_codigo"),
    )


class FamiliaSucursal(Base):
    """Activación de una familia en una sucursal."""
    __tablename__ = "familia_sucursal"

    familia_id: Mapped[str] = mapped_column(
        ForeignKey("familia.id"), primary_key=True
    )
    sucursal_id: Mapped[str] = mapped_column(
        ForeignKey("sucursal.id"), primary_key=True
    )
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    orden: Mapped[int] = mapped_column(Integer, default=0)

    familia: Mapped["Familia"] = relationship(back_populates="sucursales")
    sucursal: Mapped["Sucursal"] = relationship()


class Articulo(Base):
    __tablename__ = "articulo"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    familia_id: Mapped[str | None] = mapped_column(ForeignKey("familia.id"))
    sku: Mapped[str | None] = mapped_column(String(30))
    codigo_barras: Mapped[str | None] = mapped_column(String(30))
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    nombre_corto: Mapped[str | None] = mapped_column(String(30))  # para boleta térmica
    unidad_medida: Mapped[str] = mapped_column(String(5), default="UN")
    precio_default: Mapped[int] = mapped_column(Integer, default=0)  # con IVA
    costo_default: Mapped[int] = mapped_column(Integer, default=0)
    es_exento: Mapped[bool] = mapped_column(Boolean, default=False)
    es_compuesto: Mapped[bool] = mapped_column(Boolean, default=False)  # fase 3
    imagen_url: Mapped[str | None] = mapped_column(String(255))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    empresa: Mapped["Empresa"] = relationship(back_populates="articulos")
    familia: Mapped["Familia | None"] = relationship(back_populates="articulos")
    sucursales: Mapped[list["ArticuloSucursal"]] = relationship(
        back_populates="articulo"
    )

    __table_args__ = (
        UniqueConstraint("empresa_id", "sku", name="uq_articulo_sku"),
        Index("ix_articulo_barras", "codigo_barras"),
    )


class ArticuloSucursal(Base):
    """Precio y disponibilidad de un artículo en una sucursal."""
    __tablename__ = "articulo_sucursal"

    articulo_id: Mapped[str] = mapped_column(
        ForeignKey("articulo.id"), primary_key=True
    )
    sucursal_id: Mapped[str] = mapped_column(
        ForeignKey("sucursal.id"), primary_key=True
    )
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    precio_venta: Mapped[int | None] = mapped_column(Integer)  # NULL = usa default
    costo: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    articulo: Mapped["Articulo"] = relationship(back_populates="sucursales")
    sucursal: Mapped["Sucursal"] = relationship()


class PrecioHistorial(Base):
    """Auditoría de cambios de precio."""
    __tablename__ = "precio_historial"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    articulo_id: Mapped[str] = mapped_column(ForeignKey("articulo.id"), nullable=False)
    sucursal_id: Mapped[str | None] = mapped_column(ForeignKey("sucursal.id"))
    precio_anterior: Mapped[int] = mapped_column(Integer)
    precio_nuevo: Mapped[int] = mapped_column(Integer)
    usuario_id: Mapped[str | None] = mapped_column(ForeignKey("usuario.id"))
    fecha: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_precio_hist_articulo", "articulo_id", "fecha"),
    )


# ═══════════════════════════════════════════════════════════════
# VENTAS (POS)
# ═══════════════════════════════════════════════════════════════

class SesionCaja(Base):
    __tablename__ = "sesion_caja"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sucursal_id: Mapped[str] = mapped_column(ForeignKey("sucursal.id"), nullable=False)
    caja_id: Mapped[str] = mapped_column(ForeignKey("caja.id"), nullable=False)
    usuario_id: Mapped[str] = mapped_column(ForeignKey("usuario.id"), nullable=False)
    apertura_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    cierre_at: Mapped[datetime | None] = mapped_column(DateTime)
    monto_apertura: Mapped[int] = mapped_column(Integer, default=0)
    monto_cierre_esperado: Mapped[int | None] = mapped_column(Integer)
    monto_cierre_real: Mapped[int | None] = mapped_column(Integer)
    diferencia: Mapped[int | None] = mapped_column(Integer)
    observacion: Mapped[str | None] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(20), default="abierta")
    # Estados: abierta, cerrada, cerrada_forzada
    reporte_z: Mapped[dict | None] = mapped_column(JSON)

    ventas: Mapped[list["Venta"]] = relationship(back_populates="sesion_caja")

    __table_args__ = (
        Index("ix_sesion_sucursal", "sucursal_id", "apertura_at"),
    )


class Venta(Base):
    __tablename__ = "venta"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    sucursal_id: Mapped[str] = mapped_column(ForeignKey("sucursal.id"), nullable=False)
    sesion_caja_id: Mapped[str | None] = mapped_column(ForeignKey("sesion_caja.id"))
    usuario_id: Mapped[str] = mapped_column(ForeignKey("usuario.id"), nullable=False)
    fecha: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # DTE
    tipo_dte: Mapped[int] = mapped_column(Integer, nullable=False)  # 39, 41, 33, 34...
    folio: Mapped[int | None] = mapped_column(Integer)
    # Receptor (para facturas)
    receptor_rut: Mapped[str | None] = mapped_column(String(12))
    receptor_razon: Mapped[str | None] = mapped_column(String(100))
    receptor_giro: Mapped[str | None] = mapped_column(String(80))
    receptor_dir: Mapped[str | None] = mapped_column(String(120))
    receptor_comuna: Mapped[str | None] = mapped_column(String(50))
    # Montos
    monto_neto: Mapped[int | None] = mapped_column(Integer)
    monto_exento: Mapped[int | None] = mapped_column(Integer)
    iva: Mapped[int | None] = mapped_column(Integer)
    monto_total: Mapped[int] = mapped_column(Integer, default=0)
    # Estado
    estado: Mapped[str] = mapped_column(String(15), default="completada")
    # Estados: completada, anulada
    # Sync
    sync_status: Mapped[str] = mapped_column(String(15), default="pendiente")
    # Sync: pendiente, enviado, confirmado_sii, error_sii
    track_id_sii: Mapped[str | None] = mapped_column(String(20))
    # XML
    ted_xml: Mapped[str | None] = mapped_column(Text)
    xml_firmado: Mapped[str | None] = mapped_column(Text)

    # Relaciones
    empresa: Mapped["Empresa"] = relationship()
    sucursal: Mapped["Sucursal"] = relationship(back_populates="ventas")
    sesion_caja: Mapped["SesionCaja | None"] = relationship(back_populates="ventas")
    items: Mapped[list["VentaItem"]] = relationship(back_populates="venta")
    pagos: Mapped[list["Pago"]] = relationship(back_populates="venta")

    __table_args__ = (
        Index("ix_venta_fecha", "empresa_id", "sucursal_id", "fecha"),
        Index("ix_venta_folio", "empresa_id", "tipo_dte", "folio"),
    )


class VentaItem(Base):
    __tablename__ = "venta_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    venta_id: Mapped[str] = mapped_column(ForeignKey("venta.id"), nullable=False)
    articulo_id: Mapped[str | None] = mapped_column(ForeignKey("articulo.id"))
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    cantidad: Mapped[float] = mapped_column(Float, default=1)
    precio_unitario: Mapped[int] = mapped_column(Integer, default=0)
    descuento_pct: Mapped[float | None] = mapped_column(Float)
    descuento_monto: Mapped[int | None] = mapped_column(Integer)
    monto_linea: Mapped[int] = mapped_column(Integer, default=0)
    es_exento: Mapped[bool] = mapped_column(Boolean, default=False)

    venta: Mapped["Venta"] = relationship(back_populates="items")


class Pago(Base):
    __tablename__ = "pago"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    venta_id: Mapped[str] = mapped_column(ForeignKey("venta.id"), nullable=False)
    medio: Mapped[str] = mapped_column(String(20), nullable=False)
    # Medios: efectivo, debito, credito, transferencia
    monto: Mapped[int] = mapped_column(Integer, nullable=False)
    referencia: Mapped[str | None] = mapped_column(String(50))  # nro transacción

    venta: Mapped["Venta"] = relationship(back_populates="pagos")


# ═══════════════════════════════════════════════════════════════
# DTE / SII
# ═══════════════════════════════════════════════════════════════

class CafFolio(Base):
    """CAFs cargados por empresa y tipo DTE.

    Cada registro es un rango de folios autorizado por el SII.
    folio_actual avanza monotónicamente (nunca retrocede).
    Cuando folio_actual > rango_hasta → estado='agotado'.
    """
    __tablename__ = "caf_folio"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    tipo_dte: Mapped[int] = mapped_column(Integer, nullable=False)
    rango_desde: Mapped[int] = mapped_column(Integer, nullable=False)
    rango_hasta: Mapped[int] = mapped_column(Integer, nullable=False)
    folio_actual: Mapped[int] = mapped_column(Integer, nullable=False)
    caf_xml_raw: Mapped[bytes] = mapped_column(Text, nullable=False)  # XML completo del CAF
    rut_emisor: Mapped[str | None] = mapped_column(String(12))
    fecha_autorizacion: Mapped[str | None] = mapped_column(String(10))
    estado: Mapped[str] = mapped_column(String(10), default="activo")
    # Estados: activo, agotado
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_caf_empresa_tipo", "empresa_id", "tipo_dte", "estado"),
    )


class DteEmitido(Base):
    """Registro de cada DTE emitido — requerido para conservación 6 años (proveedor SII)."""
    __tablename__ = "dte_emitido"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    sucursal_id: Mapped[str | None] = mapped_column(ForeignKey("sucursal.id"), nullable=True)
    venta_id: Mapped[str | None] = mapped_column(ForeignKey("venta.id"))
    # DTE
    tipo_dte: Mapped[int] = mapped_column(Integer, nullable=False)
    folio: Mapped[int] = mapped_column(Integer, nullable=False)
    fecha_emision: Mapped[date] = mapped_column(Date, nullable=False)
    # Receptor
    receptor_rut: Mapped[str | None] = mapped_column(String(12))
    receptor_razon: Mapped[str | None] = mapped_column(String(100))
    # Montos
    monto_neto: Mapped[int | None] = mapped_column(Integer)
    monto_exento: Mapped[int | None] = mapped_column(Integer)
    iva: Mapped[int | None] = mapped_column(Integer)
    monto_total: Mapped[int] = mapped_column(Integer, default=0)
    # XML y firma (conservación obligatoria)
    xml_firmado: Mapped[str | None] = mapped_column(Text)
    ted_xml: Mapped[str | None] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(String(255))
    # SII
    track_id: Mapped[str | None] = mapped_column(String(20))
    estado_sii: Mapped[str] = mapped_column(String(15), default="pendiente")
    # Estados: pendiente, enviado, aceptado, rechazado, reparo
    glosa_sii: Mapped[str | None] = mapped_column(String(255))
    fecha_consulta_sii: Mapped[datetime | None] = mapped_column(DateTime)
    estado_receptor: Mapped[str | None] = mapped_column(String(15))
    # Estados receptor: pendiente, aceptado, reclamado
    # Sync
    sync_status: Mapped[str] = mapped_column(String(15), default="local")
    # Sync: local, enviado, confirmado
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    empresa: Mapped["Empresa"] = relationship()
    venta: Mapped["Venta | None"] = relationship()

    __table_args__ = (
        UniqueConstraint("empresa_id", "tipo_dte", "folio", name="uq_dte_folio"),
        Index("ix_dte_empresa_fecha", "empresa_id", "fecha_emision"),
        Index("ix_dte_track", "track_id"),
    )


class RcofDiario(Base):
    """RCOF enviado al SII por sucursal y día."""
    __tablename__ = "rcof_diario"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    sucursal_id: Mapped[str] = mapped_column(ForeignKey("sucursal.id"), nullable=False)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    xml_firmado: Mapped[str | None] = mapped_column(Text)
    track_id: Mapped[str | None] = mapped_column(String(20))
    estado_sii: Mapped[str] = mapped_column(String(15), default="pendiente")
    # Estados: pendiente, enviado, aceptado, rechazado
    resumen: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("empresa_id", "sucursal_id", "fecha", name="uq_rcof_dia"),
    )


class LibroGenerado(Base):
    """Libro de compra/venta/guía generado y enviado al SII."""
    __tablename__ = "libro_generado"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    empresa_id: Mapped[str] = mapped_column(ForeignKey("empresa.id"), nullable=False)
    tipo_libro: Mapped[str] = mapped_column(String(10), nullable=False)  # VENTA, COMPRA, GUIA
    periodo: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    xml_firmado: Mapped[str | None] = mapped_column(Text)
    track_id: Mapped[str | None] = mapped_column(String(20))
    estado_sii: Mapped[str] = mapped_column(String(15), default="pendiente")
    # Estados: pendiente, enviado, aceptado, rechazado
    resumen_json: Mapped[str | None] = mapped_column(Text)  # JSON with summary data
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    empresa: Mapped["Empresa"] = relationship()

    __table_args__ = (
        UniqueConstraint("empresa_id", "tipo_libro", "periodo", name="uq_libro_empresa_tipo_periodo"),
    )


class ArqueoDetalle(Base):
    """Detalle del arqueo por denominación."""
    __tablename__ = "arqueo_detalle"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sesion_caja_id: Mapped[str] = mapped_column(
        ForeignKey("sesion_caja.id"), nullable=False
    )
    denominacion: Mapped[int] = mapped_column(Integer, nullable=False)
    # 20000, 10000, 5000, 2000, 1000, 500, 100, 50, 10
    cantidad: Mapped[int] = mapped_column(Integer, default=0)
    subtotal: Mapped[int] = mapped_column(Integer, default=0)

    sesion_caja: Mapped["SesionCaja"] = relationship()


# ═══════════════════════════════════════════════════════════════
# STOCK (preparado para Fase 2, tablas creadas pero vacías)
# ═══════════════════════════════════════════════════════════════

class Bodega(Base):
    __tablename__ = "bodega"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    sucursal_id: Mapped[str] = mapped_column(ForeignKey("sucursal.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(50), nullable=False)
    tipo: Mapped[str] = mapped_column(String(20), default="venta")
    # Tipos: venta, produccion, insumos
    es_default: Mapped[bool] = mapped_column(Boolean, default=False)


class Stock(Base):
    __tablename__ = "stock"

    articulo_id: Mapped[str] = mapped_column(
        ForeignKey("articulo.id"), primary_key=True
    )
    bodega_id: Mapped[str] = mapped_column(
        ForeignKey("bodega.id"), primary_key=True
    )
    cantidad: Mapped[float] = mapped_column(Float, default=0)
    stock_minimo: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class MovimientoStock(Base):
    __tablename__ = "movimiento_stock"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    bodega_id: Mapped[str] = mapped_column(ForeignKey("bodega.id"), nullable=False)
    articulo_id: Mapped[str] = mapped_column(ForeignKey("articulo.id"), nullable=False)
    tipo: Mapped[str] = mapped_column(String(15), nullable=False)
    # Tipos: venta, compra, merma, traspaso, ajuste, produccion
    cantidad: Mapped[float] = mapped_column(Float, nullable=False)  # +/-
    referencia_id: Mapped[str | None] = mapped_column(String(36))
    referencia_tipo: Mapped[str | None] = mapped_column(String(20))
    usuario_id: Mapped[str | None] = mapped_column(ForeignKey("usuario.id"))
    fecha: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_mov_bodega_fecha", "bodega_id", "fecha"),
    )
