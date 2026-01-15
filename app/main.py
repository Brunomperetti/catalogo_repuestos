from fastapi import FastAPI, UploadFile, File, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import List
import pandas as pd
import zipfile
import shutil
import os
import re
from urllib.parse import quote
from pathlib import Path
from io import BytesIO

# PDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

from app.database import SessionLocal, engine, Base
from app import models

app = FastAPI()

# ---------------------------------------------------
# STARTUP (Render-safe)
# ---------------------------------------------------
@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)

# ---------------------------------------------------
# Static & Templates
# ---------------------------------------------------
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# ---------------------------------------------------
# DB Dependency
# ---------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------------------------------
# Empresa activa (última creada)
# ---------------------------------------------------
def get_empresa_activa(db: Session):
    return (
        db.query(models.Empresa)
        .order_by(models.Empresa.id.desc())
        .first()
    )

# ---------------------------------------------------
# HOME PANEL
# ---------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def upload_view(request: Request, msg: str = "", error: str = ""):
    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "msg": msg,
            "error": error,
        },
    )

# ---------------------------------------------------
# CREAR EMPRESA (ÚNICO ENDPOINT)
# ---------------------------------------------------
@app.post("/empresa/crear_panel")
async def crear_empresa_panel(
    nombre: str = Form(...),
    slug: str = Form(...),
    whatsapp: str = Form(""),
    logo: UploadFile = File(None),
    banner: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    nombre = nombre.strip()
    slug = slug.strip().lower()
    slug = re.sub(r"[^a-z0-9\-]", "-", slug)
    slug = re.sub(r"-+", "-", slug)

    if not nombre or not slug:
        return RedirectResponse(url="/?error=Datos incompletos", status_code=303)

    existe = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if existe:
        return RedirectResponse(url="/?error=La empresa ya existe", status_code=303)

    empresa = models.Empresa(
        nombre=nombre,
        slug=slug,
        whatsapp=whatsapp.strip()
    )
    db.add(empresa)
    db.commit()
    db.refresh(empresa)

    base_path = Path("app/static/empresas") / empresa.slug
    productos_path = base_path / "productos"
    base_path.mkdir(parents=True, exist_ok=True)
    productos_path.mkdir(exist_ok=True)

    if logo:
        with open(base_path / "logo.png", "wb") as f:
            f.write(await logo.read())

    if banner:
        with open(base_path / "banner.jpg", "wb") as f:
            f.write(await banner.read())

    return RedirectResponse(
        url="/?msg=Empresa creada correctamente",
        status_code=303
    )
    # ---------------------------------------------------
# SUBIR EXCEL
# ---------------------------------------------------
@app.post("/upload_excel")
def upload_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):

    try:
        empresa = get_empresa_activa(db)
        if not empresa:
            msg = quote("No hay empresa activa. Creá una empresa primero.")
            return RedirectResponse(url=f"/?error={msg}", status_code=303)

        IMAGES_PATH = f"app/static/empresas/{empresa.slug}/productos/"
        os.makedirs(IMAGES_PATH, exist_ok=True)

        df = pd.read_excel(file.file)
        df.columns = [c.strip().lower() for c in df.columns]

        required = ["codigo", "descripcion", "precio"]
        for col in required:
            if col not in df.columns:
                msg = quote(f"Falta columna obligatoria: {col}")
                return RedirectResponse(url=f"/?error={msg}", status_code=303)

        nuevos = 0
        actualizados = 0

        for _, row in df.iterrows():
            codigo = str(row.get("codigo", "")).strip()
            if not codigo:
                continue

            existe = db.query(models.Producto).filter(
                models.Producto.codigo == codigo,
                models.Producto.empresa_id == empresa.id
            ).first()

            imagen_archivo = f"{codigo}.jpg"
            imagen_path = os.path.join(IMAGES_PATH, imagen_archivo)
            imagen_url = imagen_archivo if os.path.exists(imagen_path) else ""

            if existe:
                existe.descripcion = str(row.get("descripcion", "")) or existe.descripcion
                existe.precio = float(row.get("precio", existe.precio))
                existe.imagen = imagen_url or existe.imagen
                actualizados += 1
            else:
                producto = models.Producto(
                    codigo=codigo,
                    descripcion=str(row.get("descripcion", "")),
                    precio=float(row.get("precio", 0)),
                    imagen=imagen_url,
                    empresa_id=empresa.id
                )
                db.add(producto)
                nuevos += 1

        db.commit()

        msg = quote(f"Productos cargados. Nuevos: {nuevos}, Actualizados: {actualizados}")
        return RedirectResponse(url=f"/?msg={msg}", status_code=303)

    except Exception as e:
        print("Error Excel:", e)
        msg = quote("Error al procesar el Excel.")
        return RedirectResponse(url=f"/?error={msg}", status_code=303)

