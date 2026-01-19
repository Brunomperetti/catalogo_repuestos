from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from .database import Base


class Empresa(Base):
    __tablename__ = "empresas"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True)
    whatsapp = Column(String, nullable=True)

    # PASO 5 — estado de publicación del catálogo
    publicado = Column(Boolean, default=False, nullable=False)

    productos = relationship(
        "Producto",
        back_populates="empresa",
        cascade="all, delete-orphan"
    )


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

    # Imagen del producto (se resuelve dinámicamente,
    # pero dejamos la columna para futuras ediciones individuales)
    imagen_url = Column(String, nullable=True)

    empresa = relationship("Empresa", back_populates="productos")

