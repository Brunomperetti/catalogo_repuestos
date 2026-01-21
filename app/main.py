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
# EMPRESA ACTIVA (en memoria)
# ---------------------------------------------------
EMPRESA_ACTIVA_ID = None

def get_empresa_activa(db: Session):
    """
    Si hay EMPRESA_ACTIVA_ID seteada, usa esa.
    Si no, usa la última creada (fallback).
    """
    global EMPRESA_ACTIVA_ID

    if EMPRESA_ACTIVA_ID is not None:
        empresa = db.query(models.Empresa).filter(models.Empresa.id == EMPRESA_ACTIVA_ID).first()
        if empresa:
            return empresa

    # fallback: última creada
    return db.query(models.Empresa).order_by(models.Empresa.id.desc()).first()


@app.get("/empresa/activar/{slug}")
def activar_empresa(slug: str, db: Session = Depends(get_db)):
    """
    Setea la empresa activa por slug.
    Luego, /upload_excel y /upload_zip cargan en esa empresa.
    """
    global EMPRESA_ACTIVA_ID

    slug = (slug or "").strip().lower()
    empresa = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if not empresa:
        return {"error": "Empresa no encontrada", "slug": slug}

    EMPRESA_ACTIVA_ID = empresa.id
    return {"status": "ok", "empresa_activa": {"id": empresa.id, "slug": empresa.slug, "nombre": empresa.nombre}}


@app.get("/empresa/activa")
def ver_empresa_activa(db: Session = Depends(get_db)):
    """
    Devuelve qué empresa está activa ahora.
    """
    empresa = get_empresa_activa(db)
    if not empresa:
        return {"empresa_activa": None}
    return {"empresa_activa": {"id": empresa.id, "slug": empresa.slug, "nombre": empresa.nombre}}

@app.post("/empresa/actualizar_imagenes")
async def actualizar_imagenes_empresa(
    logo: UploadFile = File(None),
    banner: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    empresa = get_empresa_activa(db)
    if not empresa:
        return RedirectResponse(url="/?error=No hay empresa activa", status_code=303)

    base_path = Path("app/static/empresas") / empresa.slug
    base_path.mkdir(parents=True, exist_ok=True)

    if logo:
        with open(base_path / "logo.png", "wb") as f:
            f.write(await logo.read())

    if banner:
        with open(base_path / "banner.jpg", "wb") as f:
            f.write(await banner.read())

    return RedirectResponse(url="/?msg=Imágenes actualizadas", status_code=303)


@app.post("/empresa/activar_panel")
def activar_empresa_panel(
    slug: str = Form(...),
    db: Session = Depends(get_db),
):
    global EMPRESA_ACTIVA_ID

    slug = (slug or "").strip().lower()
    empresa = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()

    if not empresa:
        return RedirectResponse(url="/?error=Empresa no encontrada", status_code=303)

    EMPRESA_ACTIVA_ID = empresa.id
    return RedirectResponse(url="/", status_code=303)

@app.get("/admin/productos", response_class=HTMLResponse)
def admin_productos(request: Request, db: Session = Depends(get_db)):
    empresa = get_empresa_activa(db)
    if not empresa:
        return HTMLResponse("<h1>No hay empresa activa</h1>", status_code=400)

    productos = (
        db.query(models.Producto)
        .filter(models.Producto.empresa_id == empresa.id)
        .order_by(models.Producto.codigo)
        .all()
    )

    return templates.TemplateResponse(
        "admin_productos.html",
        {
            "request": request,
            "empresa": empresa,
            "productos": productos,
        },
    )

@app.post("/admin/productos/{producto_id}/actualizar")
async def actualizar_producto(
    producto_id: int,
    descripcion: str = Form(...),
    precio: float = Form(...),
    activo: bool = Form(False),
    imagen: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    producto = db.query(models.Producto).filter(models.Producto.id == producto_id).first()
    if not producto:
        return RedirectResponse(url="/admin/productos", status_code=303)

    producto.descripcion = descripcion
    producto.precio = precio
    producto.activo = activo

    # actualizar imagen individual
    if imagen:
        empresa = producto.empresa
        img_path = Path(f"app/static/empresas/{empresa.slug}/productos")
        img_path.mkdir(parents=True, exist_ok=True)

        # borrar imágenes viejas
        for ext in [".jpg", ".png", ".jpeg", ".webp"]:
            old = img_path / f"{producto.codigo}{ext}"
            if old.exists():
                old.unlink()

        # guardar nueva imagen
        ext = Path(imagen.filename).suffix.lower()
        filename = f"{producto.codigo}{ext}"

        with open(img_path / filename, "wb") as f:
            f.write(await imagen.read())

    db.commit()
    return RedirectResponse(url="/admin/productos", status_code=303)

@app.get("/admin/borrar_empresa/{empresa_id}")
def borrar_empresa_get(empresa_id: int, db: Session = Depends(get_db)):
    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    if not empresa:
        return HTMLResponse("<h1>Empresa no encontrada</h1>", status_code=404)

    slug = empresa.slug

    # borrar de DB
    db.delete(empresa)
    db.commit()

    # borrar carpeta estática
    path = Path(f"app/static/empresas/{slug}")
    if path.exists():
        shutil.rmtree(path)

    return HTMLResponse(
        f"<h1>Empresa {slug} eliminada correctamente</h1>"
    )


    


# ---------------------------------------------------
# HOME PANEL
# ---------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def upload_view(request: Request, msg: str = "", error: str = "", db: Session = Depends(get_db)):
    empresas = db.query(models.Empresa).order_by(models.Empresa.nombre).all()
    empresa_activa = get_empresa_activa(db)
    import time


    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "msg": msg,
            "error": error,
            "empresas": empresas,
            "empresa_activa": empresa_activa,
            "time": int(time.time())
        },
    )

