from fastapi import FastAPI, UploadFile, File, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse   # >>> AGREGADO PARA PDF <<<
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List
import pandas as pd
import zipfile
import shutil
import os
from urllib.parse import quote

# >>> AGREGADO PARA PDF <<<
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from io import BytesIO
# >>> FIN AGREGADO <<<

from app.database import engine, Base, get_db
from app import models
from fastapi.templating import Jinja2Templates

# Crear tablas
Base.metadata.create_all(bind=engine)

app = FastAPI()

# STATIC
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Templates
templates = Jinja2Templates(directory="app/templates")

IMAGES_PATH = "app/static/images/"


# ░░░░░ HOME (PANEL) ░░░░░
@app.get("/", response_class=HTMLResponse)
def upload_view(
    request: Request,
    msg: str = "",
    error: str = "",
):
    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "msg": msg,
            "error": error,
        },
    )


# ░░░░░ SUBIR EXCEL ░░░░░
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
            imagen_url = imagen_archivo if os.path.exists(os.path.join(IMAGES_PATH, imagen_archivo)) else ""

            if existe:
                existe.descripcion = str(row.get("descripcion", "")) or existe.descripcion
                existe.categoria = str(row.get("categoria", "")) if "categoria" in df.columns else existe.categoria
                existe.marca = str(row.get("marca", "")) if "marca" in df.columns else existe.marca
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
                        stock_val = 0

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

        msg = quote(f"Excel procesado con éxito. Nuevos: {nuevos} | Actualizados: {actualizados}")
        return RedirectResponse(url=f"/?msg={msg}", status_code=303)

    except Exception as e:
        print("Error al procesar Excel:", e)
        msg = quote("Error de conexión al enviar el Excel.")
        return RedirectResponse(url=f"/?error={msg}", status_code=303)


# ░░░░░ SUBIR ZIP ░░░░░
@app.post("/upload_zip")
def upload_zip(file: UploadFile = File(...)):

    try:
        temp_path = "temp_images.zip"

        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        with zipfile.ZipFile(temp_path, "r") as zip_ref:
            zip_ref.extractall(IMAGES_PATH)

        os.remove(temp_path)

        msg = quote("Imágenes cargadas correctamente. Se usarán por código (ej: VR-150.jpg).")
        return RedirectResponse(url=f"/?msg={msg}", status_code=303)

    except Exception as e:
        print("Error al procesar ZIP:", e)
        msg = quote("Error al procesar el ZIP de imágenes.")
        return RedirectResponse(url=f"/?error={msg}", status_code=303)


# ░░░░░ BORRAR TODOS ░░░░░
@app.post("/delete_all_products")
def delete_all_products(db: Session = Depends(get_db)):

    try:
        db.query(models.Producto).delete()
        db.commit()
        msg = quote("Todos los productos fueron eliminados correctamente.")
        return RedirectResponse(url=f"/?msg={msg}", status_code=303)
    except Exception as e:
        print("Error al borrar productos:", e)
        msg = quote("Error al borrar los productos.")
        return RedirectResponse(url=f"/?error={msg}", status_code=303)


# ░░░░░ CATÁLOGO ░░░░░
@app.get("/catalogo/{slug}", response_class=HTMLResponse)
def catalogo(
    slug: str,
    request: Request,
    q: str = "",
    categoria: str = "",
    db: Session = Depends(get_db),
):

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

    categorias: List[str] = sorted([c[0] for c in categorias_raw if c[0]])

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


# ░░░░░ GENERAR PDF DEL PEDIDO (CORREGIDO) ░░░░░
# >>> AGREGADO PARA PDF <<<
@app.post("/pedido/pdf")
async def generar_pdf(data: dict):

    empresa = data.get("empresa", "Pedido de Cliente")
    items: List[dict] = data.get("items", [])

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)

    width, height = A4
    y = height - 40

    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, y, f"{empresa}")
    y -= 40

    c.setFont("Helvetica", 11)
    total = 0

    for item in items:
        linea = f'{item["cantidad"]}x  {item["codigo"]}  -  {item["descripcion"]}'
        c.drawString(40, y, linea)
        y -= 18

        precio_linea = f'Precio: ${item["precio"]:.2f}   Subtotal: ${item["precio"] * item["cantidad"]:.2f}'
        c.drawString(60, y, precio_linea)
        y -= 22

        total += item["precio"] * item["cantidad"]

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
# >>> FIN AGREGADO <<<
