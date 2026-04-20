from fastapi import FastAPI, UploadFile, File, Depends, Request, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text, func, case, or_
from pydantic import BaseModel
from typing import List
import pandas as pd
import zipfile
import shutil
import os
import re
import json
import math
import uuid
import hashlib
import hmac
import secrets
from urllib.parse import quote
from pathlib import Path
from io import BytesIO
from datetime import datetime, timezone

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
PRODUCTOS_MEDIA_TYPE = "productos"
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PRICE_POLICY_VALUES = {"mostrar", "consultar", "automatico"}
STOCK_POLICY_VALUES = {"mostrar", "ocultar", "automatico"}
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
    ensure_usuario_columns()
    ensure_default_admin_user()
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
        if "politica_precio_catalogo" not in columns:
            conn.execute(text("ALTER TABLE empresas ADD COLUMN politica_precio_catalogo VARCHAR DEFAULT 'automatico'"))
        if "politica_stock_catalogo" not in columns:
            conn.execute(text("ALTER TABLE empresas ADD COLUMN politica_stock_catalogo VARCHAR DEFAULT 'mostrar'"))
        conn.execute(
            text(
                "UPDATE empresas "
                "SET politica_precio_catalogo = 'automatico' "
                "WHERE politica_precio_catalogo IS NULL "
                "OR politica_precio_catalogo NOT IN ('mostrar','consultar','automatico')"
            )
        )
        conn.execute(
            text(
                "UPDATE empresas "
                "SET politica_stock_catalogo = 'mostrar' "
                "WHERE politica_stock_catalogo IS NULL "
                "OR politica_stock_catalogo NOT IN ('mostrar','ocultar','automatico')"
            )
        )


def ensure_usuario_columns():
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "usuarios" not in tables:
        return

    columns = {col["name"] for col in inspector.get_columns("usuarios")}
    with engine.begin() as conn:
        if "rol" not in columns:
            conn.execute(text("ALTER TABLE usuarios ADD COLUMN rol VARCHAR DEFAULT 'cliente'"))
        if "activo" not in columns:
            conn.execute(text("ALTER TABLE usuarios ADD COLUMN activo BOOLEAN DEFAULT TRUE"))
        if "empresa_id" not in columns:
            conn.execute(text("ALTER TABLE usuarios ADD COLUMN empresa_id INTEGER"))

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


def panel_redirect(empresa_slug: str | None = None, msg: str = "", error: str = "", path: str = "/admin"):
    params = []
    if empresa_slug:
        params.append(f"empresa={quote(empresa_slug)}")
    if msg:
        params.append(f"msg={quote(msg)}")
    if error:
        params.append(f"error={quote(error)}")
    query = "&".join(params)
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=303)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _, salt, saved = password_hash.split("$", 2)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310000).hex()
        return hmac.compare_digest(digest, saved)
    except Exception:
        return False


def ensure_default_admin_user():
    username = (os.getenv("ADMIN_USER", "admin").strip() or "admin").lower()
    raw_password = os.getenv("ADMIN_PASSWORD", "admin123").strip() or "admin123"
    with SessionLocal() as db:
        existing = db.query(models.Usuario).filter(models.Usuario.username == username).first()
        if existing:
            return
        user = models.Usuario(
            username=username,
            password_hash=hash_password(raw_password),
            rol="admin",
            activo=True,
            empresa_id=None,
        )
        db.add(user)
        db.commit()


def get_current_user(request: Request, db: Session) -> models.Usuario | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(models.Usuario).filter(models.Usuario.id == user_id, models.Usuario.activo == True).first()


def require_login(request: Request, db: Session):
    user = get_current_user(request, db)
    if user:
        return user
    next_path = quote(str(request.url.path))
    return RedirectResponse(url=f"/login?next={next_path}", status_code=303)


def require_admin(request: Request, db: Session):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if user.rol != "admin":
        return RedirectResponse(url="/cliente?error=No tenés permisos para acceder al panel admin", status_code=303)
    return user


def get_user_empresa(user: models.Usuario, db: Session):
    if user.rol == "cliente":
        if not user.empresa_id:
            return None
        return db.query(models.Empresa).filter(models.Empresa.id == user.empresa_id).first()
    return None


def resolve_empresa_for_user(user: models.Usuario, db: Session, slug: str | None):
    if user.rol == "admin":
        return get_empresa_by_slug(db, slug) or get_default_empresa(db)
    return get_user_empresa(user, db)


def get_dashboard_path(user: models.Usuario) -> str:
    return "/admin" if user.rol == "admin" else "/cliente"


def redirect_for_user(user: models.Usuario, empresa_slug: str | None = None, msg: str = "", error: str = ""):
    return panel_redirect(
        empresa_slug=empresa_slug,
        msg=msg,
        error=error,
        path=get_dashboard_path(user),
    )


def can_access_empresa(user: models.Usuario, empresa_slug: str | None, db: Session):
    empresa = resolve_empresa_for_user(user, db, empresa_slug)
    if not empresa:
        return None
    if user.rol == "cliente" and user.empresa_id != empresa.id:
        return None
    return empresa


LEAD_SESSION_KEY = "catalog_lead_sessions"
EVENT_TYPES = {
    "catalog_entered",
    "search_performed",
    "product_viewed",
    "cart_item_added",
    "whatsapp_clicked",
    "pdf_downloaded",
}
EVENT_TYPE_LABELS = {
    "catalog_entered": "Ingresó al catálogo",
    "search_performed": "Realizó búsqueda",
    "product_viewed": "Vio producto",
    "cart_item_added": "Agregó al pedido",
    "whatsapp_clicked": "Click en WhatsApp",
    "pdf_downloaded": "Descargó PDF",
}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class CatalogEventPayload(BaseModel):
    event_type: str
    product_code: str | None = None
    search_term: str | None = None
    metadata: dict | None = None


def utc_now():
    return datetime.now(timezone.utc)


def get_lead_session_for_slug(request: Request, slug: str) -> dict | None:
    sessions = request.session.get(LEAD_SESSION_KEY) or {}
    lead_session = sessions.get(slug)
    if isinstance(lead_session, dict):
        return lead_session
    return None


def set_lead_session_for_slug(request: Request, slug: str, lead_id: int, session_token: str):
    sessions = request.session.get(LEAD_SESSION_KEY) or {}
    sessions[slug] = {"lead_id": lead_id, "session_token": session_token}
    request.session[LEAD_SESSION_KEY] = sessions


def clear_lead_session_for_slug(request: Request, slug: str):
    sessions = request.session.get(LEAD_SESSION_KEY) or {}
    if slug in sessions:
        sessions.pop(slug, None)
        request.session[LEAD_SESSION_KEY] = sessions


