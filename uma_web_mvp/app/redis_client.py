import json
import time
from typing import Any

from .config import Settings

try:
    import redis
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover - optional dependency fallback
    redis = None

    class RedisError(Exception):
        pass


_client = None
_client_url = ""
_disabled_until = 0.0
_warned_unavailable = False
_warned_missing = False


def _warn(message: str) -> None:
    print(f"[REDIS] warning {message}", flush=True)


def get_redis(settings: Settings):
    global _client, _client_url, _disabled_until, _warned_unavailable, _warned_missing
    if not settings.redis_enabled:
        return None
    if redis is None:
        if not _warned_missing:
            _warn("dependency_missing fallback=local")
            _warned_missing = True
        return None
    now = time.monotonic()
    if now < _disabled_until:
        return None
    if _client is not None and _client_url == settings.redis_url:
        return _client
    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            retry_on_timeout=False,
            health_check_interval=30,
        )
        client.ping()
        _client = client
        _client_url = settings.redis_url
        _warned_unavailable = False
        print("[REDIS] connected", flush=True)
        return _client
    except Exception as exc:
        _client = None
        _client_url = ""
        _disabled_until = now + 30
        if not _warned_unavailable:
            _warn(f"unavailable error={type(exc).__name__} fallback=local")
            _warned_unavailable = True
        return None


def _safe(settings: Settings, callback, default=None):
    global _client, _client_url, _disabled_until
    client = get_redis(settings)
    if client is None:
        return default
    try:
        return callback(client)
    except RedisError as exc:
        _client = None
        _client_url = ""
        _disabled_until = time.monotonic() + 30
        _warn(f"operation_failed error={type(exc).__name__} fallback=local")
        return default


def cache_get_json(settings: Settings, key: str) -> Any | None:
    raw = _safe(settings, lambda client: client.get(key))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def cache_set_json(settings: Settings, key: str, value: Any, ttl_seconds: int) -> bool:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return bool(_safe(settings, lambda client: client.setex(key, max(1, int(ttl_seconds)), payload), False))


def incr_with_ttl(settings: Settings, key: str, ttl_seconds: int) -> int | None:
    def op(client):
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        if int(ttl) < 0:
            client.expire(key, max(1, int(ttl_seconds)))
        return int(count)

    return _safe(settings, op)


def get_int(settings: Settings, key: str) -> int | None:
    raw = _safe(settings, lambda client: client.get(key))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def exists(settings: Settings, key: str) -> bool | None:
    value = _safe(settings, lambda client: client.exists(key))
    return None if value is None else bool(value)


def set_nx_ex(settings: Settings, key: str, value: str, ttl_seconds: int) -> bool | None:
    result = _safe(settings, lambda client: client.set(key, value, nx=True, ex=max(1, int(ttl_seconds))))
    return None if result is None else bool(result)


def set_ex(settings: Settings, key: str, value: str, ttl_seconds: int) -> bool:
    return bool(_safe(settings, lambda client: client.setex(key, max(1, int(ttl_seconds)), value), False))


def delete(settings: Settings, *keys: str) -> int | None:
    clean_keys = [key for key in keys if key]
    if not clean_keys:
        return 0
    return _safe(settings, lambda client: client.delete(*clean_keys))
