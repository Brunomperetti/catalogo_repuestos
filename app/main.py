from fastapi import FastAPI, UploadFile, File, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List
import pandas as pd
import zipfile
import shutil
import os
from urllib.parse import quote

# PDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from io import BytesIO

from app.database import SessionLocal, engine, Base
from app import models
from fastapi.templating import Jinja2Templates


app = FastAPI()

# ---------------------------------------------------
# EMPRESA ACTIVA (MVP)
# ---------------------------------------------------
EMPRESA_ACTIVA_ID = None


# ---------------------------------------------------
# STARTUP EVENT (CLAVE PARA RENDER)
# ---------------------------------------------------
@app.on_event("startup")
def on_startup():
    """
    Se ejecuta cuando la app YA levantó y el puerto está abierto.
    Acá recién tocamos la base.
    """
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------
# Static & Templates
# ---------------------------------------------------
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

IMAGES_PATH = "app/static/images/"



# ---------------------------------------------------
# DB Dependency
# ---------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from fastapi import Form
from fastapi.responses import JSONResponse

EMPRESAS_STATIC_DIR = "app/static/empresas"


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def save_upload_file(upload_file: UploadFile, dest_path: str):
    # guarda el archivo subido en disco
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(upload_file.file, f)


@app.post("/empresa/crear_panel")
def crear_empresa_panel(
    nombre: str = Form(...),
    slug: str = Form(...),
    whatsapp: str = Form(""),
    logo: UploadFile = File(None),
    banner: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """
    Crea una empresa y opcionalmente guarda logo + banner en:
    app/static/empresas/<slug>/logo.<ext>
    app/static/empresas/<slug>/banner.<ext>
    """
    nombre = (nombre or "").strip()
    slug = (slug or "").strip().lower()
    whatsapp = (whatsapp or "").strip()

    if not nombre or not slug:
        return JSONResponse({"error": "nombre y slug son obligatorios"}, status_code=400)

    existe = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if existe:
        return JSONResponse({"error": "Ya existe una empresa con ese slug"}, status_code=400)

    # crear empresa en DB
    empresa = models.Empresa(nombre=nombre, slug=slug, whatsapp=whatsapp)
    db.add(empresa)
    db.commit()
    db.refresh(empresa)

    # carpeta por empresa
    empresa_dir = os.path.join(EMPRESAS_STATIC_DIR, slug)
    ensure_dir(empresa_dir)

    # guardar logo
    if logo is not None:
        ext = os.path.splitext(logo.filename or "")[1].lower() or ".png"
        logo_path = os.path.join(empresa_dir, f"logo{ext}")
        save_upload_file(logo, logo_path)

        # si tu modelo tiene campos, guardamos la ruta relativa para usar en templates
        if hasattr(empresa, "logo_url"):
            empresa.logo_url = f"empresas/{slug}/logo{ext}"

    # guardar banner
    if banner is not None:
        ext = os.path.splitext(banner.filename or "")[1].lower() or ".jpg"
        banner_path = os.path.join(empresa_dir, f"banner{ext}")
        save_upload_file(banner, banner_path)

        if hasattr(empresa, "banner_url"):
            empresa.banner_url = f"empresas/{slug}/banner{ext}"

    db.commit()

    return {
        "status": "ok",
        "empresa_id": empresa.id,
        "slug": empresa.slug,
        "catalogo_url": f"/catalogo/{empresa.slug}",
    }
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
# SUBIR EXCEL
# ---------------------------------------------------
@app.post("/upload_excel")
def upload_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):

    try:
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
                models.Producto.empresa_id == 1,
            ).first()

            imagen_archivo = f"{codigo}.jpg"
            imagen_path = os.path.join(IMAGES_PATH, imagen_archivo)
            imagen_url = imagen_archivo if os.path.exists(imagen_path) else ""

            if existe:
                existe.descripcion = str(row.get("descripcion", "")) or existe.descripcion
                existe.categoria = (
                    str(row.get("categoria", "")) if "categoria" in df.columns else existe.categoria
                )
                existe.marca = (
                    str(row.get("marca", "")) if "marca" in df.columns else existe.marca
                )
                existe.precio = float(row.get("precio", existe.precio or 0))

                if "stock" in df.columns:
                    try:
                        existe.stock = int(row.get("stock", existe.stock or 0))
                    except Exception:
                        pass

                if imagen_url:
                    existe.imagen_url = imagen_url

                actualizados += 1

            else:
                stock_val = 0
                if "stock" in df.columns:
                    try:
                        stock_val = int(row.get("stock", 0))
                    except Exception:
                        pass

                producto = models.Producto(
                    empresa_id=1,
                    codigo=codigo,
                    descripcion=str(row.get("descripcion", "")),
                    categoria=str(row.get("categoria", "")) if "categoria" in df.columns else "",
                    marca=str(row.get("marca", "")) if "marca" in df.columns else "",
                    precio=float(row.get("precio", 0)),
                    stock=stock_val,
                    imagen_url=imagen_url,
                )

                db.add(producto)
                nuevos += 1

        db.commit()

        msg = quote(f"Excel procesado. Nuevos: {nuevos} | Actualizados: {actualizados}")
        return RedirectResponse(url=f"/?msg={msg}", status_code=303)

    except Exception as e:
        print("Error Excel:", e)
        msg = quote("Error procesando el archivo.")
        return RedirectResponse(url=f"/?error={msg}", status_code=303)


