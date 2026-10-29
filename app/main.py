import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router
from app.categories.router import router as categories_router
from app.core.config import settings
from app.core.logging_config import configure_logging
from app.cron.router import router as cron_router
from app.discovery.router import router as discovery_router
from app.events.router import router as events_router
from app.feed.router import router as feed_router
from app.groups.router import router as groups_router
from app.health.router import router as health_router
from app.messaging.router import group_chat_router as messaging_groups_router
from app.messaging.router import router as messaging_router
from app.notifications.router import router as notifications_router
from app.organisers.router import router as organisers_router
from app.payments.router import router as payments_router
from app.profiles.router import router as profiles_router
from app.public.router import router as public_router
from app.sources.router import router as sources_router
from app.studio.router import router as studio_router
from app.uploads.router import router as uploads_router

load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s ...", settings.PROJECT_NAME)
    if settings.is_local:
        logger.info("Local environment: %s", settings.ENVIRONMENT)
    yield
    logger.info("Shutting down %s ...", settings.PROJECT_NAME)


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Order matters only for readability: public reads first, then the signed-in
# surfaces, then the admin console and the machine endpoints.
app.include_router(health_router, prefix=settings.API_PREFIX)
app.include_router(auth_router, prefix=settings.API_PREFIX)
app.include_router(public_router, prefix=settings.API_PREFIX)
app.include_router(categories_router, prefix=settings.API_PREFIX)
app.include_router(events_router, prefix=settings.API_PREFIX)
app.include_router(feed_router, prefix=settings.API_PREFIX)
app.include_router(discovery_router, prefix=settings.API_PREFIX)
app.include_router(organisers_router, prefix=settings.API_PREFIX)
app.include_router(profiles_router, prefix=settings.API_PREFIX)
app.include_router(groups_router, prefix=settings.API_PREFIX)
app.include_router(messaging_groups_router, prefix=settings.API_PREFIX)
app.include_router(messaging_router, prefix=settings.API_PREFIX)
app.include_router(notifications_router, prefix=settings.API_PREFIX)
app.include_router(studio_router, prefix=settings.API_PREFIX)
app.include_router(payments_router, prefix=settings.API_PREFIX)
app.include_router(uploads_router, prefix=settings.API_PREFIX)
app.include_router(admin_router, prefix=settings.API_PREFIX)
app.include_router(sources_router, prefix=settings.API_PREFIX)
app.include_router(cron_router, prefix=settings.API_PREFIX)


@app.get("/")
async def root() -> dict:
    return {"service": settings.PROJECT_NAME, "docs": "/docs"}
