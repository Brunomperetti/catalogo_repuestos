from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey, DateTime, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .database import Base


class Empresa(Base):
    __tablename__ = "empresas"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True)
    whatsapp = Column(String, nullable=True)
    logo_url = Column(String, nullable=True)
    banner_url = Column(String, nullable=True)
    politica_precio_catalogo = Column(String, nullable=False, default="automatico")
    politica_stock_catalogo = Column(String, nullable=False, default="mostrar")

    productos = relationship(
        "Producto",
        back_populates="empresa",
        cascade="all, delete-orphan"
    )
    usuarios = relationship("Usuario", back_populates="empresa")
    leads = relationship("CatalogLead", back_populates="empresa_rel", cascade="all, delete-orphan")
    lead_events = relationship("CatalogLeadEvent", back_populates="empresa", cascade="all, delete-orphan")


class Producto(Base):
    __tablename__ = "productos"

    id = Column(Integer, primary_key=True, index=True)

    empresa_id = Column(
        Integer,
        ForeignKey("empresas.id", ondelete="CASCADE"),
        nullable=False
    )

    codigo = Column(String, nullable=False)
    descripcion = Column(String, nullable=False)

    categoria = Column(String, nullable=True)
    marca = Column(String, nullable=True)

    precio = Column(Float, nullable=False)
    stock = Column(Integer, default=0)
    activo = Column(Boolean, default=True)

    # Imagen del producto (para edición individual futura)
    imagen_url = Column(String, nullable=True)

    empresa = relationship("Empresa", back_populates="productos")


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    rol = Column(String, nullable=False, default="cliente")  # admin | cliente
    activo = Column(Boolean, nullable=False, default=True)

    empresa_id = Column(
        Integer,
        ForeignKey("empresas.id", ondelete="SET NULL"),
        nullable=True
    )

    empresa = relationship("Empresa", back_populates="usuarios")


class CatalogLead(Base):
    __tablename__ = "catalog_leads"

    id = Column(Integer, primary_key=True, index=True)
    empresa_catalogo_id = Column(
        Integer,
        ForeignKey("empresas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    nombre = Column(String, nullable=False)
    empresa = Column(String, nullable=False)
    email = Column(String, nullable=False, index=True)
    telefono = Column(String, nullable=True)
    fecha_ingreso = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    ultima_actividad = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    session_token = Column(String, nullable=True, index=True)

    empresa_rel = relationship("Empresa", back_populates="leads")
    eventos = relationship("CatalogLeadEvent", back_populates="lead", cascade="all, delete-orphan")


class CatalogLeadEvent(Base):
    __tablename__ = "catalog_lead_events"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(
        Integer,
        ForeignKey("catalog_leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    empresa_catalogo_id = Column(
        Integer,
        ForeignKey("empresas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = Column(String, nullable=False, index=True)
    product_code = Column(String, nullable=True)
    search_term = Column(String, nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    lead = relationship("CatalogLead", back_populates="eventos")
    empresa = relationship("Empresa", back_populates="lead_events")