def get_active_catalog_lead(request: Request, slug: str, empresa_id: int, db: Session) -> models.CatalogLead | None:
    lead_session = get_lead_session_for_slug(request, slug)
    if not lead_session:
        return None

    lead_id = lead_session.get("lead_id")
    session_token = lead_session.get("session_token")
    if not lead_id or not session_token:
        clear_lead_session_for_slug(request, slug)
        return None

    lead = (
        db.query(models.CatalogLead)
        .filter(
            models.CatalogLead.id == int(lead_id),
            models.CatalogLead.empresa_catalogo_id == empresa_id,
            models.CatalogLead.session_token == str(session_token),
        )
        .first()
    )
    if not lead:
        clear_lead_session_for_slug(request, slug)
        return None
    return lead


def register_catalog_event(
    db: Session,
    lead: models.CatalogLead,
    empresa_id: int,
    event_type: str,
    product_code: str | None = None,
    search_term: str | None = None,
    metadata: dict | None = None,
):
    if event_type not in EVENT_TYPES:
        return

    event = models.CatalogLeadEvent(
        lead_id=lead.id,
        empresa_catalogo_id=empresa_id,
        event_type=event_type,
        product_code=clean_text(product_code, default="") or None,
        search_term=clean_text(search_term, default="") or None,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False) if metadata else None,
        created_at=utc_now(),
    )
    lead.ultima_actividad = utc_now()
    db.add(event)
    db.add(lead)
    db.commit()


def parse_bool_query_flag(value: str | None) -> bool | None:
    clean = clean_text(value, default="").lower()
    if clean in {"1", "true", "si", "sí", "yes"}:
        return True
    if clean in {"0", "false", "no"}:
        return False
    return None


def build_lead_metrics_subquery(db: Session, empresa_id: int):
    return (
        db.query(
            models.CatalogLeadEvent.lead_id.label("lead_id"),
            func.sum(case((models.CatalogLeadEvent.event_type == "search_performed", 1), else_=0)).label("search_count"),
            func.sum(case((models.CatalogLeadEvent.event_type == "product_viewed", 1), else_=0)).label("product_view_count"),
            func.sum(case((models.CatalogLeadEvent.event_type == "cart_item_added", 1), else_=0)).label("cart_add_count"),
            func.max(case((models.CatalogLeadEvent.event_type == "whatsapp_clicked", 1), else_=0)).label("has_whatsapp_click"),
            func.max(case((models.CatalogLeadEvent.event_type == "pdf_downloaded", 1), else_=0)).label("has_pdf_download"),
            func.max(models.CatalogLeadEvent.created_at).label("last_event_at"),
        )
        .filter(models.CatalogLeadEvent.empresa_catalogo_id == empresa_id)
        .group_by(models.CatalogLeadEvent.lead_id)
        .subquery()
    )


def list_catalog_leads_for_admin(
    db: Session,
    empresa_id: int,
    search_query: str = "",
    whatsapp_filter: bool | None = None,
    pdf_filter: bool | None = None,
    cart_filter: bool | None = None,
):
    metrics_sq = build_lead_metrics_subquery(db, empresa_id)
    query = (
        db.query(
            models.CatalogLead,
            metrics_sq.c.search_count,
            metrics_sq.c.product_view_count,
            metrics_sq.c.cart_add_count,
            metrics_sq.c.has_whatsapp_click,
            metrics_sq.c.has_pdf_download,
            metrics_sq.c.last_event_at,
        )
        .outerjoin(metrics_sq, metrics_sq.c.lead_id == models.CatalogLead.id)
        .filter(models.CatalogLead.empresa_catalogo_id == empresa_id)
    )

    q = clean_text(search_query, default="")
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(
                models.CatalogLead.nombre.ilike(pattern),
                models.CatalogLead.empresa.ilike(pattern),
                models.CatalogLead.email.ilike(pattern),
            )
        )

    if whatsapp_filter is True:
        query = query.filter(func.coalesce(metrics_sq.c.has_whatsapp_click, 0) > 0)
    if pdf_filter is True:
        query = query.filter(func.coalesce(metrics_sq.c.has_pdf_download, 0) > 0)
    if cart_filter is True:
        query = query.filter(func.coalesce(metrics_sq.c.cart_add_count, 0) > 0)

    rows = (
        query
        .order_by(
            func.coalesce(metrics_sq.c.last_event_at, models.CatalogLead.ultima_actividad, models.CatalogLead.fecha_ingreso).desc(),
            models.CatalogLead.id.desc(),
        )
        .all()
    )

    lead_rows = []
    for (
        lead,
        search_count,
        product_view_count,
        cart_add_count,
        has_whatsapp_click,
        has_pdf_download,
        last_event_at,
    ) in rows:
        lead_rows.append(
            {
                "lead": lead,
                "search_count": int(search_count or 0),
                "product_view_count": int(product_view_count or 0),
                "cart_add_count": int(cart_add_count or 0),
                "has_whatsapp_click": bool(has_whatsapp_click or 0),
                "has_pdf_download": bool(has_pdf_download or 0),
                "last_event_at": last_event_at,
            }
        )
    return lead_rows


def get_lead_summary_from_events(events: list[models.CatalogLeadEvent]) -> dict:
    summary = {
        "search_count": 0,
        "product_view_count": 0,
        "cart_add_count": 0,
        "has_whatsapp_click": False,
        "has_pdf_download": False,
    }
    for event in events:
        if event.event_type == "search_performed":
            summary["search_count"] += 1
        elif event.event_type == "product_viewed":
            summary["product_view_count"] += 1
        elif event.event_type == "cart_item_added":
            summary["cart_add_count"] += 1
        elif event.event_type == "whatsapp_clicked":
            summary["has_whatsapp_click"] = True
        elif event.event_type == "pdf_downloaded":
            summary["has_pdf_download"] = True
    return summary


def build_lead_timeline_rows(events: list[models.CatalogLeadEvent]) -> list[dict]:
    rows = []
    for event in events:
        metadata = {}
        if event.metadata_json:
            try:
                parsed = json.loads(event.metadata_json)
                metadata = parsed if isinstance(parsed, dict) else {}
            except Exception:
                metadata = {}
        rows.append(
            {
                "id": event.id,
                "created_at": event.created_at,
                "event_type": event.event_type,
                "event_label": EVENT_TYPE_LABELS.get(event.event_type, event.event_type),
                "search_term": event.search_term,
                "product_code": event.product_code,
                "metadata": metadata,
            }
        )
    return rows


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


def normalize_price_policy(value: str | None) -> str:
    policy = clean_text(value, default="automatico").lower()
    return policy if policy in PRICE_POLICY_VALUES else "automatico"


def normalize_stock_policy(value: str | None) -> str:
    policy = clean_text(value, default="mostrar").lower()
    return policy if policy in STOCK_POLICY_VALUES else "mostrar"


def has_valid_price(value) -> bool:
    if value is None:
        return False
    try:
        parsed = float(value)
        return math.isfinite(parsed) and parsed > 0
    except Exception:
        return False


