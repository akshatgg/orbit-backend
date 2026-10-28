import json
import logging
from typing import List, Optional

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Environment -----------------------------------------------------------
    ENVIRONMENT: str = "local"
    APP_TIMEZONE: str = "Europe/London"
    DEFAULT_TIMEZONE: str = "Europe/London"

    # Database --------------------------------------------------------------
    DATABASE_URL: Optional[str] = None

    # Auth ------------------------------------------------------------------
    JWT_SECRET: str = "orbit-backend-dev-jwt-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 720  # 30 days — students open Orbit between lectures
    SESSION_COOKIE: str = "orbit_session"
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_URL: str = "http://localhost:8090"
    # Founder/ops account promoted on signup, matching Orbit's super_admin_email().
    SUPER_ADMIN_EMAIL: Optional[str] = None
    MAGIC_LINK_TTL_MINUTES: int = 60
    OAUTH_STATE_TTL_MINUTES: int = 10

    # Google OAuth ----------------------------------------------------------
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    # Email (Resend) --------------------------------------------------------
    RESEND_API_KEY: Optional[str] = None
    EMAIL_FROM: str = "Orbit <hello@orbit.example>"

    # Stripe ----------------------------------------------------------------
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None
    CURRENCY: str = "GBP"

    # Uploads ---------------------------------------------------------------
    CLOUDINARY_CLOUD_NAME: Optional[str] = None
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None
    IMAGE_MAX_BYTES: dict = {"event-covers": 5 * 1024 * 1024, "organiser-logos": 2 * 1024 * 1024}

    # Web push --------------------------------------------------------------
    VAPID_PUBLIC_KEY: Optional[str] = None
    VAPID_PRIVATE_KEY: Optional[str] = None
    VAPID_SUBJECT: str = "mailto:hello@orbit.example"

    # Cron ------------------------------------------------------------------
    CRON_SECRET: str = "orbit-backend-dev-cron-secret-change-me"

    # API -------------------------------------------------------------------
    API_PREFIX: str = "/api"
    PROJECT_NAME: str = "Orbit-Backend"
    VERSION: str = "0.1.0"
    CORS_ORIGINS: str = ""
    DEFAULT_PAGE_SIZE: int = 24
    MAX_PAGE_SIZE: int = 100

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"

    @property
    def is_local(self) -> bool:
        return self.ENVIRONMENT.lower().startswith("local")

    @property
    def allowed_origins(self) -> List[str]:
        raw = (self.CORS_ORIGINS or "").strip()
        if not raw:
            return []
        try:
            if raw.startswith("["):
                return [str(o).strip() for o in json.loads(raw) if str(o).strip()]
        except Exception:
            logger.warning("CORS_ORIGINS is not valid JSON; falling back to CSV")
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def google_configured(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    @property
    def stripe_configured(self) -> bool:
        return bool(self.STRIPE_SECRET_KEY)

    @property
    def cloudinary_configured(self) -> bool:
        return bool(
            self.CLOUDINARY_CLOUD_NAME
            and self.CLOUDINARY_API_KEY
            and self.CLOUDINARY_API_SECRET
        )


settings = Settings()