# ---------------------------------------------------
# SUBIR ZIP DE IMÁGENES
# ---------------------------------------------------
@app.post("/upload_zip")
def upload_zip(file: UploadFile = File(...), db: Session = Depends(get_db)):

    try:
        temp_path = "temp_images.zip"

        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        empresa = get_empresa_activa(db)
        if not empresa:
            msg = quote("No hay empresa activa.")
            return RedirectResponse(url=f"/?error={msg}", status_code=303)

        IMAGES_PATH = f"app/static/empresas/{empresa.slug}/productos/"
        os.makedirs(IMAGES_PATH, exist_ok=True)

        with zipfile.ZipFile(temp_path, "r") as zip_ref:
            zip_ref.extractall(IMAGES_PATH)

        os.remove(temp_path)

        msg = quote("Imágenes cargadas correctamente.")
        return RedirectResponse(url=f"/?msg={msg}", status_code=303)

    except Exception as e:
        print("Error ZIP:", e)
        msg = quote("Error al procesar el ZIP.")
        return RedirectResponse(url=f"/?error={msg}", status_code=303)

# ---------------------------------------------------
# BORRAR PRODUCTOS
# ---------------------------------------------------
@app.post("/delete_all_products")
def delete_all_products(db: Session = Depends(get_db)):
    db.query(models.Producto).delete()
    db.commit()
    msg = quote("Productos eliminados.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)

# ---------------------------------------------------
# CATÁLOGO
# ---------------------------------------------------
@app.get("/catalogo/{slug}", response_class=HTMLResponse)
def catalogo(slug: str, request: Request, q: str = "", categoria: str = "", db: Session = Depends(get_db)):

    empresa = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if not empresa:
        return HTMLResponse("<h1>Empresa no encontrada</h1>", status_code=404)

    query_db = db.query(models.Producto).filter(models.Producto.empresa_id == empresa.id)

    if q:
        query_db = query_db.filter(models.Producto.descripcion.ilike(f"%{q}%"))
    if categoria:
        query_db = query_db.filter(models.Producto.categoria == categoria)

    productos = query_db.all()

    categorias = sorted({
        p.categoria for p in productos if p.categoria
    })

    return templates.TemplateResponse(
        "catalogo.html",
        {
            "request": request,
            "productos": productos,
            "empresa": empresa,
            "categorias": categorias,
            "categoria_actual": categoria,
            "query": q,
        },
    )

# ---------------------------------------------------
# PDF PEDIDO
# ---------------------------------------------------
@app.post("/pedido/pdf")
async def generar_pdf(data: dict):

    empresa = data.get("empresa", "Pedido")
    items: List[dict] = data.get("items", [])

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)

    y = A4[1] - 40
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, y, empresa)
    y -= 40

    total = 0
    c.setFont("Helvetica", 11)

    for item in items:
        subtotal = item["precio"] * item["cantidad"]
        c.drawString(40, y, f'{item["cantidad"]}x {item["codigo"]} - {item["descripcion"]}')
        y -= 18
        c.drawString(60, y, f'${subtotal:.2f}')
        y -= 22
        total += subtotal

    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"TOTAL: ${total:.2f}")

    c.save()
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=pedido.pdf"}
    )

# ---------------------------------------------------
# DEBUG
# ---------------------------------------------------
@app.get("/debug/empresas")
def listar_empresas(db: Session = Depends(get_db)):
    return [
        {
            "id": e.id,
            "nombre": e.nombre,
            "slug": e.slug,
            "whatsapp": e.whatsapp
        }
        for e in db.query(models.Empresa).all()
    ]

@app.post("/empresa/borrar/{empresa_id}")
def borrar_empresa(empresa_id: int, db: Session = Depends(get_db)):
    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    if not empresa:
        return {"error": "Empresa no encontrada"}
    db.delete(empresa)
    db.commit()
    return {"status": "ok"}


