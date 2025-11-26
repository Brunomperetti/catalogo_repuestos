from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "postgresql://catalogo_db_3kjj_user:epW1OkLFqrmWclI21RjeRmmI1BenzWih@dpg-d4j4e1ur433s7394ght0-a.oregon-postgres.render.com/catalogo_db_3kjj"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

