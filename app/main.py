from fastapi import FastAPI, UploadFile, File, Depends, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text
from typing import List
import pandas as pd
import zipfile
import shutil
import os
import re
import json
import math
import uuid
from urllib.parse import quote
from pathlib import Path
from io import BytesIO
from tempfile import TemporaryDirectory

# PDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

from app.database import SessionLocal, engine, Base
from app import models

app = FastAPI()
APP_BUILD = "2026-04-15-cachefix-v3"
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "app/storage")).resolve()
MEDIA_BASE_DIR = STORAGE_DIR / "empresas"
MEDIA_URL_PREFIX = "/media"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_BASE_DIR.mkdir(parents=True, exist_ok=True)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "cambia-esto-en-render"),
    same_site="lax",
    https_only=False,
)

# ---------------------------------------------------
# STARTUP (Render-safe)
# ---------------------------------------------------
@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    ensure_empresa_media_columns()
    print("CODEX_SIGNATURE_2026_04_15")
    route_paths = sorted(
        {
            getattr(r, "path", "")
            for r in app.routes
            if getattr(r, "path", "")
        }
    )
    print(
        "[catalogo] startup build=",
        APP_BUILD,
        " commit=",
        os.getenv("RENDER_GIT_COMMIT", ""),
        " service=",
        os.getenv("RENDER_SERVICE_ID", ""),
    )
    print("[catalogo] routes=", ", ".join(route_paths))

# ---------------------------------------------------
# Static & Templates
# ---------------------------------------------------
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount(MEDIA_URL_PREFIX, StaticFiles(directory=str(STORAGE_DIR)), name="media")
templates = Jinja2Templates(directory="app/templates")


def ensure_empresa_media_columns():
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("empresas")}
    with engine.begin() as conn:
        if "logo_url" not in columns:
            conn.execute(text("ALTER TABLE empresas ADD COLUMN logo_url VARCHAR"))
        if "banner_url" not in columns:
            conn.execute(text("ALTER TABLE empresas ADD COLUMN banner_url VARCHAR"))

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
# RESOLUCIÓN DE EMPRESA (sin estado global)
# ---------------------------------------------------
def get_empresa_by_slug(db: Session, slug: str | None):
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    return db.query(models.Empresa).filter(models.Empresa.slug == slug).first()


def get_default_empresa(db: Session):
    return db.query(models.Empresa).order_by(models.Empresa.nombre.asc()).first()


def panel_redirect(empresa_slug: str | None = None, msg: str = "", error: str = ""):
    params = []
    if empresa_slug:
        params.append(f"empresa={quote(empresa_slug)}")
    if msg:
        params.append(f"msg={quote(msg)}")
    if error:
        params.append(f"error={quote(error)}")
    query = "&".join(params)
    return RedirectResponse(url=f"/?{query}" if query else "/", status_code=303)


def require_admin(request: Request):
    if request.session.get("is_admin"):
        return None
    next_path = quote(request.url.path)
    return RedirectResponse(url=f"/admin/login?next={next_path}", status_code=303)


def clean_text(value, default=""):
    if value is None or pd.isna(value):
        return default
    text = str(value).strip()
    if text.lower() == "nan":
        return default
    return text


def clean_price(value, default=0.0):
    if value is None or pd.isna(value):
        return default
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except Exception:
        return default


def clean_stock(value, default=0):
    if value is None or pd.isna(value):
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def get_empresa_media_dir(slug: str, media_type: str) -> Path:
    safe_slug = re.sub(r"[^a-z0-9\-]", "-", (slug or "").strip().lower())
    safe_slug = re.sub(r"-+", "-", safe_slug).strip("-")
    return MEDIA_BASE_DIR / safe_slug / media_type


def build_media_url(slug: str, media_type: str, filename: str) -> str:
    return f"{MEDIA_URL_PREFIX}/empresas/{slug}/{media_type}/{filename}"


def safe_unique_filename(upload: UploadFile, prefix: str) -> str:
    ext = Path(upload.filename or "").suffix.lower()
    ext = re.sub(r"[^a-z0-9.]", "", ext)
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        ext = ".jpg"
    return f"{prefix}-{uuid.uuid4().hex}{ext}"


