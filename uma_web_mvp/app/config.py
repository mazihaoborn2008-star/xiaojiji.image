from functools import lru_cache
import base64
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_windows_path(raw: str) -> str:
    """Resolve a Windows-style path (C:/... or C:\\) to a valid path under WSL if needed."""
    if not raw:
        return raw
    p = Path(raw)
    if p.is_absolute():
        return str(p.resolve())
    m = re.match(r'^([A-Za-z]):[\\/](.*)', raw)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace('\\', '/')
        resolved = Path(f'/mnt/{drive}/{rest}')
        if resolved.exists() or resolved.parent.exists():
            return str(resolved)
    return str(Path(raw).resolve())


def _is_local_origin(origin: str) -> bool:
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost"}


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_ENV = os.environ.get("APP_ENV", "").strip().lower()
if _APP_ENV == "local":
    _candidate = _PROJECT_ROOT / ".env.local"
    if not _candidate.is_file():
        raise RuntimeError("APP_ENV=local requires .env.local; refusing to fall back to production .env")
    _ENV_PATH = _candidate
else:
    _ENV_PATH = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_PATH), env_file_encoding="utf-8", extra="ignore")

    app_name: str = "UMA Web MVP"
    app_env: str = Field(default=_APP_ENV or "production", alias="APP_ENV")
    app_origin: str = "http://127.0.0.1:8000"
    host: str = "127.0.0.1"
    port: int = 8000
    redis_enabled: bool = False
    redis_url: str = "redis://127.0.0.1:6379/0"
    adult_content_filter_enabled: bool = False

    raw_bot_dir: str = Field(default=r"E:\discord-BOT", alias="BOT_DIR")
    raw_balance_db: str = Field(default=r"E:\discord-BOT\balance.db", alias="BALANCE_DB")
    raw_bot_output_dir: str = Field(default=r"E:\discord-BOT\output", alias="BOT_OUTPUT_DIR")
    raw_input_image_dir: str = Field(default=r"E:\discord-BOT\input_images", alias="INPUT_IMAGE_DIR")
    raw_payment_qr_path: str = Field(default=r"E:\discord-BOT\payment_qr.png", alias="PAYMENT_QR_PATH")

    @property
    def bot_dir(self) -> Path:
        return Path(_resolve_windows_path(self.raw_bot_dir))

    @property
    def balance_db(self) -> Path:
        return Path(_resolve_windows_path(self.raw_balance_db))

    @property
    def bot_output_dir(self) -> Path:
        return Path(_resolve_windows_path(self.raw_bot_output_dir))

    @property
    def input_image_dir(self) -> Path:
        return Path(_resolve_windows_path(self.raw_input_image_dir))

    @property
    def payment_qr_path(self) -> Path:
        return Path(_resolve_windows_path(self.raw_payment_qr_path))

    def resolve_output_path(self, stored_path: str) -> Path:
        if not stored_path:
            raise ValueError("存储路径为空")
        normalized = Path(stored_path)
        resolved = normalized.resolve() if normalized.is_absolute() else (self.bot_output_dir / normalized).resolve()
        bot_abs = self.bot_output_dir.resolve()
        allowed_roots = [bot_abs]
        if self.is_local_env() and self.mock_worker_enabled:
            allowed_roots.append(self.mock_output_path.resolve())
        if not any(root in resolved.parents or resolved == root for root in allowed_roots):
            raise ValueError("图片路径无效")
        if not resolved.is_file():
            raise FileNotFoundError("图片文件不存在")
        return resolved

    owner_user_id: str = "1438579357009580052"
    owner_free_generation: bool = True
    price_fen_per_image: int = 1
    max_queue_size: int = 20
    max_active_tasks_per_user: int = 10
    generation_submit_user_limit: int = 20
    generation_submit_window_seconds: int = 60
    cancel_submit_user_limit: int = 60
    cancel_submit_window_seconds: int = 60
    max_input_image_bytes: int = 12 * 1024 * 1024
    estimated_generation_seconds: int = 18
    estimated_agent_seconds: int = 51
    generation_worker_count: int = 1

    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = "http://127.0.0.1:8000/auth/discord/callback"
    admin_discord_user_id: str = "1438579357009580052"

    session_secret: str = ""
    session_max_age_seconds: int = 604800
    jwt_secret: str = ""
    cookie_secure: bool = False
    session_days: int = 7
    max_active_sessions_per_account: int = 10

    dev_auth_bypass: bool = False
    dev_user_id: str = "1438579357009580052"
    dev_username: str = "Local Developer"

    agent_enabled: bool = False
    agent_provider: str = "openai"
    agent_base_url: str = "http://127.0.0.1:11434/v1"
    agent_api_key: str = ""
    agent_model: str = "qwen2.5:32b"
    agent_timeout_seconds: int = 45
    agent_max_concurrency: int = 1
    agent_keep_alive: str = "0"

    smart_agent_enabled: bool = False
    smart_agent_v2_enabled: bool = False
    smart_agent_legacy_enabled: bool = True
    smart_agent_cost_credits: int = 5
    agent_surcharge_credits: int = 1
    smart_agent_rate_window_seconds: int = 600
    smart_agent_chat_user_limit: int = 30
    smart_agent_chat_ip_limit: int = 100
    smart_agent_chat_emergency_ip_limit: int = 300
    smart_agent_chat_emergency_window_seconds: int = 600
    smart_agent_generate_user_limit: int = 10
    smart_agent_generate_ip_limit: int = 40
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: int = 90
    deepseek_chat_timeout_seconds: int = 180
    deepseek_chat_max_output_tokens: int = 4096
    deepseek_max_retries: int = 2
    fast_translator_enabled: bool = False
    fast_translator_cost_credits: int = 2
    fast_translation_claim_ttl_seconds: int = 180
    fast_translation_max_attempts: int = 2
    fast_translation_recovery_interval_seconds: int = 30
    ai_support_enabled: bool = False
    ai_support_max_history: int = 20
    ai_support_rate_limit_per_minute: int = 10
    mock_worker_enabled: bool = False
    mock_worker_poll_seconds: int = 2
    mock_generation_seconds: int = 3
    mock_output_dir: str = "test_data/mock_output"
    smart_agent_prompt_xlsx_path: str = r"C:\Users\Administrator\Desktop\agent.xlsx"
    comfyui_workflow_dir: str = r"D:\ComfyUI-aki-v3\ComfyUI\user\default\workflows"

    @property
    def smart_agent_prompt_xlsx(self) -> Path:
        return Path(_resolve_windows_path(self.smart_agent_prompt_xlsx_path))

    @property
    def comfyui_workflow_directory(self) -> Path:
        return Path(_resolve_windows_path(self.comfyui_workflow_dir))

    @property
    def mock_output_path(self) -> Path:
        return Path(_resolve_windows_path(self.mock_output_dir))

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    @property
    def loaded_env_path(self) -> Path:
        return _ENV_PATH

    def is_local_env(self) -> bool:
        return str(self.app_env or "").strip().lower() == "local"

    def validate_local_isolation(self) -> None:
        if not self.is_local_env():
            return
        test_data_root = (_PROJECT_ROOT / "test_data").resolve()
        db_path = self.balance_db.resolve()
        output_path = self.bot_output_dir.resolve()
        mock_path = self.mock_output_path.resolve()
        if test_data_root not in db_path.parents and db_path != test_data_root:
            raise RuntimeError("APP_ENV=local requires BALANCE_DB under test_data")
        if db_path.name.lower() == "balance.db":
            raise RuntimeError("APP_ENV=local refuses production balance.db")
        for label, path in (("BOT_OUTPUT_DIR", output_path), ("MOCK_OUTPUT_DIR", mock_path)):
            if test_data_root not in path.parents and path != test_data_root:
                raise RuntimeError(f"APP_ENV=local requires {label} under test_data")
        if str(self.redis_url or "").endswith("/0") and self.redis_enabled:
            raise RuntimeError("APP_ENV=local refuses Redis DB 0")

    email_auth_enabled: bool = False
    email_otp_secret: str = ""
    email_identity_secret: str = ""
    email_encryption_key: str = ""
    email_otp_expire_seconds: int = 300
    email_otp_resend_seconds: int = 10
    email_otp_max_attempts: int = 5
    email_otp_max_sends_per_10_min: int = 5
    email_otp_max_per_hour: int = 10
    email_otp_max_per_ip_per_hour: int = 20
    password_login_max_failed_attempts: int = 5
    password_login_lock_seconds: int = 900
    password_login_max_attempts_per_email_15_min: int = 10
    password_login_max_attempts_per_ip_15_min: int = 30
    password_reset_token_seconds: int = 600

    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "小击击生图"
    smtp_from_email: str = ""
    smtp_connect_timeout: int = 10
    smtp_send_timeout: int = 15
    email_send_max_concurrency: int = 2
    email_send_timeout_seconds: int = 20

    asb_transfer_enabled: bool = False
    asb_bank_name: str = "ASB"
    asb_payee_name: str = ""
    asb_account_number: str = ""
    wechat_payment_link: str = Field(default="", alias="WECHAT_PAYMENT_LINK")
    referral_campaign_enabled: bool = False
    referral_campaign_version: str = "referral_campaign_v1"
    referral_invitee_bonus_credits: int = 10
    referral_inviter_bonus_credits: int = 20
    topup_wechat_expires_hours: int = 24
    topup_asb_expires_days: int = 7
    topup_paid_review_days: int = 7
    image_refund_window_hours: int = 24
    mimo_image_review_enabled: bool = False
    mimo_image_review_model: str = "mimo-v2.5"
    mimo_image_review_base_url: str = ""
    mimo_image_review_api_key: str = ""
    mimo_image_review_timeout_seconds: int = 90
    mimo_image_review_min_confidence: float = 0.85
    mimo_image_review_min_severity: int = 85

    @field_validator("app_origin")
    @classmethod
    def strip_origin(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("fast_translation_claim_ttl_seconds")
    @classmethod
    def validate_claim_ttl(cls, value: int) -> int:
        return max(30, min(3600, value))

    @field_validator("fast_translation_max_attempts")
    @classmethod
    def validate_max_attempts(cls, value: int) -> int:
        return max(1, min(5, value))

    @field_validator("fast_translation_recovery_interval_seconds")
    @classmethod
    def validate_recovery_interval(cls, value: int) -> int:
        return max(5, min(300, value))

    @property
    def smtp_from_address(self) -> str:
        return self.smtp_from_email or self.smtp_username

    @property
    def email_identity_secret_value(self) -> str:
        return self.email_identity_secret or self.email_otp_secret

    def email_encryption_key_bytes(self) -> bytes:
        raw = self.email_encryption_key.strip()
        if not raw:
            raise ValueError("EMAIL_ENCRYPTION_KEY 缺失")
        try:
            key = base64.urlsafe_b64decode(raw.encode("ascii"))
        except Exception as exc:
            raise ValueError("EMAIL_ENCRYPTION_KEY 格式无效") from exc
        if len(key) != 32:
            raise ValueError("EMAIL_ENCRYPTION_KEY 必须解码为 32 字节")
        return key

    def has_valid_email_encryption_key(self) -> bool:
        try:
            self.email_encryption_key_bytes()
            return True
        except ValueError:
            return False

    def is_email_auth_available(self) -> bool:
        return (
            self.email_auth_enabled
            and bool(self.email_otp_secret)
            and len(self.email_otp_secret) >= 32
            and bool(self.email_identity_secret_value)
            and len(self.email_identity_secret_value) >= 32
            and self.has_valid_email_encryption_key()
            and bool(self.smtp_username)
            and bool(self.smtp_password)
        )

    def validate_runtime(self) -> None:
        if "trycloudflare.com" in self.app_origin.lower():
            raise RuntimeError("APP_ORIGIN 不允许使用 trycloudflare.com")

        origin = urlparse(self.app_origin)
        if origin.scheme not in {"http", "https"} or not origin.netloc:
            raise RuntimeError("APP_ORIGIN 必须是完整 URL")
        if self.cookie_secure and origin.scheme != "https":
            raise RuntimeError("COOKIE_SECURE=true 时 APP_ORIGIN 必须使用 HTTPS")

        if self.dev_auth_bypass:
            if not _is_local_origin(self.app_origin):
                raise RuntimeError("DEV_AUTH_BYPASS=true 只允许 APP_ORIGIN 为 localhost 或 127.0.0.1")
            return

        missing = []
        if not self.discord_client_id:
            missing.append("DISCORD_CLIENT_ID")
        if not self.discord_client_secret:
            missing.append("DISCORD_CLIENT_SECRET")
        if not self.admin_discord_user_id or not self.admin_discord_user_id.isdigit():
            missing.append("ADMIN_DISCORD_USER_ID")
        if not self.session_secret or len(self.session_secret) < 32:
            missing.append("SESSION_SECRET(至少32字符)")

        redirect = urlparse(self.discord_redirect_uri)
        if redirect.scheme not in {"http", "https"} or not redirect.netloc:
            missing.append("DISCORD_REDIRECT_URI(完整URL)")
        elif redirect.path != "/auth/discord/callback":
            missing.append("DISCORD_REDIRECT_URI(/auth/discord/callback)")
        elif redirect.scheme != "https" and redirect.hostname not in {"127.0.0.1", "localhost"}:
            missing.append("DISCORD_REDIRECT_URI(非本机必须HTTPS)")

        if self.email_auth_enabled:
            if not self.email_otp_secret or len(self.email_otp_secret) < 32:
                missing.append("EMAIL_OTP_SECRET(至少32字符)")
            if not self.email_identity_secret_value or len(self.email_identity_secret_value) < 32:
                missing.append("EMAIL_IDENTITY_SECRET或EMAIL_OTP_SECRET(至少32字符)")
            if not self.has_valid_email_encryption_key():
                missing.append("EMAIL_ENCRYPTION_KEY(base64编码32字节)")
            if not self.smtp_username:
                missing.append("SMTP_USERNAME")
            if not self.smtp_password:
                missing.append("SMTP_PASSWORD")

        if missing:
            raise RuntimeError("缺少或无效的安全配置：" + ", ".join(missing))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