def resolve_price_display(policy: str, precio) -> dict:
    price_ok = has_valid_price(precio)
    if policy == "consultar":
        return {"mostrar_numerico": False, "texto": "Consultar"}
    if policy == "automatico":
        if price_ok:
            return {"mostrar_numerico": True, "texto": f"${float(precio):,.2f}"}
        return {"mostrar_numerico": False, "texto": "Consultar"}
    if price_ok:
        return {"mostrar_numerico": True, "texto": f"${float(precio):,.2f}"}
    return {"mostrar_numerico": False, "texto": "Consultar"}


def resolve_stock_display(policy: str, stock_value) -> dict:
    stock_num = clean_stock(stock_value, default=0)
    if policy == "ocultar":
        return {"visible": False, "texto": "", "clase": ""}
    if policy == "automatico" and stock_num <= 0:
        return {"visible": False, "texto": "", "clase": ""}
    if stock_num <= 0:
        return {"visible": True, "texto": "Sin stock", "clase": "stock-gray"}
    if stock_num <= 15:
        return {"visible": True, "texto": "Stock bajo", "clase": "stock-red"}
    if stock_num <= 60:
        return {"visible": True, "texto": "Stock medio", "clase": "stock-yellow"}
    return {"visible": True, "texto": "Stock alto", "clase": "stock-green"}


def get_empresa_media_dir(slug: str, media_type: str) -> Path:
    safe_slug = re.sub(r"[^a-z0-9\-]", "-", (slug or "").strip().lower())
    safe_slug = re.sub(r"-+", "-", safe_slug).strip("-")
    return MEDIA_BASE_DIR / safe_slug / media_type


def build_media_url(slug: str, media_type: str, filename: str) -> str:
    return f"{MEDIA_URL_PREFIX}/empresas/{slug}/{media_type}/{filename}"


def get_productos_media_dir(slug: str) -> Path:
    return get_empresa_media_dir(slug, PRODUCTOS_MEDIA_TYPE)


def sanitize_codigo_for_filename(codigo: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._\-]", "_", (codigo or "").strip())
    return safe or "producto"


def build_producto_media_url(slug: str, filename: str) -> str:
    return build_media_url(slug, PRODUCTOS_MEDIA_TYPE, filename)


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as src, open(destination, "wb") as dst:
        shutil.copyfileobj(src, dst)


def resolve_producto_imagen_url(producto: models.Producto, empresa_slug: str, migrate_legacy: bool = True) -> str:
    productos_dir = get_productos_media_dir(empresa_slug)
    productos_dir.mkdir(parents=True, exist_ok=True)
    fallback_url = "/static/img/no-image.png"
    codigo_safe = sanitize_codigo_for_filename(producto.codigo)

    existing_name = ""
    if producto.imagen_url:
        existing_name = Path(producto.imagen_url).name
        if existing_name:
            current_path = productos_dir / existing_name
            if current_path.exists():
                return build_producto_media_url(empresa_slug, existing_name)

    for ext in ALLOWED_IMAGE_EXTENSIONS:
        candidate = productos_dir / f"{codigo_safe}{ext}"
        if candidate.exists():
            return build_producto_media_url(empresa_slug, candidate.name)

    legacy_dir = Path("app/static/empresas") / empresa_slug / "productos"
    if legacy_dir.exists():
        for ext in ALLOWED_IMAGE_EXTENSIONS:
            legacy_file = legacy_dir / f"{codigo_safe}{ext}"
            if legacy_file.exists():
                if migrate_legacy:
                    target_name = existing_name if existing_name else legacy_file.name
                    target_path = productos_dir / target_name
                    if not target_path.exists():
                        _copy_file(legacy_file, target_path)
                    return build_producto_media_url(empresa_slug, target_name)
                return f"/static/empresas/{empresa_slug}/productos/{legacy_file.name}"

    return fallback_url


def build_unique_slug(db: Session, base_slug: str) -> str:
    base_slug = (base_slug or "").strip().lower()
    base_slug = re.sub(r"[^a-z0-9\-]", "-", base_slug)
    base_slug = re.sub(r"-+", "-", base_slug).strip("-")
    if not base_slug:
        base_slug = "empresa"

    exists = db.query(models.Empresa).filter(models.Empresa.slug == base_slug).first()
    if not exists:
        return base_slug

    i = 2
    while True:
        candidate = f"{base_slug}-copia-{i}"
        exists = db.query(models.Empresa).filter(models.Empresa.slug == candidate).first()
        if not exists:
            return candidate
        i += 1


def _zip_safe_members(zip_ref: zipfile.ZipFile):
    for member in zip_ref.infolist():
        member_name = member.filename.replace("\\", "/")
        if member_name.endswith("/"):
            continue
        parts = [p for p in Path(member_name).parts if p not in ("", ".", "..")]
        if not parts:
            continue
        yield member, Path(*parts)


def _copy_zip_prefix(zip_ref: zipfile.ZipFile, prefix: str, target_dir: Path):
    normalized_prefix = prefix.rstrip("/") + "/"
    for member, safe_path in _zip_safe_members(zip_ref):
        safe_str = safe_path.as_posix()
        if not safe_str.startswith(normalized_prefix):
            continue
        relative_str = safe_str[len(normalized_prefix):]
        if not relative_str:
            continue
        destination = target_dir / Path(relative_str)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zip_ref.open(member, "r") as src, open(destination, "wb") as dst:
            shutil.copyfileobj(src, dst)


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


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "next": next,
            "error": error,
        },
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    username_clean = clean_text(username, default="").lower()
    user = db.query(models.Usuario).filter(models.Usuario.username == username_clean, models.Usuario.activo == True).first()
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse(url=f"/login?next={quote(next or '/admin')}&error=Credenciales inválidas", status_code=303)

    request.session.clear()
    request.session["user_id"] = user.id
    request.session["role"] = user.rol
    request.session["empresa_id"] = user.empresa_id

    if next and next not in {"/", "/login"}:
        return RedirectResponse(url=next, status_code=303)
    return RedirectResponse(url=get_dashboard_path(user), status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/admin/login")
def admin_login_compat():
    return RedirectResponse(url="/login", status_code=303)


@app.get("/admin/logout")
def admin_logout_compat():
    return RedirectResponse(url="/logout", status_code=303)


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
def activar_empresa(slug: str, request: Request, db: Session = Depends(get_db)):
    """
    Endpoint de compatibilidad:
    redirecciona el panel al contexto de empresa indicado por query string.
    """
    empresa = get_empresa_by_slug(db, slug)
    if not empresa:
        return {"error": "Empresa no encontrada", "slug": slug}

    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url=f"{get_dashboard_path(user)}?empresa={quote(empresa.slug)}", status_code=303)


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
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
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
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user

    if user.rol != "admin":
        return redirect_for_user(user, error="No tenés permisos para cambiar de empresa")

    empresa = get_empresa_by_slug(db, slug)

    if not empresa:
        return redirect_for_user(user, error="Empresa no encontrada")

    return redirect_for_user(user, empresa_slug=empresa.slug)