async def replace_empresa_media(empresa: models.Empresa, media_type: str, upload: UploadFile) -> str:
    target_dir = get_empresa_media_dir(empresa.slug, media_type)
    target_dir.mkdir(parents=True, exist_ok=True)

    for old_file in target_dir.iterdir():
        if old_file.is_file():
            old_file.unlink()

    filename = safe_unique_filename(upload, prefix=media_type)
    file_path = target_dir / filename
    with open(file_path, "wb") as f:
        f.write(await upload.read())

    return build_media_url(empresa.slug, media_type, filename)


def replace_empresa_media_from_bytes(empresa: models.Empresa, media_type: str, original_name: str, content: bytes) -> str:
    target_dir = get_empresa_media_dir(empresa.slug, media_type)
    target_dir.mkdir(parents=True, exist_ok=True)

    for old_file in target_dir.iterdir():
        if old_file.is_file():
            old_file.unlink()

    ext = Path(original_name or "").suffix.lower()
    ext = re.sub(r"[^a-z0-9.]", "", ext)
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        ext = ".jpg"

    filename = f"{media_type}-{uuid.uuid4().hex}{ext}"
    file_path = target_dir / filename
    with open(file_path, "wb") as f:
        f.write(content)

    return build_media_url(empresa.slug, media_type, filename)


def media_url_to_file_path(url: str | None) -> Path | None:
    if not url:
        return None
    if url.startswith(f"{MEDIA_URL_PREFIX}/"):
        relative = url[len(MEDIA_URL_PREFIX) + 1:]
        return STORAGE_DIR / relative
    if url.startswith("/static/"):
        relative = url[len("/static/"):]
        return Path("app/static") / relative
    return None


def resolve_empresa_media_file(empresa: models.Empresa, media_type: str) -> Path | None:
    if media_type == "logo":
        candidates = [empresa.logo_url, get_empresa_logo_url(empresa)]
    else:
        candidates = [empresa.banner_url, get_empresa_banner_url(empresa)]

    for url in candidates:
        media_path = media_url_to_file_path(url)
        if media_path and media_path.exists() and media_path.is_file():
            return media_path
    return None


def build_unique_slug(db: Session, base_slug: str) -> str:
    base_slug = re.sub(r"[^a-z0-9\-]", "-", (base_slug or "").strip().lower())
    base_slug = re.sub(r"-+", "-", base_slug).strip("-") or "empresa"
    candidate = base_slug
    i = 1
    while db.query(models.Empresa).filter(models.Empresa.slug == candidate).first():
        candidate = f"{base_slug}-copia-{i}"
        i += 1
    return candidate


def clear_empresa_media_folder(slug: str, media_type: str):
    target_dir = get_empresa_media_dir(slug, media_type)
    if not target_dir.exists():
        return
    for old_file in target_dir.iterdir():
        if old_file.is_file():
            old_file.unlink()


def get_empresa_logo_url(empresa: models.Empresa | None) -> str:
    if not empresa:
        return "/static/images/logo.png"
    if empresa.logo_url:
        return empresa.logo_url
    legacy = Path(f"app/static/empresas/{empresa.slug}/logo.png")
    if legacy.exists():
        return f"/static/empresas/{empresa.slug}/logo.png"
    return "/static/images/logo.png"


def get_empresa_banner_url(empresa: models.Empresa | None) -> str:
    if not empresa:
        return "/static/images/banner.jpg"
    if empresa.banner_url:
        return empresa.banner_url
    legacy = Path(f"app/static/empresas/{empresa.slug}/banner.jpg")
    if legacy.exists():
        return f"/static/empresas/{empresa.slug}/banner.jpg"
    return "/static/images/banner.jpg"


def serialize_producto(producto: models.Producto) -> dict:
    return {
        "codigo": producto.codigo,
        "descripcion": producto.descripcion,
        "categoria": producto.categoria,
        "marca": producto.marca,
        "precio": float(producto.precio or 0),
        "stock": int(producto.stock or 0),
        "activo": bool(producto.activo),
    }


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(next: str = "/"):
    return HTMLResponse(
        f"""
        <html><body style="font-family:Arial;background:#0b1220;color:#fff;padding:30px;">
        <h2>Panel privado</h2>
        <form method="post" action="/admin/login">
            <input type="hidden" name="next" value="{next}">
            <label>Contraseña:</label><br>
            <input type="password" name="password" required style="padding:8px;margin:8px 0;"><br>
            <button type="submit" style="padding:10px 16px;">Ingresar</button>
        </form>
        </body></html>
        """
    )


