import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import string
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .catalog import CONTROL_CHARACTER_KEYS, STYLE_BY_KEY
from .config import Settings

MIN_ACTIVE_REMAINING_SECONDS = 3
SMART_AGENT_CHARGE_REASON = "smart_agent_charge"
MAX_CHARACTER_KEY_LENGTH = 1024


def _validate_character_key(character_key: str | None) -> str:
    """Validate character_key for database storage without truncation.

    Raises ValueError if the value exceeds MAX_CHARACTER_KEY_LENGTH.
    Returns the validated string (or empty string for None/empty).
    """
    key = str(character_key or "").strip()
    if not key:
        return ""
    if len(key) > MAX_CHARACTER_KEY_LENGTH:
        raise ValueError(
            f"character_key exceeds maximum length ({MAX_CHARACTER_KEY_LENGTH})"
        )
    return key
SMART_AGENT_REFUND_REASON = "smart_agent_refund"
SMART_AGENT_UNINTENDED_REFUND_REASON = "smart_agent_unintended_generation_refund"
SEVERE_DEFORMATION_REFUND_REASON = "severe_deformation_auto_refund"
SMART_AGENT_STATUS = "smart_planning"
ACTIVE_STATUSES_SQL = "'smart_planning','queued','translating','processing'"

ALLOWED_MODES = {"txt2img", "img2img", "controlnet"}
ALLOWED_CONTROL_TYPES = {"depth", "pose"}