@app.post("/empresa/editar_panel")
def editar_empresa_panel(
    request: Request,
    empresa_slug_actual: str = Form(...),
    nombre: str = Form(...),
    whatsapp: str = Form(""),
    editar_slug: str = Form("0"),
    nuevo_slug: str = Form(""),
    db: Session = Depends(get_db),
):
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
        return auth

    empresa = get_empresa_by_slug(db, empresa_slug_actual)
    if not empresa:
        return panel_redirect(error="Empresa no encontrada.")

    nombre_limpio = clean_text(nombre, default="")
    whatsapp_limpio = clean_text(whatsapp, default="") or None
    if not nombre_limpio:
        return panel_redirect(empresa_slug=empresa.slug, error="El nombre de la empresa no puede estar vacío.")

    slug_original = empresa.slug
    slug_final = slug_original

    if editar_slug == "1":
        nuevo_slug_limpio = clean_text(nuevo_slug, default="").lower()
        nuevo_slug_limpio = re.sub(r"[^a-z0-9\-]", "-", nuevo_slug_limpio)
        nuevo_slug_limpio = re.sub(r"-+", "-", nuevo_slug_limpio).strip("-")

        if not nuevo_slug_limpio:
            return panel_redirect(empresa_slug=slug_original, error="Slug inválido.")

        if nuevo_slug_limpio != slug_original:
            existe = db.query(models.Empresa).filter(models.Empresa.slug == nuevo_slug_limpio).first()
            if existe:
                return panel_redirect(empresa_slug=slug_original, error="Ese slug ya existe.")
            slug_final = nuevo_slug_limpio

    empresa.nombre = nombre_limpio
    empresa.whatsapp = whatsapp_limpio

    if slug_final != slug_original:
        old_static_dir = Path("app/static/empresas") / slug_original
        new_static_dir = Path("app/static/empresas") / slug_final
        if old_static_dir.exists():
            if new_static_dir.exists():
                shutil.rmtree(new_static_dir)
            old_static_dir.rename(new_static_dir)

        old_storage_dir = MEDIA_BASE_DIR / slug_original
        new_storage_dir = MEDIA_BASE_DIR / slug_final
        if old_storage_dir.exists():
            if new_storage_dir.exists():
                shutil.rmtree(new_storage_dir)
            old_storage_dir.rename(new_storage_dir)

        if empresa.logo_url:
            empresa.logo_url = empresa.logo_url.replace(f"/empresas/{slug_original}/", f"/empresas/{slug_final}/")
        if empresa.banner_url:
            empresa.banner_url = empresa.banner_url.replace(f"/empresas/{slug_original}/", f"/empresas/{slug_final}/")

        productos = db.query(models.Producto).filter(models.Producto.empresa_id == empresa.id).all()
        for p in productos:
            if p.imagen_url:
                p.imagen_url = p.imagen_url.replace(f"/empresas/{slug_original}/", f"/empresas/{slug_final}/")

        empresa.slug = slug_final

    db.add(empresa)
    db.commit()

    return panel_redirect(empresa_slug=empresa.slug, msg="Empresa actualizada correctamente.")


@app.post("/empresa/politicas_catalogo")
def actualizar_politicas_catalogo(
    request: Request,
    empresa_slug: str = Form(...),
    politica_precio_catalogo: str = Form("automatico"),
    politica_stock_catalogo: str = Form("mostrar"),
    db: Session = Depends(get_db),
):
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
        return auth

    empresa = get_empresa_by_slug(db, empresa_slug)
    if not empresa:
        return panel_redirect(error="Empresa no encontrada.")

    empresa.politica_precio_catalogo = normalize_price_policy(politica_precio_catalogo)
    empresa.politica_stock_catalogo = normalize_stock_policy(politica_stock_catalogo)
    db.add(empresa)
    db.commit()
    return panel_redirect(empresa_slug=empresa.slug, msg="Configuración de visualización actualizada.")

@app.get("/admin/productos", response_class=HTMLResponse)
@app.get("/cliente/productos", response_class=HTMLResponse)
def admin_productos(
    request: Request,
    empresa: str | None = Query(default=None),
    q: str = Query(default=""),
    db: Session = Depends(get_db)
):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user

    empresa = can_access_empresa(user, empresa, db)
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
            "is_admin": user.rol == "admin",
        },
    )