@app.post("/admin/login")
def admin_login(
    request: Request,
    password: str = Form(...),
    next: str = Form("/")
):
    admin_password = os.getenv("ADMIN_PASSWORD", "admin123").strip()

    if password != admin_password:
        return RedirectResponse(url="/admin/login?next=/&error=1", status_code=303)

    request.session["is_admin"] = True
    return RedirectResponse(url=next or "/", status_code=303)


@app.get("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/_build")
@app.get("/build")
@app.get("/__build")
def build_info():
    return {
        "build": APP_BUILD,
        "render_git_commit": os.getenv("RENDER_GIT_COMMIT", ""),
        "render_service_id": os.getenv("RENDER_SERVICE_ID", ""),
    }


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "build": APP_BUILD,
    }


@app.get("/healthz/{tail:path}")
@app.get("/healthz{tail:path}")
def healthz_fallback(tail: str):
    """
    Fallback útil para URLs mal pegadas, por ejemplo:
    /healthzhttps://.../_build
    """
    tail = tail or ""
    if "build" in tail.lower():
        return RedirectResponse(url="/_build", status_code=307)
    return JSONResponse(
        {
            "ok": True,
            "build": APP_BUILD,
            "note": "Ruta inválida detectada. Probá /healthz o /_build.",
            "tail": tail,
        }
    )


@app.get("/empresa/activar/{slug}")
def activar_empresa(slug: str, db: Session = Depends(get_db)):
    """
    Endpoint de compatibilidad:
    redirecciona el panel al contexto de empresa indicado por query string.
    """
    empresa = get_empresa_by_slug(db, slug)
    if not empresa:
        return {"error": "Empresa no encontrada", "slug": slug}

    return RedirectResponse(url=f"/?empresa={quote(empresa.slug)}", status_code=303)


@app.get("/empresa/activa")
def ver_empresa_activa(
    slug: str | None = Query(default=None),
    db: Session = Depends(get_db)
):
    """
    Devuelve qué empresa está activa ahora.
    """
    empresa = get_empresa_by_slug(db, slug) or get_default_empresa(db)
    if not empresa:
        return {"empresa_activa": None}
    return {"empresa_activa": {"id": empresa.id, "slug": empresa.slug, "nombre": empresa.nombre}}