# ---------------------------------------------------
# SUBIR ZIP DE IMÁGENES
# ---------------------------------------------------
@app.post("/upload_zip")
def upload_zip(file: UploadFile = File(...)):

    try:
        temp_path = "temp_images.zip"

        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

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
# BORRAR TODOS LOS PRODUCTOS
# ---------------------------------------------------
@app.post("/delete_all_products")
def delete_all_products(db: Session = Depends(get_db)):

    try:
        db.query(models.Producto).delete()
        db.commit()

        msg = quote("Productos eliminados.")
        return RedirectResponse(url=f"/?msg={msg}", status_code=303)

    except Exception as e:
        print("Error borrando:", e)
        msg = quote("Error al borrar productos.")
        return RedirectResponse(url=f"/?error={msg}", status_code=303)


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

    categorias_raw = (
        db.query(models.Producto.categoria)
        .filter(models.Producto.empresa_id == empresa.id)
        .distinct()
        .all()
    )

    categorias = sorted([c[0] for c in categorias_raw if c[0]])

    productos_json = [
        {
            "id": p.id,
            "codigo": p.codigo,
            "descripcion": p.descripcion,
            "marca": p.marca,
            "precio": p.precio,
            "stock": p.stock,
            "imagen_url": p.imagen_url,
            "categoria": p.categoria,
        }
        for p in productos
    ]

    return templates.TemplateResponse(
        "catalogo.html",
        {
            "request": request,
            "productos": productos,
            "productos_json": productos_json,
            "empresa": empresa,
            "categorias": categorias,
            "categoria_actual": categoria,
            "query": q,
        },
    )


# ---------------------------------------------------
# PDF DEL PEDIDO
# ---------------------------------------------------
@app.post("/pedido/pdf")
async def generar_pdf(data: dict):

    empresa = data.get("empresa", "Pedido de Cliente")
    items: List[dict] = data.get("items", [])

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)

    width, height = A4
    y = height - 40

    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, y, empresa)
    y -= 40

    c.setFont("Helvetica", 11)
    total = 0

    for item in items:
        c.drawString(40, y, f'{item["cantidad"]}x {item["codigo"]} - {item["descripcion"]}')
        y -= 18

        subtotal = item["precio"] * item["cantidad"]
        c.drawString(60, y, f'Precio: ${item["precio"]:.2f} - Subtotal: ${subtotal:.2f}')
        y -= 22

        total += subtotal

        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - 40

    c.setFont("Helvetica-Bold", 14)
    y -= 20
    c.drawString(40, y, f"TOTAL: ${total:.2f}")

    c.save()
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=pedido.pdf"}
    )


# ---------------------------------------------------
# CREAR EMPRESA
# ---------------------------------------------------
@app.post("/empresa/crear")
def crear_empresa(data: dict, db: Session = Depends(get_db)):

    nombre = data.get("nombre")
    slug = data.get("slug")
    whatsapp = data.get("whatsapp", "")

    if not nombre or not slug:
        return {"error": "nombre y slug son obligatorios"}

    existe = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if existe:
        return {"error": "Ya existe una empresa con ese slug"}

    empresa = models.Empresa(
        nombre=nombre,
        slug=slug,
        whatsapp=whatsapp
    )

    db.add(empresa)
    db.commit()
    db.refresh(empresa)

    return {
        "status": "ok",
        "empresa_id": empresa.id,
        "slug": empresa.slug
    }


# ---------------------------------------------------
# LISTAR EMPRESAS (DEBUG)
# ---------------------------------------------------
@app.get("/debug/empresas")
def listar_empresas(db: Session = Depends(get_db)):
    empresas = db.query(models.Empresa).all()
    return [
        {
            "id": e.id,
            "nombre": e.nombre,
            "slug": e.slug,
            "whatsapp": e.whatsapp
        }
        for e in empresas
    ]