def connect(settings: Settings) -> sqlite3.Connection:
    settings.balance_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.balance_db, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema(settings: Settings) -> None:
    conn = connect(settings)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            balance_fen INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT PRIMARY KEY,
            style_key TEXT,
            lora_weight REAL NOT NULL DEFAULT 1.0,
            last_width INTEGER,
            last_height INTEGER,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS generation_tasks (
            job_code TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            channel_id TEXT,
            prompt TEXT NOT NULL,
            original_prompt TEXT,
            effective_prompt TEXT,
            use_agent INTEGER NOT NULL DEFAULT 0,
            agent_mode TEXT NOT NULL DEFAULT 'normal',
            smart_agent_request TEXT,
            smart_agent_plan_json TEXT,
            smart_agent_error TEXT,
            workflow_key TEXT,
            loras_json TEXT,
            prompt_source TEXT,
            client_request_id TEXT,
            style_key TEXT NOT NULL,
            lora_weight REAL NOT NULL DEFAULT 1.0,
            width INTEGER NOT NULL DEFAULT 1024,
            height INTEGER NOT NULL DEFAULT 1536,
            generation_mode TEXT NOT NULL DEFAULT 'txt2img',
            input_image_path TEXT,
            denoise REAL NOT NULL DEFAULT 0.5,
            control_type TEXT NOT NULL DEFAULT 'depth',
            control_character TEXT NOT NULL DEFAULT 'prompt',
            auto_tagger INTEGER NOT NULL DEFAULT 0,
            charged_fen INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at INTEGER NOT NULL,
            active_started_at INTEGER,
            translating_started_at INTEGER,
            started_at INTEGER,
            finished_at INTEGER,
            error TEXT,
            error_code TEXT,
            comfy_prompt_id TEXT,
            comfy_queue_number INTEGER,
            comfy_submitted_at INTEGER,
            comfy_completed_at INTEGER,
            translation_mode TEXT NOT NULL DEFAULT 'none',
            fast_translation_request_code TEXT NOT NULL DEFAULT '',
            request_fingerprint TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'discord'
        );
        CREATE TABLE IF NOT EXISTS balance_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount_fen INTEGER NOT NULL,
            reason TEXT NOT NULL,
            order_code TEXT,
            operator_id TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS generation_outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_code TEXT NOT NULL,
            label TEXT,
            file_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(job_code, file_path)
        );
        CREATE INDEX IF NOT EXISTS idx_generation_status_time ON generation_tasks(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_generation_user_time ON generation_tasks(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_outputs_job ON generation_outputs(job_code, id);

        CREATE TABLE IF NOT EXISTS image_refund_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_code TEXT UNIQUE NOT NULL,
            account_id TEXT NOT NULL,
            legacy_user_id INTEGER,
            job_code TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            reviewer_model TEXT,
            reviewer_version TEXT,
            output_ids_json TEXT NOT NULL,
            user_note TEXT,
            original_request_snapshot TEXT,
            final_prompt_snapshot TEXT,
            charged_credits INTEGER NOT NULL,
            decision TEXT,
            severity_score INTEGER,
            confidence REAL,
            reason_codes_json TEXT,
            public_reason TEXT,
            review_result_json TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            claimed_at INTEGER,
            reviewed_at INTEGER,
            refunded_at INTEGER,
            refund_ledger_id INTEGER,
            updated_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_image_refunds_job ON image_refund_reviews(job_code);
        CREATE INDEX IF NOT EXISTS idx_image_refunds_account ON image_refund_reviews(account_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_image_refunds_status ON image_refund_reviews(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_image_refunds_created ON image_refund_reviews(created_at);

        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_user_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            display_username TEXT,
            display_username_normalized TEXT,
            display_username_updated_at INTEGER,
            avatar_url TEXT,
            avatar_hash TEXT,
            welcome_credits_granted_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_login_at INTEGER,
            UNIQUE(provider, provider_user_id)
        );
        CREATE TABLE IF NOT EXISTS account_legacy_bindings (
            account_id TEXT PRIMARY KEY,
            legacy_user_id TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_accounts_provider_subject ON accounts(provider, provider_user_id);
        CREATE TABLE IF NOT EXISTS referral_codes (
            account_id TEXT PRIMARY KEY,
            referral_code TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_referral_codes_code ON referral_codes(referral_code);
        CREATE TABLE IF NOT EXISTS referral_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_account_id TEXT NOT NULL,
            invitee_account_id TEXT NOT NULL UNIQUE,
            referral_code_used TEXT NOT NULL,
            inviter_reward_credits INTEGER NOT NULL,
            invitee_reward_credits INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            rewarded_at INTEGER,
            inviter_ledger_id INTEGER,
            invitee_ledger_id INTEGER,
            FOREIGN KEY(inviter_account_id) REFERENCES accounts(id),
            FOREIGN KEY(invitee_account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_referral_relationships_code ON referral_relationships(referral_code_used);
        CREATE INDEX IF NOT EXISTS idx_referral_relationships_inviter ON referral_relationships(inviter_account_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_referral_relationships_invitee ON referral_relationships(invitee_account_id);
        CREATE TABLE IF NOT EXISTS referral_campaign_seen (
            account_id TEXT NOT NULL,
            campaign_version TEXT NOT NULL,
            seen_at INTEGER NOT NULL,
            PRIMARY KEY(account_id, campaign_version),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE TABLE IF NOT EXISTS account_sessions (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            session_id_hash TEXT NOT NULL UNIQUE,
            csrf_token_hash TEXT NOT NULL,
            provider TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            user_agent_hash TEXT,
            ip_hash TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_account_sessions_account_active
            ON account_sessions(account_id, revoked_at, expires_at, last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_account_sessions_hash ON account_sessions(session_id_hash);

        CREATE TABLE IF NOT EXISTS web_users (
            id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            last_login_at INTEGER,
            is_disabled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS auth_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_subject TEXT NOT NULL,
            email TEXT,
            email_verified INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            last_login_at INTEGER,
            UNIQUE(provider, provider_subject),
            FOREIGN KEY(user_id) REFERENCES web_users(id)
        );
        CREATE TABLE IF NOT EXISTS web_user_bindings (
            web_user_id TEXT NOT NULL,
            legacy_user_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(web_user_id),
            UNIQUE(legacy_user_id)
        );
        CREATE TABLE IF NOT EXISTS email_login_codes_secure (
            id TEXT PRIMARY KEY,
            email_identity TEXT NOT NULL,
            purpose TEXT NOT NULL DEFAULT 'email_login',
            code_digest TEXT NOT NULL,
            requested_ip_hash TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER,
            failed_attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_email_codes_identity_time ON email_login_codes_secure(email_identity, created_at);
        CREATE INDEX IF NOT EXISTS idx_email_codes_ip_time ON email_login_codes_secure(requested_ip_hash, created_at);
        CREATE TABLE IF NOT EXISTS oauth_login_states (
            state_hash TEXT PRIMARY KEY,
            browser_nonce_hash TEXT NOT NULL,
            provider TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER,
            redirect_after_login TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_oauth_states_provider_time ON oauth_login_states(provider, created_at);
        CREATE TABLE IF NOT EXISTS account_email_identities (
            account_id TEXT PRIMARY KEY,
            email_hmac TEXT NOT NULL UNIQUE,
            email_ciphertext TEXT NOT NULL,
            email_nonce TEXT NOT NULL,
            email_masked TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_account_email_hmac ON account_email_identities(email_hmac);
        CREATE TABLE IF NOT EXISTS email_password_credentials (
            account_id TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            password_algo TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            password_changed_at INTEGER NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until INTEGER,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_email_password_locked
            ON email_password_credentials(locked_until);
        CREATE TABLE IF NOT EXISTS email_password_reset_tokens (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            email_hmac TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            requested_ip_hash TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_password_reset_account_time
            ON email_password_reset_tokens(account_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_password_reset_hash
            ON email_password_reset_tokens(token_hash);
        CREATE TABLE IF NOT EXISTS email_password_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_hmac TEXT,
            ip_hash TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_password_attempts_email_time
            ON email_password_login_attempts(email_hmac, created_at);
        CREATE INDEX IF NOT EXISTS idx_password_attempts_ip_time
            ON email_password_login_attempts(ip_hash, created_at);
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_account_id TEXT NOT NULL,
            target_account_id TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_admin_audit_time ON admin_audit_log(created_at);
        CREATE TABLE IF NOT EXISTS feedback_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            legacy_user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            user_display TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            sent_at INTEGER,
            sent_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_status_time ON feedback_reports(status, created_at);
        CREATE TABLE IF NOT EXISTS support_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_code TEXT UNIQUE NOT NULL,
            account_id TEXT NOT NULL,
            legacy_user_id TEXT,
            category TEXT NOT NULL DEFAULT 'general',
            subject TEXT,
            related_feedback_id INTEGER,
            related_topup_code TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'normal',
            created_by_admin_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            closed_at INTEGER,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_support_threads_account ON support_threads(account_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_support_threads_updated ON support_threads(updated_at);
        CREATE INDEX IF NOT EXISTS idx_support_threads_status ON support_threads(status, updated_at);
        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            sender_type TEXT NOT NULL,
            sender_account_id TEXT,
            sender_admin_id TEXT,
            body TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            read_by_user_at INTEGER,
            read_by_admin_at INTEGER,
            FOREIGN KEY(thread_id) REFERENCES support_threads(id)
        );
        CREATE INDEX IF NOT EXISTS idx_support_messages_thread ON support_messages(thread_id, id);
        CREATE INDEX IF NOT EXISTS idx_support_messages_user_unread
            ON support_messages(thread_id, sender_type, read_by_user_at);
        CREATE INDEX IF NOT EXISTS idx_support_messages_admin_unread
            ON support_messages(thread_id, sender_type, read_by_admin_at);
        CREATE TABLE IF NOT EXISTS legacy_user_sequence (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_negative_id INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO legacy_user_sequence(id, next_negative_id) VALUES (1, -1);

        CREATE TABLE IF NOT EXISTS smart_agent_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_code TEXT UNIQUE NOT NULL,
            account_id INTEGER NOT NULL,
            legacy_user_id TEXT NOT NULL,
            title TEXT DEFAULT '',
            memory_summary TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sac_user ON smart_agent_conversations(legacy_user_id, updated_at);

        CREATE TABLE IF NOT EXISTS smart_agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            safe_content TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            intent TEXT NOT NULL DEFAULT '',
            client_request_id TEXT DEFAULT '',
            processing_started_at INTEGER,
            processed_at INTEGER,
            error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sam_conv ON smart_agent_messages(conversation_id, id);

        CREATE TABLE IF NOT EXISTS smart_agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            job_code TEXT DEFAULT '',
            event_type TEXT NOT NULL,
            public_message TEXT NOT NULL,
            private_detail TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sae_conv_time ON smart_agent_events(conversation_id, id);

        CREATE TABLE IF NOT EXISTS smart_agent_prompt_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL UNIQUE,
            message_id INTEGER,
            prompt_draft TEXT NOT NULL,
            prompt_version INTEGER NOT NULL DEFAULT 1,
            resolved_character_key TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'prompt_ready',
            ready_at INTEGER,
            generation_job_code TEXT DEFAULT '',
            plan_json TEXT NOT NULL,
            request_text TEXT NOT NULL,
            workflow_key TEXT NOT NULL,
            loras_json TEXT DEFAULT '[]',
            prompt_source TEXT DEFAULT '',
            workflow_source TEXT DEFAULT '',
            fallback_level TEXT DEFAULT '',
            width INTEGER NOT NULL DEFAULT 1024,
            height INTEGER NOT NULL DEFAULT 1024,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES smart_agent_conversations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_smart_prompt_status ON smart_agent_prompt_drafts(status, updated_at);

        CREATE TABLE IF NOT EXISTS translation_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_code TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            client_request_id TEXT,
            translation_mode TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            character_match_source TEXT NOT NULL DEFAULT 'none',
            character_keys_json TEXT NOT NULL DEFAULT '[]',
            original_text TEXT NOT NULL,
            refined_prompt TEXT,
            charged_credits INTEGER NOT NULL DEFAULT 0,
            ledger_id INTEGER,
            status TEXT NOT NULL,
            error_code TEXT DEFAULT '',
            started_at INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            generation_job_code TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            finished_at INTEGER
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_translation_user_client_request
            ON translation_requests(user_id, client_request_id)
            WHERE client_request_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_translation_user_time
            ON translation_requests(user_id, created_at);

        CREATE TABLE IF NOT EXISTS ai_support_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_code TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ai_support_conversations_user
            ON ai_support_conversations(user_id, updated_at);
        CREATE TABLE IF NOT EXISTS ai_support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            safe_content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'done',
            referenced_job_code TEXT DEFAULT '',
            FOREIGN KEY(conversation_id) REFERENCES ai_support_conversations(id)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_support_messages_conversation
            ON ai_support_messages(conversation_id, created_at, id);
        """)
        refund_columns = {row[1] for row in conn.execute("PRAGMA table_info(image_refund_reviews)").fetchall()}
        if "manual_review_requested_at" not in refund_columns:
            conn.execute("ALTER TABLE image_refund_reviews ADD COLUMN manual_review_requested_at INTEGER")
        if "manual_review_decided_at" not in refund_columns:
            conn.execute("ALTER TABLE image_refund_reviews ADD COLUMN manual_review_decided_at INTEGER")
        if "manual_review_decision" not in refund_columns:
            conn.execute("ALTER TABLE image_refund_reviews ADD COLUMN manual_review_decision TEXT DEFAULT ''")
        if "manual_review_admin_id" not in refund_columns:
            conn.execute("ALTER TABLE image_refund_reviews ADD COLUMN manual_review_admin_id TEXT DEFAULT ''")
        if "manual_review_reason" not in refund_columns:
            conn.execute("ALTER TABLE image_refund_reviews ADD COLUMN manual_review_reason TEXT DEFAULT ''")
        if "manual_review_attempts" not in refund_columns:
            conn.execute("ALTER TABLE image_refund_reviews ADD COLUMN manual_review_attempts INTEGER NOT NULL DEFAULT 0")
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(smart_agent_messages)").fetchall()}
        if "status" not in message_columns:
            conn.execute("ALTER TABLE smart_agent_messages ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
        if "intent" not in message_columns:
            conn.execute("ALTER TABLE smart_agent_messages ADD COLUMN intent TEXT NOT NULL DEFAULT ''")
        if "client_request_id" not in message_columns:
            conn.execute("ALTER TABLE smart_agent_messages ADD COLUMN client_request_id TEXT DEFAULT ''")
        if "processing_started_at" not in message_columns:
            conn.execute("ALTER TABLE smart_agent_messages ADD COLUMN processing_started_at INTEGER")
        if "processed_at" not in message_columns:
            conn.execute("ALTER TABLE smart_agent_messages ADD COLUMN processed_at INTEGER")
        if "error" not in message_columns:
            conn.execute("ALTER TABLE smart_agent_messages ADD COLUMN error TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sam_queue ON smart_agent_messages(status, created_at, id)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()}
        if "source" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'discord'")
        if "original_prompt" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN original_prompt TEXT")
        if "effective_prompt" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN effective_prompt TEXT")
        if "use_agent" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN use_agent INTEGER NOT NULL DEFAULT 0")
        if "agent_mode" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'normal'")
        if "smart_agent_request" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN smart_agent_request TEXT")
        if "smart_agent_plan_json" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN smart_agent_plan_json TEXT")
        if "smart_agent_error" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN smart_agent_error TEXT")
        if "workflow_key" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN workflow_key TEXT")
        if "loras_json" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN loras_json TEXT")
        if "prompt_source" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN prompt_source TEXT")
        if "conversation_code" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN conversation_code TEXT")
        if "character_key" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN character_key TEXT")
        if "workflow_source" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN workflow_source TEXT")
        if "fallback_level" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN fallback_level TEXT")
        if "client_request_id" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN client_request_id TEXT")
        if "translating_started_at" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN translating_started_at INTEGER")
        if "active_started_at" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN active_started_at INTEGER")
        if "error_code" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN error_code TEXT")
        if "comfy_prompt_id" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN comfy_prompt_id TEXT")
        if "comfy_queue_number" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN comfy_queue_number INTEGER")
        if "comfy_submitted_at" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN comfy_submitted_at INTEGER")
        if "comfy_completed_at" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN comfy_completed_at INTEGER")
        if "mock_result" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN mock_result TEXT")
        if "translation_mode" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN translation_mode TEXT NOT NULL DEFAULT 'none'")
        if "fast_translation_request_code" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN fast_translation_request_code TEXT NOT NULL DEFAULT ''")
        if "request_fingerprint" not in columns:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE generation_tasks SET original_prompt = prompt WHERE original_prompt IS NULL")
        conn.execute("UPDATE generation_tasks SET effective_prompt = prompt WHERE effective_prompt IS NULL AND use_agent = 0")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_user_client_request "
            "ON generation_tasks(user_id, client_request_id) WHERE client_request_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_generation_fast_translation_request "
            "ON generation_tasks(fast_translation_request_code) WHERE fast_translation_request_code <> ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_request_fingerprint "
            "ON generation_tasks(user_id, request_fingerprint) WHERE request_fingerprint <> ''"
        )
        account_columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "last_login_at" not in account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_login_at INTEGER")
        if "welcome_credits_granted_at" not in account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN welcome_credits_granted_at INTEGER")
        if "display_username" not in account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN display_username TEXT")
        if "display_username_normalized" not in account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN display_username_normalized TEXT")
        if "display_username_updated_at" not in account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN display_username_updated_at INTEGER")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_display_username_normalized "
            "ON accounts(display_username_normalized) WHERE display_username_normalized IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_welcome_bonus_once "
            "ON balance_ledger(user_id) WHERE reason='welcome_bonus'"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_referral_invitee_once "
            "ON balance_ledger(user_id, order_code) WHERE reason='referral_invitee_bonus'"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_referral_inviter_once "
            "ON balance_ledger(user_id, order_code) WHERE reason='referral_inviter_bonus'"
        )
        code_columns = {row[1] for row in conn.execute("PRAGMA table_info(email_login_codes_secure)").fetchall()}
        if "purpose" not in code_columns:
            conn.execute("ALTER TABLE email_login_codes_secure ADD COLUMN purpose TEXT NOT NULL DEFAULT 'email_login'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_codes_identity_purpose_time "
            "ON email_login_codes_secure(email_identity, purpose, created_at)"
        )
        # ── Disambiguation columns for smart_agent_conversations ──
        conv_columns = {row[1] for row in conn.execute("PRAGMA table_info(smart_agent_conversations)").fetchall()}
        if "pending_character_term" not in conv_columns:
            conn.execute("ALTER TABLE smart_agent_conversations ADD COLUMN pending_character_term TEXT DEFAULT ''")
        if "pending_character_candidates" not in conv_columns:
            conn.execute("ALTER TABLE smart_agent_conversations ADD COLUMN pending_character_candidates TEXT DEFAULT ''")
        if "pending_original_request" not in conv_columns:
            conn.execute("ALTER TABLE smart_agent_conversations ADD COLUMN pending_original_request TEXT DEFAULT ''")
        if "pending_constraints" not in conv_columns:
            conn.execute("ALTER TABLE smart_agent_conversations ADD COLUMN pending_constraints TEXT DEFAULT ''")
        if "pending_disambiguation_at" not in conv_columns:
            conn.execute("ALTER TABLE smart_agent_conversations ADD COLUMN pending_disambiguation_at INTEGER")
        if "pending_disambiguation_json" not in conv_columns:
            conn.execute("ALTER TABLE smart_agent_conversations ADD COLUMN pending_disambiguation_json TEXT DEFAULT ''")
        # ── Structured draft JSON for smart_agent_prompt_drafts ──
        draft_columns = {row[1] for row in conn.execute("PRAGMA table_info(smart_agent_prompt_drafts)").fetchall()}
        if "structured_draft_json" not in draft_columns:
            conn.execute("ALTER TABLE smart_agent_prompt_drafts ADD COLUMN structured_draft_json TEXT DEFAULT ''")

        # ── Request fingerprint for fast translation idempotency ──
        tr_columns = {row[1] for row in conn.execute("PRAGMA table_info(translation_requests)").fetchall()}
        if "request_fingerprint" not in tr_columns:
            conn.execute("ALTER TABLE translation_requests ADD COLUMN request_fingerprint TEXT DEFAULT ''")
        if "started_at" not in tr_columns:
            conn.execute("ALTER TABLE translation_requests ADD COLUMN started_at INTEGER")
        if "attempt_count" not in tr_columns:
            conn.execute("ALTER TABLE translation_requests ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
        if "generation_job_code" not in tr_columns:
            conn.execute("ALTER TABLE translation_requests ADD COLUMN generation_job_code TEXT DEFAULT ''")

        # ── Idempotency table for smart agent task creation ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_agent_request_idempotency (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                client_request_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL DEFAULT '',
                task_id TEXT DEFAULT '',
                request_status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, client_request_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_user "
            "ON smart_agent_request_idempotency(user_id, created_at)"
        )

        # ── Billing events table for refund idempotency ──
        conn.execute("""
            CREATE TABLE IF NOT EXISTS smart_agent_billing_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                event_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                metadata TEXT DEFAULT '',
                UNIQUE(event_key)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_events_user "
            "ON smart_agent_billing_events(user_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_events_task "
            "ON smart_agent_billing_events(task_id)"
        )

        conn.commit()
    finally:
        conn.close()


def validate_resolution(width: int, height: int) -> tuple[int, int]:
    width, height = int(width), int(height)
    if width < 512 or height < 512:
        raise ValueError("宽和高不能低于 512")
    if width > 2048 or height > 2048:
        raise ValueError("宽和高不能超过 2048")
    if width * height > 1536 * 1536:
        raise ValueError("总像素过大，容易爆显存")
    if width % 4 or height % 4:
        raise ValueError("宽和高必须是 4 的倍数")
    return width, height


def validate_task_payload(
    *, user_id: str, mode: str, style_key: str, prompt: str, width: int, height: int,
    denoise: float, control_type: str, control_character: str, has_input: bool,
    owner_user_id: str,
) -> None:
    if mode not in ALLOWED_MODES:
        raise ValueError("未知生成模式")
    style = STYLE_BY_KEY.get(style_key)
    if not style:
        raise ValueError("未知画风")
    if style.get("owner_only") and user_id != owner_user_id:
        raise PermissionError("该画风仅管理员可用")
    if mode not in style["modes"]:
        raise ValueError("该画风不支持当前生成模式")
    prompt = prompt.strip()
    if len(prompt) > 3000:
        raise ValueError("Prompt 最多 3000 字符")
    if mode == "controlnet":
        if style_key != "controlnet":
            raise ValueError("ControlNet 模式必须选择 ControlNet 画风")
        if not has_input:
            raise ValueError("ControlNet 必须上传控制图")
        if control_character not in CONTROL_CHARACTER_KEYS:
            raise ValueError("未知 ControlNet 角色")
        if control_character == "prompt" and not prompt:
            raise ValueError("原文本模式下 Prompt 不能为空")
    elif mode == "img2img":
        if not has_input:
            raise ValueError("图生图必须上传参考图")
        if not prompt:
            raise ValueError("图生图 Prompt 不能为空")
    elif not prompt:
        raise ValueError("文生图 Prompt 不能为空")
    if control_type not in ALLOWED_CONTROL_TYPES:
        raise ValueError("未知 ControlNet 类型")
    if not 0 <= float(denoise) <= 1:
        raise ValueError("Denoise / ControlNet 强度必须在 0 到 1")
    validate_resolution(width, height)


def make_job_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "GEN-" + "".join(secrets.choice(alphabet) for _ in range(12))


def make_translation_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "TR-" + "".join(secrets.choice(alphabet) for _ in range(12))


def get_me(settings: Settings, user_id: str) -> dict[str, Any]:
    conn = connect(settings)
    try:
        conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (user_id,))
        conn.commit()
        row = conn.execute("SELECT balance_fen FROM users WHERE user_id = ?", (user_id,)).fetchone()
        s = conn.execute(
            "SELECT style_key, lora_weight, last_width, last_height FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return {"balance_fen": int(row[0] if row else 0), "settings": dict(s) if s else None}
    finally:
        conn.close()


def calculate_generation_charge(
    settings: Settings,
    *,
    user_id: str,
    style_key: str,
    use_agent: bool,
) -> int:
    """Calculate the total charge for a generation task.

    Pricing:
    - Normal generation: 1 credit
    - Normal + Agent polish/translation: 2 credits
    - Anima double sampling: 2 credits
    - Anima + Agent: 3 credits
    - Smart Agent: 5 credits (handled separately)
    - Owner: always free
    """
    if settings.owner_free_generation and user_id == settings.owner_user_id:
        return 0
    cost_multiplier = 2 if style_key == "anima_owner" else 1
    base_cost = int(settings.price_fen_per_image) * cost_multiplier
    agent_surcharge = max(0, int(settings.agent_surcharge_credits)) if use_agent else 0
    return base_cost + agent_surcharge


_ALLOWED_MOCK_RESULTS = {"", "success", "failed", "timeout"}


def create_task_atomic(
    settings: Settings,
    *, job_code: str, user_id: str, username: str, prompt: str, style_key: str,
    lora_weight: float, width: int, height: int, mode: str, input_image_path: str | None,
    denoise: float, control_type: str, control_character: str, auto_tagger: bool,
    use_agent: bool = False, client_request_id: str | None = None,
    prompt_source: str = "", character_key: str = "", mock_result: str = "",
    original_prompt: str | None = None,
    fast_translation_request_code: str | None = None,
) -> dict[str, Any]:
    clean_mock = str(mock_result or "").strip().lower()
    if clean_mock:
        if not (settings.is_local_env() and settings.mock_worker_enabled):
            raise RuntimeError("mock_result is only allowed in local test environment")
        if clean_mock not in _ALLOWED_MOCK_RESULTS:
            raise RuntimeError(f"invalid mock_result: {clean_mock}")
        mock_result = clean_mock
    validate_task_payload(
        user_id=user_id, mode=mode, style_key=style_key, prompt=prompt, width=width, height=height,
        denoise=denoise, control_type=control_type, control_character=control_character,
        has_input=bool(input_image_path), owner_user_id=settings.owner_user_id,
    )
    is_free = settings.owner_free_generation and user_id == settings.owner_user_id
    charged_fen = calculate_generation_charge(
        settings,
        user_id=user_id,
        style_key=style_key,
        use_agent=use_agent,
    )
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # ── Idempotency check: same client_request_id → return existing ──
        request_id = str(client_request_id or "").strip()[:80] or None
        if request_id:
            existing = conn.execute(
                """
                SELECT job_code, charged_fen, status
                FROM generation_tasks
                WHERE user_id=? AND client_request_id=?
                """,
                (user_id, request_id),
            ).fetchone()
            if existing:
                conn.commit()
                return {
                    "job_code": existing["job_code"],
                    "charged_fen": int(existing["charged_fen"]),
                    "status": existing["status"],
                    "deduped": True,
                }

        # ── Validate fast_translation_request_code if provided ──
        ft_code = str(fast_translation_request_code or "").strip()
        if ft_code:
            tr = conn.execute(
                "SELECT id,request_code,charged_credits,refined_prompt,original_text,"
                "character_keys_json,translation_mode,status,client_request_id "
                "FROM translation_requests WHERE request_code=? AND user_id=?",
                (ft_code, user_id),
            ).fetchone()
            if not tr:
                raise ValueError("fast_translation_not_found")
            if str(tr["status"] or "") != "done":
                raise ValueError("fast_translation_not_ready")
            refined = str(tr["refined_prompt"] or "").strip()
            if not refined:
                raise ValueError("fast_translation_not_ready")
            # Use server-side validated data
            prompt = refined
            original_prompt = str(tr["original_text"] or "")
            prompt_source = f"fast_translate:{tr['request_code']}"
            character_key = str(tr["character_keys_json"] or "")
            use_agent = False

        global_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE status IN ({ACTIVE_STATUSES_SQL})"
        ).fetchone()[0])
        if global_open >= settings.max_queue_size:
            raise RuntimeError("当前全局队列已满")
        user_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE user_id=? AND status IN ({ACTIVE_STATUSES_SQL})",
            (user_id,),
        ).fetchone()[0])
        if user_open >= settings.max_active_tasks_per_user:
            raise RuntimeError("你当前未完成的任务太多，请等待或取消后再提交")

        conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (user_id,))
        if charged_fen:
            cur = conn.execute(
                "UPDATE users SET balance_fen = balance_fen - ? WHERE user_id = ? AND balance_fen >= ?",
                (charged_fen, user_id, charged_fen),
            )
            if cur.rowcount != 1:
                raise RuntimeError("余额不足")
            conn.execute(
                "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
                "VALUES (?, ?, 'generate_charge', ?, ?, ?)",
                (user_id, -charged_fen, job_code, user_id, now),
            )

        conn.execute(
            """
            INSERT INTO user_settings(user_id, style_key, lora_weight, last_width, last_height, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                style_key=excluded.style_key,
                lora_weight=excluded.lora_weight,
                last_width=excluded.last_width,
                last_height=excluded.last_height,
                updated_at=excluded.updated_at
            """,
            (user_id, style_key, float(lora_weight), width, height, now),
        )
        conn.execute(
            """
            INSERT INTO generation_tasks(
                job_code,user_id,username,channel_id,prompt,original_prompt,effective_prompt,use_agent,
                client_request_id,style_key,lora_weight,width,height,
                generation_mode,input_image_path,denoise,control_type,control_character,auto_tagger,
                workflow_key,prompt_source,character_key,mock_result,charged_fen,status,created_at,source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'queued', ?, 'web')
            """,
            (
                job_code, user_id, username[:120], None, prompt, (original_prompt or prompt), None if use_agent else prompt,
                1 if use_agent else 0, request_id, style_key, float(lora_weight), width, height,
                mode, input_image_path, float(denoise), control_type, control_character,
                1 if auto_tagger else 0, style_key, str(prompt_source or "")[:120],
                _validate_character_key(character_key), str(mock_result or "")[:20], charged_fen, now,
            ),
        )
        conn.commit()
        return {"job_code": job_code, "charged_fen": charged_fen, "status": "queued", "deduped": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _compute_fast_translation_fingerprint(
    *,
    user_id: str,
    text: str,
    character_keys: list[str],
    source: str,
) -> str:
    """Compute stable SHA-256 fingerprint for fast translation request.

    Covers only fields that affect the final translation result.
    """
    normalized_keys = sorted(set(k for k in character_keys if k))
    mode = "characters" if normalized_keys else "none"
    payload = {
        "user_id": user_id,
        "text": text,
        "mode": mode,
        "character_keys": normalized_keys,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_generation_fingerprint(
    *,
    user_id: str,
    original_prompt: str,
    translation_mode: str,
    character_keys: list[str] | None,
    character_resolution_decision: str,
    style_key: str,
    width: int,
    height: int,
    lora_weight: float,
    mode: str,
    denoise: float,
    control_type: str,
    control_character: str,
    auto_tagger: bool,
    workflow_key: str,
    input_image_stable_id: str = "",
) -> str:
    """Compute stable fingerprint for generation task idempotency.

    Covers all fields that affect task behavior.
    Excludes client_request_id, job_code, timestamps, random values.
    """
    normalized_chars = sorted(set(k for k in (character_keys or []) if k))
    payload = json.dumps(
        {
            "user_id": user_id,
            "original_prompt": original_prompt.strip(),
            "translation_mode": translation_mode.strip(),
            "character_keys": normalized_chars,
            "character_resolution_decision": character_resolution_decision.strip(),
            "style_key": style_key.strip(),
            "width": width,
            "height": height,
            "lora_weight": lora_weight,
            "mode": mode.strip(),
            "denoise": denoise,
            "control_type": control_type.strip(),
            "control_character": control_character.strip(),
            "auto_tagger": auto_tagger,
            "workflow_key": workflow_key.strip(),
            "input_image_stable_id": input_image_stable_id.strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_fast_translation_task_atomic(
    settings: Settings,
    *,
    job_code: str,
    user_id: str,
    username: str,
    original_prompt: str,
    translation_mode: str,
    style_key: str,
    lora_weight: float,
    width: int,
    height: int,
    mode: str,
    input_image_path: str | None,
    denoise: float,
    control_type: str,
    control_character: str,
    auto_tagger: bool,
    character_keys: list[str] | None = None,
    character_resolution_decision: str = "none",
    client_request_id: str | None = None,
    mock_result: str = "",
    workflow_key: str = "",
) -> dict[str, Any]:
    """Atomically create a fast translation task.

    Single transaction:
    1. Idempotency check
    2. Active task limit (10)
    3. 20/60 submission rate limit
    4. Global queue limit
    5. Calculate server-authoritative fees
    6. Check total balance
    7. Deduct total fees
    8. Write ledger entries
    9. Create translation_request (status=queued)
    10. Create generation_task (status=translating)
    11. Return job_code immediately
    """
    clean_mock = str(mock_result or "").strip().lower()
    if clean_mock:
        if not (settings.is_local_env() and settings.mock_worker_enabled):
            raise RuntimeError("mock_result is only allowed in local test environment")
        if clean_mock not in _ALLOWED_MOCK_RESULTS:
            raise RuntimeError(f"invalid mock_result: {clean_mock}")
        mock_result = clean_mock

    raw = str(original_prompt or "").strip()
    if not raw:
        raise ValueError("prompt_required")
    if len(raw) > 3000:
        raise ValueError("prompt_too_long")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in raw):
        raise ValueError("prompt_contains_invalid_characters")

    validate_task_payload(
        user_id=user_id, mode=mode, style_key=style_key, prompt=raw, width=width, height=height,
        denoise=denoise, control_type=control_type, control_character=control_character,
        has_input=bool(input_image_path), owner_user_id=settings.owner_user_id,
    )

    now = int(time.time())
    request_id = str(client_request_id or "").strip()[:80] or None
    resolved_chars = sorted(set(k for k in (character_keys or []) if k))
    request_code = make_translation_code()

    # Server-authoritative fee calculation
    is_owner = settings.owner_free_generation and user_id == settings.owner_user_id
    cost_multiplier = 2 if style_key == "anima_owner" else 1
    base_cost = int(settings.price_fen_per_image) * cost_multiplier
    fast_translate_cost = max(0, int(settings.fast_translator_cost_credits))
    total_charge = (base_cost + fast_translate_cost) if not is_owner else 0

    fingerprint = compute_generation_fingerprint(
        user_id=user_id,
        original_prompt=raw,
        translation_mode="fast",
        character_keys=resolved_chars,
        character_resolution_decision=character_resolution_decision,
        style_key=style_key,
        width=width,
        height=height,
        lora_weight=lora_weight,
        mode=mode,
        denoise=denoise,
        control_type=control_type,
        control_character=control_character,
        auto_tagger=auto_tagger,
        workflow_key=workflow_key or style_key,
        input_image_stable_id=str(input_image_path or ""),
    )

    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")

        # 1. Idempotency check
        if request_id:
            existing = conn.execute(
                "SELECT job_code, charged_fen, status, request_fingerprint "
                "FROM generation_tasks WHERE user_id=? AND client_request_id=?",
                (user_id, request_id),
            ).fetchone()
            if existing:
                existing_fp = str(dict(existing).get("request_fingerprint") or "")
                if existing_fp and existing_fp != fingerprint:
                    conn.rollback()
                    raise ValueError("client_request_id_conflict")
                if not existing_fp:
                    # Legacy row: cannot safely compare, treat as conflict
                    conn.rollback()
                    raise ValueError("client_request_id_conflict")
                conn.commit()
                return {
                    "job_code": existing["job_code"],
                    "charged_fen": int(existing["charged_fen"]),
                    "status": existing["status"],
                    "deduped": True,
                }

        # 2. Active task limit
        ACTIVE_STATUSES = "'smart_planning','translating','queued','processing'"
        user_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE user_id=? AND status IN ({ACTIVE_STATUSES})",
            (user_id,),
        ).fetchone()[0])
        if user_open >= settings.max_active_tasks_per_user:
            conn.rollback()
            raise RuntimeError("active_task_limit")

        # 3. 20/60 rate limit (DB-authoritative)
        window = max(1, int(settings.generation_submit_window_seconds or 60))
        limit = max(1, int(settings.generation_submit_user_limit or 20))
        cutoff = now - window
        recent_count = int(conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE user_id=? AND created_at>=?",
            (user_id, cutoff),
        ).fetchone()[0])
        if recent_count >= limit:
            conn.rollback()
            raise RuntimeError("generation_rate_limited")

        # 4. Global queue limit
        global_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE status IN ({ACTIVE_STATUSES})"
        ).fetchone()[0])
        if global_open >= settings.max_queue_size:
            conn.rollback()
            raise RuntimeError("queue_full")

        # 5-6. Balance check
        conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (user_id,))
        if total_charge:
            balance_row = conn.execute(
                "SELECT balance_fen FROM users WHERE user_id=?", (user_id,),
            ).fetchone()
            if not balance_row or int(balance_row[0]) < total_charge:
                conn.rollback()
                raise RuntimeError("insufficient_credits")

            # 7. Deduct total
            cur = conn.execute(
                "UPDATE users SET balance_fen=balance_fen-? WHERE user_id=? AND balance_fen>=?",
                (total_charge, user_id, total_charge),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise RuntimeError("insufficient_credits")

            # 8. Ledger entries
            if base_cost:
                conn.execute(
                    "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) "
                    "VALUES (?,?,'generate_charge',?,?,?)",
                    (user_id, -base_cost, job_code, user_id, now),
                )
            if fast_translate_cost:
                conn.execute(
                    "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) "
                    "VALUES (?,?,'fast_translate_charge',?,?,?)",
                    (user_id, -fast_translate_cost, request_code, user_id, now),
                )

        # 9. Create translation_request
        tr_fingerprint = _compute_fast_translation_fingerprint(
            user_id=user_id,
            text=raw,
            character_keys=resolved_chars,
            source=character_resolution_decision,
        )
        conn.execute(
            """INSERT INTO translation_requests(
                request_code,user_id,client_request_id,translation_mode,model,
                character_match_source,character_keys_json,original_text,
                charged_credits,status,created_at,request_fingerprint,generation_job_code
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request_code, user_id, request_id, "fast", settings.deepseek_model,
                character_resolution_decision,
                json.dumps(resolved_chars, ensure_ascii=False),
                raw, fast_translate_cost, "queued", now, tr_fingerprint, job_code,
            ),
        )

        # 10. Save user settings
        conn.execute(
            """INSERT INTO user_settings(user_id, style_key, lora_weight, last_width, last_height, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                style_key=excluded.style_key,
                lora_weight=excluded.lora_weight,
                last_width=excluded.last_width,
                last_height=excluded.last_height,
                updated_at=excluded.updated_at""",
            (user_id, style_key, float(lora_weight), width, height, now),
        )

        # 11. Create generation_task (status=translating)
        conn.execute(
            """INSERT INTO generation_tasks(
                job_code,user_id,username,channel_id,prompt,original_prompt,effective_prompt,
                use_agent,client_request_id,style_key,lora_weight,width,height,
                generation_mode,input_image_path,denoise,control_type,control_character,auto_tagger,
                workflow_key,prompt_source,character_key,mock_result,charged_fen,status,created_at,source,
                translation_mode,fast_translation_request_code,request_fingerprint,translating_started_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_code, user_id, username[:120], None,
                raw,  # prompt placeholder - will be replaced by worker
                raw,  # original_prompt
                None,  # effective_prompt - NULL until translation completes
                0,  # use_agent
                request_id, style_key, float(lora_weight), width, height,
                mode, input_image_path, float(denoise), control_type, control_character,
                1 if auto_tagger else 0,
                workflow_key or style_key,
                f"fast_translate_pending:{request_code}",
                _validate_character_key(json.dumps(resolved_chars, ensure_ascii=False) if resolved_chars else ""),
                str(mock_result or "")[:20],
                total_charge,
                "translating",
                now,
                "web",
                "fast",
                request_code,
                fingerprint,
                now,
            ),
        )

        conn.commit()
        return {
            "job_code": job_code,
            "charged_fen": total_charge,
            "status": "translating",
            "deduped": False,
            "request_code": request_code,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fail_fast_translation_task_refund_atomic(
    settings: Settings,
    *,
    job_code: str,
    error_code: str = "deepseek_failed",
) -> bool:
    """Atomically refund a failed fast translation task.

    Single transaction:
    1. Check generation task still translating
    2. Check translation request belongs to this task
    3. Refund generation charge
    4. Refund fast translation charge
    5. Write refund ledger entries
    6. Mark translation_request = failed_refunded
    7. Mark generation_task = failed_refunded

    Idempotent: duplicate calls are no-op.
    """
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, charged_fen, status, fast_translation_request_code "
            "FROM generation_tasks WHERE job_code=?",
            (job_code,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        row_dict = dict(row)
        current_status = str(row_dict.get("status") or "")
        # Already refunded or terminal → no-op
        if current_status in {"failed_refunded", "cancelled_refunded", "done"}:
            conn.rollback()
            return False
        if current_status != "translating":
            conn.rollback()
            return False

        user_id = str(row_dict["user_id"])
        total_charged = int(row_dict["charged_fen"] or 0)
        ft_code = str(row_dict.get("fast_translation_request_code") or "")

        # Find translation request
        tr = None
        if ft_code:
            tr = conn.execute(
                "SELECT id, charged_credits, status FROM translation_requests "
                "WHERE request_code=? AND user_id=? AND generation_job_code=?",
                (ft_code, user_id, job_code),
            ).fetchone()

        # Check refund idempotency
        existing_gen_refund = conn.execute(
            "SELECT id FROM balance_ledger WHERE order_code=? AND reason='generate_failed_refund' LIMIT 1",
            (job_code,),
        ).fetchone()
        if existing_gen_refund:
            conn.rollback()
            return False

        # Update generation task status
        cur = conn.execute(
            "UPDATE generation_tasks SET status='failed_refunded', finished_at=?, error=?, error_code=? "
            "WHERE job_code=? AND status='translating'",
            (now, error_code, error_code, job_code),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False

        # Update translation request status
        if tr:
            conn.execute(
                "UPDATE translation_requests SET status='failed_refunded', finished_at=?, error_code=? WHERE id=?",
                (now, error_code, tr["id"]),
            )

        # Refund generation charge
        if total_charged:
            conn.execute(
                "UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?",
                (total_charged, user_id),
            )
            conn.execute(
                "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) "
                "VALUES (?,?,'generate_failed_refund',?,?,?)",
                (user_id, total_charged, job_code, "fast_translator", now),
            )

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _compute_request_fingerprint(
    *,
    user_id: str,
    request_text: str,
    cost_credits: int,
    client_request_id: str | None = None,
    prompt_source: str = "",
    character_keys: list[str] | None = None,
    workflow_key: str = "",
    width: int = 0,
    height: int = 0,
    translation_mode: str = "",
) -> str:
    """Compute stable fingerprint for idempotency check.

    Includes all business fields that affect task behavior.
    Excludes timestamps, random IDs, and transient state.
    client_request_id is excluded (it's the idempotency key itself).
    """
    import hashlib
    # Normalize character keys: sort for stable comparison
    normalized_chars = sorted(character_keys) if character_keys else []
    payload = json.dumps(
        {
            "user_id": user_id,
            "request_text": request_text.strip(),
            "cost_credits": cost_credits,
            "prompt_source": prompt_source.strip(),
            "character_keys": normalized_chars,
            "workflow_key": workflow_key.strip(),
            "width": width,
            "height": height,
            "translation_mode": translation_mode.strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_idempotency(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    request_id: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    """Check idempotency table for existing record.

    Returns:
        None if no existing record.
        {"task_id": ..., "status": "completed", "deduped": True} if same fingerprint.
        Raises ValueError if fingerprint mismatch (conflict).
    """
    existing = conn.execute(
        "SELECT task_id, request_fingerprint, request_status FROM smart_agent_request_idempotency "
        "WHERE user_id=? AND client_request_id=?",
        (user_id, request_id),
    ).fetchone()
    if not existing:
        return None
    if existing["request_fingerprint"] != fingerprint:
        raise ValueError(
            "client_request_id_conflict: "
            "The same client_request_id was already used with different request content."
        )
    # Same fingerprint - return existing task
    return {
        "task_id": existing["task_id"] or "",
        "status": existing["request_status"],
        "deduped": True,
    }


def _insert_idempotency_record(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    request_id: str,
    fingerprint: str,
    now: int,
) -> None:
    """Insert idempotency record. Raises on UNIQUE violation."""
    conn.execute(
        "INSERT INTO smart_agent_request_idempotency "
        "(user_id, client_request_id, request_fingerprint, request_status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'pending', ?, 0)",
        (user_id, request_id, fingerprint, now),
    )


def _complete_idempotency_record(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    request_id: str,
    task_id: str,
    now: int,
) -> None:
    """Update idempotency record with task_id and mark completed."""
    conn.execute(
        "UPDATE smart_agent_request_idempotency "
        "SET task_id=?, request_status='completed', updated_at=? "
        "WHERE user_id=? AND client_request_id=?",
        (task_id, now, user_id, request_id),
    )


def _insert_refund_event(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    task_id: str,
    amount: int,
    reason: str,
    now: int,
) -> bool:
    """Insert refund billing event. Returns True if inserted, False if duplicate.

    Event key uses only task_id to prevent cross-reason duplicate refunds.
    Reason is stored as metadata, not part of the unique key.
    """
    event_key = f"smart-agent-refund:{task_id}"
    metadata = json.dumps({"reason": reason}, ensure_ascii=False)
    try:
        conn.execute(
            "INSERT INTO smart_agent_billing_events "
            "(user_id, task_id, event_type, amount, event_key, created_at, metadata) "
            "VALUES (?, ?, 'refund', ?, ?, ?, ?)",
            (user_id, task_id, amount, event_key, now, metadata),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def create_smart_agent_task_atomic(
    settings: Settings,
    *,
    job_code: str,
    user_id: str,
    username: str,
    request_text: str,
    cost_credits: int,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    request_text = (request_text or "").strip()
    if not request_text:
        raise ValueError("请输入想生成的画面")
    if len(request_text) > 1200:
        raise ValueError("需求描述最多 1200 字")
    charged_fen = max(1, int(cost_credits))
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        request_id = str(client_request_id or "").strip()[:80] or None

        # ── Idempotency check via dedicated table ──
        if request_id:
            fingerprint = _compute_request_fingerprint(
                user_id=user_id,
                request_text=request_text,
                cost_credits=cost_credits,
                client_request_id=request_id,
            )
            # Try to check existing record first
            idem_result = _check_idempotency(
                conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint,
            )
            if idem_result:
                # Same fingerprint, already completed → return existing
                if idem_result.get("task_id"):
                    existing_task = conn.execute(
                        "SELECT job_code, charged_fen, status FROM generation_tasks WHERE job_code=?",
                        (idem_result["task_id"],),
                    ).fetchone()
                    conn.commit()
                    return {
                        "job_code": existing_task["job_code"] if existing_task else idem_result["task_id"],
                        "charged_fen": int(existing_task["charged_fen"]) if existing_task else charged_fen,
                        "status": existing_task["status"] if existing_task else "queued",
                        "deduped": True,
                    }
                # Pending record exists with same fingerprint → return deduped
                conn.commit()
                return {
                    "job_code": idem_result.get("task_id") or job_code,
                    "charged_fen": charged_fen,
                    "status": "queued",
                    "deduped": True,
                }
            # No existing record → insert idempotency placeholder
            try:
                _insert_idempotency_record(
                    conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint, now=now,
                )
            except sqlite3.IntegrityError:
                # Concurrent insert won. Re-check.
                conn.rollback()
                conn = connect(settings)
                conn.execute("BEGIN IMMEDIATE")
                idem_result = _check_idempotency(
                    conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint,
                )
                if idem_result and idem_result.get("task_id"):
                    existing_task = conn.execute(
                        "SELECT job_code, charged_fen, status FROM generation_tasks WHERE job_code=?",
                        (idem_result["task_id"],),
                    ).fetchone()
                    conn.commit()
                    return {
                        "job_code": existing_task["job_code"] if existing_task else idem_result["task_id"],
                        "charged_fen": int(existing_task["charged_fen"]) if existing_task else charged_fen,
                        "status": existing_task["status"] if existing_task else "queued",
                        "deduped": True,
                    }
                # Concurrent insert with same fingerprint, still pending
                conn.commit()
                return {
                    "job_code": job_code,
                    "charged_fen": charged_fen,
                    "status": "queued",
                    "deduped": True,
                }

        # ── Queue limits ──
        global_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE status IN ({ACTIVE_STATUSES_SQL})"
        ).fetchone()[0])
        if global_open >= settings.max_queue_size:
            raise RuntimeError("当前全局队列已满")
        user_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE user_id=? AND status IN ({ACTIVE_STATUSES_SQL})",
            (user_id,),
        ).fetchone()[0])
        if user_open >= settings.max_active_tasks_per_user:
            raise RuntimeError("你当前未完成的任务太多，请等待或取消后再提交")

        # ── Balance deduction (same transaction) ──
        conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (user_id,))
        cur = conn.execute(
            "UPDATE users SET balance_fen = balance_fen - ? WHERE user_id = ? AND balance_fen >= ?",
            (charged_fen, user_id, charged_fen),
        )
        if cur.rowcount != 1:
            raise RuntimeError("余额不足")
        conn.execute(
            "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, -charged_fen, SMART_AGENT_CHARGE_REASON, job_code, user_id, now),
        )

        # ── Task creation (same transaction) ──
        conn.execute(
            """
            INSERT INTO generation_tasks(
                job_code,user_id,username,channel_id,prompt,original_prompt,effective_prompt,use_agent,
                agent_mode,smart_agent_request,client_request_id,style_key,lora_weight,width,height,
                generation_mode,input_image_path,denoise,control_type,control_character,auto_tagger,
                charged_fen,status,created_at,source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'web')
            """,
            (
                job_code, user_id, username[:120], None, request_text, request_text, None, 0,
                "smart_agent", request_text, request_id, "style_a", 1.0, 1024, 1536,
                "txt2img", None, 0.5, "depth", "prompt", 0,
                charged_fen, SMART_AGENT_STATUS, now,
            ),
        )

        # ── Update idempotency record with task_id (same transaction) ──
        if request_id:
            _complete_idempotency_record(
                conn, user_id=user_id, request_id=request_id, task_id=job_code, now=now,
            )

        conn.commit()
        return {"job_code": job_code, "charged_fen": charged_fen, "status": SMART_AGENT_STATUS, "deduped": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_smart_agent_queued_task_atomic(
    settings: Settings,
    *,
    job_code: str,
    user_id: str,
    username: str,
    request_text: str,
    cost_credits: int,
    plan_json: str,
    prompt: str,
    workflow_key: str,
    loras_json: str,
    prompt_source: str,
    width: int,
    height: int,
    conversation_code: str = "",
    character_key: str = "",
    workflow_source: str = "",
    fallback_level: str = "",
    client_request_id: str | None = None,
) -> dict[str, Any]:
    request_text = (request_text or "").strip()
    prompt = (prompt or "").strip()
    if not request_text:
        raise ValueError("请输入想生成的画面")
    if len(request_text) > 1200:
        raise ValueError("需求描述最多 1200 字")
    if not prompt:
        raise ValueError("Smart Agent prompt is empty")
    validate_resolution(width, height)
    charged_fen = max(1, int(cost_credits))
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        request_id = str(client_request_id or "").strip()[:80] or None

        # ── Idempotency check via dedicated table ──
        if request_id:
            fingerprint = _compute_request_fingerprint(
                user_id=user_id,
                request_text=request_text,
                cost_credits=cost_credits,
                prompt_source=prompt_source,
                character_keys=[character_key] if character_key else [],
                workflow_key=workflow_key,
                width=width,
                height=height,
            )
            idem_result = _check_idempotency(
                conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint,
            )
            if idem_result:
                if idem_result.get("task_id"):
                    existing_task = conn.execute(
                        "SELECT job_code, charged_fen, status FROM generation_tasks WHERE job_code=?",
                        (idem_result["task_id"],),
                    ).fetchone()
                    conn.commit()
                    return {
                        "job_code": existing_task["job_code"] if existing_task else idem_result["task_id"],
                        "charged_fen": int(existing_task["charged_fen"]) if existing_task else charged_fen,
                        "status": existing_task["status"] if existing_task else "queued",
                        "deduped": True,
                    }
                conn.commit()
                return {"job_code": job_code, "charged_fen": charged_fen, "status": "queued", "deduped": True}
            try:
                _insert_idempotency_record(
                    conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint, now=now,
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                conn = connect(settings)
                conn.execute("BEGIN IMMEDIATE")
                idem_result = _check_idempotency(
                    conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint,
                )
                if idem_result and idem_result.get("task_id"):
                    existing_task = conn.execute(
                        "SELECT job_code, charged_fen, status FROM generation_tasks WHERE job_code=?",
                        (idem_result["task_id"],),
                    ).fetchone()
                    conn.commit()
                    return {
                        "job_code": existing_task["job_code"] if existing_task else idem_result["task_id"],
                        "charged_fen": int(existing_task["charged_fen"]) if existing_task else charged_fen,
                        "status": existing_task["status"] if existing_task else "queued",
                        "deduped": True,
                    }
                conn.commit()
                return {"job_code": job_code, "charged_fen": charged_fen, "status": "queued", "deduped": True}
        global_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE status IN ({ACTIVE_STATUSES_SQL})"
        ).fetchone()[0])
        if global_open >= settings.max_queue_size:
            raise RuntimeError("当前全局队列已满")
        user_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE user_id=? AND status IN ({ACTIVE_STATUSES_SQL})",
            (user_id,),
        ).fetchone()[0])
        if user_open >= settings.max_active_tasks_per_user:
            raise RuntimeError("你当前未完成的任务太多，请等待或取消后再提交")

        conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (user_id,))
        cur = conn.execute(
            "UPDATE users SET balance_fen = balance_fen - ? WHERE user_id = ? AND balance_fen >= ?",
            (charged_fen, user_id, charged_fen),
        )
        if cur.rowcount != 1:
            raise RuntimeError("余额不足")
        conn.execute(
            "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, -charged_fen, SMART_AGENT_CHARGE_REASON, job_code, user_id, now),
        )
        conn.execute(
            """
            INSERT INTO generation_tasks(
                job_code,user_id,username,channel_id,prompt,original_prompt,effective_prompt,use_agent,
                agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,client_request_id,
                style_key,lora_weight,width,height,generation_mode,input_image_path,denoise,control_type,
                control_character,auto_tagger,workflow_key,loras_json,prompt_source,charged_fen,status,created_at,source
                ,conversation_code,character_key,workflow_source,fallback_level
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'web',?,?,?,?)
            """,
            (
                job_code, user_id, username[:120], None, prompt[:3000], request_text, prompt[:3000], 0,
                "smart_agent", request_text, plan_json, None, request_id,
                workflow_key, 1.0, int(width), int(height), "txt2img", None, 0.5, "depth",
                "prompt", 0, workflow_key, loras_json, prompt_source[:120], charged_fen, "queued", now,
                str(conversation_code or "")[:80], _validate_character_key(character_key),
                str(workflow_source or "")[:80], str(fallback_level or "")[:80],
            ),
        )
        if request_id:
            _complete_idempotency_record(
                conn, user_id=user_id, request_id=request_id, task_id=job_code, now=now,
            )
        conn.commit()
        return {"job_code": job_code, "charged_fen": charged_fen, "status": "queued", "deduped": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def confirm_smart_agent_prompt_draft_atomic(
    settings: Settings,
    *,
    conversation_id: int,
    user_id: str,
    username: str,
    job_code: str,
    cost_credits: int,
    conversation_code: str = "",
    client_request_id: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    charged_fen = max(1, int(cost_credits))
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        draft = conn.execute(
            "SELECT * FROM smart_agent_prompt_drafts WHERE conversation_id=?",
            (int(conversation_id),),
        ).fetchone()
        if not draft:
            raise RuntimeError("prompt_draft_not_ready")
        draft = dict(draft)  # Convert sqlite3.Row to dict for .get() support
        if draft["generation_job_code"] and str(draft["status"] or "") == "generated":
            existing = conn.execute(
                "SELECT job_code, charged_fen, status FROM generation_tasks WHERE job_code=?",
                (draft["generation_job_code"],),
            ).fetchone()
            conn.commit()
            return {
                "job_code": str(draft["generation_job_code"]),
                "charged_fen": int(existing["charged_fen"] if existing else charged_fen),
                "status": str(existing["status"] if existing else "queued"),
                "already_created": True,
                "prompt_version": int(draft["prompt_version"] or 1),
            }
        if str(draft["status"] or "") != "prompt_ready":
            raise RuntimeError("prompt_draft_not_ready")
        # 清除旧任务关联，确保新确认创建新任务
        if draft["generation_job_code"]:
            conn.execute(
                "UPDATE smart_agent_prompt_drafts SET generation_job_code='' WHERE id=?",
                (draft["id"],),
            )
            draft = dict(draft)
            draft["generation_job_code"] = ""

        request_id = str(client_request_id or "").strip()[:80]
        if not request_id:
            request_id = f"smart-confirm:{int(conversation_id)}:{int(draft['prompt_version'] or 1)}"
        else:
            # 追加 prompt_version 确保每次确认的幂等键唯一
            request_id = f"{request_id}:v{int(draft['prompt_version'] or 1)}"

        # ── Idempotency check via dedicated table ──
        fingerprint = _compute_request_fingerprint(
            user_id=user_id,
            request_text=str(draft.get("request_text") or ""),
            cost_credits=cost_credits,
            prompt_source=str(draft.get("prompt_source") or ""),
            character_keys=[str(draft.get("resolved_character_key") or "")] if draft.get("resolved_character_key") else [],
            workflow_key=str(draft.get("workflow_key") or ""),
            width=int(draft.get("width") or 1024),
            height=int(draft.get("height") or 1024),
        )
        idem_result = _check_idempotency(
            conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint,
        )
        if idem_result:
            if idem_result.get("task_id"):
                existing_task = conn.execute(
                    "SELECT job_code, charged_fen, status FROM generation_tasks WHERE job_code=?",
                    (idem_result["task_id"],),
                ).fetchone()
                conn.execute(
                    "UPDATE smart_agent_prompt_drafts SET status='generated', generation_job_code=?, updated_at=? WHERE id=?",
                    (idem_result["task_id"], now, draft["id"]),
                )
                conn.commit()
                return {
                    "job_code": existing_task["job_code"] if existing_task else idem_result["task_id"],
                    "charged_fen": int(existing_task["charged_fen"]) if existing_task else charged_fen,
                    "status": str(existing_task["status"] if existing_task else "queued"),
                    "already_created": True,
                    "prompt_version": int(draft["prompt_version"] or 1),
                }
            conn.commit()
            return {
                "job_code": request_id,
                "charged_fen": charged_fen,
                "status": "queued",
                "already_created": True,
                "prompt_version": int(draft["prompt_version"] or 1),
            }
        try:
            _insert_idempotency_record(
                conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint, now=now,
            )
        except Exception:
            conn.rollback()
            conn = connect(settings)
            conn.execute("BEGIN IMMEDIATE")
            idem_result = _check_idempotency(
                conn, user_id=user_id, request_id=request_id, fingerprint=fingerprint,
            )
            if idem_result and idem_result.get("task_id"):
                existing_task = conn.execute(
                    "SELECT job_code, charged_fen, status FROM generation_tasks WHERE job_code=?",
                    (idem_result["task_id"],),
                ).fetchone()
                conn.execute(
                    "UPDATE smart_agent_prompt_drafts SET status='generated', generation_job_code=?, updated_at=? WHERE id=?",
                    (idem_result["task_id"], now, draft["id"]),
                )
                conn.commit()
                return {
                    "job_code": existing_task["job_code"] if existing_task else idem_result["task_id"],
                    "charged_fen": int(existing_task["charged_fen"]) if existing_task else charged_fen,
                    "status": str(existing_task["status"] if existing_task else "queued"),
                    "already_created": True,
                    "prompt_version": int(draft["prompt_version"] or 1),
                }
            conn.commit()
            return {
                "job_code": request_id,
                "charged_fen": charged_fen,
                "status": "queued",
                "already_created": True,
                "prompt_version": int(draft["prompt_version"] or 1),
            }

        existing = conn.execute(
            "SELECT job_code, charged_fen, status FROM generation_tasks WHERE user_id=? AND client_request_id=?",
            (user_id, request_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE smart_agent_prompt_drafts SET status='generated', generation_job_code=?, updated_at=? WHERE id=?",
                (existing["job_code"], now, draft["id"]),
            )
            _complete_idempotency_record(
                conn, user_id=user_id, request_id=request_id, task_id=existing["job_code"], now=now,
            )
            conn.commit()
            return {
                "job_code": existing["job_code"],
                "charged_fen": int(existing["charged_fen"]),
                "status": existing["status"],
                "already_created": True,
                "prompt_version": int(draft["prompt_version"] or 1),
            }

        global_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE status IN ({ACTIVE_STATUSES_SQL})"
        ).fetchone()[0])
        if global_open >= settings.max_queue_size:
            raise RuntimeError("当前全局队列已满")
        user_open = int(conn.execute(
            f"SELECT COUNT(*) FROM generation_tasks WHERE user_id=? AND status IN ({ACTIVE_STATUSES_SQL})",
            (user_id,),
        ).fetchone()[0])
        if user_open >= settings.max_active_tasks_per_user:
            raise RuntimeError("你当前未完成的任务太多，请等待或取消后再提交")

        prompt = str(draft["prompt_draft"] or "").strip()
        if not prompt:
            raise RuntimeError("prompt_draft_not_ready")
        validate_resolution(int(draft["width"] or 1024), int(draft["height"] or 1024))
        conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (user_id,))
        cur = conn.execute(
            "UPDATE users SET balance_fen = balance_fen - ? WHERE user_id = ? AND balance_fen >= ?",
            (charged_fen, user_id, charged_fen),
        )
        if cur.rowcount != 1:
            raise RuntimeError("余额不足")
        conn.execute(
            "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, -charged_fen, SMART_AGENT_CHARGE_REASON, job_code, user_id, now),
        )
        workflow_key = str(draft["workflow_key"] or "")
        conn.execute(
            """
            INSERT INTO generation_tasks(
                job_code,user_id,username,channel_id,prompt,original_prompt,effective_prompt,use_agent,
                agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,client_request_id,
                style_key,lora_weight,width,height,generation_mode,input_image_path,denoise,control_type,
                control_character,auto_tagger,workflow_key,loras_json,prompt_source,charged_fen,status,created_at,source,
                conversation_code,character_key,workflow_source,fallback_level
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'web',?,?,?,?)
            """,
            (
                job_code, user_id, username[:120], None, prompt[:3000], str(draft["request_text"] or ""),
                prompt[:3000], 0, "smart_agent", str(draft["request_text"] or ""), str(draft["plan_json"] or ""),
                None, request_id, workflow_key, 1.0, int(draft["width"] or 1024), int(draft["height"] or 1024),
                "txt2img", None, 0.5, "depth", "prompt", 0, workflow_key, str(draft["loras_json"] or "[]"),
                str(draft["prompt_source"] or "")[:120], charged_fen, "queued", now,
                str(conversation_code or "")[:80], _validate_character_key(draft.get("resolved_character_key")), str(draft["workflow_source"] or "")[:80],
                str(draft["fallback_level"] or "")[:80],
            ),
        )
        conn.execute(
            "UPDATE smart_agent_prompt_drafts SET status='generated', generation_job_code=?, updated_at=? WHERE id=?",
            (job_code, now, draft["id"]),
        )
        _complete_idempotency_record(
            conn, user_id=user_id, request_id=request_id, task_id=job_code, now=now,
        )
        conn.commit()
        return {
            "job_code": job_code,
            "charged_fen": charged_fen,
            "status": "queued",
            "already_created": False,
            "prompt_version": int(draft["prompt_version"] or 1),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next_smart_agent_task(settings: Settings, claim_ttl_seconds: int = 180) -> dict[str, Any] | None:
    now = int(time.time())
    stale_before = now - max(30, int(claim_ttl_seconds))
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT *
            FROM generation_tasks
            WHERE status='smart_planning'
              AND (
                    smart_agent_error IS NULL
                    OR (smart_agent_error LIKE 'claim:%' AND CAST(substr(smart_agent_error, 7) AS INTEGER) < ?)
                  )
            ORDER BY created_at ASC, rowid ASC
            LIMIT 1
            """,
            (stale_before,),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            "UPDATE generation_tasks SET smart_agent_error=? WHERE job_code=? AND status='smart_planning'",
            (f"claim:{now}", row["job_code"]),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_smart_agent_plan(
    settings: Settings,
    *,
    job_code: str,
    plan_json: str,
    prompt: str,
    workflow_key: str,
    loras_json: str,
    prompt_source: str,
    width: int,
    height: int,
    character_key: str = "",
    workflow_source: str = "",
    fallback_level: str = "",
) -> bool:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Smart Agent prompt is empty")
    validate_resolution(width, height)
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE generation_tasks
            SET prompt=?,
                effective_prompt=?,
                smart_agent_plan_json=?,
                smart_agent_error=NULL,
                workflow_key=?,
                loras_json=?,
                prompt_source=?,
                style_key=?,
                width=?,
                height=?,
                generation_mode='txt2img',
                character_key=?,
                workflow_source=?,
                fallback_level=?,
                status='queued',
                created_at=?
            WHERE job_code=? AND status='smart_planning' AND agent_mode='smart_agent'
            """,
            (
                prompt[:3000], prompt[:3000], plan_json, workflow_key, loras_json,
                prompt_source[:120], workflow_key, int(width), int(height),
                _validate_character_key(character_key), str(workflow_source or "")[:80], str(fallback_level or "")[:80],
                now, job_code,
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fail_smart_agent_task_refund(
    settings: Settings,
    *,
    job_code: str,
    error: str,
    error_code: str = "smart_agent_error",
) -> bool:
    now = int(time.time())
    safe_error = str(error or "Smart Agent planning failed")[:1000]
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT user_id, charged_fen, status
            FROM generation_tasks
            WHERE job_code=? AND agent_mode='smart_agent'
            """,
            (job_code,),
        ).fetchone()
        if not row or row["status"] != SMART_AGENT_STATUS:
            conn.rollback()
            return False
        charged_fen = int(row["charged_fen"] or 0)

        # ── Billing event idempotency check ──
        refund_inserted = _insert_refund_event(
            conn,
            user_id=row["user_id"],
            task_id=job_code,
            amount=charged_fen,
            reason=SMART_AGENT_REFUND_REASON,
            now=now,
        )
        if not refund_inserted:
            # Already refunded at ledger level
            conn.rollback()
            return False

        # ── Update task status (conditional) ──
        cur = conn.execute(
            """
            UPDATE generation_tasks
            SET status='failed_refunded', finished_at=?, smart_agent_error=?, error=?, error_code=?
            WHERE job_code=? AND status='smart_planning'
            """,
            (now, safe_error, safe_error, error_code[:80], job_code),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False

        # ── Balance refund (same transaction) ──
        if charged_fen:
            conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (charged_fen, row["user_id"]))
            conn.execute(
                "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
                "VALUES (?, ?, ?, ?, 'smart_agent', ?)",
                (row["user_id"], charged_fen, SMART_AGENT_REFUND_REASON, job_code, now),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def refund_unintended_smart_agent_generation(settings: Settings, job_code: str) -> dict[str, Any]:
    code = str(job_code or "").strip().upper()
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT job_code, user_id, charged_fen, status, agent_mode
            FROM generation_tasks
            WHERE job_code=?
            """,
            (code,),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"job_code": code, "exists": False, "refunded": False, "amount_fen": 0}
        existing = conn.execute(
            """
            SELECT id, amount_fen
            FROM balance_ledger
            WHERE order_code=? AND reason=?
            LIMIT 1
            """,
            (code, SMART_AGENT_UNINTENDED_REFUND_REASON),
        ).fetchone()
        if existing:
            conn.commit()
            return {
                "job_code": code,
                "exists": True,
                "refunded": False,
                "already_refunded": True,
                "amount_fen": int(existing["amount_fen"] or 0),
                "status": row["status"],
            }
        charged_fen = int(row["charged_fen"] or 0)
        if charged_fen <= 0:
            conn.commit()
            return {
                "job_code": code,
                "exists": True,
                "refunded": False,
                "already_refunded": False,
                "amount_fen": 0,
                "status": row["status"],
            }
        conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (charged_fen, row["user_id"]))
        conn.execute(
            "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
            "VALUES (?, ?, ?, ?, 'smart_agent_intent_fix', ?)",
            (row["user_id"], charged_fen, SMART_AGENT_UNINTENDED_REFUND_REASON, code, now),
        )
        conn.commit()
        return {
            "job_code": code,
            "exists": True,
            "refunded": True,
            "already_refunded": False,
            "amount_fen": charged_fen,
            "status": row["status"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def make_conversation_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "SAC-" + "".join(secrets.choice(alphabet) for _ in range(12))


def create_conversation(settings: Settings, *, legacy_user_id: str, account_id: int, title: str = "") -> dict[str, Any]:
    code = make_conversation_code()
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO smart_agent_conversations(conversation_code,account_id,legacy_user_id,title,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (code, int(account_id), legacy_user_id, title[:200], now, now),
        )
        conv_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return {"id": conv_id, "conversation_code": code, "title": title[:200], "created_at": now}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_conversations(settings: Settings, *, legacy_user_id: str) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT id, conversation_code, title, memory_summary, status, created_at, updated_at "
            "FROM smart_agent_conversations WHERE legacy_user_id=? AND status='active' ORDER BY updated_at DESC LIMIT 50",
            (legacy_user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_conversation(settings: Settings, *, conversation_code: str, legacy_user_id: str) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT id, conversation_code, title, memory_summary, status, created_at, updated_at "
            "FROM smart_agent_conversations WHERE conversation_code=? AND legacy_user_id=?",
            (conversation_code, legacy_user_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_conversation_by_code(settings: Settings, *, conversation_code: str) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT id, conversation_code, legacy_user_id, title, memory_summary, status, created_at, updated_at "
            "FROM smart_agent_conversations WHERE conversation_code=?",
            (conversation_code,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def add_conversation_message(
    settings: Settings,
    *,
    conversation_id: int,
    role: str,
    content: str,
    safe_content: str = "",
    status: str | None = None,
    intent: str = "",
    client_request_id: str | None = None,
) -> int:
    now = int(time.time())
    title_seed = (safe_content or content or "").strip().replace("\n", " ")[:80]
    message_status = status or ("pending" if role == "user" else "done")
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO smart_agent_messages(
                conversation_id,role,content,safe_content,created_at,status,intent,client_request_id
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                conversation_id,
                role,
                content,
                safe_content or "",
                now,
                message_status,
                intent or "",
                client_request_id or "",
            ),
        )
        if role == "user" and title_seed:
            conn.execute(
                "UPDATE smart_agent_conversations SET title=? WHERE id=? AND (title IS NULL OR title='')",
                (title_seed, conversation_id),
            )
        conn.execute(
            "UPDATE smart_agent_conversations SET updated_at=? WHERE id=?",
            (now, conversation_id),
        )
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return msg_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_conversation_event(
    settings: Settings, *, conversation_id: int, event_type: str, public_message: str,
    private_detail: str = "", job_code: str = "",
) -> int:
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO smart_agent_events(conversation_id,job_code,event_type,public_message,private_detail,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (conversation_id, job_code[:80], event_type, public_message, (private_detail or "")[:2000], now),
        )
        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return event_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_smart_agent_prompt_draft(
    settings: Settings,
    *,
    conversation_id: int,
    message_id: int | None,
    prompt_draft: str,
    plan_json: str,
    request_text: str,
    workflow_key: str,
    loras_json: str,
    prompt_source: str,
    character_key: str,
    workflow_source: str,
    fallback_level: str,
    width: int,
    height: int,
    structured_draft_json: str = "",
) -> dict[str, Any]:
    prompt = (prompt_draft or "").strip()
    if not prompt:
        raise ValueError("Smart Agent prompt is empty")
    validate_resolution(width, height)
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT prompt_version FROM smart_agent_prompt_drafts WHERE conversation_id=?",
            (int(conversation_id),),
        ).fetchone()
        next_version = int(existing["prompt_version"] or 0) + 1 if existing else 1
        conn.execute(
            """
            INSERT INTO smart_agent_prompt_drafts(
                conversation_id,message_id,prompt_draft,prompt_version,resolved_character_key,status,ready_at,
                generation_job_code,plan_json,request_text,workflow_key,loras_json,prompt_source,
                workflow_source,fallback_level,width,height,structured_draft_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,'prompt_ready',?,'',?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                message_id=excluded.message_id,
                prompt_draft=excluded.prompt_draft,
                prompt_version=excluded.prompt_version,
                resolved_character_key=excluded.resolved_character_key,
                status='prompt_ready',
                ready_at=excluded.ready_at,
                generation_job_code='',
                plan_json=excluded.plan_json,
                request_text=excluded.request_text,
                workflow_key=excluded.workflow_key,
                loras_json=excluded.loras_json,
                prompt_source=excluded.prompt_source,
                workflow_source=excluded.workflow_source,
                fallback_level=excluded.fallback_level,
                width=excluded.width,
                height=excluded.height,
                structured_draft_json=excluded.structured_draft_json,
                updated_at=excluded.updated_at
            """,
            (
                int(conversation_id),
                int(message_id) if message_id else None,
                prompt[:3000],
                next_version,
                _validate_character_key(character_key),
                now,
                plan_json,
                request_text[:2000],
                workflow_key[:120],
                loras_json or "[]",
                prompt_source[:120],
                workflow_source[:80],
                fallback_level[:80],
                int(width),
                int(height),
                str(structured_draft_json or "")[:4000],
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM smart_agent_prompt_drafts WHERE conversation_id=?",
            (int(conversation_id),),
        ).fetchone()
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_smart_agent_prompt_draft(settings: Settings, *, conversation_id: int) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM smart_agent_prompt_drafts WHERE conversation_id=?",
            (int(conversation_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_conversation_summary(settings: Settings, *, conversation_id: int, memory_summary: str) -> None:
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_conversations SET memory_summary=?, updated_at=? WHERE id=?",
            (memory_summary[:2000], now, conversation_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_conversation_messages(settings: Settings, *, conversation_id: int, limit: int = 50) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT id, role, content, safe_content, created_at, status, intent, processed_at, error FROM smart_agent_messages "
            "WHERE conversation_id=? ORDER BY id ASC LIMIT ?",
            (conversation_id, max(1, min(100, limit))),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_smart_agent_message_status(
    settings: Settings,
    *,
    message_id: int,
    status: str,
    error: str = "",
) -> None:
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE smart_agent_messages
            SET status=?, error=?, processed_at=CASE WHEN ? IN ('done','failed') THEN ? ELSE processed_at END
            WHERE id=?
            """,
            (status, error[:500], status, now, int(message_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next_smart_agent_chat_message(settings: Settings) -> dict[str, Any] | None:
    """Claim one pending Smart Agent user message, never parallelizing a conversation."""
    now = int(time.time())
    stale_after = now - max(300, int(getattr(settings, "deepseek_chat_timeout_seconds", 180)) * 2)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE smart_agent_messages
            SET status='pending', processing_started_at=NULL, error='recovered_after_restart'
            WHERE role='user' AND status='processing'
              AND COALESCE(processing_started_at, 0) < ?
            """,
            (stale_after,),
        )
        row = conn.execute(
            """
            SELECT
                m.id AS message_id,
                m.conversation_id,
                m.content,
                m.safe_content,
                m.intent,
                m.client_request_id,
                c.conversation_code,
                c.legacy_user_id,
                c.account_id,
                COALESCE(NULLIF(a.display_name, ''), NULLIF(e.email_masked, ''), 'Smart Agent User') AS username
            FROM smart_agent_messages m
            JOIN smart_agent_conversations c ON c.id=m.conversation_id
            LEFT JOIN accounts a ON a.id=c.account_id
            LEFT JOIN account_email_identities e ON e.account_id=c.account_id
            WHERE m.role='user' AND m.status='pending'
              AND NOT EXISTS (
                  SELECT 1 FROM smart_agent_messages busy
                  WHERE busy.conversation_id=m.conversation_id
                    AND busy.role='user'
                    AND busy.status='processing'
              )
            ORDER BY m.created_at ASC, m.id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            "UPDATE smart_agent_messages SET status='processing', processing_started_at=?, error='' WHERE id=?",
            (now, int(row["message_id"])),
        )
        conn.execute(
            "UPDATE smart_agent_conversations SET updated_at=? WHERE id=?",
            (now, int(row["conversation_id"])),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_conversation_events(settings: Settings, *, conversation_id: int, after_id: int = 0) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT id, event_type, public_message, job_code, created_at FROM smart_agent_events "
            "WHERE conversation_id=? AND id > ? ORDER BY id ASC",
            (conversation_id, after_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def clear_conversation(settings: Settings, *, conversation_id: int) -> None:
    """清空对话消息、事件、记忆、草稿和人物状态，但保留 conversation 记录本身。"""
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM smart_agent_messages WHERE conversation_id=?",
            (conversation_id,),
        )
        conn.execute(
            "DELETE FROM smart_agent_events WHERE conversation_id=?",
            (conversation_id,),
        )
        # ⚠ 清空 prompt draft:防止旧人物状态污染新对话
        conn.execute(
            "DELETE FROM smart_agent_prompt_drafts WHERE conversation_id=?",
            (conversation_id,),
        )
        conn.execute(
            "UPDATE smart_agent_conversations SET memory_summary='', "
            "pending_character_term='', pending_character_candidates='', "
            "pending_original_request='', pending_constraints='', "
            "pending_disambiguation_json='', "
            "pending_disambiguation_at=NULL, updated_at=? WHERE id=?",
            (now, conversation_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_pending_disambiguation(
    settings: Settings,
    *,
    conversation_id: int,
    term: str,
    candidates: str,
    original_request: str,
    constraints: str,
) -> None:
    """保存待确认的人物歧义状态。"""
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_conversations SET "
            "pending_character_term=?, pending_character_candidates=?, "
            "pending_original_request=?, pending_constraints=?, "
            "pending_disambiguation_at=?, updated_at=? WHERE id=?",
            (
                str(term or "")[:200],
                str(candidates or "")[:4000],
                str(original_request or "")[:2000],
                str(constraints or "")[:2000],
                now, now, int(conversation_id),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_pending_disambiguation(settings: Settings, *, conversation_id: int) -> dict[str, Any] | None:
    """获取待确认的人物歧义状态。"""
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT pending_character_term, pending_character_candidates, "
            "pending_original_request, pending_constraints, pending_disambiguation_at "
            "FROM smart_agent_conversations WHERE id=? AND pending_disambiguation_at IS NOT NULL "
            "AND pending_character_candidates != ''",
            (int(conversation_id),),
        ).fetchone()
        if not row:
            return None
        term = str(row["pending_character_term"] or "")
        candidates_raw = str(row["pending_character_candidates"] or "")
        original_request = str(row["pending_original_request"] or "")
        constraints = str(row["pending_constraints"] or "")
        if not candidates_raw:
            return None
        try:
            candidates = json.loads(candidates_raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(candidates, list) or len(candidates) < 2:
            return None
        return {
            "term": term,
            "candidates": candidates,
            "original_request": original_request,
            "constraints": constraints,
            "disambiguation_at": int(row["pending_disambiguation_at"] or 0),
        }
    finally:
        conn.close()


def resolve_pending_disambiguation(
    settings: Settings,
    *,
    conversation_id: int,
    selected_character_key: str,
    selected_character_name: str,
) -> None:
    """标记歧义已解决（保留原始请求和约束供后续使用）。"""
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_conversations SET "
            "pending_character_term=?, pending_disambiguation_at=NULL, updated_at=? WHERE id=?",
            (f"resolved:{selected_character_key}", now, int(conversation_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_pending_disambiguation_json(
    settings: Settings,
    *,
    conversation_id: int,
    disambiguation_json: dict[str, Any],
) -> None:
    """保存新版 pending_disambiguation JSON 结构。"""
    import json as _json
    now = int(time.time())
    json_str = _json.dumps(disambiguation_json, ensure_ascii=False, separators=(",", ":"))
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_conversations SET "
            "pending_disambiguation_json=?, "
            "pending_disambiguation_at=?, updated_at=? WHERE id=?",
            (json_str, now, now, int(conversation_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_pending_disambiguation_json(
    settings: Settings, *, conversation_id: int
) -> dict[str, Any] | None:
    """获取新版 pending_disambiguation JSON。"""
    import json as _json
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT pending_disambiguation_json, pending_disambiguation_at "
            "FROM smart_agent_conversations WHERE id=? AND pending_disambiguation_at IS NOT NULL "
            "AND pending_disambiguation_json != ''",
            (int(conversation_id),),
        ).fetchone()
        if not row or not row["pending_disambiguation_json"]:
            return None
        try:
            data = _json.loads(str(row["pending_disambiguation_json"]))
        except (_json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict) or not data.get("groups"):
            return None
        # 检查是否过期（超过24小时）
        created_at = int(data.get("created_at", 0))
        if created_at and int(time.time()) - created_at > 86400:
            return None
        return data
    finally:
        conn.close()


def supersede_pending_disambiguation(
    settings: Settings, *, conversation_id: int
) -> None:
    """标记旧 pending 为 superseded。"""
    import json as _json
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 获取旧 pending
        row = conn.execute(
            "SELECT pending_disambiguation_json FROM smart_agent_conversations WHERE id=? AND pending_disambiguation_json != ''",
            (int(conversation_id),),
        ).fetchone()
        if row and row["pending_disambiguation_json"]:
            try:
                old = _json.loads(str(row["pending_disambiguation_json"]))
                old["status"] = "superseded"
                old["superseded_at"] = now
                conn.execute(
                    "UPDATE smart_agent_conversations SET pending_disambiguation_json=?, pending_disambiguation_at=NULL, updated_at=? WHERE id=?",
                    (_json.dumps(old, ensure_ascii=False, separators=(",", ":")), now, int(conversation_id)),
                )
            except _json.JSONDecodeError:
                # 无效 JSON，直接清除
                conn.execute(
                    "UPDATE smart_agent_conversations SET pending_disambiguation_json='', pending_disambiguation_at=NULL, updated_at=? WHERE id=?",
                    (now, int(conversation_id)),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_pending_disambiguation(settings: Settings, *, conversation_id: int) -> None:
    """清除歧义状态。"""
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE smart_agent_conversations SET "
            "pending_character_term='', pending_character_candidates='', "
            "pending_original_request='', pending_constraints='', "
            "pending_disambiguation_json='', "
            "pending_disambiguation_at=NULL, updated_at=? WHERE id=?",
            (now, int(conversation_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def backfill_image_to_conversation(
    settings: Settings,
    *,
    job_code: str,
    image_url: str,
    caption: str = "",
) -> bool:
    """将生成完成的图片回填到 Smart Agent 对话流中。

    从 generation_tasks.smart_agent_plan_json 中提取 conversation 信息，
    然后插入一条 image 消息和 image_generated 事件。

    返回 True 表示成功回填，False 表示该任务不关联任何 conversation。
    """
    import json as _json

    conn = connect(settings)
    try:
        # Read plan_json from the task
        row = conn.execute(
            "SELECT smart_agent_plan_json FROM generation_tasks WHERE job_code=?",
            (job_code,),
        ).fetchone()
        if not row or not row[0]:
            return False

        try:
            plan = _json.loads(row[0])
        except (_json.JSONDecodeError, TypeError):
            return False

        conv_id = plan.get("conversation_id")
        if not conv_id:
            return False

        output_rows = conn.execute(
            "SELECT id, label FROM generation_outputs WHERE job_code=? ORDER BY id ASC",
            (job_code,),
        ).fetchall()
        outputs = [
            {
                "url": f"/api/outputs/{row['id']}",
                "caption": row["label"] or (f"{idx + 1}/{len(output_rows)}" if len(output_rows) > 1 else ""),
            }
            for idx, row in enumerate(output_rows)
        ]
        if not outputs and image_url:
            outputs = [{"url": image_url, "caption": caption or ""}]

        img_content = _json.dumps({
            "url": outputs[0]["url"] if outputs else image_url,
            "outputs": outputs,
            "job_code": job_code,
            "caption": caption or "已经帮你生成好了喵～",
        }, ensure_ascii=False)

        # Insert image message into conversation
        now = int(time.time())
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO smart_agent_messages(conversation_id,role,content,safe_content,created_at) VALUES (?,?,?,?,?)",
            (conv_id, "image", img_content, img_content, now),
        )
        conn.execute(
            "UPDATE smart_agent_conversations SET updated_at=? WHERE id=?",
            (now, conv_id),
        )
        # Insert image_generated event so frontend polling picks it up
        conn.execute(
            "INSERT INTO smart_agent_events(conversation_id,job_code,event_type,public_message,private_detail,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (conv_id, job_code, "image_generated", img_content, "", now),
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _active_duration_seconds(item: dict[str, Any]) -> int | None:
    if item.get("status") != "done":
        return None
    started = item.get("active_started_at")
    finished = item.get("finished_at")
    if not started or not finished:
        return None
    try:
        duration = int(finished) - int(started)
    except (TypeError, ValueError):
        return None
    if duration < 0:
        print(f"[TASK_API] job={item.get('job_code')} invalid_active_duration", flush=True)
        return None
    return duration


def _append_active_duration(item: dict[str, Any]) -> dict[str, Any]:
    item["active_duration_seconds"] = _active_duration_seconds(item)
    return item


def safe_json_loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _normalize_task_item(item: dict[str, Any]) -> dict[str, Any]:
    item["loras"] = safe_json_loads(item.get("loras_json"), [])
    item["smart_agent_plan"] = safe_json_loads(item.get("smart_agent_plan_json"), {})
    item["workflow_key"] = item.get("workflow_key") or item.get("style_key") or ""
    if item.get("original_prompt") is None:
        item["original_prompt"] = item.get("prompt") or ""
    if item.get("effective_prompt") is None and not int(item.get("use_agent") or 0):
        item["effective_prompt"] = item.get("prompt") or ""
    return item


def list_user_tasks(settings: Settings, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT job_code,prompt,original_prompt,effective_prompt,use_agent,style_key,width,height,generation_mode,denoise,control_type,
                   control_character,auto_tagger,charged_fen,status,created_at,active_started_at,translating_started_at,started_at,finished_at,error,
                   agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,workflow_key,loras_json,prompt_source
            FROM generation_tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, min(max(limit, 1), 100)),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            _normalize_task_item(item)
            _append_active_duration(item)
            output_rows = conn.execute(
                "SELECT id,label,created_at FROM generation_outputs WHERE job_code=? ORDER BY id",
                (item["job_code"],),
            ).fetchall()
            item["outputs"] = [_serialize_output(x, idx) for idx, x in enumerate(output_rows, start=1)]
            result.append(item)
        return result
    finally:
        conn.close()


def get_latest_task(settings: Settings, user_id: str) -> dict[str, Any] | None:
    """Get the single most recent task for a user (stable ordering)."""
    conn = connect(settings)
    try:
        row = conn.execute(
            """
            SELECT job_code,prompt,original_prompt,effective_prompt,use_agent,style_key,width,height,generation_mode,denoise,control_type,
                   control_character,auto_tagger,charged_fen,status,created_at,active_started_at,translating_started_at,started_at,finished_at,error,
                   agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,workflow_key,loras_json,prompt_source
            FROM generation_tasks WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        _normalize_task_item(item)
        _append_active_duration(item)
        output_rows = conn.execute(
            "SELECT id,label,created_at FROM generation_outputs WHERE job_code=? ORDER BY id",
            (item["job_code"],),
        ).fetchall()
        item["outputs"] = [_serialize_output(x, idx) for idx, x in enumerate(output_rows, start=1)]
        return item
    finally:
        conn.close()


def get_task_by_job_code(settings: Settings, user_id: str, job_code: str) -> dict[str, Any] | None:
    """Get a specific task by job_code, scoped to the requesting user."""
    conn = connect(settings)
    try:
        row = conn.execute(
            """
            SELECT job_code,prompt,original_prompt,effective_prompt,use_agent,style_key,width,height,generation_mode,denoise,control_type,
                   control_character,auto_tagger,charged_fen,status,created_at,active_started_at,translating_started_at,started_at,finished_at,error,
                   agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,workflow_key,loras_json,prompt_source
            FROM generation_tasks WHERE job_code=? AND user_id=?
            """,
            (job_code.upper(), user_id),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        _normalize_task_item(item)
        _append_active_duration(item)
        output_rows = conn.execute(
            "SELECT id,label,created_at FROM generation_outputs WHERE job_code=? ORDER BY id",
            (item["job_code"],),
        ).fetchall()
        item["outputs"] = [_serialize_output(x, idx) for idx, x in enumerate(output_rows, start=1)]
        return item
    finally:
        conn.close()


def _queue_estimate_settings(settings: Settings) -> tuple[int, int, int]:
    per_job = max(1, int(settings.estimated_generation_seconds))
    agent = max(0, int(settings.estimated_agent_seconds))
    workers = max(1, int(settings.generation_worker_count))
    return per_job, agent, workers


def _processing_remaining_seconds(started_at: Any, now: int, per_job: int) -> int:
    if not started_at:
        return per_job
    try:
        elapsed = max(0, now - int(started_at))
    except (TypeError, ValueError):
        return per_job
    return max(MIN_ACTIVE_REMAINING_SECONDS, per_job - elapsed)


def _agent_remaining_seconds(started_at: Any, now: int, agent_seconds: int) -> int:
    if not started_at:
        return agent_seconds
    try:
        elapsed = max(0, now - int(started_at))
    except (TypeError, ValueError):
        return agent_seconds
    if agent_seconds <= 0:
        return 0
    return max(MIN_ACTIVE_REMAINING_SECONDS, agent_seconds - elapsed)


def _task_duration_seconds(row: Any, now: int, per_job: int, agent_seconds: int) -> int:
    status = str(row["status"])
    if status == "smart_planning":
        return max(1, agent_seconds) + per_job
    if status == "processing":
        return _processing_remaining_seconds(row["started_at"], now, per_job)
    if status == "translating":
        return _agent_remaining_seconds(row["translating_started_at"], now, agent_seconds) + per_job
    if status == "queued":
        needs_agent = bool(int(row["use_agent"] or 0)) and not row["effective_prompt"]
        return per_job + (agent_seconds if needs_agent else 0)
    return 0


def _estimate_wait_seconds(
    *,
    active_durations: list[int],
    workers: int,
) -> int:
    lane_count = max(1, workers)
    lanes = [0 for _ in range(lane_count)]
    for value in active_durations:
        index = min(range(lane_count), key=lambda i: lanes[i])
        lanes[index] += max(0, int(value))
    return min(lanes) if lanes else 0


def _schedule_duration_before_new(durations: list[int], workers: int) -> int:
    lane_count = max(1, workers)
    lanes = [0 for _ in range(lane_count)]
    for value in durations:
        index = min(range(lane_count), key=lambda i: lanes[i])
        lanes[index] += max(0, int(value))
    return min(lanes) if lanes else 0


def _active_task_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT rowid, status, created_at, use_agent, effective_prompt, translating_started_at, started_at, agent_mode
        FROM generation_tasks
        WHERE status IN ('smart_planning','translating','processing','queued')
        ORDER BY
          CASE status WHEN 'smart_planning' THEN 0 WHEN 'translating' THEN 1 WHEN 'processing' THEN 2 ELSE 3 END,
          created_at ASC,
          rowid ASC
        """
    ).fetchall()


def get_queue_status(settings: Settings) -> dict[str, int]:
    per_job, agent, workers = _queue_estimate_settings(settings)
    now = int(time.time())
    conn = connect(settings)
    try:
        queued_total = int(conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE status='queued'"
        ).fetchone()[0])
        smart_planning_count = int(conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE status='smart_planning'"
        ).fetchone()[0])
        translating_count = int(conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE status='translating'"
        ).fetchone()[0])
        processing_count = int(conn.execute(
            "SELECT COUNT(*) FROM generation_tasks WHERE status='processing'"
        ).fetchone()[0])
        active_rows = _active_task_rows(conn)
        active_durations = [_task_duration_seconds(row, now, per_job, agent) for row in active_rows]
        wait_seconds = _estimate_wait_seconds(
            active_durations=active_durations,
            workers=workers,
        )
        return {
            "queued_total": queued_total,
            "smart_planning_count": smart_planning_count,
            "translating_count": translating_count,
            "processing_count": processing_count,
            "estimated_wait_seconds": wait_seconds,
            "estimated_generation_seconds": per_job,
            "estimated_agent_seconds": agent,
            "estimated_total_seconds": wait_seconds + per_job,
            "worker_count": workers,
        }
    finally:
        conn.close()


def get_task_queue_position(settings: Settings, user_id: str, job_code: str) -> dict[str, int | str] | None:
    per_job, agent, workers = _queue_estimate_settings(settings)
    now = int(time.time())
    conn = connect(settings)
    try:
        row = conn.execute(
            """
            SELECT rowid, job_code, status, created_at, use_agent, effective_prompt, translating_started_at, started_at, agent_mode
            FROM generation_tasks
            WHERE job_code=? AND user_id=?
            """,
            (job_code.upper(), user_id),
        ).fetchone()
        if not row:
            return None

        status = row["status"]
        base = {
            "status": status,
            "estimated_generation_seconds": per_job,
            "estimated_agent_seconds": agent,
            "worker_count": workers,
        }
        if status == "smart_planning":
            remaining = _task_duration_seconds(row, now, per_job, agent)
            return {
                **base,
                "jobs_ahead": 0,
                "position": 0,
                "processing_count": 0,
                "estimated_wait_seconds": 0,
                "estimated_total_seconds": remaining,
            }
        if status == "queued":
            ahead_rows = conn.execute(
                """
                SELECT rowid, status, created_at, use_agent, effective_prompt, translating_started_at, started_at, agent_mode
                FROM generation_tasks
                WHERE status IN ('smart_planning','translating','processing')
                   OR (
                        status='queued'
                        AND (created_at < ? OR (created_at = ? AND rowid < ?))
                      )
                ORDER BY
                  CASE status WHEN 'smart_planning' THEN 0 WHEN 'translating' THEN 1 WHEN 'processing' THEN 2 ELSE 3 END,
                  created_at ASC,
                  rowid ASC
                """,
                (row["created_at"], row["created_at"], row["rowid"]),
            ).fetchall()
            jobs_ahead = len(ahead_rows)
            durations = [_task_duration_seconds(item, now, per_job, agent) for item in ahead_rows]
            wait_seconds = _estimate_wait_seconds(
                active_durations=durations,
                workers=workers,
            )
            own_seconds = _task_duration_seconds(row, now, per_job, agent)
            return {
                **base,
                "jobs_ahead": jobs_ahead,
                "position": jobs_ahead + 1,
                "processing_count": sum(1 for item in ahead_rows if item["status"] == "processing"),
                "estimated_wait_seconds": wait_seconds,
                "estimated_total_seconds": wait_seconds + own_seconds,
            }
        if status == "translating":
            remaining = _task_duration_seconds(row, now, per_job, agent)
            return {
                **base,
                "jobs_ahead": 0,
                "position": 0,
                "processing_count": 0,
                "estimated_wait_seconds": 0,
                "estimated_total_seconds": remaining,
            }
        if status == "processing":
            remaining = _processing_remaining_seconds(row["started_at"], now, per_job)
            return {
                **base,
                "jobs_ahead": 0,
                "position": 0,
                "processing_count": 1,
                "estimated_wait_seconds": 0,
                "estimated_total_seconds": remaining,
            }
        return {
            **base,
            "jobs_ahead": 0,
            "position": 0,
            "processing_count": 0,
            "estimated_wait_seconds": 0,
            "estimated_total_seconds": 0,
        }
    finally:
        conn.close()


def list_user_tasks_paginated(settings: Settings, user_id: str, limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], bool]:
    """Get paginated tasks for a user. Returns (items, has_more)."""
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT job_code,prompt,original_prompt,effective_prompt,use_agent,style_key,width,height,generation_mode,denoise,control_type,
                   control_character,auto_tagger,charged_fen,status,created_at,active_started_at,translating_started_at,started_at,finished_at,error,
                   agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,workflow_key,loras_json,prompt_source
            FROM generation_tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?
            """,
            (user_id, limit + 1, offset),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        result = []
        for row in rows:
            item = dict(row)
            _normalize_task_item(item)
            _append_active_duration(item)
            output_rows = conn.execute(
                "SELECT id,label,created_at FROM generation_outputs WHERE job_code=? ORDER BY id",
                (item["job_code"],),
            ).fetchall()
            item["outputs"] = [_serialize_output(x, idx) for idx, x in enumerate(output_rows, start=1)]
            result.append(item)
        return result, has_more
    finally:
        conn.close()


def _serialize_output(row: sqlite3.Row, output_index: int = 1) -> dict[str, Any]:
    item = dict(row)
    output_id = item["id"]
    label = (item.get("label") or "").strip()
    item["output_index"] = output_index
    item["output_label"] = label or f"输出 {output_index}"
    item["url"] = f"/api/outputs/{output_id}"
    item["download_url"] = f"/api/outputs/{output_id}/download"
    return item


def _append_outputs(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        _normalize_task_item(item)
        _append_active_duration(item)
        output_rows = conn.execute(
            "SELECT id,label,created_at FROM generation_outputs WHERE job_code=? ORDER BY id",
            (item["job_code"],),
        ).fetchall()
        item["outputs"] = [_serialize_output(x, idx) for idx, x in enumerate(output_rows, start=1)]
    return items


def list_user_tasks_filtered(
    settings: Settings,
    user_id: str,
    status_filter: str = "all",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    limit = min(max(int(limit), 1), 50)
    offset = max(int(offset), 0)
    status_filter = (status_filter or "all").lower()
    conn = connect(settings)
    try:
        select_sql = """
            SELECT job_code,prompt,original_prompt,effective_prompt,use_agent,style_key,width,height,generation_mode,denoise,control_type,
                   control_character,auto_tagger,charged_fen,status,created_at,active_started_at,translating_started_at,started_at,finished_at,error,
                   agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,workflow_key,loras_json,prompt_source
            FROM generation_tasks
            WHERE user_id=?
        """
        params: list[Any] = [user_id]
        if status_filter == "active":
            select_sql += " AND status IN ('smart_planning','queued','translating','processing')"
            order_sql = """
                ORDER BY
                  CASE status WHEN 'smart_planning' THEN 0 WHEN 'translating' THEN 1 WHEN 'processing' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END,
                  created_at ASC,
                  rowid ASC
            """
        elif status_filter == "completed":
            select_sql += " AND status IN ('done','failed_refunded','cancelled_refunded')"
            order_sql = "ORDER BY COALESCE(finished_at, created_at) DESC, rowid DESC"
        else:
            order_sql = """
                ORDER BY
                  CASE status WHEN 'smart_planning' THEN 0 WHEN 'translating' THEN 1 WHEN 'processing' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END,
                  COALESCE(finished_at, created_at) DESC,
                  rowid DESC
            """
        rows = conn.execute(
            f"{select_sql} {order_sql} LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        ).fetchall()
        has_more = len(rows) > limit
        items = [dict(row) for row in rows[:limit]]
        return _append_outputs(conn, items), has_more
    finally:
        conn.close()


def get_latest_relevant_task(settings: Settings, user_id: str) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            """
            SELECT job_code,prompt,original_prompt,effective_prompt,use_agent,style_key,width,height,generation_mode,denoise,control_type,
                   control_character,auto_tagger,charged_fen,status,created_at,active_started_at,translating_started_at,started_at,finished_at,error,
                   agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,workflow_key,loras_json,prompt_source
            FROM generation_tasks
            WHERE user_id=?
              AND status='done'
              AND EXISTS (SELECT 1 FROM generation_outputs WHERE generation_outputs.job_code = generation_tasks.job_code)
            ORDER BY
              COALESCE(finished_at, created_at) DESC,
              rowid DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if row:
            return _append_outputs(conn, [dict(row)])[0]

        row = conn.execute(
            """
            SELECT job_code,prompt,original_prompt,effective_prompt,use_agent,style_key,width,height,generation_mode,denoise,control_type,
                   control_character,auto_tagger,charged_fen,status,created_at,active_started_at,translating_started_at,started_at,finished_at,error,
                   agent_mode,smart_agent_request,smart_agent_plan_json,smart_agent_error,workflow_key,loras_json,prompt_source
            FROM generation_tasks
            WHERE user_id=? AND status IN ('smart_planning','queued','translating','processing')
            ORDER BY created_at ASC, rowid ASC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return _append_outputs(conn, [dict(row)])[0]
    finally:
        conn.close()


def get_user_task_summary(settings: Settings, user_id: str) -> dict[str, int]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM generation_tasks
            WHERE user_id=? AND status IN ('smart_planning','queued','translating','processing')
            GROUP BY status
            """,
            (user_id,),
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        queued = counts.get("queued", 0)
        smart_planning = counts.get("smart_planning", 0)
        translating = counts.get("translating", 0)
        processing = counts.get("processing", 0)
        return {
            "active_count": smart_planning + queued + translating + processing,
            "smart_planning_count": smart_planning,
            "queued_count": queued,
            "translating_count": translating,
            "processing_count": processing,
        }
    finally:
        conn.close()


def cancel_task_atomic(settings: Settings, user_id: str, job_code: str) -> tuple[int, str | None, int]:
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT charged_fen,status,input_image_path,client_request_id,prompt_source,"
            "fast_translation_request_code,translation_mode "
            "FROM generation_tasks WHERE job_code=? AND user_id=?",
            (job_code, user_id),
        ).fetchone()
        if not row:
            raise LookupError("任务不存在")
        row_dict = dict(row)
        current_status = str(row_dict.get("status") or "")
        if current_status not in {"queued", "translating"}:
            raise RuntimeError("只有 queued 或 translating 任务可以取消")
        charged_fen = int(row_dict["charged_fen"])
        translation_mode = str(row_dict.get("translation_mode") or "none")
        ft_code = str(row_dict.get("fast_translation_request_code") or "")

        cur = conn.execute(
            "UPDATE generation_tasks SET status='cancelled_refunded',finished_at=?,error='cancelled from web' "
            "WHERE job_code=? AND user_id=? AND status IN ('queued','translating')",
            (now, job_code, user_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("任务状态已变化，无法取消")
        total_refunded = 0
        if charged_fen:
            conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (charged_fen, user_id))
            conn.execute(
                "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) "
                "VALUES (?,?,'generate_cancel_refund',?,?,?)",
                (user_id, charged_fen, job_code, user_id, now),
            )
            total_refunded += charged_fen

        # Refund translation fee using explicit server-side binding
        # Use fast_translation_request_code (server-generated, trustworthy)
        # NOTE: For fast translation (translation_mode='fast'), charged_fen already
        # includes the translation cost, so we only update the translation request status
        # without additional refund.
        if ft_code:
            tr = conn.execute(
                "SELECT id,request_code,charged_credits,status FROM translation_requests "
                "WHERE request_code=? AND user_id=? AND generation_job_code=?",
                (ft_code, user_id, job_code),
            ).fetchone()
            if tr and str(tr["status"] or "") in {"queued", "processing", "done"}:
                if translation_mode != "fast":
                    # Only separately refund for old-style fast translations where
                    # charged_fen does NOT include translation cost
                    tr_charged = int(tr["charged_credits"] or 0)
                    if tr_charged:
                        existing_refund = conn.execute(
                            "SELECT id FROM balance_ledger WHERE order_code=? AND reason='fast_translate_cancel_refund' LIMIT 1",
                            (tr["request_code"],),
                        ).fetchone()
                        if not existing_refund:
                            conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (tr_charged, user_id))
                            conn.execute(
                                "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) "
                                "VALUES (?,?,'fast_translate_cancel_refund',?,?,?)",
                                (user_id, tr_charged, tr["request_code"], user_id, now),
                            )
                            total_refunded += tr_charged
                conn.execute(
                    "UPDATE translation_requests SET status='cancelled_refunded', finished_at=? WHERE id=?",
                    (now, tr["id"]),
                )

        conn.commit()
        # Read authoritative balance after commit
        bal_row = conn.execute("SELECT balance_fen FROM users WHERE user_id=?", (user_id,)).fetchone()
        new_balance = int(bal_row[0]) if bal_row else 0
        return total_refunded, row_dict["input_image_path"], new_balance
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_output_owned(settings: Settings, user_id: str, output_id: int) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            """
            SELECT o.id,o.job_code,o.label,o.file_path,o.created_at
            FROM generation_outputs o
            JOIN generation_tasks t ON t.job_code=o.job_code
            WHERE o.id=? AND t.user_id=?
            """,
            (int(output_id), user_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def make_image_refund_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "IRR-" + "".join(secrets.choice(alphabet) for _ in range(12))


def _summarize_text(value: str, limit: int = 180) -> str:
    clean = " ".join(str(value or "").split())
    return clean[:limit]


def _image_refund_output_items(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {
            "id": int(row["id"]),
            "label": row["label"] or "",
            "url": f"/api/outputs/{int(row['id'])}",
            "created_at": int(row["created_at"] or 0),
        }
        for row in rows
    ]


def _has_full_refund_for_job(conn: sqlite3.Connection, user_id: str, job_code: str, charged: int) -> bool:
    refunded = conn.execute(
        """
        SELECT COALESCE(SUM(amount_fen), 0)
        FROM balance_ledger
        WHERE user_id=? AND order_code=? AND amount_fen > 0
        """,
        (user_id, job_code),
    ).fetchone()[0]
    return int(refunded or 0) >= int(charged or 0)


def list_image_refund_eligible_tasks(settings: Settings, legacy_user_id: str, account_id: str) -> list[dict[str, Any]]:
    now = int(time.time())
    cutoff = now - max(1, int(settings.image_refund_window_hours or 24)) * 3600
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT job_code, original_prompt, effective_prompt, prompt, workflow_key, workflow_source,
                   charged_fen, status, created_at, finished_at, source
            FROM generation_tasks
            WHERE user_id=? AND status='done' AND charged_fen > 0
              AND COALESCE(finished_at, created_at) >= ?
              AND EXISTS (SELECT 1 FROM generation_outputs WHERE generation_outputs.job_code=generation_tasks.job_code)
              AND NOT EXISTS (SELECT 1 FROM image_refund_reviews WHERE image_refund_reviews.job_code=generation_tasks.job_code)
            ORDER BY COALESCE(finished_at, created_at) DESC
            LIMIT 50
            """,
            (legacy_user_id, cutoff),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            charged = int(row["charged_fen"] or 0)
            if _has_full_refund_for_job(conn, legacy_user_id, row["job_code"], charged):
                continue
            out_rows = conn.execute(
                "SELECT id,label,created_at FROM generation_outputs WHERE job_code=? ORDER BY id",
                (row["job_code"],),
            ).fetchall()
            items.append({
                "job_code": row["job_code"],
                "created_at": int(row["created_at"] or 0),
                "finished_at": int(row["finished_at"] or row["created_at"] or 0),
                "original_prompt_preview": _summarize_text(row["original_prompt"] or row["prompt"] or ""),
                "charged_credits": charged,
                "workflow_key": row["workflow_key"] or "",
                "workflow_source": row["workflow_source"] or "",
                "outputs": _image_refund_output_items(out_rows),
            })
        return items
    finally:
        conn.close()


def list_image_refunds_for_user(settings: Settings, account_id: str) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            """
            SELECT review_code, job_code, status, charged_credits, decision, severity_score, confidence,
                   public_reason, created_at, reviewed_at, refunded_at,
                   manual_review_requested_at, manual_review_decided_at, manual_review_decision,
                   manual_review_reason, manual_review_attempts
            FROM image_refund_reviews
            WHERE account_id=?
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (str(account_id),),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["can_request_manual_review"] = _can_request_manual_review(item)
            items.append(item)
        return items
    finally:
        conn.close()


def get_image_refund_for_user(settings: Settings, account_id: str, review_code: str) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM image_refund_reviews WHERE review_code=? AND account_id=?",
            (review_code, str(account_id)),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["can_request_manual_review"] = _can_request_manual_review(item)
        item["outputs"] = []
        try:
            output_ids = json.loads(item.get("output_ids_json") or "[]")
        except json.JSONDecodeError:
            output_ids = []
        for output_id in output_ids:
            item["outputs"].append({"id": int(output_id), "url": f"/api/outputs/{int(output_id)}"})
        return item
    finally:
        conn.close()


def request_manual_image_refund_review(
    settings: Settings,
    *,
    account_id: str,
    review_code: str,
    user_note: str = "",
) -> dict[str, Any]:
    now = int(time.time())
    code = review_code.upper().strip()
    note = _summarize_text(user_note, 500)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM image_refund_reviews WHERE review_code=? AND account_id=?",
            (code, str(account_id)),
        ).fetchone()
        if not row:
            raise LookupError("审核记录不存在")
        item = dict(row)
        status = str(item.get("status") or "")
        if status == "manual_rejected":
            raise RuntimeError("manual_review_final_rejected")
        if status in {"refunded", "refund_completed"} or item.get("refunded_at"):
            raise RuntimeError("该任务已退款完成")
        if int(item.get("manual_review_attempts") or 0) >= 1:
            conn.commit()
            item["already_requested"] = True
            item["can_request_manual_review"] = False
            return item
        if not _can_request_manual_review(item):
            raise RuntimeError("manual_review_not_available")
        merged_note = note or str(item.get("manual_review_reason") or "")
        conn.execute(
            """
            UPDATE image_refund_reviews
            SET status='manual_review_requested',
                manual_review_requested_at=?,
                manual_review_attempts=1,
                manual_review_reason=?,
                public_reason='已提交人工复审，等待管理员处理。',
                updated_at=?
            WHERE id=? AND manual_review_attempts < 1
            """,
            (now, merged_note, now, item["id"]),
        )
        record_admin_audit(conn, str(account_id), str(account_id), "image_refund_manual_review_requested")
        updated = conn.execute("SELECT * FROM image_refund_reviews WHERE id=?", (item["id"],)).fetchone()
        conn.commit()
        result = dict(updated)
        result["already_requested"] = False
        result["can_request_manual_review"] = False
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_image_refund_review(
    settings: Settings,
    *,
    account_id: str,
    legacy_user_id: str,
    job_code: str,
    user_note: str,
) -> dict[str, Any]:
    code = job_code.upper().strip()
    now = int(time.time())
    cutoff = now - max(1, int(settings.image_refund_window_hours or 24)) * 3600
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM image_refund_reviews WHERE job_code=?", (code,)).fetchone()
        if existing:
            if str(existing["account_id"]) != str(account_id):
                raise LookupError("任务不存在")
            if str(existing["status"] or "") == "manual_rejected":
                raise RuntimeError("manual_review_final_rejected")
            if str(existing["status"] or "") == "refunded" or existing["refunded_at"]:
                raise RuntimeError("该任务已经退款，不能重复申请")
            conn.commit()
            return {"created": False, "review": dict(existing)}
        row = conn.execute(
            """
            SELECT job_code,user_id,original_prompt,effective_prompt,prompt,workflow_key,workflow_source,
                   charged_fen,status,created_at,finished_at,source
            FROM generation_tasks WHERE job_code=? AND user_id=?
            """,
            (code, str(legacy_user_id)),
        ).fetchone()
        if not row or row["status"] != "done":
            raise LookupError("任务不存在")
        charged = int(row["charged_fen"] or 0)
        if charged <= 0:
            raise RuntimeError("该任务没有扣除 credits，不能申请退款")
        if int(row["finished_at"] or row["created_at"] or 0) < cutoff:
            raise RuntimeError("该任务已超过退款申请时限")
        if _has_full_refund_for_job(conn, str(legacy_user_id), code, charged):
            raise RuntimeError("该任务已经退款，不能重复申请")
        output_rows = conn.execute(
            "SELECT id,label,created_at FROM generation_outputs WHERE job_code=? ORDER BY id",
            (code,),
        ).fetchall()
        if not output_rows:
            raise RuntimeError("该任务没有可审核的输出图片")
        review_code = make_image_refund_code()
        output_ids = [int(out["id"]) for out in output_rows]
        conn.execute(
            """
            INSERT INTO image_refund_reviews(
                review_code,account_id,legacy_user_id,job_code,status,reviewer_model,reviewer_version,
                output_ids_json,user_note,original_request_snapshot,final_prompt_snapshot,charged_credits,
                attempt_count,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                review_code,
                str(account_id),
                int(legacy_user_id) if str(legacy_user_id).lstrip("-").isdigit() else None,
                code,
                "pending",
                "",
                "",
                json.dumps(output_ids, separators=(",", ":")),
                _summarize_text(user_note, 500),
                _summarize_text(row["original_prompt"] or row["prompt"] or "", 1000),
                _summarize_text(row["effective_prompt"] or row["prompt"] or "", 1500),
                charged,
                0,
                now,
                now,
            ),
        )
        created = conn.execute("SELECT * FROM image_refund_reviews WHERE review_code=?", (review_code,)).fetchone()
        conn.commit()
        return {"created": True, "review": dict(created)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next_image_refund_review(settings: Settings) -> dict[str, Any] | None:
    now = int(time.time())
    stale = now - 10 * 60
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM image_refund_reviews
            WHERE (status='pending' OR (status='reviewing' AND COALESCE(claimed_at,0) < ?))
              AND attempt_count < 3
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (stale,),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            "UPDATE image_refund_reviews SET status='reviewing', claimed_at=?, attempt_count=attempt_count+1, updated_at=? WHERE id=?",
            (now, now, row["id"]),
        )
        updated = conn.execute("SELECT * FROM image_refund_reviews WHERE id=?", (row["id"],)).fetchone()
        conn.commit()
        return dict(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_auto_refund_status(status: str) -> str:
    value = str(status or "").strip()
    if value == "rejected":
        return "auto_rejected"
    if value == "manual_review":
        return "manual_review_available"
    return value


def _can_request_manual_review(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if status in {"refunded", "refund_completed", "manual_rejected", "refund_pending"}:
        return False
    if int(item.get("manual_review_attempts") or 0) >= 1:
        return False
    return status in {"auto_rejected", "auto_failed", "manual_review_available", "manual_review", "rejected", "failed", "error"}


def save_image_refund_review_result(settings: Settings, review_code: str, result: dict[str, Any], *, status: str) -> None:
    now = int(time.time())
    status = _normalize_auto_refund_status(status)
    decision = str(result.get("decision") or "manual_review")[:32]
    reason_codes = result.get("reason_codes") if isinstance(result.get("reason_codes"), list) else []
    public_reason = _summarize_text(str(result.get("public_reason_zh") or result.get("public_reason") or ""), 120)
    conn = connect(settings)
    try:
        conn.execute(
            """
            UPDATE image_refund_reviews
            SET status=?, reviewer_model=?, reviewer_version=?, decision=?, severity_score=?, confidence=?, reason_codes_json=?,
                public_reason=?, review_result_json=?, reviewed_at=?, updated_at=?
            WHERE review_code=?
            """,
            (
                status,
                str(settings.mimo_image_review_model or "")[:80],
                "mimo_review_v1",
                decision,
                int(result.get("severity_score") or 0),
                float(result.get("confidence") or 0.0),
                json.dumps(reason_codes, ensure_ascii=False, separators=(",", ":")),
                public_reason,
                json.dumps({k: result.get(k) for k in (
                    "decision", "all_outputs_severely_deformed", "severity_score", "confidence",
                    "reason_codes", "minor_only", "six_fingers_only", "usable_output_exists", "public_reason_zh"
                )}, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
                review_code,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def refund_image_review_atomic(settings: Settings, review_code: str, operator_id: str) -> dict[str, Any]:
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM image_refund_reviews WHERE review_code=?", (review_code,)).fetchone()
        if not row:
            raise LookupError("审核记录不存在")
        if row["refunded_at"] or row["refund_ledger_id"]:
            conn.commit()
            return {"refunded": False, "already_refunded": True, "amount": int(row["charged_credits"] or 0)}
        charged = int(row["charged_credits"] or 0)
        if charged <= 0:
            raise RuntimeError("退款金额无效")
        legacy_user_id = str(row["legacy_user_id"])
        job_code = str(row["job_code"])
        if _has_full_refund_for_job(conn, legacy_user_id, job_code, charged):
            conn.execute(
                "UPDATE image_refund_reviews SET status='refunded', refunded_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            conn.commit()
            return {"refunded": False, "already_refunded": True, "amount": charged}
        order_code = f"{review_code}:{job_code}"
        existing = conn.execute(
            "SELECT id FROM balance_ledger WHERE order_code=? AND reason=?",
            (order_code, SEVERE_DEFORMATION_REFUND_REASON),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE image_refund_reviews SET status='refunded', refunded_at=?, refund_ledger_id=?, updated_at=? WHERE id=?",
                (now, existing["id"], now, row["id"]),
            )
            conn.commit()
            return {"refunded": False, "already_refunded": True, "amount": charged}
        conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (charged, legacy_user_id))
        cur = conn.execute(
            "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) VALUES (?,?,?,?,?,?)",
            (legacy_user_id, charged, SEVERE_DEFORMATION_REFUND_REASON, order_code, operator_id, now),
        )
        ledger_id = cur.lastrowid
        conn.execute(
            "UPDATE image_refund_reviews SET status='refunded', refunded_at=?, refund_ledger_id=?, updated_at=?, manual_review_decided_at=COALESCE(manual_review_decided_at, ?), manual_review_decision=CASE WHEN manual_review_attempts > 0 THEN 'approved' ELSE manual_review_decision END, manual_review_admin_id=CASE WHEN manual_review_attempts > 0 THEN ? ELSE manual_review_admin_id END WHERE id=?",
            (now, ledger_id, now, now, operator_id[:120], row["id"]),
        )
        conn.commit()
        return {"refunded": True, "already_refunded": False, "amount": charged, "ledger_id": ledger_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_image_refunds_admin(settings: Settings, status: str = "", limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    conn = connect(settings)
    try:
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE status=?"
            params.append(status)
        params.extend([min(max(int(limit), 1), 200), max(int(offset), 0)])
        rows = conn.execute(
            f"SELECT * FROM image_refund_reviews {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_image_refund_admin(settings: Settings, review_code: str) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT * FROM image_refund_reviews WHERE review_code=?", (review_code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_image_refund_status(settings: Settings, review_code: str, status: str, *, public_reason: str = "") -> dict[str, Any]:
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE image_refund_reviews SET status=?, public_reason=COALESCE(NULLIF(?, ''), public_reason), updated_at=? WHERE review_code=?",
            (status, _summarize_text(public_reason, 120), now, review_code),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM image_refund_reviews WHERE review_code=?", (review_code,)).fetchone()
        if not row:
            raise LookupError("审核记录不存在")
        return dict(row)
    finally:
        conn.close()


def reject_manual_image_refund_review(
    settings: Settings,
    review_code: str,
    *,
    operator_id: str,
    public_reason: str = "",
) -> dict[str, Any]:
    now = int(time.time())
    reason = _summarize_text(public_reason or "人工复审未通过，不能再次申请退款。", 120)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM image_refund_reviews WHERE review_code=?", (review_code,)).fetchone()
        if not row:
            raise LookupError("审核记录不存在")
        item = dict(row)
        status = str(item.get("status") or "")
        if status in {"refunded", "refund_completed"} or item.get("refunded_at"):
            raise RuntimeError("refund_final_state_locked")
        if status in {"manual_rejected", "refund_rejected"}:
            conn.commit()
            return item
        conn.execute(
            """
            UPDATE image_refund_reviews
            SET status='manual_rejected',
                public_reason=?,
                manual_review_decided_at=?,
                manual_review_decision='rejected',
                manual_review_admin_id=?,
                updated_at=?
            WHERE id=?
            """,
            (reason, now, str(operator_id or ""), now, item["id"]),
        )
        updated = conn.execute("SELECT * FROM image_refund_reviews WHERE id=?", (item["id"],)).fetchone()
        conn.commit()
        return dict(updated)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user_settings_row(settings: Settings, user_id: str) -> dict[str, Any] | None:
    """Get user_settings row for a user."""
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT style_key, lora_weight, last_width, last_height, updated_at FROM user_settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_user_settings(
    settings: Settings, user_id: str, *, style_key: str, lora_weight: float, width: int, height: int,
) -> dict[str, Any]:
    """Validate and save user settings. Returns saved dict."""
    # Validate style_key
    if style_key not in STYLE_BY_KEY:
        raise ValueError(f"未知画风: {style_key}")

    # Validate lora_weight range
    lora_weight = float(lora_weight)
    if not (0.0 <= lora_weight <= 2.0):
        raise ValueError("LoRA 权重必须在 0.0 到 2.0 之间")

    # Validate resolution
    width, height = validate_resolution(width, height)

    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute(
            """
            INSERT INTO user_settings(user_id, style_key, lora_weight, last_width, last_height, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                style_key=excluded.style_key,
                lora_weight=excluded.lora_weight,
                last_width=excluded.last_width,
                last_height=excluded.last_height,
                updated_at=excluded.updated_at
            """,
            (user_id, style_key, lora_weight, width, height, now),
        )
        conn.commit()
        return {
            "style_key": style_key,
            "lora_weight": lora_weight,
            "last_width": width,
            "last_height": height,
            "updated_at": now,
        }
    finally:
        conn.close()


def safe_cleanup_input(settings: Settings, path_text: str | None) -> None:
    if not path_text:
        return
    try:
        path = Path(path_text).resolve()
        base = settings.input_image_dir.resolve()
        if path.is_file() and base in path.parents:
            path.unlink(missing_ok=True)
    except Exception:
        pass


# =====================
# Internal account / legacy binding functions
# =====================
def find_or_create_discord_account(
    conn: sqlite3.Connection,
    discord_id: str,
    display_name: str,
    avatar_hash: str | None = None,
    avatar_url: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    row = conn.execute(
        "SELECT id, provider, provider_user_id, display_name, display_username, avatar_url, avatar_hash, created_at, updated_at, last_login_at "
        "FROM accounts WHERE provider='discord' AND provider_user_id=?",
        (discord_id,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE accounts SET display_name=?, avatar_url=?, avatar_hash=?, updated_at=?, last_login_at=? WHERE id=?",
            (display_name[:120], avatar_url, avatar_hash, now, now, row["id"]),
        )
        return {
            "id": row["id"],
            "provider": "discord",
            "provider_user_id": discord_id,
            "display_name": display_name[:120],
            "display_username": row["display_username"],
            "avatar_url": avatar_url,
            "avatar_hash": avatar_hash,
            "created_at": row["created_at"],
            "updated_at": now,
            "last_login_at": now,
            "is_new": False,
        }

    account_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO accounts(id, provider, provider_user_id, display_name, avatar_url, avatar_hash, created_at, updated_at, last_login_at) "
        "VALUES (?, 'discord', ?, ?, ?, ?, ?, ?, ?)",
        (account_id, discord_id, display_name[:120], avatar_url, avatar_hash, now, now, now),
    )
    return {
        "id": account_id,
        "provider": "discord",
        "provider_user_id": discord_id,
        "display_name": display_name[:120],
        "display_username": None,
        "avatar_url": avatar_url,
        "avatar_hash": avatar_hash,
        "created_at": now,
        "updated_at": now,
        "last_login_at": now,
        "is_new": True,
    }


def bind_account_legacy_user(conn: sqlite3.Connection, account_id: str, legacy_user_id: str) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO account_legacy_bindings(account_id, legacy_user_id, created_at) VALUES (?, ?, ?)",
        (account_id, legacy_user_id, now),
    )


def get_account_legacy_user_id(conn: sqlite3.Connection, account_id: str) -> str | None:
    row = conn.execute(
        "SELECT legacy_user_id FROM account_legacy_bindings WHERE account_id=?",
        (account_id,),
    ).fetchone()
    return row[0] if row else None


WELCOME_BONUS_CREDITS = 10
WELCOME_BONUS_REASON = "welcome_bonus"
REFERRAL_INVITEE_REASON = "referral_invitee_bonus"
REFERRAL_INVITER_REASON = "referral_inviter_bonus"
REFERRAL_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
RESERVED_DISPLAY_USERNAMES = {
    "admin",
    "administrator",
    "system",
    "support",
    "小击击官方",
    "管理员",
}


def normalize_display_username(value: str) -> tuple[str, str]:
    display = str(value or "").strip()
    if len(display) < 2 or len(display) > 20:
        raise ValueError("用户名长度必须为 2～20 个字符。")
    if any(ord(ch) < 32 for ch in display) or any(ch in "<>\"'`" for ch in display):
        raise ValueError("用户名包含不支持的字符。")
    if not re.fullmatch(r"[\w\-\u4e00-\u9fff]+", display, flags=re.UNICODE):
        raise ValueError("用户名只能包含中文、英文字母、数字、下划线和短横线。")
    normalized = display.casefold()
    if normalized in RESERVED_DISPLAY_USERNAMES:
        raise ValueError("该用户名不可使用。")
    return display, normalized


def display_label_from_account(account: dict[str, Any] | sqlite3.Row | None) -> str:
    if not account:
        return "用户"
    custom = str(account["display_username"] or "").strip() if "display_username" in account.keys() else ""
    if custom:
        return custom
    provider = str(account["provider"] or "")
    if provider == "email":
        masked = account["email_masked"] if "email_masked" in account.keys() else ""
        return str(masked or account["display_name"] or "邮箱用户")
    return str(account["display_name"] or "Discord 用户")


def set_account_display_username(conn: sqlite3.Connection, account_id: str, value: str) -> dict[str, Any]:
    display, normalized = normalize_display_username(value)
    now = int(time.time())
    try:
        cur = conn.execute(
            """
            UPDATE accounts
            SET display_username=?, display_username_normalized=?, display_username_updated_at=?, updated_at=?
            WHERE id=?
            """,
            (display, normalized, now, now, account_id),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("该用户名已被使用。") from exc
    if cur.rowcount != 1:
        raise LookupError("account_not_found")
    return {"display_username": display, "updated_at": now}


def generate_referral_code(length: int = 8) -> str:
    return "".join(secrets.choice(REFERRAL_CODE_ALPHABET) for _ in range(length))


def normalize_referral_code(value: str | None) -> str:
    code = str(value or "").strip().upper().replace(" ", "")
    if not code:
        return ""
    if len(code) > 32 or any(ch not in REFERRAL_CODE_ALPHABET for ch in code):
        raise ValueError("邀请码无效，请检查后重新输入。")
    return code


def get_or_create_referral_code(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT referral_code, created_at FROM referral_codes WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if row:
        return {"referral_code": row["referral_code"], "created_at": int(row["created_at"]), "created": False}
    if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        raise LookupError("account_not_found")
    now = int(time.time())
    for length in (8, 9, 10):
        for _ in range(24):
            code = generate_referral_code(length)
            try:
                conn.execute(
                    "INSERT INTO referral_codes(account_id, referral_code, created_at) VALUES (?, ?, ?)",
                    (account_id, code, now),
                )
                return {"referral_code": code, "created_at": now, "created": True}
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("邀请码生成失败，请稍后重试。")


def get_referral_code(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT referral_code, created_at FROM referral_codes WHERE account_id=?",
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


def get_referral_stats(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS invited_count,
            COALESCE(SUM(CASE WHEN status='rewarded' THEN inviter_reward_credits ELSE 0 END), 0) AS reward_credits
        FROM referral_relationships
        WHERE inviter_account_id=?
        """,
        (account_id,),
    ).fetchone()
    return {
        "invited_count": int(row["invited_count"] or 0) if row else 0,
        "inviter_reward_credits": int(row["reward_credits"] or 0) if row else 0,
    }


def apply_referral_bonus_for_new_account(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    invitee_account_id: str,
    invite_code: str | None,
) -> dict[str, Any]:
    if not settings.referral_campaign_enabled:
        return {"applied": False, "reason": "campaign_disabled"}
    code = normalize_referral_code(invite_code)
    if not code:
        return {"applied": False, "reason": "empty_code"}
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    inviter_code = conn.execute(
        "SELECT account_id, referral_code FROM referral_codes WHERE referral_code=?",
        (code,),
    ).fetchone()
    if not inviter_code:
        raise ValueError("邀请码无效，请检查后重新输入。")
    inviter_account_id = str(inviter_code["account_id"])
    if inviter_account_id == invitee_account_id:
        raise ValueError("不能使用自己的邀请码。")
    existing = conn.execute(
        "SELECT id, status FROM referral_relationships WHERE invitee_account_id=?",
        (invitee_account_id,),
    ).fetchone()
    if existing:
        return {"applied": False, "reason": "already_referred", "relationship_id": int(existing["id"])}

    inviter_legacy_id = get_account_legacy_user_id(conn, inviter_account_id)
    invitee_legacy_id = get_account_legacy_user_id(conn, invitee_account_id)
    if not inviter_legacy_id or not invitee_legacy_id:
        raise RuntimeError("邀请奖励账户绑定缺失。")
    invitee_bonus = max(0, int(settings.referral_invitee_bonus_credits or 0))
    inviter_bonus = max(0, int(settings.referral_inviter_bonus_credits or 0))
    if invitee_bonus <= 0 and inviter_bonus <= 0:
        return {"applied": False, "reason": "bonus_disabled"}

    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO referral_relationships(
            inviter_account_id, invitee_account_id, referral_code_used,
            inviter_reward_credits, invitee_reward_credits, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (inviter_account_id, invitee_account_id, code, inviter_bonus, invitee_bonus, now),
    )
    relationship_id = int(cur.lastrowid)
    order_code = f"referral:{invitee_account_id}"
    conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (inviter_legacy_id,))
    conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (invitee_legacy_id,))

    invitee_ledger_id = None
    inviter_ledger_id = None
    if invitee_bonus > 0:
        cur = conn.execute(
            "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
            "VALUES (?, ?, ?, ?, 'system', ?)",
            (invitee_legacy_id, invitee_bonus, REFERRAL_INVITEE_REASON, order_code, now),
        )
        invitee_ledger_id = int(cur.lastrowid)
        conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (invitee_bonus, invitee_legacy_id))
    if inviter_bonus > 0:
        cur = conn.execute(
            "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
            "VALUES (?, ?, ?, ?, 'system', ?)",
            (inviter_legacy_id, inviter_bonus, REFERRAL_INVITER_REASON, order_code, now),
        )
        inviter_ledger_id = int(cur.lastrowid)
        conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (inviter_bonus, inviter_legacy_id))
    conn.execute(
        """
        UPDATE referral_relationships
        SET status='rewarded', rewarded_at=?, inviter_ledger_id=?, invitee_ledger_id=?
        WHERE id=?
        """,
        (now, inviter_ledger_id, invitee_ledger_id, relationship_id),
    )
    return {
        "applied": True,
        "relationship_id": relationship_id,
        "invitee_bonus_credits": invitee_bonus,
        "inviter_bonus_credits": inviter_bonus,
    }


def has_seen_referral_campaign(conn: sqlite3.Connection, account_id: str, version: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM referral_campaign_seen WHERE account_id=? AND campaign_version=?",
        (account_id, version),
    ).fetchone()
    return bool(row)


def mark_referral_campaign_seen(conn: sqlite3.Connection, account_id: str, version: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO referral_campaign_seen(account_id, campaign_version, seen_at) VALUES (?, ?, ?)",
        (account_id, version, int(time.time())),
    )


def grant_welcome_bonus_if_needed(
    conn: sqlite3.Connection,
    account_id: str,
    *,
    legacy_user_id: str | None = None,
    amount_credits: int = WELCOME_BONUS_CREDITS,
) -> dict[str, Any]:
    """Grant the one-time welcome credits for an account inside the caller's transaction."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    now = int(time.time())
    account = conn.execute(
        "SELECT id, welcome_credits_granted_at FROM accounts WHERE id=?",
        (account_id,),
    ).fetchone()
    if not account:
        return {"granted": False, "reason": "account_missing", "amount_credits": 0}
    if account["welcome_credits_granted_at"]:
        return {"granted": False, "reason": "already_marked", "amount_credits": 0}

    legacy_id = legacy_user_id or get_account_legacy_user_id(conn, account_id)
    if not legacy_id:
        return {"granted": False, "reason": "legacy_missing", "amount_credits": 0}
    existing = conn.execute(
        "SELECT id FROM balance_ledger WHERE user_id=? AND reason=? LIMIT 1",
        (legacy_id, WELCOME_BONUS_REASON),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE accounts SET welcome_credits_granted_at=?, updated_at=? WHERE id=? AND welcome_credits_granted_at IS NULL",
            (now, now, account_id),
        )
        return {"granted": False, "reason": "already_ledgered", "amount_credits": 0}

    amount = int(amount_credits)
    if amount <= 0:
        return {"granted": False, "reason": "invalid_amount", "amount_credits": 0}
    conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (legacy_id,))
    try:
        conn.execute(
            "INSERT INTO balance_ledger(user_id, amount_fen, reason, order_code, operator_id, created_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (legacy_id, amount, WELCOME_BONUS_REASON, "system", now),
        )
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE accounts SET welcome_credits_granted_at=?, updated_at=? WHERE id=? AND welcome_credits_granted_at IS NULL",
            (now, now, account_id),
        )
        return {"granted": False, "reason": "already_ledgered", "amount_credits": 0}
    conn.execute("UPDATE users SET balance_fen = balance_fen + ? WHERE user_id=?", (amount, legacy_id))
    conn.execute(
        "UPDATE accounts SET welcome_credits_granted_at=?, updated_at=? WHERE id=?",
        (now, now, account_id),
    )
    return {"granted": True, "reason": WELCOME_BONUS_REASON, "amount_credits": amount}


def get_account_by_id(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, provider, provider_user_id, display_name, display_username, avatar_url, avatar_hash, created_at, updated_at, last_login_at "
        "FROM accounts WHERE id=?",
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


def create_account_session(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    session_id_hash: str,
    csrf_token_hash: str,
    provider: str,
    expires_at: int,
    user_agent_hash: str | None,
    ip_hash: str | None,
    max_active_sessions: int,
) -> dict[str, Any]:
    now = int(time.time())
    session_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO account_sessions(
            id, account_id, session_id_hash, csrf_token_hash, provider,
            created_at, last_seen_at, expires_at, revoked_at, user_agent_hash, ip_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            session_id,
            account_id,
            session_id_hash,
            csrf_token_hash,
            provider,
            now,
            now,
            int(expires_at),
            user_agent_hash,
            ip_hash,
        ),
    )
    cleanup_account_sessions(conn, now)
    enforce_account_session_limit(conn, account_id, max_active_sessions, now)
    return {
        "id": session_id,
        "account_id": account_id,
        "session_id_hash": session_id_hash,
        "csrf_token_hash": csrf_token_hash,
        "provider": provider,
        "created_at": now,
        "last_seen_at": now,
        "expires_at": int(expires_at),
        "revoked_at": None,
    }


def cleanup_account_sessions(conn: sqlite3.Connection, now: int | None = None) -> None:
    now = int(now or time.time())
    conn.execute(
        "UPDATE account_sessions SET revoked_at=? WHERE revoked_at IS NULL AND expires_at<=?",
        (now, now),
    )


def enforce_account_session_limit(
    conn: sqlite3.Connection,
    account_id: str,
    max_active_sessions: int,
    now: int | None = None,
) -> None:
    now = int(now or time.time())
    keep_count = max(1, int(max_active_sessions or 10))
    rows = conn.execute(
        """
        SELECT id
        FROM account_sessions
        WHERE account_id=? AND revoked_at IS NULL AND expires_at>?
        ORDER BY last_seen_at DESC, created_at DESC
        """,
        (account_id, now),
    ).fetchall()
    revoke_ids = [row["id"] for row in rows[keep_count:]]
    if not revoke_ids:
        return
    placeholders = ",".join("?" for _ in revoke_ids)
    conn.execute(
        f"UPDATE account_sessions SET revoked_at=? WHERE id IN ({placeholders})",
        (now, *revoke_ids),
    )


def get_active_account_session(
    conn: sqlite3.Connection,
    *,
    session_id_hash: str,
    now: int | None = None,
) -> dict[str, Any] | None:
    now = int(now or time.time())
    row = conn.execute(
        """
        SELECT
            s.id AS session_row_id,
            s.account_id,
            s.session_id_hash,
            s.csrf_token_hash,
            s.provider AS session_provider,
            s.created_at AS session_created_at,
            s.last_seen_at,
            s.expires_at,
            s.revoked_at,
            a.provider,
            a.provider_user_id,
            a.display_name,
            a.display_username,
            a.avatar_url,
            a.avatar_hash,
            a.created_at,
            a.updated_at,
            a.last_login_at
        FROM account_sessions s
        JOIN accounts a ON a.id=s.account_id
        WHERE s.session_id_hash=?
          AND s.revoked_at IS NULL
          AND s.expires_at>?
        """,
        (session_id_hash, now),
    ).fetchone()
    return dict(row) if row else None


def touch_account_session(
    conn: sqlite3.Connection,
    *,
    session_id_hash: str,
    now: int | None = None,
    throttle_seconds: int = 300,
) -> None:
    now = int(now or time.time())
    conn.execute(
        """
        UPDATE account_sessions
        SET last_seen_at=?
        WHERE session_id_hash=?
          AND revoked_at IS NULL
          AND expires_at>?
          AND last_seen_at<?
        """,
        (now, session_id_hash, now, now - int(throttle_seconds)),
    )


def revoke_account_session(conn: sqlite3.Connection, *, session_id_hash: str, now: int | None = None) -> None:
    now = int(now or time.time())
    conn.execute(
        "UPDATE account_sessions SET revoked_at=? WHERE session_id_hash=? AND revoked_at IS NULL",
        (now, session_id_hash),
    )


def revoke_all_account_sessions(conn: sqlite3.Connection, *, account_id: str, now: int | None = None) -> int:
    now = int(now or time.time())
    cur = conn.execute(
        "UPDATE account_sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL",
        (now, account_id),
    )
    return int(cur.rowcount or 0)


def validate_session_csrf(
    conn: sqlite3.Connection,
    *,
    session_id_hash: str,
    csrf_token_hash: str,
    now: int | None = None,
) -> bool:
    now = int(now or time.time())
    row = conn.execute(
        """
        SELECT 1
        FROM account_sessions
        WHERE session_id_hash=?
          AND csrf_token_hash=?
          AND revoked_at IS NULL
          AND expires_at>?
        """,
        (session_id_hash, csrf_token_hash, now),
    ).fetchone()
    return bool(row)


def get_account_identities(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    row = get_account_by_id(conn, account_id)
    if not row:
        return []
    if row["provider"] == "email":
        email_row = get_email_identity_for_account(conn, account_id)
        return [{
            "provider": "email",
            "email_masked": email_row["email_masked"] if email_row else row["display_name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }]
    return [{
        "provider": row["provider"],
        "provider_user_id": row["provider_user_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }]
# =====================
# Secure email auth functions
# =====================
def normalize_email_for_identity(email: str) -> str:
    normalized = email.strip().lower()
    if len(normalized) > 254:
        raise ValueError("邮箱地址过长")
    local, sep, domain = normalized.partition("@")
    if not sep or not local or not domain or "." not in domain:
        raise ValueError("邮箱格式不正确")
    return normalized


def mask_email(email: str) -> str:
    normalized = normalize_email_for_identity(email)
    local, _, domain = normalized.partition("@")
    local_hint = local[:2] if len(local) > 1 else local[:1]
    domain_hint = domain[:1]
    return f"{local_hint}***@{domain_hint}***"


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw.encode("ascii"))


def encrypt_email(settings: Settings, email: str) -> tuple[str, str]:
    normalized = normalize_email_for_identity(email)
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(settings.email_encryption_key_bytes())
    ciphertext = aesgcm.encrypt(nonce, normalized.encode("utf-8"), b"uma-email-identity-v1")
    return _b64_encode(ciphertext), _b64_encode(nonce)


def decrypt_email(settings: Settings, ciphertext: str, nonce: str) -> str:
    aesgcm = AESGCM(settings.email_encryption_key_bytes())
    plaintext = aesgcm.decrypt(_b64_decode(nonce), _b64_decode(ciphertext), b"uma-email-identity-v1")
    return plaintext.decode("utf-8")


def email_identity_digest(settings: Settings, email: str) -> str:
    normalized = normalize_email_for_identity(email)
    secret = settings.email_identity_secret_value
    return hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def digest_email_otp(settings: Settings, email_identity: str, code: str, purpose: str) -> str:
    return hmac.new(
        settings.email_otp_secret.encode("utf-8"),
        f"{purpose}:{email_identity}:{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_otp_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(6))


def allocate_negative_legacy_user_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT next_negative_id FROM legacy_user_sequence WHERE id=1").fetchone()
    next_id = int(row["next_negative_id"] if row else -1)
    conn.execute(
        "INSERT OR REPLACE INTO legacy_user_sequence(id, next_negative_id) VALUES (1, ?)",
        (next_id - 1,),
    )
    return str(next_id)


def get_email_account_by_hmac(conn: sqlite3.Connection, settings: Settings, email: str) -> dict[str, Any] | None:
    identity = email_identity_digest(settings, email)
    row = conn.execute(
        "SELECT id, provider, provider_user_id, display_name, display_username, avatar_url, avatar_hash, created_at, updated_at, last_login_at "
        "FROM accounts WHERE provider='email' AND provider_user_id=?",
        (identity,),
    ).fetchone()
    return dict(row) if row else None


def upsert_account_email_identity(conn: sqlite3.Connection, settings: Settings, account_id: str, email: str) -> None:
    now = int(time.time())
    email_hmac = email_identity_digest(settings, email)
    ciphertext, nonce = encrypt_email(settings, email)
    masked = mask_email(email)
    conn.execute(
        """
        INSERT INTO account_email_identities(
            account_id, email_hmac, email_ciphertext, email_nonce, email_masked, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            email_hmac=excluded.email_hmac,
            email_ciphertext=excluded.email_ciphertext,
            email_nonce=excluded.email_nonce,
            email_masked=excluded.email_masked,
            updated_at=excluded.updated_at
        """,
        (account_id, email_hmac, ciphertext, nonce, masked, now, now),
    )


def mark_account_login(conn: sqlite3.Connection, account_id: str) -> None:
    now = int(time.time())
    conn.execute("UPDATE accounts SET last_login_at=?, updated_at=? WHERE id=?", (now, now, account_id))


def create_email_account(
    conn: sqlite3.Connection,
    settings: Settings,
    email: str,
    invite_code: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    identity = email_identity_digest(settings, email)
    display_name = mask_email(email)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT id, provider, provider_user_id, display_name, avatar_url, avatar_hash, created_at, updated_at, last_login_at "
        "FROM accounts WHERE provider='email' AND provider_user_id=?",
        (identity,),
    ).fetchone()
    if existing:
        raise ValueError("该邮箱已经注册，请使用邮箱登录。")

    account_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO accounts(id, provider, provider_user_id, display_name, avatar_url, avatar_hash, created_at, updated_at, last_login_at) "
        "VALUES (?, 'email', ?, ?, NULL, NULL, ?, ?, ?)",
        (account_id, identity, display_name, now, now, now),
    )
    bind_account_legacy_user(conn, account_id, allocate_negative_legacy_user_id(conn))
    legacy_user_id = get_account_legacy_user_id(conn, account_id)
    grant_welcome_bonus_if_needed(conn, account_id, legacy_user_id=legacy_user_id)
    upsert_account_email_identity(conn, settings, account_id, email)
    referral_result = apply_referral_bonus_for_new_account(
        conn,
        settings,
        invitee_account_id=account_id,
        invite_code=invite_code,
    )
    return {
        "id": account_id,
        "provider": "email",
        "provider_user_id": identity,
        "display_name": display_name,
        "display_username": None,
        "avatar_url": None,
        "avatar_hash": None,
        "created_at": now,
        "updated_at": now,
        "last_login_at": now,
        "is_new": True,
        "referral": referral_result,
    }


def find_or_create_email_account(conn: sqlite3.Connection, settings: Settings, email: str) -> dict[str, Any]:
    account = get_email_account_by_hmac(conn, settings, email)
    if account:
        upsert_account_email_identity(conn, settings, account["id"], email)
        mark_account_login(conn, account["id"])
        account["display_name"] = mask_email(email)
        account["last_login_at"] = int(time.time())
        account["is_new"] = False
        return account
    return create_email_account(conn, settings, email)


def create_secure_email_code(settings: Settings, email: str, purpose: str, ip_hash: str | None = None) -> str | None:
    now = int(time.time())
    identity = email_identity_digest(settings, email)
    conn = connect(settings)
    try:
        recent = conn.execute(
            "SELECT created_at FROM email_login_codes_secure WHERE email_identity=? ORDER BY created_at DESC LIMIT 1",
            (identity,),
        ).fetchone()
        if recent and (now - int(recent["created_at"])) < settings.email_otp_resend_seconds:
            return None

        hour_ago = now - 3600
        ten_minutes_ago = now - 600
        ten_minute_count = conn.execute(
            "SELECT COUNT(*) FROM email_login_codes_secure WHERE email_identity=? AND created_at>?",
            (identity, ten_minutes_ago),
        ).fetchone()[0]
        if int(ten_minute_count) >= settings.email_otp_max_sends_per_10_min:
            return None

        hourly_count = conn.execute(
            "SELECT COUNT(*) FROM email_login_codes_secure WHERE email_identity=? AND created_at>?",
            (identity, hour_ago),
        ).fetchone()[0]
        if int(hourly_count) >= settings.email_otp_max_per_hour:
            return None

        if ip_hash:
            ip_count = conn.execute(
                "SELECT COUNT(*) FROM email_login_codes_secure WHERE requested_ip_hash=? AND created_at>?",
                (ip_hash, hour_ago),
            ).fetchone()[0]
            if int(ip_count) >= settings.email_otp_max_per_ip_per_hour:
                return None

        code = generate_otp_code()
        conn.execute(
            "UPDATE email_login_codes_secure SET consumed_at=? "
            "WHERE email_identity=? AND purpose=? AND consumed_at IS NULL AND expires_at>?",
            (now, identity, purpose, now),
        )
        conn.execute(
            "INSERT INTO email_login_codes_secure(id, email_identity, purpose, code_digest, requested_ip_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                identity,
                purpose,
                digest_email_otp(settings, identity, code, purpose),
                ip_hash,
                now,
                now + settings.email_otp_expire_seconds,
            ),
        )
        conn.commit()
        return code
    finally:
        conn.close()


def create_secure_email_login_code(settings: Settings, email: str, ip_hash: str | None = None) -> str | None:
    return create_secure_email_code(settings, email, "email_login", ip_hash)


def verify_secure_email_code(settings: Settings, email: str, code: str, purpose: str) -> bool:
    now = int(time.time())
    identity = email_identity_digest(settings, email)
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, code_digest, expires_at, failed_attempts "
            "FROM email_login_codes_secure WHERE email_identity=? AND purpose=? AND consumed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (identity, purpose),
        ).fetchone()
        if not row:
            conn.rollback()
            return False

        if now > int(row["expires_at"]):
            conn.execute("UPDATE email_login_codes_secure SET consumed_at=? WHERE id=?", (now, row["id"]))
            conn.commit()
            return False

        if int(row["failed_attempts"]) >= settings.email_otp_max_attempts:
            conn.execute("UPDATE email_login_codes_secure SET consumed_at=? WHERE id=?", (now, row["id"]))
            conn.commit()
            return False

        actual_digest = digest_email_otp(settings, identity, code, purpose)
        if not hmac.compare_digest(actual_digest, row["code_digest"]):
            conn.execute(
                "UPDATE email_login_codes_secure SET failed_attempts=failed_attempts+1 WHERE id=?",
                (row["id"],),
            )
            conn.commit()
            return False

        conn.execute("UPDATE email_login_codes_secure SET consumed_at=? WHERE id=?", (now, row["id"]))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_secure_email_login_code(settings: Settings, email: str, code: str) -> bool:
    return verify_secure_email_code(settings, email, code, "email_login")


def cleanup_expired_codes(settings: Settings) -> int:
    now = int(time.time())
    conn = connect(settings)
    try:
        cur = conn.execute(
            "DELETE FROM email_login_codes_secure WHERE expires_at < ? OR consumed_at < ?",
            (now, now - 86400),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def oauth_state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def oauth_nonce_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def create_oauth_login_state(
    conn: sqlite3.Connection,
    *,
    state: str,
    browser_nonce: str,
    provider: str,
    redirect_after_login: str | None,
    ttl_seconds: int = 600,
) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO oauth_login_states(
            state_hash, browser_nonce_hash, provider, created_at, expires_at, redirect_after_login
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            oauth_state_hash(state),
            oauth_nonce_hash(browser_nonce),
            provider,
            now,
            now + ttl_seconds,
            redirect_after_login or "/",
        ),
    )


def consume_oauth_login_state(
    conn: sqlite3.Connection,
    *,
    state: str,
    browser_nonce: str,
    provider: str,
) -> str | None:
    now = int(time.time())
    state_digest = oauth_state_hash(state)
    nonce_digest = oauth_nonce_hash(browser_nonce)
    row = conn.execute(
        """
        SELECT state_hash, browser_nonce_hash, expires_at, used_at, redirect_after_login
        FROM oauth_login_states
        WHERE state_hash=? AND provider=?
        """,
        (state_digest, provider),
    ).fetchone()
    if not row:
        return None
    if not hmac.compare_digest(state_digest, row["state_hash"]):
        return None
    if not hmac.compare_digest(nonce_digest, row["browser_nonce_hash"]):
        return None
    if row["used_at"] is not None or now > int(row["expires_at"]):
        return None
    conn.execute("UPDATE oauth_login_states SET used_at=? WHERE state_hash=?", (now, state_digest))
    return row["redirect_after_login"] or "/"


def cleanup_oauth_login_states(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "DELETE FROM oauth_login_states WHERE expires_at<? OR used_at<?",
        (now - 3600, now - 86400),
    )


def get_email_identity_for_account(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT account_id, email_hmac, email_ciphertext, email_nonce, email_masked, created_at, updated_at "
        "FROM account_email_identities WHERE account_id=?",
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


def get_email_password_credential(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT account_id, password_hash, password_algo, created_at, updated_at,
               password_changed_at, failed_attempts, locked_until
        FROM email_password_credentials
        WHERE account_id=?
        """,
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


def email_account_has_password(conn: sqlite3.Connection, account_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM email_password_credentials WHERE account_id=?",
        (account_id,),
    ).fetchone()
    return bool(row)


def upsert_email_password_credential(
    conn: sqlite3.Connection,
    account_id: str,
    password_hash: str,
    password_algo: str = "argon2id",
    now: int | None = None,
) -> None:
    now = int(now or time.time())
    conn.execute(
        """
        INSERT INTO email_password_credentials(
            account_id, password_hash, password_algo, created_at, updated_at,
            password_changed_at, failed_attempts, locked_until
        ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
        ON CONFLICT(account_id) DO UPDATE SET
            password_hash=excluded.password_hash,
            password_algo=excluded.password_algo,
            updated_at=excluded.updated_at,
            password_changed_at=excluded.password_changed_at,
            failed_attempts=0,
            locked_until=NULL
        """,
        (account_id, password_hash, password_algo, now, now, now),
    )


def record_email_password_attempt(
    conn: sqlite3.Connection,
    *,
    email_hmac: str | None,
    ip_hash: str | None,
    success: bool,
    now: int | None = None,
) -> None:
    now = int(now or time.time())
    conn.execute(
        "INSERT INTO email_password_login_attempts(email_hmac, ip_hash, success, created_at) VALUES (?, ?, ?, ?)",
        (email_hmac, ip_hash, 1 if success else 0, now),
    )
    conn.execute("DELETE FROM email_password_login_attempts WHERE created_at<?", (now - 86400,))


def count_recent_password_attempts(
    conn: sqlite3.Connection,
    *,
    email_hmac: str | None = None,
    ip_hash: str | None = None,
    window_seconds: int = 900,
    success: bool | None = None,
    now: int | None = None,
) -> int:
    now = int(now or time.time())
    where = ["created_at>?"]
    params: list[Any] = [now - int(window_seconds)]
    if email_hmac:
        where.append("email_hmac=?")
        params.append(email_hmac)
    if ip_hash:
        where.append("ip_hash=?")
        params.append(ip_hash)
    if success is not None:
        where.append("success=?")
        params.append(1 if success else 0)
    row = conn.execute(
        f"SELECT COUNT(*) FROM email_password_login_attempts WHERE {' AND '.join(where)}",
        tuple(params),
    ).fetchone()
    return int(row[0] if row else 0)


def register_password_failure(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    max_failed_attempts: int,
    lock_seconds: int,
    now: int | None = None,
) -> int:
    now = int(now or time.time())
    row = conn.execute(
        "SELECT failed_attempts FROM email_password_credentials WHERE account_id=?",
        (account_id,),
    ).fetchone()
    next_failed = int(row["failed_attempts"] if row else 0) + 1
    locked_until = now + int(lock_seconds) if next_failed >= int(max_failed_attempts) else None
    conn.execute(
        "UPDATE email_password_credentials SET failed_attempts=?, locked_until=?, updated_at=? WHERE account_id=?",
        (next_failed, locked_until, now, account_id),
    )
    return next_failed


def clear_password_failures(conn: sqlite3.Connection, account_id: str, now: int | None = None) -> None:
    now = int(now or time.time())
    conn.execute(
        "UPDATE email_password_credentials SET failed_attempts=0, locked_until=NULL, updated_at=? WHERE account_id=?",
        (now, account_id),
    )


def create_password_reset_token(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    account_id: str,
    email: str,
    token_hash: str,
    requested_ip_hash: str | None,
    ttl_seconds: int,
    now: int | None = None,
) -> None:
    now = int(now or time.time())
    email_hmac = email_identity_digest(settings, email)
    conn.execute(
        "UPDATE email_password_reset_tokens SET used_at=? WHERE account_id=? AND used_at IS NULL",
        (now, account_id),
    )
    conn.execute(
        """
        INSERT INTO email_password_reset_tokens(
            id, account_id, email_hmac, token_hash, requested_ip_hash, created_at, expires_at, used_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (str(uuid.uuid4()), account_id, email_hmac, token_hash, requested_ip_hash, now, now + int(ttl_seconds)),
    )


def consume_password_reset_token(
    conn: sqlite3.Connection,
    *,
    token_hash: str,
    now: int | None = None,
) -> dict[str, Any] | None:
    now = int(now or time.time())
    row = conn.execute(
        """
        SELECT id, account_id, email_hmac, expires_at, used_at
        FROM email_password_reset_tokens
        WHERE token_hash=?
        """,
        (token_hash,),
    ).fetchone()
    if not row or row["used_at"] is not None or now > int(row["expires_at"]):
        return None
    conn.execute("UPDATE email_password_reset_tokens SET used_at=? WHERE id=?", (now, row["id"]))
    return dict(row)


def list_admin_accounts(
    conn: sqlite3.Connection,
    limit: int = 100,
    offset: int = 0,
    *,
    query: str | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    limit = min(max(int(limit), 1), 200)
    offset = max(int(offset), 0)
    where = ""
    params: list[Any] = []
    q = str(query or "").strip()
    if q:
        q = q[:120]
        like = f"%{q}%"
        normalized = q.casefold()
        clauses = [
            "a.id = ?",
            "b.legacy_user_id = ?",
            "LOWER(a.display_username_normalized) LIKE ?",
            "LOWER(a.display_name) LIKE ?",
            "LOWER(a.provider_user_id) LIKE ?",
            "LOWER(COALESCE(e.email_masked, '')) LIKE ?",
            "UPPER(COALESCE(rc.referral_code, '')) = ?",
        ]
        params.extend([q, q, f"%{normalized}%", f"%{normalized}%", f"%{normalized}%", f"%{normalized}%", normalized.upper()])
        if "@" in q and settings:
            try:
                clauses.append("e.email_hmac = ?")
                params.append(email_identity_digest(settings, q))
            except ValueError:
                pass
        where = "WHERE " + " OR ".join(f"({clause})" for clause in clauses)
    rows = conn.execute(
        f"""
        SELECT
            a.id AS account_id,
            a.provider,
            a.display_name,
            a.display_username,
            a.created_at,
            a.updated_at,
            a.last_login_at,
            b.legacy_user_id,
            u.balance_fen,
            e.email_masked,
            rc.referral_code,
            COUNT(DISTINCT t.job_code) AS task_count,
            COUNT(DISTINCT r.code) AS topup_count,
            COALESCE(rs.referral_count, 0) AS referral_count,
            COALESCE(rs.referral_reward_credits, 0) AS referral_reward_credits
        FROM accounts a
        LEFT JOIN account_legacy_bindings b ON b.account_id=a.id
        LEFT JOIN users u ON u.user_id=b.legacy_user_id
        LEFT JOIN account_email_identities e ON e.account_id=a.id
        LEFT JOIN referral_codes rc ON rc.account_id=a.id
        LEFT JOIN (
            SELECT
                inviter_account_id,
                COUNT(*) AS referral_count,
                COALESCE(SUM(CASE WHEN status='rewarded' THEN inviter_reward_credits ELSE 0 END), 0) AS referral_reward_credits
            FROM referral_relationships
            GROUP BY inviter_account_id
        ) rs ON rs.inviter_account_id=a.id
        LEFT JOIN generation_tasks t ON t.user_id=b.legacy_user_id
        LEFT JOIN recharge_requests r ON r.user_id=b.legacy_user_id
        {where}
        GROUP BY a.id
        ORDER BY a.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def reveal_account_email(conn: sqlite3.Connection, settings: Settings, account_id: str) -> str | None:
    row = get_email_identity_for_account(conn, account_id)
    if not row:
        return None
    return decrypt_email(settings, row["email_ciphertext"], row["email_nonce"])


def record_admin_audit(conn: sqlite3.Connection, admin_account_id: str, target_account_id: str, action: str) -> None:
    conn.execute(
        "INSERT INTO admin_audit_log(admin_account_id, target_account_id, action, created_at) VALUES (?, ?, ?, ?)",
        (admin_account_id, target_account_id, action, int(time.time())),
    )


def create_feedback_report(
    settings: Settings,
    *,
    account_id: str,
    legacy_user_id: str,
    provider: str,
    user_display: str,
    category: str,
    message: str,
) -> int:
    now = int(time.time())
    conn = connect(settings)
    try:
        cur = conn.execute(
            """
            INSERT INTO feedback_reports(
                account_id, legacy_user_id, provider, user_display, category, message, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (account_id, legacy_user_id, provider, user_display, category, message, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


SUPPORT_CATEGORIES = {"general", "payment", "feedback", "account", "generation", "security", "other"}
SUPPORT_PRIORITIES = {"normal", "important", "urgent"}
SUPPORT_STATUSES = {"open", "closed"}


def normalize_support_category(value: str | None) -> str:
    category = str(value or "general").strip().lower()
    return category if category in SUPPORT_CATEGORIES else "general"


def normalize_support_priority(value: str | None) -> str:
    priority = str(value or "normal").strip().lower()
    return priority if priority in SUPPORT_PRIORITIES else "normal"


def clean_support_message(value: str) -> str:
    body = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise ValueError("消息内容不能为空")
    if len(body) > 2000:
        raise ValueError("消息内容最多 2000 字符")
    return body


def clean_support_subject(value: str | None) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:160]


def make_support_thread_code(conn: sqlite3.Connection) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = "SUP-" + "".join(secrets.choice(alphabet) for _ in range(12))
        exists = conn.execute("SELECT 1 FROM support_threads WHERE thread_code=?", (code,)).fetchone()
        if not exists:
            return code
    raise RuntimeError("生成消息会话编号失败")


def get_account_public_profile(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            a.id AS account_id,
            a.provider,
            a.display_name,
            a.display_username,
            a.display_username_updated_at,
            a.created_at,
            a.last_login_at,
            b.legacy_user_id,
            u.balance_fen,
            e.email_masked,
            rc.referral_code,
            rc.created_at AS referral_code_created_at
        FROM accounts a
        LEFT JOIN account_legacy_bindings b ON b.account_id=a.id
        LEFT JOIN users u ON u.user_id=b.legacy_user_id
        LEFT JOIN account_email_identities e ON e.account_id=a.id
        LEFT JOIN referral_codes rc ON rc.account_id=a.id
        WHERE a.id=?
        """,
        (account_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["display_label"] = display_label_from_account(row)
    item.update(get_referral_stats(conn, account_id))
    return item


def support_account_display(account: dict[str, Any] | sqlite3.Row | None) -> str:
    if not account:
        return "未知账号"
    custom = str(account["display_username"] or "").strip() if "display_username" in account.keys() else ""
    if custom:
        return custom
    provider = str(account["provider"] or "")
    if provider == "email":
        return str(account["email_masked"] or "邮箱用户")
    return str(account["display_name"] or "Discord 用户")


def _support_thread_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["unread_user_count"] = int(item.get("unread_user_count") or 0)
    item["unread_admin_count"] = int(item.get("unread_admin_count") or 0)
    return item


def create_support_thread(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    admin_account_id: str,
    category: str,
    subject: str | None,
    message: str,
    related_feedback_id: int | None = None,
    related_topup_code: str | None = None,
    priority: str = "normal",
) -> dict[str, Any]:
    account = get_account_public_profile(conn, account_id)
    if not account:
        raise LookupError("account_not_found")
    now = int(time.time())
    code = make_support_thread_code(conn)
    body = clean_support_message(message)
    cur = conn.execute(
        """
        INSERT INTO support_threads(
            thread_code, account_id, legacy_user_id, category, subject, related_feedback_id,
            related_topup_code, status, priority, created_by_admin_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
        """,
        (
            code,
            account_id,
            account.get("legacy_user_id"),
            normalize_support_category(category),
            clean_support_subject(subject),
            int(related_feedback_id) if related_feedback_id else None,
            str(related_topup_code or "").strip()[:40] or None,
            normalize_support_priority(priority),
            admin_account_id,
            now,
            now,
        ),
    )
    thread_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO support_messages(
            thread_id, sender_type, sender_admin_id, body, created_at, read_by_admin_at
        ) VALUES (?, 'admin', ?, ?, ?, ?)
        """,
        (thread_id, admin_account_id, body, now, now),
    )
    return get_support_thread_by_code(conn, code, include_counts=True) or {"thread_code": code}


def get_support_thread_by_code(
    conn: sqlite3.Connection,
    thread_code: str,
    *,
    account_id: str | None = None,
    include_counts: bool = False,
) -> dict[str, Any] | None:
    code = str(thread_code or "").strip().upper()
    filters = ["t.thread_code=?"]
    params: list[Any] = [code]
    if account_id is not None:
        filters.append("t.account_id=?")
        params.append(account_id)
    if include_counts:
        sql = f"""
            SELECT
                t.*,
                a.provider,
                a.display_name,
                e.email_masked,
                u.balance_fen,
                COALESCE(SUM(CASE WHEN m.sender_type='admin' AND m.read_by_user_at IS NULL THEN 1 ELSE 0 END), 0)
                    AS unread_user_count,
                COALESCE(SUM(CASE WHEN m.sender_type='user' AND m.read_by_admin_at IS NULL THEN 1 ELSE 0 END), 0)
                    AS unread_admin_count,
                MAX(m.created_at) AS last_message_at
            FROM support_threads t
            LEFT JOIN accounts a ON a.id=t.account_id
            LEFT JOIN account_email_identities e ON e.account_id=t.account_id
            LEFT JOIN users u ON u.user_id=t.legacy_user_id
            LEFT JOIN support_messages m ON m.thread_id=t.id
            WHERE {' AND '.join(filters)}
            GROUP BY t.id
        """
    else:
        sql = f"SELECT * FROM support_threads t WHERE {' AND '.join(filters)}"
    row = conn.execute(sql, tuple(params)).fetchone()
    return _support_thread_row_to_dict(row) if row else None


def list_support_threads_for_user(conn: sqlite3.Connection, account_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            t.*,
            COALESCE(SUM(CASE WHEN m.sender_type='admin' AND m.read_by_user_at IS NULL THEN 1 ELSE 0 END), 0)
                AS unread_user_count,
            0 AS unread_admin_count,
            MAX(m.created_at) AS last_message_at
        FROM support_threads t
        LEFT JOIN support_messages m ON m.thread_id=t.id
        WHERE t.account_id=?
        GROUP BY t.id
        ORDER BY t.updated_at DESC
        LIMIT ?
        """,
        (account_id, min(max(int(limit), 1), 200)),
    ).fetchall()
    return [_support_thread_row_to_dict(row) for row in rows]


def list_support_threads_admin(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    status: str | None = None,
    category: str | None = None,
    unread_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if account_id:
        filters.append("t.account_id=?")
        params.append(account_id)
    if status in SUPPORT_STATUSES:
        filters.append("t.status=?")
        params.append(status)
    if category in SUPPORT_CATEGORIES:
        filters.append("t.category=?")
        params.append(category)
    where = "WHERE " + " AND ".join(filters) if filters else ""
    having = "HAVING unread_admin_count > 0" if unread_only else ""
    sql = f"""
        SELECT
            t.*,
            a.provider,
            a.display_name,
            e.email_masked,
            u.balance_fen,
            COALESCE(SUM(CASE WHEN m.sender_type='admin' AND m.read_by_user_at IS NULL THEN 1 ELSE 0 END), 0)
                AS unread_user_count,
            COALESCE(SUM(CASE WHEN m.sender_type='user' AND m.read_by_admin_at IS NULL THEN 1 ELSE 0 END), 0)
                AS unread_admin_count,
            MAX(m.created_at) AS last_message_at
        FROM support_threads t
        LEFT JOIN accounts a ON a.id=t.account_id
        LEFT JOIN account_email_identities e ON e.account_id=t.account_id
        LEFT JOIN users u ON u.user_id=t.legacy_user_id
        LEFT JOIN support_messages m ON m.thread_id=t.id
        {where}
        GROUP BY t.id
        {having}
        ORDER BY t.updated_at DESC
        LIMIT ?
    """
    params.append(min(max(int(limit), 1), 200))
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_support_thread_row_to_dict(row) for row in rows]


def list_support_messages(conn: sqlite3.Connection, thread_id: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, thread_id, sender_type, sender_account_id, sender_admin_id, body, created_at,
               read_by_user_at, read_by_admin_at
        FROM support_messages
        WHERE thread_id=?
        ORDER BY id ASC
        LIMIT ?
        """,
        (int(thread_id), min(max(int(limit), 1), 500)),
    ).fetchall()
    return [dict(row) for row in rows]


def add_support_message(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    sender_type: str,
    body: str,
    sender_account_id: str | None = None,
    sender_admin_id: str | None = None,
) -> dict[str, Any]:
    sender_type = str(sender_type or "").strip().lower()
    if sender_type not in {"admin", "user", "system"}:
        raise ValueError("未知消息发送者")
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO support_messages(
            thread_id, sender_type, sender_account_id, sender_admin_id, body, created_at,
            read_by_user_at, read_by_admin_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(thread_id),
            sender_type,
            sender_account_id,
            sender_admin_id,
            clean_support_message(body),
            now,
            now if sender_type == "user" else None,
            now if sender_type == "admin" else None,
        ),
    )
    conn.execute("UPDATE support_threads SET updated_at=? WHERE id=?", (now, int(thread_id)))
    row = conn.execute("SELECT * FROM support_messages WHERE id=?", (int(cur.lastrowid),)).fetchone()
    return dict(row)


def mark_support_thread_read_by_user(conn: sqlite3.Connection, thread_id: int) -> int:
    now = int(time.time())
    cur = conn.execute(
        """
        UPDATE support_messages
        SET read_by_user_at=?
        WHERE thread_id=? AND sender_type='admin' AND read_by_user_at IS NULL
        """,
        (now, int(thread_id)),
    )
    return int(cur.rowcount)


def mark_support_thread_read_by_admin(conn: sqlite3.Connection, thread_id: int) -> int:
    now = int(time.time())
    cur = conn.execute(
        """
        UPDATE support_messages
        SET read_by_admin_at=?
        WHERE thread_id=? AND sender_type='user' AND read_by_admin_at IS NULL
        """,
        (now, int(thread_id)),
    )
    return int(cur.rowcount)


def set_support_thread_status(conn: sqlite3.Connection, thread_id: int, status: str) -> None:
    if status not in SUPPORT_STATUSES:
        raise ValueError("未知会话状态")
    now = int(time.time())
    conn.execute(
        "UPDATE support_threads SET status=?, updated_at=?, closed_at=? WHERE id=?",
        (status, now, now if status == "closed" else None, int(thread_id)),
    )


def get_support_unread_count(conn: sqlite3.Connection, account_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM support_messages m
        JOIN support_threads t ON t.id=m.thread_id
        WHERE t.account_id=? AND m.sender_type='admin' AND m.read_by_user_at IS NULL
        """,
        (account_id,),
    ).fetchone()
    return int(row[0] if row else 0)


def get_support_important_unread(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT t.thread_code, t.subject, t.category, t.priority, m.id AS message_id, m.body, m.created_at
        FROM support_messages m
        JOIN support_threads t ON t.id=m.thread_id
        WHERE t.account_id=?
          AND m.sender_type='admin'
          AND m.read_by_user_at IS NULL
          AND t.priority IN ('important', 'urgent')
        ORDER BY m.created_at ASC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


def get_pending_topup_submit_reminder(
    conn: sqlite3.Connection,
    *,
    legacy_user_id: str,
    now: int | None = None,
) -> dict[str, Any] | None:
    current = int(now or time.time())
    rows = conn.execute(
        """
        SELECT code, created_at, payment_method
        FROM recharge_requests
        WHERE user_id=?
          AND status='created'
          AND code IS NOT NULL
          AND code <> ''
          AND created_at <= ?
          AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY created_at ASC
        LIMIT 20
        """,
        (str(legacy_user_id), current - 600, current),
    ).fetchall()
    if not rows:
        return None
    first = dict(rows[0])
    first["count"] = len(rows)
    first["reminder_id"] = f"topup-submit:{first['code']}"
    return first
