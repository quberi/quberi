from functools import lru_cache
from typing import Optional

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUBERI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    app_version: str = "0.1.0"
    api_version: str = "v1"
    default_locale: str = "en-US"
    allowed_origins: str = "http://localhost:5173,http://localhost:8080"

    postgres_dsn: SecretStr
    redis_url: SecretStr
    qdrant_url: str

    minio_endpoint: str
    minio_access_key: SecretStr
    minio_secret_key: SecretStr
    minio_bucket: str = "quberi"
    minio_secure: bool = False

    health_timeout_seconds: float = 3.0

    # Bootstrap-only credential used to gate the /api/v1/admin/* surface.
    # When unset (or set to an empty string) every admin endpoint returns 401
    # so a fresh install cannot be reached from the outside by accident.
    platform_admin_token: Optional[SecretStr] = None

    # Master key for at-rest encryption of stored credentials (Fernet,
    # urlsafe base64, 32 raw bytes). Required for any AI-provider or
    # document-source binding that stores an API key. See
    # ``quberi_api.services.security.crypto``.
    encryption_key: Optional[SecretStr] = None
    # Key that signs the short-lived preview links handed to outside tools
    # (``services.external_image_links``). Falls back to ``encryption_key``; with
    # neither set, no links are issued and picture search answers without previews.
    external_link_signing_key: Optional[SecretStr] = None

    # Platform-provided AI defaults. When ``default_deepseek_key`` is
    # set, a matching ``is_platform_default`` ``ai_provider_bindings``
    # row (kind=openai_compat, vendor=deepseek, base_url=api.deepseek.com)
    # is materialised at API boot so every org has a working chat model.
    # ``default_openai_key`` does the same for a second platform vendor
    # (vendor=openai, base_url=api.openai.com) — both can be configured
    # at once; which one an org actually gets by default is controlled by
    # ``default_chat_model`` (whichever vendor serves that model id).
    default_deepseek_key: Optional[SecretStr] = None
    default_openai_key: Optional[SecretStr] = None
    default_chat_model: str = "deepseek-chat"
    default_embedding_model: str = "bge-m3"
    # Image search (SigLIP2, multilingual). On by default; the model is loaded the first
    # time an image is indexed or searched, never at start-up, so an
    # instance nobody sends images to pays nothing for it. The switch is
    # an emergency brake (for example a host that runs short of memory), not
    # a per-organisation or per-space option.
    image_search_enabled: bool = True
    default_image_embedding_model: str = "siglip2-base"
    # Reading text out of pictures (OCR), shared by picture indexing and scanned PDFs.
    # On by default: the local engine sends nothing off the host. The model runs in a
    # child process that is started on first use and stopped when idle, so an instance
    # nobody sends pictures to pays nothing for it. ``ocr_provider`` is a deployment
    # choice like the chat model; only ``local`` exists today.
    image_ocr_enabled: bool = True
    # Pictures that were indexed before their text was read (or whose reading is out of
    # date) are read in the background, this many at a time, once an hour. 0 turns the
    # catch-up off; new pictures are still read when they are uploaded.
    ocr_backfill_batch: int = 30
    # Picture descriptions by a vision model (an organisation turns them on and chooses the
    # model). The most one organisation may have made per day, and how many already-indexed
    # pictures the hourly catch-up queues per run (0 turns the catch-up off; new pictures are
    # still described when they are uploaded). Each description is a paid call.
    vision_daily_limit: int = 200
    # Documents indexed before passages recorded their place (page, slide, sheet row, paragraph,
    # line) are indexed again, this many per hour. 0 turns it off; such documents then show a
    # chunk number until someone re-indexes them.
    reindex_positions_batch: int = 20
    vision_backfill_batch: int = 10
    ocr_provider: str = "local"
    # Longest side the text detector sees. Raised from the model's 960 because small
    # print is lost on a full page; capped at 2000 in code because memory climbs steeply
    # past it (see services/ai/ocr/models.py).
    ocr_det_side: int = 1600
    ocr_min_confidence: float = 0.5
    ocr_max_lines: int = 500
    ocr_threads: int = 2
    # The child is stopped if it holds more than this. Measured on real scanned pages (two
    # CPU cores): the child peaks at about 1.25 GB while reading a page, and the process
    # that owns it stays near 0.17 GB. This limit is the guard against a runaway input, not
    # the working size, so it sits well above the peak. It must still be below the memory the
    # host can really give: a host with less free memory than this (the worker alone uses
    # about 0.5 GB, more once the embedding model is loaded) can run out before the guard
    # acts. Give the worker enough memory, or turn OCR off with QUBERI_IMAGE_OCR_ENABLED=false.
    ocr_max_memory_mb: int = 2048
    ocr_timeout_s: float = 60.0
    ocr_startup_timeout_s: float = 300.0
    ocr_idle_unload_s: float = 300.0

    # Free monthly chat-message quota when an org uses the platform-shared
    # model (no BYOK key). Counted per org, reset on the 1st of each month.
    # BYOK orgs are never metered. ``0`` means unlimited for that tier.
    free_message_quota_personal: int = 100
    free_message_quota_team: int = 0  # 0 = unlimited (enterprise/contract)

    # Guest demo assistant ("ask Quberi about Quberi", no sign-in). It spends
    # the platform's model budget on anonymous traffic, so it is capped twice:
    # per client IP per day, and across all guests per day (the hard ceiling
    # on cost, which holds even if an IP is spoofed). 0 disables a cap.
    guest_daily_ip_limit: int = 10
    guest_daily_global_limit: int = 300
    guest_max_question_chars: int = 500
    guest_max_answer_tokens: int = 600

    # Guest sandbox uploads (anonymous visitors trying the product with a file).
    # Counted separately from questions. Per client IP per day, and across all
    # guests per day (the hard ceiling on storage + embedding cost).
    guest_daily_upload_ip_limit: int = 3
    guest_daily_upload_global_limit: int = 100

    # arq worker → Redis. Falls back to the shared ``redis_url`` so a
    # dev machine with a single Redis instance doesn't need extra config.
    arq_redis_url: Optional[SecretStr] = None

    def resolved_arq_redis_url(self) -> str:
        if self.arq_redis_url is not None:
            return self.arq_redis_url.get_secret_value()
        return self.redis_url.get_secret_value()

    # Feishu (Lark) integration. All optional so the app boots even
    # without credentials configured; the integration surface simply
    # reports "not configured" until they land.
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[SecretStr] = None
    feishu_base_url: str = "https://open.feishu.cn/open-apis"
    # Event subscription (webhook) settings. ``encrypt_key`` decrypts the
    # AES payload when Encrypt Key is set in the console; ``verification_token``
    # authenticates inbound events. Both blank = no webhook configured.
    feishu_encrypt_key: Optional[SecretStr] = None
    feishu_verification_token: Optional[SecretStr] = None
    # Public base URL of this deployment (e.g. the Cloudflare Tunnel
    # hostname). Used to build the OAuth redirect_uri + webhook URL.
    public_base_url: Optional[str] = None

    # Where the browser app is served from, e.g. ``https://ai.quberi.com``.
    # Distinct from ``public_base_url`` (the *API's* public origin, where
    # OAuth providers call back): a full-page sign-in has to send the user
    # back to the web app, not to the API. Optional — when unset we fall
    # back to the first ``allowed_origins`` entry, which in a deployment is
    # already the web app's origin.
    web_base_url: Optional[str] = None

    # Minimal scope requested for Feishu *login* (SSO). Just enough to read
    # the user's basic profile (open_id / union_id / name). The exact scope
    # string must be enabled in the Feishu console.
    feishu_login_scopes: str = "contact:user.base:readonly"

    # Google OAuth (Sign in with Google). Optional, same pattern as Feishu:
    # the app boots fine without credentials, the login/link endpoints just
    # report "not configured" until they're set. Client ID/secret come from
    # a Google Cloud "OAuth client ID" (Web application) — see
    # services/auth/google_login.py for the redirect_uri it must match.
    google_client_id: Optional[str] = None
    google_client_secret: Optional[SecretStr] = None

    # SMS (phone verification-code login). No real provider is wired yet —
    # when ``sms_dev_log_only`` is true the code is only logged (never sent),
    # so phone login works locally without an SMS vendor. Real provider
    # fields (Aliyun/Tencent) land alongside a concrete sender later.
    sms_dev_log_only: bool = True

    # ------------------------------------------------------------------
    # Email (SMTP) — bootstrap for the platform email sender.
    # ------------------------------------------------------------------
    # These seed the ``platform_email_settings`` DB row on first boot when
    # the table is empty; after that the DB row (editable via the admin
    # API) is authoritative. The password is only ever stored sealed in
    # the DB — env holds the bootstrap plaintext for the first write.
    smtp_host: Optional[str] = None
    smtp_port: int = 465
    smtp_username: Optional[str] = None
    smtp_password: Optional[SecretStr] = None
    smtp_from: Optional[str] = None
    smtp_use_ssl: bool = True

    # Secondary SMTP provider (failover). Seeded alongside the primary; the
    # sender tries providers in priority order until one delivers.
    smtp2_host: Optional[str] = None
    smtp2_port: int = 465
    smtp2_username: Optional[str] = None
    smtp2_password: Optional[SecretStr] = None
    smtp2_from: Optional[str] = None
    smtp2_use_ssl: bool = True

    # ------------------------------------------------------------------
    # Auth / session
    # ------------------------------------------------------------------
    # Session token TTL (seconds). The token is a Fernet-sealed payload
    # carried as a bearer credential; TTL is enforced on read.
    session_ttl_seconds: int = 60 * 60 * 24 * 14  # 14 days

    # --- Platform admin (the operator's /admin console) ---
    # The first account is created at boot when none exists. With no password
    # configured, a random one is generated and written to the log ONCE; that
    # account must change it at first sign-in. There is never a built-in
    # default password.
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: Optional[SecretStr] = None
    # How long an admin console session lasts. Shorter than a member session.
    platform_admin_session_ttl_seconds: int = 60 * 60 * 12  # 12 hours
    # --- Traffic page (the /admin console's summary of the reverse proxy's access log) ---
    # Path of the proxy's JSON access log as seen from this container (env
    # ``QUBERI_ACCESS_LOG_PATH``). Empty = the Traffic page is off. It is a fixed server
    # setting: no request can name a path.
    access_log_path: str = ""
    # Comma-separated host names this deployment serves (env ``QUBERI_TRAFFIC_SITE_HOSTS``).
    # A request for any other Host (a bare address, a scanner's made-up domain) is counted as
    # "direct or unknown host". Empty = only address-literal and empty hosts count as such.
    traffic_site_hosts: str = ""
    # Verification code lifetime + per-email send cooldown (seconds).
    verification_code_ttl_seconds: int = 60 * 10  # 10 minutes
    verification_code_cooldown_seconds: int = 60  # 1 send / minute / email
    verification_code_max_attempts: int = 5

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def traffic_hosts(self) -> list[str]:
        return [h.strip().lower() for h in self.traffic_site_hosts.split(",") if h.strip()]

    @property
    def web_origin(self) -> Optional[str]:
        """Origin of the web app, for redirects back from OAuth providers.

        Always comes from server configuration, never from a request, so it
        cannot be turned into an open redirect.
        """

        if self.web_base_url and self.web_base_url.strip():
            return self.web_base_url.strip().rstrip("/")
        origins = self.cors_origins
        return origins[0].rstrip("/") if origins else None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
