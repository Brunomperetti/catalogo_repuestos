from app.database import Base, engine
from app import models

print("Creando tablas...")
Base.metadata.create_all(bind=engine)
print("Listo.")
