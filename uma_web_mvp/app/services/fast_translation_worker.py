"""Fast translation background worker.

Polls for queued fast translation requests, calls DeepSeek,
merges character tags, and promotes tasks to queued status.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.agent import _apply_character_registry_to_refined_prompt
from app.config import Settings
from app.db import connect, fail_fast_translation_task_refund_atomic
from app.services.deepseek_service import DeepSeekError, DeepSeekService
from app.services.fast_translator_service import FAST_TRANSLATOR_SYSTEM_PROMPT, _safe_tags_from_model
from app.smart_agent.character_preferences import split_prompt_tags

FAST_TRANSLATION_CLAIM_TTL = 180  # seconds before a stale claim is reclaimed
FAST_TRANSLATION_MAX_ATTEMPTS = 2  # first attempt + 1 crash recovery retry


def claim_next_fast_translation(settings: Settings) -> dict[str, Any] | None:
    """Claim one queued fast translation request with its associated generation task."""
    now = int(time.time())
    stale_before = now - FAST_TRANSLATION_CLAIM_TTL
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Find a queued OR stale-processing translation request with a translating generation task
        row = conn.execute(
            """
            SELECT tr.id AS tr_id, tr.request_code, tr.original_text,
                   tr.character_keys_json, tr.character_match_source,
                   tr.attempt_count, tr.generation_job_code,
                   gt.job_code, gt.user_id, gt.status AS gt_status
            FROM translation_requests tr
            JOIN generation_tasks gt ON gt.fast_translation_request_code = tr.request_code
            WHERE gt.status = 'translating'
              AND gt.fast_translation_request_code = tr.request_code
              AND (
                    (tr.status = 'queued')
                    OR
                    (tr.status = 'processing' AND tr.started_at < ? AND tr.attempt_count < ?)
                  )
            ORDER BY tr.created_at ASC, tr.id ASC
            LIMIT 1
            """,
            (stale_before, FAST_TRANSLATION_MAX_ATTEMPTS),
        ).fetchone()
        if not row:
            conn.commit()
            return None

        row_dict = dict(row)
        tr_id = row_dict["tr_id"]

        # Mark as processing (works for both queued→processing and stale→reclaimed)
        conn.execute(
            "UPDATE translation_requests SET status='processing', started_at=?, attempt_count=attempt_count+1 WHERE id=? AND status IN ('queued','processing')",
            (now, tr_id),
        )
        conn.commit()
        return row_dict
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_stale_fast_translation_tasks(settings: Settings) -> int:
    """Recover stale processing translation tasks.

    For tasks with attempt_count < max_attempts:
      requeue (processing → queued, started_at=NULL)
    For tasks with attempt_count >= max_attempts:
      fail and refund via atomic failure logic.
    """
    now = int(time.time())
    stale_before = now - FAST_TRANSLATION_CLAIM_TTL
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT tr.id, tr.request_code, tr.generation_job_code, tr.attempt_count "
            "FROM translation_requests tr "
            "WHERE tr.status = 'processing' AND tr.started_at < ?",
            (stale_before,),
        ).fetchall()
        requeued = 0
        failed = 0
        for row in rows:
            row_dict = dict(row)
            tr_id = row_dict["id"]
            attempt = int(row_dict.get("attempt_count") or 0)
            job_code = str(row_dict.get("generation_job_code") or "")
            request_code = str(row_dict.get("request_code") or "")

            if attempt < FAST_TRANSLATION_MAX_ATTEMPTS:
                # Requeue: processing → queued, clear started_at
                conn.execute(
                    "UPDATE translation_requests SET status='queued', started_at=NULL, error_code='stale_requeued' "
                    "WHERE id=? AND status='processing'",
                    (tr_id,),
                )
                requeued += 1
            else:
                # Max attempts exceeded: fail and refund
                if job_code:
                    try:
                        fail_fast_translation_task_refund_atomic(
                            settings,
                            job_code=job_code,
                            error_code="fast_translate_stale_max_attempts",
                        )
                        failed += 1
                    except Exception:
                        pass
        conn.commit()
        if requeued or failed:
            print(
                f"[FAST_TRANSLATION_WORKER] recovery: requeued={requeued} failed={failed}",
                flush=True,
            )
        return requeued + failed
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()


def complete_fast_translation(
    settings: Settings,
    *,
    request_code: str,
    job_code: str,
    final_prompt: str,
    character_key: str,
) -> bool:
    """Atomically promote a fast translation task to queued.

    Only succeeds if generation task is still translating and translation request is processing.
    """
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Conditional update: only if both states match
        cur_tr = conn.execute(
            "UPDATE translation_requests SET status='done', refined_prompt=?, finished_at=? "
            "WHERE request_code=? AND status='processing'",
            (final_prompt, now, request_code),
        )
        if cur_tr.rowcount != 1:
            conn.rollback()
            return False

        cur_gt = conn.execute(
            "UPDATE generation_tasks SET "
            "status='queued', "
            "prompt=?, "
            "effective_prompt=?, "
            "prompt_source=?, "
            "character_key=?, "
            "queued_at=? "
            "WHERE job_code=? AND status='translating' AND fast_translation_request_code=?",
            (
                final_prompt[:3000],
                final_prompt[:3000],
                f"fast_translate:{request_code}",
                character_key[:1024] if character_key else "",
                now,
                job_code,
                request_code,
            ),
        )
        if cur_gt.rowcount != 1:
            conn.rollback()
            return False

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def fast_translation_worker_loop(settings: Settings) -> None:
    """Main worker loop: poll for fast translation tasks and process them."""
    poll_interval = max(1, int(getattr(settings, "mock_worker_poll_seconds", 2)))
    recovery_interval = 30  # seconds between recovery runs
    last_recovery = 0
    first_run = True
    print("[FAST_TRANSLATION_WORKER] started", flush=True)

    while True:
        try:
            if not settings.fast_translator_enabled or not settings.deepseek_api_key:
                await asyncio.sleep(5)
                continue

            now_ts = int(time.time())
            # Run recovery before first claim, then periodically
            if first_run or now_ts - last_recovery >= recovery_interval:
                recovered = await asyncio.to_thread(recover_stale_fast_translation_tasks, settings)
                if recovered:
                    print(f"[FAST_TRANSLATION_WORKER] recovery processed {recovered} stale tasks", flush=True)
                last_recovery = now_ts
                first_run = False

            task = await asyncio.to_thread(claim_next_fast_translation, settings)
            if not task:
                await asyncio.sleep(poll_interval)
                continue

            request_code = str(task["request_code"])
            job_code = str(task["job_code"])
            original_text = str(task["original_text"] or "")
            character_keys_json = str(task["character_keys_json"] or "[]")
            character_match_source = str(task["character_match_source"] or "none")

            print(
                f"[FAST_TRANSLATION_WORKER] claimed request={request_code} job={job_code}",
                flush=True,
            )

            try:
                # Parse character keys
                try:
                    character_keys = json.loads(character_keys_json)
                    if not isinstance(character_keys, list):
                        character_keys = []
                except (json.JSONDecodeError, TypeError):
                    character_keys = []

                # Call DeepSeek
                ds = DeepSeekService(settings)
                data = await ds.complete_json(
                    system_prompt=FAST_TRANSLATOR_SYSTEM_PROMPT,
                    user_prompt=original_text,
                    temperature=0.15,
                    max_tokens=1200,
                    timeout_seconds=settings.deepseek_timeout_seconds,
                    purpose="fast_translator",
                )
                scene_prompt = _safe_tags_from_model(data)

                # Merge character tags
                final_prompt = _apply_character_registry_to_refined_prompt(
                    original_text,
                    scene_prompt,
                    resolved_character_ids=character_keys,
                    disable_character_library=character_match_source == "none",
                )
                if not final_prompt:
                    raise RuntimeError("empty_prompt")

                # Save character key as JSON string for storage
                char_key_str = json.dumps(character_keys, ensure_ascii=False) if character_keys else ""

                ok = complete_fast_translation(
                    settings,
                    request_code=request_code,
                    job_code=job_code,
                    final_prompt=final_prompt,
                    character_key=char_key_str,
                )
                if ok:
                    print(
                        f"[FAST_TRANSLATION_WORKER] completed request={request_code} job={job_code}",
                        flush=True,
                    )
                else:
                    # Task was cancelled while we were translating
                    print(
                        f"[FAST_TRANSLATION_WORKER] discarded request={request_code} job={job_code} (state changed)",
                        flush=True,
                    )

            except (DeepSeekError, RuntimeError) as exc:
                error_code = getattr(exc, "code", type(exc).__name__)
                print(
                    f"[FAST_TRANSLATION_WORKER] failed request={request_code} job={job_code} error={error_code}",
                    flush=True,
                )
                refunded = fail_fast_translation_task_refund_atomic(
                    settings,
                    job_code=job_code,
                    error_code=str(error_code),
                )
                if refunded:
                    print(
                        f"[FAST_TRANSLATION_WORKER] refunded request={request_code} job={job_code}",
                        flush=True,
                    )

            except Exception as exc:
                print(
                    f"[FAST_TRANSLATION_WORKER] unexpected error request={request_code} job={job_code} error={type(exc).__name__}",
                    flush=True,
                )
                try:
                    fail_fast_translation_task_refund_atomic(
                        settings,
                        job_code=job_code,
                        error_code="fast_translate_failed",
                    )
                except Exception:
                    pass

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[FAST_TRANSLATION_WORKER] loop error: {type(exc).__name__}", flush=True)
            await asyncio.sleep(5)