# ---------------------------------------------------
# CREAR EMPRESA
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

            imagen_url = ""

            for ext in [".jpg", ".png", ".jpeg", ".webp"]:
                nombre = f"{codigo}{ext}"
                path_img = os.path.join(IMAGES_PATH, nombre)
                if os.path.exists(path_img):
                    imagen_url = nombre
                    break


            if existe:
                existe.descripcion = str(row.get("descripcion", "")) or existe.descripcion
                existe.precio = float(row.get("precio", existe.precio))
                actualizados += 1
            else:
                producto = models.Producto(
                codigo=codigo,
                descripcion=str(row.get("descripcion", "")),
                precio=float(row.get("precio", 0)),
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
# SUBIR ZIP
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
# CATÁLOGO
# ---------------------------------------------------
@app.get("/catalogo/{slug}", response_class=HTMLResponse)
def catalogo(
    slug: str,
    request: Request,
    q: str = "",
    categoria: str = "",
    orden: str = "",
    db: Session = Depends(get_db)
):

    empresa = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if not empresa:
        return HTMLResponse("<h1>Empresa no encontrada</h1>", status_code=404)

    query_db = db.query(models.Producto).filter(
        models.Producto.empresa_id == empresa.id,
        models.Producto.activo == True
    )


    if q:
        query_db = query_db.filter(models.Producto.descripcion.ilike(f"%{q}%"))
        
    if categoria:
        query_db = query_db.filter(models.Producto.categoria == categoria)
    
    # ORDEN
    if orden == "precio-asc":
        query_db = query_db.order_by(models.Producto.precio.asc())
    elif orden == "precio-desc":
        query_db = query_db.order_by(models.Producto.precio.desc())
    elif orden == "codigo-asc":
         query_db = query_db.order_by(models.Producto.codigo.asc())
    elif orden == "marca-asc":
        query_db = query_db.order_by(models.Producto.marca.asc())

    

    productos = query_db.all()

    # armar imagen_url dinámicamente por código
    for p in productos:
        base_path = f"app/static/empresas/{empresa.slug}/productos"
        png_path = f"{base_path}/{p.codigo}.png"
        jpg_path = f"{base_path}/{p.codigo}.jpg"

        if os.path.exists(png_path):
            p.imagen_url = f"/static/empresas/{empresa.slug}/productos/{p.codigo}.png"
        elif os.path.exists(jpg_path):
            p.imagen_url = f"/static/empresas/{empresa.slug}/productos/{p.codigo}.jpg"
        else:
            p.imagen_url = "/static/img/no-image.png"



    categorias = sorted(list({p.categoria for p in productos if p.categoria}))

    productos_json = [
        {
            "id": p.id,
            "codigo": p.codigo,
            "descripcion": p.descripcion,
            "precio": p.precio,
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
        "orden_actual": orden,
        "query": q,
    },
    )



# ---------------------------------------------------
# PDF
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

    # borrar carpeta física
    empresa_path = Path(f"app/static/empresas/{empresa.slug}")
    if empresa_path.exists():
        shutil.rmtree(empresa_path)

    # borrar DB (productos se borran por cascade)
    db.delete(empresa)
    db.commit()

    return {"status": "ok"}



# ---------------------------------------------------
# DEBUG: LISTAR ARCHIVOS DE IMAGEN DE UNA EMPRESA
# ---------------------------------------------------
@app.get("/debug/imagenes/{slug}")
def debug_imagenes(slug: str):
    slug = (slug or "").strip().lower()
    path = Path(f"app/static/empresas/{slug}/productos")
    if not path.exists():
        return {"error": "Carpeta no existe", "path": str(path)}

    files = sorted([p.name for p in path.iterdir() if p.is_file()])
    # devolvemos solo los primeros 200 para no explotar la respuesta
    return {"path": str(path), "count": len(files), "files": files[:200]}

