from app.database import SessionLocal
from app.models import Empresa

db = SessionLocal()

empresa = Empresa(
    nombre="Mi Empresa Demo",
    slug="demo",
    whatsapp="5493510000000"
)

db.add(empresa)
db.commit()

print("ID de la empresa creada:", empresa.id)

db.close()
