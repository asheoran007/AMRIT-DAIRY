from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All values come from environment / .env. No secrets are defaulted."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    adapter_brand_id: str = "amrit_dairy"
    adapter_database_url: str = "sqlite:///./adapter.db"
    adapter_embed_worker: bool = False   # true = run the worker loop inside the API process (single-service hosts such as Render + one disk)
    adapter_worker_interval_s: int = 20

    shopify_shop_domain: str = ""
    shopify_webhook_secret: str = ""
    shopify_admin_token: str = ""   # optional static/offline token (authorization-code grant or still-valid legacy app)
    shopify_client_id: str = ""     # Dev Dashboard app (same Shopify organization as the store): client credentials grant, 24 h tokens auto-renewed
    shopify_client_secret: str = ""  # also the webhook HMAC secret for subscriptions created by this app (used when SHOPIFY_WEBHOOK_SECRET is blank)

    adapter_public_url: str = ""    # https://adapter.amritdairy.in — used for the OAuth redirect URI
    _stored_token: str | None = None   # offline token obtained via /shopify/auth (authorization-code grant), loaded from DB

    @property
    def shopify_configured(self) -> bool:
        return bool(self.shopify_shop_domain and (self.shopify_admin_token or self._stored_token or (self.shopify_client_id and self.shopify_client_secret)))

    def load_stored_token(self, engine) -> str | None:
        """Pick up an offline token stored by the OAuth callback (DB), if any; wins over client-credentials."""
        from .engine.shopify_oauth import load_token
        with engine.begin() as conn:
            self._stored_token = load_token(conn, self.shopify_shop_domain) if self.shopify_shop_domain else None
        return self._stored_token

    @property
    def shopify_auth_mode(self) -> str:
        if not self.shopify_configured:
            return "not_configured"
        if self.shopify_admin_token or self._stored_token:
            return "offline_token"
        return "client_credentials"

    @property
    def effective_shopify_webhook_secret(self) -> str:
        return self.shopify_webhook_secret or self.shopify_client_secret

    def shopify_token_provider(self):
        from .engine.shopify_auth import TokenProvider
        return TokenProvider(self.shopify_shop_domain, static_token=self.shopify_admin_token or self._stored_token or "",
                             client_id=self.shopify_client_id, client_secret=self.shopify_client_secret)

    razorpay_webhook_secret: str = ""
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""

    shiprocket_webhook_token: str = ""
    shiprocket_api_email: str = ""
    shiprocket_api_password: str = ""

    bitespeed_api_key: str = ""
    inbox_webhook_token: str = ""   # unguessable token embedded in the support-console webhook URL
    test_phone_allowlist: str = ""  # comma-separated E.164 numbers allowed to run TESTPAY (₹10 real gateway test); empty = disabled
    test_payment_minor: int = 1000  # ₹10
    sender_mode: str = "dry_run"  # dry_run | live_test (replies only to TEST_PHONE_ALLOWLIST) | live
    # BiteSpeed support console = Chatwoot. Replies go out through its documented API
    # (POST /api/v1/accounts/{id}/conversations/{id}/messages, header api_access_token = an agent's profile access token).
    chatwoot_base_url: str = "https://chat.bitespeed.co"
    chatwoot_account_id: int = 3122
    chatwoot_api_token: str = ""    # Profile settings -> Access Token of the agent the bot replies as (secret; Render env only)

    promo_window_start: str = "09:00"
    promo_window_end: str = "20:00"
    promo_max_per_24h: int = 1
    promo_max_per_7d: int = 2
    abandoned_checkout_first_delay_min: int = 45

    budget_monthly_cap_inr: int | None = None
    operational_reserve_inr: int | None = None
    global_kill_switch: bool = True

    cod_zones_geojson: str = "./config/cod_zones.geojson"

    alert_destination: str = ""
    reports_enabled: bool = False

    policy_version: str = Field(default="BUSINESS_RULES-2026-10-11-draft")


settings = Settings()