@app.get("/admin/productos/{producto_id}/editar", response_class=HTMLResponse)
@app.get("/cliente/productos/{producto_id}/editar", response_class=HTMLResponse)
def editar_producto_view(
    request: Request,
    producto_id: int,
    empresa: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user

    producto = (
        db.query(models.Producto)
        .filter(models.Producto.id == producto_id)
        .first()
    )
    if not producto:
        return RedirectResponse(url="/cliente/productos", status_code=303)

    if user.rol == "cliente" and user.empresa_id != producto.empresa_id:
        return RedirectResponse(url="/cliente?error=No autorizado para editar este producto", status_code=303)

    empresa_ctx = get_empresa_by_slug(db, empresa) if empresa else producto.empresa
    if not empresa_ctx:
        empresa_ctx = producto.empresa

    return templates.TemplateResponse(
        "admin_producto_editar.html",
        {
            "request": request,
            "producto": producto,
            "empresa": empresa_ctx,
            "is_admin": user.rol == "admin",
        },
    )


@app.post("/admin/productos/{producto_id}/actualizar")
@app.post("/cliente/productos/{producto_id}/actualizar")
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
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user

    producto = db.query(models.Producto).filter(models.Producto.id == producto_id).first()
    if not producto:
        return RedirectResponse(url="/cliente/productos", status_code=303)
    if user.rol == "cliente" and user.empresa_id != producto.empresa_id:
        return RedirectResponse(url="/cliente?error=No autorizado para editar este producto", status_code=303)

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
        img_path = get_productos_media_dir(empresa.slug)
        img_path.mkdir(parents=True, exist_ok=True)
        codigo_safe = sanitize_codigo_for_filename(producto.codigo)

        # borrar imágenes viejas
        for ext in ALLOWED_IMAGE_EXTENSIONS:
            old = img_path / f"{codigo_safe}{ext}"
            if old.exists():
                old.unlink()

        # guardar nueva imagen
        ext = Path(imagen.filename).suffix.lower()
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            ext = ".jpg"
        filename = f"{codigo_safe}{ext}"

        with open(img_path / filename, "wb") as f:
            f.write(await imagen.read())

        producto.imagen_url = build_producto_media_url(empresa.slug, filename)

    db.commit()
    target_empresa = empresa_slug or (producto.empresa.slug if producto.empresa else "")
    products_path = "/admin/productos" if user.rol == "admin" else "/cliente/productos"
    redirect_target = f"{products_path}?empresa={quote(target_empresa)}" if target_empresa else products_path
    return RedirectResponse(url=redirect_target, status_code=303)

@app.get("/admin/borrar_empresa/{empresa_id}")
def borrar_empresa_get(request: Request, empresa_id: int, db: Session = Depends(get_db)):
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
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
# HOME / DASHBOARDS
# ---------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home_router(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url=get_dashboard_path(user), status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_panel(
    request: Request,
    empresa: str = "",
    tab: str = "empresa",
    msg: str = "",
    error: str = "",
    lead_q: str = "",
    lead_whatsapp: str = "",
    lead_pdf: str = "",
    lead_cart: str = "",
    lead_id: int | None = None,
    db: Session = Depends(get_db)
):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user

    empresas = db.query(models.Empresa).order_by(models.Empresa.nombre).all()
    empresa_activa = get_empresa_by_slug(db, empresa) or get_default_empresa(db)
    import time
    using_default_admin_password = os.getenv("ADMIN_PASSWORD", "").strip() in {"", "admin123"}
    active_tab = clean_text(tab, default="empresa").lower()
    if active_tab not in {"empresa", "productos", "usuarios", "backup", "avanzado", "leads"}:
        active_tab = "empresa"

    lead_whatsapp_filter = parse_bool_query_flag(lead_whatsapp)
    lead_pdf_filter = parse_bool_query_flag(lead_pdf)
    lead_cart_filter = parse_bool_query_flag(lead_cart)
    leads_rows = []
    lead_selected = None
    lead_selected_summary = None
    lead_timeline = []

    if empresa_activa:
        leads_rows = list_catalog_leads_for_admin(
            db=db,
            empresa_id=empresa_activa.id,
            search_query=lead_q,
            whatsapp_filter=lead_whatsapp_filter,
            pdf_filter=lead_pdf_filter,
            cart_filter=lead_cart_filter,
        )

        if lead_id:
            lead_selected = (
                db.query(models.CatalogLead)
                .filter(
                    models.CatalogLead.id == lead_id,
                    models.CatalogLead.empresa_catalogo_id == empresa_activa.id,
                )
                .first()
            )

        if lead_selected:
            lead_events = (
                db.query(models.CatalogLeadEvent)
                .filter(
                    models.CatalogLeadEvent.lead_id == lead_selected.id,
                    models.CatalogLeadEvent.empresa_catalogo_id == empresa_activa.id,
                )
                .order_by(models.CatalogLeadEvent.created_at.desc(), models.CatalogLeadEvent.id.desc())
                .limit(200)
                .all()
            )
            lead_selected_summary = get_lead_summary_from_events(lead_events)
            lead_timeline = build_lead_timeline_rows(lead_events)


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
            "admin_username": os.getenv("ADMIN_USER", "admin"),
            "app_build": APP_BUILD,
            "active_tab": active_tab,
            "leads_rows": leads_rows,
            "lead_q": lead_q,
            "lead_q_url": quote(lead_q or ""),
            "lead_whatsapp": lead_whatsapp_filter,
            "lead_pdf": lead_pdf_filter,
            "lead_cart": lead_cart_filter,
            "lead_selected": lead_selected,
            "lead_selected_summary": lead_selected_summary,
            "lead_timeline": lead_timeline,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/cliente", response_class=HTMLResponse)
