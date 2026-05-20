from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.routes import auth, schemas_routes, exercises, workouts, nutrition

settings = get_settings()
STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    from app.database import engine
    await engine.dispose()


app = FastAPI(
    title="Gym Tracker API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.APP_ENV != "production" else None,
    redoc_url=None,
)

# No-cache headers voor PWA shell-files (index.html, sw.js, manifest.json)
# zodat iOS PWA op elke relaunch een fresh versie ophaalt. Anders zit een
# gebruiker eeuwig vast aan de cached versie zonder dat het hostname iets
# verandert (single-domain deploy = geen URL-based cache-buster).
_NO_CACHE_PATHS = {"/", "/index.html", "/sw.js", "/manifest.json"}

@app.middleware("http")
async def no_cache_shell(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in _NO_CACHE_PATHS:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# CORS (item #6 + #7: strip-trim + explicit methods)
_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Reminder-Secret"],
)

# Routes
app.include_router(auth.router)
app.include_router(schemas_routes.router)
app.include_router(exercises.router)
app.include_router(workouts.router)
app.include_router(nutrition.router)


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """Item #9: DB-ping zodat container niet 'healthy' staat als Postgres dood is."""
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}
    except Exception as e:
        return {"status": "degraded", "db": "fail", "error": str(e)[:100]}


# Serve frontend static files (index.html, sw.js, manifest.json)
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