@app.post("/empresa/actualizar_imagenes")
async def actualizar_imagenes_empresa(
    request: Request,
    empresa_slug: str = Form(...),
    logo: UploadFile = File(None),
    banner: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    auth = require_admin(request)
    if auth:
        return auth

    empresa = get_empresa_by_slug(db, empresa_slug)
    if not empresa:
        return panel_redirect(error="Empresa inválida")

    if logo:
        empresa.logo_url = await replace_empresa_media(empresa, media_type="logo", upload=logo)

    if banner:
        empresa.banner_url = await replace_empresa_media(empresa, media_type="banner", upload=banner)

    db.add(empresa)
    db.commit()

    return panel_redirect(empresa_slug=empresa.slug, msg="Imágenes actualizadas")


@app.post("/empresa/activar_panel")
def activar_empresa_panel(
    request: Request,
    slug: str = Form(...),
    db: Session = Depends(get_db),
):
    auth = require_admin(request)
    if auth:
        return auth

    empresa = get_empresa_by_slug(db, slug)

    if not empresa:
        return panel_redirect(error="Empresa no encontrada")

    return panel_redirect(empresa_slug=empresa.slug)

@app.get("/admin/productos", response_class=HTMLResponse)
def admin_productos(
    request: Request,
    empresa: str | None = Query(default=None),
    q: str = Query(default=""),
    db: Session = Depends(get_db)
):
    auth = require_admin(request)
    if auth:
        return auth

    empresa = get_empresa_by_slug(db, empresa) or get_default_empresa(db)
    if not empresa:
        return HTMLResponse("<h1>No hay empresa activa</h1>", status_code=400)

    query_db = db.query(models.Producto).filter(models.Producto.empresa_id == empresa.id)
    if q:
        q_like = f"%{q.strip()}%"
        query_db = query_db.filter(
            (models.Producto.codigo.ilike(q_like)) |
            (models.Producto.descripcion.ilike(q_like))
        )

    productos = query_db.order_by(models.Producto.codigo).all()

    return templates.TemplateResponse(
        "admin_productos.html",
        {
            "request": request,
            "empresa": empresa,
            "productos": productos,
            "query": q,
        },
    )


@app.get("/admin/productos/{producto_id}/editar", response_class=HTMLResponse)
def editar_producto_view(
    request: Request,
    producto_id: int,
    empresa: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    auth = require_admin(request)
    if auth:
        return auth

    producto = (
        db.query(models.Producto)
        .filter(models.Producto.id == producto_id)
        .first()
    )
    if not producto:
        return RedirectResponse(url="/admin/productos", status_code=303)

    empresa_ctx = get_empresa_by_slug(db, empresa) if empresa else producto.empresa
    if not empresa_ctx:
        empresa_ctx = producto.empresa

    return templates.TemplateResponse(
        "admin_producto_editar.html",
        {
            "request": request,
            "producto": producto,
            "empresa": empresa_ctx,
        },
    )


@app.post("/admin/productos/{producto_id}/actualizar")
async def actualizar_producto(
    request: Request,
    producto_id: int,
    empresa_slug: str = Form(""),
    codigo: str = Form(...),
    descripcion: str = Form(...),
    categoria: str = Form(""),
    marca: str = Form(""),
    stock: int = Form(0),
    precio: float = Form(...),
    activo: bool = Form(False),
    imagen: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    auth = require_admin(request)
    if auth:
        return auth

    producto = db.query(models.Producto).filter(models.Producto.id == producto_id).first()
    if not producto:
        return RedirectResponse(url="/admin/productos", status_code=303)

    producto.codigo = clean_text(codigo, default=producto.codigo) or producto.codigo
    producto.descripcion = descripcion
    producto.categoria = clean_text(categoria, default="") or None
    producto.marca = clean_text(marca, default="") or None
    producto.stock = stock
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
    target_empresa = empresa_slug or (producto.empresa.slug if producto.empresa else "")
    redirect_target = f"/admin/productos?empresa={quote(target_empresa)}" if target_empresa else "/admin/productos"
    return RedirectResponse(url=redirect_target, status_code=303)

@app.get("/admin/borrar_empresa/{empresa_id}")
def borrar_empresa_get(request: Request, empresa_id: int, db: Session = Depends(get_db)):
    auth = require_admin(request)
    if auth:
        return auth

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
    media_path = MEDIA_BASE_DIR / slug
    if media_path.exists():
        shutil.rmtree(media_path)

    return HTMLResponse(
        f"<h1>Empresa {slug} eliminada correctamente</h1>"
    )


    


# ---------------------------------------------------
# HOME PANEL
# ---------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def upload_view(
    request: Request,
    empresa: str = "",
    msg: str = "",
    error: str = "",
    db: Session = Depends(get_db)
):
    auth = require_admin(request)
    if auth:
        return auth

    empresas = db.query(models.Empresa).order_by(models.Empresa.nombre).all()
    empresa_activa = get_empresa_by_slug(db, empresa) or get_default_empresa(db)
    import time
    using_default_admin_password = os.getenv("ADMIN_PASSWORD", "").strip() == ""


    response = templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "msg": msg,
            "error": error,
            "empresas": empresas,
            "empresa_activa": empresa_activa,
            "empresa_query": empresa_activa.slug if empresa_activa else "",
            "empresa_logo_url": get_empresa_logo_url(empresa_activa),
            "empresa_banner_url": get_empresa_banner_url(empresa_activa),
            "time": int(time.time()),
            "using_default_admin_password": using_default_admin_password,
            "app_build": APP_BUILD,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# ---------------------------------------------------
# CREAR EMPRESA
# ---------------------------------------------------
@app.post("/empresa/crear_panel")
async def crear_empresa_panel(
    request: Request,
    nombre: str = Form(...),
    slug: str = Form(...),
    whatsapp: str = Form(""),
    logo: UploadFile = File(None),
    banner: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    auth = require_admin(request)
    if auth:
        return auth

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
        empresa.logo_url = await replace_empresa_media(empresa, media_type="logo", upload=logo)

    if banner:
        empresa.banner_url = await replace_empresa_media(empresa, media_type="banner", upload=banner)

    db.add(empresa)
    db.commit()

    return panel_redirect(empresa_slug=empresa.slug, msg="Empresa creada correctamente")


@app.post("/delete_all_products")
def delete_all_products(
    request: Request,
    empresa_slug: str = Form(...),
    db: Session = Depends(get_db)
):
    auth = require_admin(request)
    if auth:
        return auth

    empresa = get_empresa_by_slug(db, empresa_slug)
    if not empresa:
        return panel_redirect(error="Empresa inválida.")

    db.query(models.Producto).filter(models.Producto.empresa_id == empresa.id).delete()
    db.commit()

    return panel_redirect(empresa_slug=empresa.slug, msg=f"Se borraron todos los productos de {empresa.nombre}.")

# ---------------------------------------------------
# SUBIR EXCEL
# ---------------------------------------------------
@app.post("/upload_excel")
def upload_excel(
    request: Request,
    empresa_slug: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    auth = require_admin(request)
    if auth:
        return auth

    try:
        empresa = get_empresa_by_slug(db, empresa_slug)
        if not empresa:
            return panel_redirect(error="Empresa inválida. Seleccioná una empresa primero.")

        filename = (file.filename or "").lower()
        if not filename.endswith((".xlsx", ".xls")):
            return panel_redirect(empresa_slug=empresa.slug, error="Formato inválido. Subí un archivo Excel (.xlsx o .xls).")

        df = pd.read_excel(file.file)
        df.columns = [c.strip().lower() for c in df.columns]

        required = ["codigo", "descripcion", "precio"]
        for col in required:
            if col not in df.columns:
                return panel_redirect(empresa_slug=empresa.slug, error=f"Falta columna obligatoria: {col}")

        nuevos = 0
        actualizados = 0

        for _, row in df.iterrows():
            codigo = clean_text(row.get("codigo", ""))
            if not codigo:
                continue

            categoria = clean_text(row.get("categoria", ""), default="") or None
            marca = clean_text(row.get("marca", ""), default="") or None
            stock = clean_stock(row.get("stock", 0), default=0)
            precio = clean_price(row.get("precio", 0), default=0.0)
            descripcion = clean_text(row.get("descripcion", ""), default="")

            existe = db.query(models.Producto).filter(
                models.Producto.codigo == codigo,
                models.Producto.empresa_id == empresa.id
            ).first()

            if existe:
                existe.descripcion = descripcion or existe.descripcion
                existe.precio = precio
                existe.categoria = categoria
                existe.marca = marca
                existe.stock = stock
                actualizados += 1
            else:
                producto = models.Producto(
                    codigo=codigo,
                    descripcion=descripcion or codigo,
                    categoria=categoria,
                    marca=marca,
                    precio=precio,
                    stock=stock,
                    empresa_id=empresa.id
                )
                db.add(producto)
                nuevos += 1

        db.commit()

        return panel_redirect(
            empresa_slug=empresa.slug,
            msg=f"Productos cargados. Nuevos: {nuevos}, Actualizados: {actualizados}. Revisalos en /admin/productos."
        )

    except Exception as e:
        print("Error Excel:", e)
        return panel_redirect(empresa_slug=empresa_slug, error="Error al procesar el Excel.")


@app.get("/admin/empresa/exportar")
def exportar_empresa_admin(
    request: Request,
    empresa_slug: str = Query(...),
    db: Session = Depends(get_db),
):
    auth = require_admin(request)
    if auth:
        return auth

    empresa = get_empresa_by_slug(db, empresa_slug)
    if not empresa:
        return panel_redirect(error="Empresa no encontrada para exportar.")

    productos = (
        db.query(models.Producto)
        .filter(models.Producto.empresa_id == empresa.id)
        .order_by(models.Producto.codigo.asc())
        .all()
    )

    payload = {
        "format_version": 1,
        "empresa": {
            "nombre": empresa.nombre,
            "slug": empresa.slug,
            "whatsapp": empresa.whatsapp or "",
        },
        "productos": [serialize_producto(p) for p in productos],
        "assets": {"logo": None, "banner": None},
    }

    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for media_type, field in [("logo", "logo"), ("banner", "banner")]:
            media_path = resolve_empresa_media_file(empresa, media_type)
            if media_path:
                asset_name = f"assets/{field}{media_path.suffix.lower()}"
                zf.write(media_path, arcname=asset_name)
                payload["assets"][field] = asset_name

        zf.writestr("empresa.json", json.dumps(payload, ensure_ascii=False, indent=2))

    output.seek(0)
    filename = f"empresa_backup_{empresa.slug}.zip"
    return StreamingResponse(
        output,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/empresa/importar")
async def importar_empresa_admin(
    request: Request,
    file: UploadFile = File(...),
    import_mode: str = Form("duplicate"),
    empresa_slug: str = Form(""),
    db: Session = Depends(get_db),
):
    auth = require_admin(request)
    if auth:
        return auth

    filename = (file.filename or "").lower()
    if not filename.endswith(".zip"):
        return panel_redirect(empresa_slug=empresa_slug, error="Formato inválido. Subí un backup .zip generado por el sistema.")
    if import_mode not in {"duplicate", "replace"}:
        return panel_redirect(empresa_slug=empresa_slug, error="Modo de importación inválido.")

    try:
        with TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / "backup.zip"
            with open(zip_path, "wb") as f:
                f.write(await file.read())

            with zipfile.ZipFile(zip_path, "r") as zf:
                if "empresa.json" not in zf.namelist():
                    return panel_redirect(empresa_slug=empresa_slug, error="Backup inválido: falta empresa.json.")

                raw = zf.read("empresa.json")
                data = json.loads(raw.decode("utf-8"))

                empresa_data = data.get("empresa") or {}
                productos_data = data.get("productos") or []
                assets_data = data.get("assets") or {}

                source_slug = clean_text(empresa_data.get("slug"), default="")
                source_nombre = clean_text(empresa_data.get("nombre"), default="")
                if not source_slug or not source_nombre:
                    return panel_redirect(empresa_slug=empresa_slug, error="Backup inválido: datos de empresa incompletos.")

                source_slug = re.sub(r"[^a-z0-9\-]", "-", source_slug.lower())
                source_slug = re.sub(r"-+", "-", source_slug).strip("-")

                existing = db.query(models.Empresa).filter(models.Empresa.slug == source_slug).first()
                created_new = False
                replaced = False

                if existing and import_mode == "replace":
                    empresa = existing
                    empresa.nombre = source_nombre
                    empresa.whatsapp = clean_text(empresa_data.get("whatsapp"), default="")
                    db.query(models.Producto).filter(models.Producto.empresa_id == empresa.id).delete()
                    clear_empresa_media_folder(empresa.slug, "logo")
                    clear_empresa_media_folder(empresa.slug, "banner")
                    empresa.logo_url = None
                    empresa.banner_url = None
                    replaced = True
                elif existing:
                    new_slug = build_unique_slug(db, source_slug)
                    empresa = models.Empresa(
                        nombre=source_nombre,
                        slug=new_slug,
                        whatsapp=clean_text(empresa_data.get("whatsapp"), default=""),
                    )
                    db.add(empresa)
                    db.commit()
                    db.refresh(empresa)
                    created_new = True
                else:
                    empresa = models.Empresa(
                        nombre=source_nombre,
                        slug=source_slug,
                        whatsapp=clean_text(empresa_data.get("whatsapp"), default=""),
                    )
                    db.add(empresa)
                    db.commit()
                    db.refresh(empresa)
                    created_new = True

                for item in productos_data:
                    codigo = clean_text(item.get("codigo"), default="")
                    if not codigo:
                        continue
                    db.add(models.Producto(
                        empresa_id=empresa.id,
                        codigo=codigo,
                        descripcion=clean_text(item.get("descripcion"), default=codigo),
                        categoria=clean_text(item.get("categoria"), default="") or None,
                        marca=clean_text(item.get("marca"), default="") or None,
                        precio=clean_price(item.get("precio"), default=0.0),
                        stock=clean_stock(item.get("stock"), default=0),
                        activo=bool(item.get("activo", True)),
                    ))

                for media_type, key in [("logo", "logo"), ("banner", "banner")]:
                    asset_name = clean_text(assets_data.get(key), default="")
                    if not asset_name:
                        continue
                    if asset_name not in zf.namelist():
                        continue
                    content = zf.read(asset_name)
                    if media_type == "logo":
                        empresa.logo_url = replace_empresa_media_from_bytes(empresa, media_type, asset_name, content)
                    else:
                        empresa.banner_url = replace_empresa_media_from_bytes(empresa, media_type, asset_name, content)

                db.add(empresa)
                db.commit()

                status_msg = "Empresa importada correctamente."
                if created_new and existing:
                    status_msg = f"Empresa importada como nueva ({empresa.slug}) para evitar sobreescribir {source_slug}."
                if replaced:
                    status_msg = f"Empresa {empresa.slug} restaurada correctamente."

                return panel_redirect(empresa_slug=empresa.slug, msg=status_msg)

    except Exception as e:
        print("Error al importar empresa:", e)
        return panel_redirect(empresa_slug=empresa_slug, error="No se pudo importar el backup.")


# ---------------------------------------------------
# SUBIR ZIP
# ---------------------------------------------------
@app.post("/upload_zip")
def upload_zip(
    request: Request,
    empresa_slug: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    auth = require_admin(request)
    if auth:
        return auth

    try:
        temp_path = "temp_images.zip"

        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        empresa = get_empresa_by_slug(db, empresa_slug)
        if not empresa:
            return panel_redirect(error="Empresa inválida.")

        IMAGES_PATH = f"app/static/empresas/{empresa.slug}/productos/"
        os.makedirs(IMAGES_PATH, exist_ok=True)

        with zipfile.ZipFile(temp_path, "r") as zip_ref:
            zip_ref.extractall(IMAGES_PATH)

        os.remove(temp_path)

        return panel_redirect(empresa_slug=empresa.slug, msg="Imágenes cargadas correctamente.")

    except Exception as e:
        print("Error ZIP:", e)
        return panel_redirect(empresa_slug=empresa_slug, error="Error al procesar el ZIP.")

# ---------------------------------------------------
# CATÁLOGO
# ---------------------------------------------------
@app.get("/catalogo/{slug}", response_class=HTMLResponse)
def catalogo(
    slug: str,
    request: Request,
    q: str = "",
    categoria: str = "",
    marca: str = "",
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
        
    if marca:
        query_db = query_db.filter(models.Producto.marca == marca)

    
    
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



    # TODAS las categorías de la empresa (sin filtros)
    categorias = (
        db.query(models.Producto.categoria)
        .filter(
            models.Producto.empresa_id == empresa.id,
            models.Producto.categoria.isnot(None)
        )
        .distinct()
        .order_by(models.Producto.categoria)
        .all()
    )

    categorias = [c[0] for c in categorias]

    marcas = (
        db.query(models.Producto.marca)
        .filter(
            models.Producto.empresa_id == empresa.id,
            models.Producto.marca.isnot(None)
        )
        .distinct()
        .order_by(models.Producto.marca)
        .all()
    )

    marcas = [m[0] for m in marcas]



    productos_json = [
        {
            "id": p.id,
            "codigo": p.codigo,
            "descripcion": p.descripcion,
            "precio": round(float(p.precio), 2),
            "categoria": p.categoria,
            "marca": p.marca,
            "stock": p.stock,
            "imagen_url": p.imagen_url,
        }
        for p in productos
    ]

    # Export estático para descarga directa (más compatible con navegadores móviles)
    export_path = Path(f"app/static/empresas/{empresa.slug}")
    export_path.mkdir(parents=True, exist_ok=True)
    lista_precios_path = export_path / "lista_precios.json"
    lista_precios_xlsx_path = export_path / "lista_precios.xlsx"

    lista_payload = {
        "empresa": {
            "id": empresa.id,
            "slug": empresa.slug,
            "nombre": empresa.nombre,
            "whatsapp": empresa.whatsapp,
        },
        "total_productos": len(productos_json),
        "productos": productos_json,
    }

    with open(lista_precios_path, "w", encoding="utf-8") as f:
        json.dump(lista_payload, f, ensure_ascii=False, indent=2)

    # Export en el mismo formato de subida (Excel)
    df_export = pd.DataFrame(
        [
            {
                "codigo": p.codigo,
                "descripcion": p.descripcion,
                "precio": round(float(p.precio), 2),
                "categoria": p.categoria or "",
                "marca": p.marca or "",
                "stock": p.stock if p.stock is not None else 0,
            }
            for p in productos
        ]
    )
    df_export.to_excel(lista_precios_xlsx_path, index=False)

    import time

    response = templates.TemplateResponse(
        "catalogo.html",
        {
            "request": request,
            "productos": productos,
            "productos_json": productos_json,
            "empresa": empresa,
            "categorias": categorias,
            "categoria_actual": categoria,
            "marcas": marcas,
            "marca_actual": marca,
            "orden_actual": orden,
            "query": q,
            "ts_download": int(time.time()),
            "app_build": APP_BUILD,
            "empresa_logo_url": get_empresa_logo_url(empresa),
            "empresa_banner_url": get_empresa_banner_url(empresa),
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/catalogo/{slug}/lista_precio.json")
@app.get("/catalogo/{slug}/lista_precios.json")
def descargar_lista_precios_json(slug: str, db: Session = Depends(get_db)):
    empresa = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if not empresa:
        return JSONResponse({"error": "Empresa no encontrada", "slug": slug}, status_code=404)

    productos = (
        db.query(models.Producto)
        .filter(
            models.Producto.empresa_id == empresa.id,
            models.Producto.activo == True
        )
        .order_by(models.Producto.codigo.asc())
        .all()
    )

    data = {
        "empresa": {
            "id": empresa.id,
            "slug": empresa.slug,
            "nombre": empresa.nombre,
            "whatsapp": empresa.whatsapp,
        },
        "total_productos": len(productos),
        "productos": [
            {
                "codigo": p.codigo,
                "descripcion": p.descripcion,
                "categoria": p.categoria,
                "marca": p.marca,
                "precio": round(float(p.precio), 2),
                "stock": p.stock,
                "activo": p.activo,
            }
            for p in productos
        ],
    }

    filename = f"lista_precio_{empresa.slug}.json"
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/catalogo/{slug}/lista_precios.xlsx")
def descargar_lista_precios_xlsx(slug: str, db: Session = Depends(get_db)):
    empresa = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if not empresa:
        return HTMLResponse("<h1>Empresa no encontrada</h1>", status_code=404)

    productos = (
        db.query(models.Producto)
        .filter(
            models.Producto.empresa_id == empresa.id,
            models.Producto.activo == True
        )
        .order_by(models.Producto.codigo.asc())
        .all()
    )

    df = pd.DataFrame([
        {
            "codigo": p.codigo,
            "descripcion": clean_text(p.descripcion),
            "precio": clean_price(p.precio, default=0.0),
            "categoria": clean_text(p.categoria),
            "marca": clean_text(p.marca),
            "stock": clean_stock(p.stock, default=0),
        }
        for p in productos
    ])

    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    filename = f"lista_precios_{empresa.slug}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
def listar_empresas(request: Request, db: Session = Depends(get_db)):
    auth = require_admin(request)
    if auth:
        return auth

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
def borrar_empresa(request: Request, empresa_id: int, db: Session = Depends(get_db)):
    auth = require_admin(request)
    if auth:
        return auth

    empresa = db.query(models.Empresa).filter(models.Empresa.id == empresa_id).first()
    if not empresa:
        return {"error": "Empresa no encontrada"}

    # borrar carpeta física
    empresa_path = Path(f"app/static/empresas/{empresa.slug}")
    if empresa_path.exists():
        shutil.rmtree(empresa_path)
    empresa_media_path = MEDIA_BASE_DIR / empresa.slug
    if empresa_media_path.exists():
        shutil.rmtree(empresa_media_path)

    # borrar DB (productos se borran por cascade)
    db.delete(empresa)
    db.commit()

    return {"status": "ok"}



# ---------------------------------------------------
# DEBUG: LISTAR ARCHIVOS DE IMAGEN DE UNA EMPRESA
# ---------------------------------------------------
@app.get("/debug/imagenes/{slug}")
def debug_imagenes(request: Request, slug: str):
    auth = require_admin(request)
    if auth:
        return auth

    slug = (slug or "").strip().lower()
    path = Path(f"app/static/empresas/{slug}/productos")
    if not path.exists():
        return {"error": "Carpeta no existe", "path": str(path)}

    files = sorted([p.name for p in path.iterdir() if p.is_file()])
    # devolvemos solo los primeros 200 para no explotar la respuesta
    return {"path": str(path), "count": len(files), "files": files[:200]}