def cliente_panel(
    request: Request,
    msg: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user

    empresa_activa = resolve_empresa_for_user(user, db, None)
    if not empresa_activa:
        return HTMLResponse("<h1>Usuario sin empresa asignada</h1>", status_code=403)

    import time
    catalogo_public_url = str(request.url_for("catalogo", slug=empresa_activa.slug))
    whatsapp_message = f"Hola, te comparto nuestro catálogo online: {catalogo_public_url}"
    whatsapp_share_url = f"https://wa.me/?text={quote(whatsapp_message, safe='')}"
    response = templates.TemplateResponse(
        "cliente_panel.html",
        {
            "request": request,
            "msg": msg,
            "error": error,
            "empresa_activa": empresa_activa,
            "empresa_query": empresa_activa.slug,
            "empresa_logo_url": get_empresa_logo_url(empresa_activa),
            "catalogo_public_url": catalogo_public_url,
            "whatsapp_share_url": whatsapp_share_url,
            "time": int(time.time()),
            "app_build": APP_BUILD,
            "is_admin_view": user.rol == "admin",
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/panel")
def panel_alias():
    return RedirectResponse(url="/cliente", status_code=303)

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
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
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
        whatsapp=whatsapp.strip(),
        politica_precio_catalogo="automatico",
        politica_stock_catalogo="mostrar",
    )
    db.add(empresa)
    db.commit()
    db.refresh(empresa)

    get_productos_media_dir(empresa.slug).mkdir(parents=True, exist_ok=True)

    if logo:
        empresa.logo_url = await replace_empresa_media(empresa, media_type="logo", upload=logo)

    if banner:
        empresa.banner_url = await replace_empresa_media(empresa, media_type="banner", upload=banner)

    db.add(empresa)
    db.commit()

    return panel_redirect(empresa_slug=empresa.slug, msg="Empresa creada correctamente")


@app.post("/admin/usuarios/crear")
def crear_usuario_cliente(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    rol: str = Form("cliente"),
    empresa_slug: str = Form(""),
    db: Session = Depends(get_db),
):
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
        return auth

    username_clean = clean_text(username, default="").lower()
    if not username_clean or len(password) < 6:
        return panel_redirect(error="Usuario inválido o contraseña muy corta (mínimo 6).")

    if db.query(models.Usuario).filter(models.Usuario.username == username_clean).first():
        return panel_redirect(error="Ese usuario ya existe.")

    role_clean = "admin" if rol == "admin" else "cliente"
    empresa_id = None
    if role_clean == "cliente":
        empresa = get_empresa_by_slug(db, empresa_slug)
        if not empresa:
            return panel_redirect(error="Para cliente debés seleccionar empresa.")
        empresa_id = empresa.id

    user = models.Usuario(
        username=username_clean,
        password_hash=hash_password(password),
        rol=role_clean,
        activo=True,
        empresa_id=empresa_id,
    )
    db.add(user)
    db.commit()
    return panel_redirect(
        empresa_slug=empresa_slug or None,
        msg=f"Usuario '{username_clean}' creado con rol {role_clean}."
    )


@app.post("/delete_all_products")
def delete_all_products(
    request: Request,
    empresa_slug: str = Form(...),
    db: Session = Depends(get_db)
):
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
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
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user

    try:
        empresa = can_access_empresa(user, empresa_slug, db)
        if not empresa:
            return redirect_for_user(user, error="Empresa inválida. Seleccioná una empresa primero.")

        filename = (file.filename or "").lower()
        if not filename.endswith((".xlsx", ".xls")):
            return panel_redirect(empresa_slug=empresa.slug, error="Formato inválido. Subí un archivo Excel (.xlsx o .xls).")

        df = pd.read_excel(file.file)
        df.columns = [c.strip().lower() for c in df.columns]

        required = ["codigo", "descripcion", "precio"]
        for col in required:
            if col not in df.columns:
                return redirect_for_user(user, empresa_slug=empresa.slug, error=f"Falta columna obligatoria: {col}")

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

        return redirect_for_user(
            user,
            empresa_slug=empresa.slug,
            msg=f"Productos cargados. Nuevos: {nuevos}, Actualizados: {actualizados}."
        )

    except Exception as e:
        print("Error Excel:", e)
        return redirect_for_user(user, empresa_slug=empresa_slug, error="Error al procesar el Excel.")


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
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user

    try:
        empresa = can_access_empresa(user, empresa_slug, db)
        if not empresa:
            return redirect_for_user(user, error="Empresa inválida.")

        images_dir = get_productos_media_dir(empresa.slug)
        images_dir.mkdir(parents=True, exist_ok=True)
        copied = 0

        with zipfile.ZipFile(file.file, "r") as zip_ref:
            for member, safe_path in _zip_safe_members(zip_ref):
                ext = safe_path.suffix.lower()
                if ext not in ALLOWED_IMAGE_EXTENSIONS:
                    continue

                code_raw = clean_text(safe_path.stem, default="")
                code_name = sanitize_codigo_for_filename(code_raw)
                filename = f"{code_name}{ext}"
                destination = images_dir / filename
                with zip_ref.open(member, "r") as src, open(destination, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                producto = (
                    db.query(models.Producto)
                    .filter(
                        models.Producto.empresa_id == empresa.id,
                        models.Producto.codigo == code_raw
                    )
                    .first()
                )
                if producto:
                    producto.imagen_url = build_producto_media_url(empresa.slug, filename)
                copied += 1

        db.commit()
        return redirect_for_user(user, empresa_slug=empresa.slug, msg=f"Imágenes cargadas correctamente ({copied} archivos).")

    except Exception as e:
        print("Error ZIP:", e)
        return redirect_for_user(user, empresa_slug=empresa_slug, error="Error al procesar el ZIP.")


@app.get("/admin/empresa/exportar")
def exportar_empresa_completa(
    request: Request,
    empresa: str | None = Query(default=None),
    db: Session = Depends(get_db)
):
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
        return auth

    empresa_obj = get_empresa_by_slug(db, empresa) or get_default_empresa(db)
    if not empresa_obj:
        return JSONResponse({"error": "No hay empresa activa para exportar"}, status_code=400)

    productos = db.query(models.Producto).filter(models.Producto.empresa_id == empresa_obj.id).all()
    payload = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "empresa": {
            "nombre": empresa_obj.nombre,
            "slug": empresa_obj.slug,
            "whatsapp": empresa_obj.whatsapp,
            "logo_url": empresa_obj.logo_url,
            "banner_url": empresa_obj.banner_url,
            "politica_precio_catalogo": normalize_price_policy(empresa_obj.politica_precio_catalogo),
            "politica_stock_catalogo": normalize_stock_policy(empresa_obj.politica_stock_catalogo),
        },
        "productos": [
            {
                "codigo": p.codigo,
                "descripcion": p.descripcion,
                "categoria": p.categoria,
                "marca": p.marca,
                "precio": float(p.precio or 0),
                "stock": int(p.stock or 0),
                "activo": bool(p.activo),
                "imagen_url": p.imagen_url,
            }
            for p in productos
        ],
    }

    memory_file = BytesIO()
    static_empresa_dir = Path("app/static/empresas") / empresa_obj.slug
    storage_empresa_dir = MEDIA_BASE_DIR / empresa_obj.slug

    with zipfile.ZipFile(memory_file, mode="w", compression=zipfile.ZIP_DEFLATED) as zipf:
        zipf.writestr("empresa.json", json.dumps(payload, ensure_ascii=False, indent=2))

        if static_empresa_dir.exists():
            for file_path in static_empresa_dir.rglob("*"):
                if file_path.is_file():
                    arcname = Path("static_empresas") / file_path.relative_to(static_empresa_dir)
                    zipf.write(file_path, arcname.as_posix())

        if storage_empresa_dir.exists():
            for file_path in storage_empresa_dir.rglob("*"):
                if file_path.is_file():
                    arcname = Path("storage_empresas") / file_path.relative_to(storage_empresa_dir)
                    zipf.write(file_path, arcname.as_posix())

    memory_file.seek(0)
    filename = f"empresa_{empresa_obj.slug}_backup.zip"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(memory_file, media_type="application/zip", headers=headers)


@app.post("/admin/empresa/importar")
def importar_empresa_completa(
    request: Request,
    empresa_slug: str = Form(""),
    import_mode: str = Form("duplicate"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
        return auth

    mode = (import_mode or "duplicate").strip().lower()
    if mode not in {"duplicate", "replace"}:
        mode = "duplicate"

    try:
        zip_bytes = file.file.read()
        with zipfile.ZipFile(BytesIO(zip_bytes), "r") as zip_ref:
            if "empresa.json" not in zip_ref.namelist():
                return panel_redirect(empresa_slug=empresa_slug, error="ZIP inválido: falta empresa.json.")

            payload = json.loads(zip_ref.read("empresa.json").decode("utf-8"))
            empresa_data = payload.get("empresa", {}) or {}
            productos_data = payload.get("productos", []) or []

            source_slug = clean_text(empresa_data.get("slug", ""), default="")
            source_slug = re.sub(r"[^a-z0-9\-]", "-", source_slug.lower())
            source_slug = re.sub(r"-+", "-", source_slug).strip("-")
            if not source_slug:
                return panel_redirect(empresa_slug=empresa_slug, error="ZIP inválido: slug de empresa vacío.")

            existing = get_empresa_by_slug(db, source_slug)

            if mode == "replace":
                target_slug = source_slug
                if existing:
                    target_empresa = existing
                    db.query(models.Producto).filter(models.Producto.empresa_id == target_empresa.id).delete()
                    static_target = Path("app/static/empresas") / target_slug
                    if static_target.exists():
                        shutil.rmtree(static_target)
                    storage_target = MEDIA_BASE_DIR / target_slug
                    if storage_target.exists():
                        shutil.rmtree(storage_target)
                else:
                    target_empresa = models.Empresa(
                        nombre=clean_text(empresa_data.get("nombre", source_slug), default=source_slug),
                        slug=target_slug,
                        whatsapp=clean_text(empresa_data.get("whatsapp", ""), default="") or None,
                        politica_precio_catalogo=normalize_price_policy(empresa_data.get("politica_precio_catalogo")),
                        politica_stock_catalogo=normalize_stock_policy(empresa_data.get("politica_stock_catalogo")),
                    )
                    db.add(target_empresa)
                    db.flush()
            else:
                target_slug = build_unique_slug(db, source_slug)
                target_empresa = models.Empresa(
                    nombre=clean_text(empresa_data.get("nombre", source_slug), default=source_slug),
                    slug=target_slug,
                    whatsapp=clean_text(empresa_data.get("whatsapp", ""), default="") or None,
                    politica_precio_catalogo=normalize_price_policy(empresa_data.get("politica_precio_catalogo")),
                    politica_stock_catalogo=normalize_stock_policy(empresa_data.get("politica_stock_catalogo")),
                )
                db.add(target_empresa)
                db.flush()

            target_empresa.nombre = clean_text(empresa_data.get("nombre", target_empresa.nombre), default=target_empresa.nombre)
            target_empresa.whatsapp = clean_text(empresa_data.get("whatsapp", target_empresa.whatsapp or ""), default="") or None
            target_empresa.politica_precio_catalogo = normalize_price_policy(
                empresa_data.get("politica_precio_catalogo", target_empresa.politica_precio_catalogo)
            )
            target_empresa.politica_stock_catalogo = normalize_stock_policy(
                empresa_data.get("politica_stock_catalogo", target_empresa.politica_stock_catalogo)
            )
            target_empresa.logo_url = build_media_url(target_slug, "logo", "logo.png")
            target_empresa.banner_url = build_media_url(target_slug, "banner", "banner.jpg")

            for p in productos_data:
                codigo = clean_text(p.get("codigo", ""), default="")
                if not codigo:
                    continue
                codigo_safe = sanitize_codigo_for_filename(codigo)
                imported_url = clean_text(p.get("imagen_url", ""), default="") or None
                normalized_imagen_url = None
                if imported_url:
                    imported_name = Path(imported_url).name
                    if imported_name:
                        normalized_imagen_url = build_producto_media_url(target_slug, imported_name)
                if not normalized_imagen_url:
                    normalized_imagen_url = build_producto_media_url(target_slug, f"{codigo_safe}.jpg")
                db.add(models.Producto(
                    empresa_id=target_empresa.id,
                    codigo=codigo,
                    descripcion=clean_text(p.get("descripcion", codigo), default=codigo),
                    categoria=clean_text(p.get("categoria", ""), default="") or None,
                    marca=clean_text(p.get("marca", ""), default="") or None,
                    precio=clean_price(p.get("precio", 0), default=0.0),
                    stock=clean_stock(p.get("stock", 0), default=0),
                    activo=bool(p.get("activo", True)),
                    imagen_url=normalized_imagen_url,
                ))

            static_target_dir = Path("app/static/empresas") / target_slug
            storage_target_dir = MEDIA_BASE_DIR / target_slug
            _copy_zip_prefix(zip_ref, "static_empresas", static_target_dir)
            _copy_zip_prefix(zip_ref, "storage_empresas", storage_target_dir)

            legacy_productos_dir = static_target_dir / "productos"
            persistent_productos_dir = get_productos_media_dir(target_slug)
            if legacy_productos_dir.exists():
                persistent_productos_dir.mkdir(parents=True, exist_ok=True)
                for legacy_file in legacy_productos_dir.rglob("*"):
                    if not legacy_file.is_file():
                        continue
                    if legacy_file.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
                        continue
                    destination = persistent_productos_dir / legacy_file.name
                    if not destination.exists():
                        _copy_file(legacy_file, destination)

            db.add(target_empresa)
            db.commit()

            action = "reemplazada" if mode == "replace" else "importada"
            return panel_redirect(
                empresa_slug=target_slug,
                msg=f"Empresa {action} correctamente con slug '{target_slug}'."
            )

    except zipfile.BadZipFile:
        return panel_redirect(empresa_slug=empresa_slug, error="Archivo ZIP inválido.")
    except Exception as e:
        db.rollback()
        print("Error importando empresa:", e)
        return panel_redirect(empresa_slug=empresa_slug, error="Error al importar la empresa.")

# ---------------------------------------------------
# CATÁLOGO
# ---------------------------------------------------
@app.get("/catalogo/{slug}/acceso", response_class=HTMLResponse)
def catalogo_acceso(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    empresa = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if not empresa:
        return HTMLResponse("<h1>Empresa no encontrada</h1>", status_code=404)

    lead = get_active_catalog_lead(request, slug, empresa.id, db)
    if lead:
        return RedirectResponse(url=f"/catalogo/{slug}", status_code=303)

    return templates.TemplateResponse(
        "catalogo_acceso.html",
        {
            "request": request,
            "empresa": empresa,
            "empresa_logo_url": get_empresa_logo_url(empresa),
        },
    )


@app.post("/catalogo/{slug}/acceso")
def catalogo_acceso_submit(
    slug: str,
    request: Request,
    nombre: str = Form(""),
    empresa_nombre: str = Form(""),
    email: str = Form(""),
    telefono: str = Form(""),
    db: Session = Depends(get_db),
):
    empresa_obj = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if not empresa_obj:
        return HTMLResponse("<h1>Empresa no encontrada</h1>", status_code=404)

    nombre_limpio = clean_text(nombre, default="")
    empresa_limpia = clean_text(empresa_nombre, default="")
    email_limpio = clean_text(email, default="").lower()
    telefono_limpio = clean_text(telefono, default="") or None

    error = None
    if not nombre_limpio:
        error = "Completá nombre y apellido."
    elif not empresa_limpia:
        error = "Completá empresa o comercio."
    elif not email_limpio:
        error = "Completá email."
    elif not EMAIL_PATTERN.match(email_limpio):
        error = "Ingresá un email válido."

    if error:
        return templates.TemplateResponse(
            "catalogo_acceso.html",
            {
                "request": request,
                "empresa": empresa_obj,
                "empresa_logo_url": get_empresa_logo_url(empresa_obj),
                "error": error,
                "form_data": {
                    "nombre": nombre_limpio,
                    "empresa_nombre": empresa_limpia,
                    "email": email_limpio,
                    "telefono": telefono_limpio or "",
                },
            },
            status_code=400,
        )

    lead = (
        db.query(models.CatalogLead)
        .filter(
            models.CatalogLead.empresa_catalogo_id == empresa_obj.id,
            models.CatalogLead.email == email_limpio,
        )
        .first()
    )

    new_token = secrets.token_urlsafe(32)
    now = utc_now()
    if not lead:
        lead = models.CatalogLead(
            empresa_catalogo_id=empresa_obj.id,
            nombre=nombre_limpio,
            empresa=empresa_limpia,
            email=email_limpio,
            telefono=telefono_limpio,
            fecha_ingreso=now,
            ultima_actividad=now,
            session_token=new_token,
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
    else:
        lead.nombre = nombre_limpio
        lead.empresa = empresa_limpia
        lead.telefono = telefono_limpio
        lead.session_token = new_token
        lead.ultima_actividad = now
        db.add(lead)
        db.commit()
        db.refresh(lead)

    set_lead_session_for_slug(request, slug, lead.id, new_token)
    register_catalog_event(
        db=db,
        lead=lead,
        empresa_id=empresa_obj.id,
        event_type="catalog_entered",
        metadata={"source": "access_form"},
    )
    return RedirectResponse(url=f"/catalogo/{slug}", status_code=303)


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
    lead = get_active_catalog_lead(request, slug, empresa.id, db)
    if not lead:
        return RedirectResponse(url=f"/catalogo/{slug}/acceso", status_code=303)

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
    price_policy = normalize_price_policy(empresa.politica_precio_catalogo)
    stock_policy = normalize_stock_policy(empresa.politica_stock_catalogo)

    changed_image_urls = False
    for p in productos:
        resolved_url = resolve_producto_imagen_url(p, empresa.slug, migrate_legacy=True)
        if p.imagen_url != resolved_url:
            p.imagen_url = resolved_url
            changed_image_urls = True
        price_display = resolve_price_display(price_policy, p.precio)
        stock_display = resolve_stock_display(stock_policy, p.stock)
        p.catalog_price_numeric = price_display["mostrar_numerico"]
        p.catalog_price_text = price_display["texto"]
        p.catalog_stock_visible = stock_display["visible"]
        p.catalog_stock_text = stock_display["texto"]
        p.catalog_stock_class = stock_display["clase"]

    if changed_image_urls:
        db.commit()



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
            "precio_mostrable": bool(getattr(p, "catalog_price_numeric", False)),
            "precio_texto": getattr(p, "catalog_price_text", "Consultar"),
            "categoria": p.categoria,
            "marca": p.marca,
            "stock": p.stock,
            "stock_visible": bool(getattr(p, "catalog_stock_visible", False)),
            "stock_texto": getattr(p, "catalog_stock_text", ""),
            "stock_clase": getattr(p, "catalog_stock_class", ""),
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
                "politica_precio_catalogo": price_policy,
                "politica_stock_catalogo": stock_policy,
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
            "price_policy": price_policy,
            "stock_policy": stock_policy,
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
            "lead_data": {
                "nombre": lead.nombre,
                "empresa": lead.empresa,
                "email": lead.email,
                "telefono": lead.telefono or "",
            },
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.post("/catalogo/{slug}/track")
def track_catalog_event(
    slug: str,
    request: Request,
    payload: CatalogEventPayload,
    db: Session = Depends(get_db),
):
    empresa = db.query(models.Empresa).filter(models.Empresa.slug == slug).first()
    if not empresa:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    lead = get_active_catalog_lead(request, slug, empresa.id, db)
    if not lead:
        raise HTTPException(status_code=401, detail="Lead no identificado para esta sesión")

    event_type = clean_text(payload.event_type, default="")
    if event_type not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de evento inválido")

    register_catalog_event(
        db=db,
        lead=lead,
        empresa_id=empresa.id,
        event_type=event_type,
        product_code=payload.product_code,
        search_term=payload.search_term,
        metadata=payload.metadata or {},
    )
    return {"ok": True}


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
    buyer: dict = data.get("buyer", {}) or {}

    buyer_nombre = str(buyer.get("nombre", "")).strip()
    buyer_comercio = str(buyer.get("comercio", "")).strip()
    buyer_telefono = str(buyer.get("telefono", "")).strip()
    buyer_direccion = str(buyer.get("direccion", "")).strip()
    buyer_cuit = str(buyer.get("cuit", "")).strip()
    buyer_email = str(buyer.get("email", "")).strip()
    buyer_obs = str(buyer.get("observaciones", "")).strip()

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)

    y = A4[1] - 40
    x = 40
    bottom_limit = 55

    def draw_line(text: str, font: str = "Helvetica", size: int = 11, indent: int = 0, line_gap: int = 17):
        nonlocal y
        if y <= bottom_limit:
            c.showPage()
            y = A4[1] - 40
        c.setFont(font, size)
        c.drawString(x + indent, y, text)
        y -= line_gap

    draw_line(empresa, font="Helvetica-Bold", size=18, line_gap=30)

    draw_line("Datos del comprador", font="Helvetica-Bold", size=12, line_gap=18)
    draw_line(f"Nombre y apellido: {buyer_nombre or '-'}")
    draw_line(f"Comercio / empresa: {buyer_comercio or '-'}")
    draw_line(f"Teléfono: {buyer_telefono or '-'}")
    draw_line(f"Dirección: {buyer_direccion or '-'}")
    if buyer_cuit:
        draw_line(f"CUIT: {buyer_cuit}")
    if buyer_email:
        draw_line(f"Email: {buyer_email}")
    if buyer_obs:
        draw_line(f"Observaciones: {buyer_obs}")

    y -= 5
    draw_line("Detalle del pedido", font="Helvetica-Bold", size=12, line_gap=18)

    total = 0.0
    has_consult_price = False
    for item in items:
        cantidad = float(item.get("cantidad", 0))
        precio = float(item.get("precio", 0))
        precio_texto = clean_text(item.get("precio_texto", ""), default="")
        precio_mostrable = bool(item.get("precio_mostrable", True))
        subtotal = precio * cantidad
        codigo = item.get("codigo", "")
        descripcion = item.get("descripcion", "")
        draw_line(f'{int(cantidad)}x {codigo} - {descripcion}')
        if precio_mostrable:
            draw_line(f'${precio:.2f} c/u · Subtotal: ${subtotal:.2f}', indent=15, line_gap=19)
            total += subtotal
        else:
            has_consult_price = True
            draw_line(f'Precio: {precio_texto or "Consultar"}', indent=15, line_gap=19)

    y -= 5
    if has_consult_price:
        draw_line("TOTAL ESTIMADO: Consultar", font="Helvetica-Bold", size=14, line_gap=20)
    else:
        draw_line(f"TOTAL ESTIMADO: ${total:.2f}", font="Helvetica-Bold", size=14, line_gap=20)

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
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
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
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
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
def debug_imagenes(request: Request, slug: str, db: Session = Depends(get_db)):
    auth = require_admin(request, db)
    if isinstance(auth, RedirectResponse):
        return auth

    slug = (slug or "").strip().lower()
    path = get_productos_media_dir(slug)
    if not path.exists():
        return {"error": "Carpeta no existe", "path": str(path)}

    files = sorted([p.name for p in path.iterdir() if p.is_file()])
    # devolvemos solo los primeros 200 para no explotar la respuesta
    return {"path": str(path), "count": len(files), "files": files[:200]}
