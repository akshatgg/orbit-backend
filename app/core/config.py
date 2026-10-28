import json
import logging
import math
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
    # Parent domain for the session cookie (e.g. ".example.com") so the frontend's
    # host receives the cookie the API host sets. Blank = host-only (localhost).
    SESSION_COOKIE_DOMAIN: Optional[str] = None
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_URL: str = "http://localhost:8090"
    # Founder/ops account promoted on signup, matching Orbit's super_admin_email().
    SUPER_ADMIN_EMAIL: Optional[str] = None
    MAGIC_LINK_TTL_MINUTES: int = 60
    OAUTH_STATE_TTL_MINUTES: int = 10

    # Google OAuth ----------------------------------------------------------
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    # Email -----------------------------------------------------------------
    # SMTP when SMTP_HOST, SMTP_USER and SMTP_PASS are all set, else Resend when
    # RESEND_API_KEY is set, else no email (locally the email is logged instead).
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: Optional[str] = None  # 465 (implicit TLS) when blank; others use STARTTLS
    SMTP_USER: Optional[str] = None
    SMTP_PASS: Optional[str] = None
    RESEND_API_KEY: Optional[str] = None
    # Blank: "ORBIT <SMTP_USER>" over SMTP, "ORBIT <onboarding@resend.dev>" over Resend.
    EMAIL_FROM: Optional[str] = None

    # Stripe ----------------------------------------------------------------
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None
    # Signing secret of the Connect webhook endpoint (connected-account events
    # such as account.updated). Either secret, or a comma-separated list, verifies.
    STRIPE_CONNECT_WEBHOOK_SECRET: Optional[str] = None
    # Orbit's cut of each paid ticket, in percent ("5" or "5%"). Anything that
    # is not a positive number means no fee; values above 100 are capped.
    PLATFORM_FEE_PERCENT: str = "0"
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
    def stripe_webhook_secrets(self) -> List[str]:
        raw = [self.STRIPE_WEBHOOK_SECRET or "", self.STRIPE_CONNECT_WEBHOOK_SECRET or ""]
        return [s.strip() for value in raw for s in value.split(",") if s.strip()]

    @property
    def platform_fee_percent(self) -> float:
        raw = (self.PLATFORM_FEE_PERCENT or "").strip().removesuffix("%")
        try:
            value = float(raw)
        except ValueError:
            return 0.0
        if not math.isfinite(value) or value <= 0:
            return 0.0
        return min(value, 100.0)

    @property
    def push_configured(self) -> bool:
        return bool(self.VAPID_PUBLIC_KEY and self.VAPID_PRIVATE_KEY)

    @property
    def cloudinary_configured(self) -> bool:
        return bool(
            self.CLOUDINARY_CLOUD_NAME
            and self.CLOUDINARY_API_KEY
            and self.CLOUDINARY_API_SECRET
        )


settings = Settings()
