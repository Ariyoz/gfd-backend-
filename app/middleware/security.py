"""
Security middleware — defense in depth for the GFD API.

Layers:
  1. SecurityHeadersMiddleware  — HTTP security headers on every response
  2. RequestIDMiddleware        — Trace every request with a UUID
  3. RateLimitMiddleware        — Per-IP + per-user rate limiting (brute-force protection)
  4. BruteForceMiddleware       — Login/auth endpoint lockout after N failures
  5. RequestLoggingMiddleware   — Structured security audit logging
  6. InputSanitizationMiddleware — Block path traversal, SQLi patterns, known scanner paths
  7. CryptoWebhookGuard        — HMAC verification for NOWPayments + Paystack webhooks
"""

import hashlib
import hmac
import json
import re
import time
import uuid
import logging
from collections import defaultdict
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

logger = logging.getLogger("gfd.security")

# ── In-memory stores (replace with Redis in multi-instance deployment) ────────
_rate_store:  dict[str, list[float]] = defaultdict(list)
_brute_store: dict[str, list[float]] = defaultdict(list)
_blocked_ips: dict[str, float]       = {}   # IP -> block_until timestamp
_store_lock = Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Security Headers
# ═══════════════════════════════════════════════════════════════════════════════

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to ALL responses."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        response = await call_next(request)

        # Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Force HTTPS for 2 years
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"

        # No referrer leakage
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Restrict browser features
        response.headers["Permissions-Policy"] = (
            "geolocation=(), payment=(), camera=(self), "
            "microphone=(self), usb=(), bluetooth=(), serial=()"
        )

        # Block iframes (clickjacking)
        response.headers["X-Frame-Options"] = "DENY"

        # Disable XSS filter (modern browsers — CSP is the real protection)
        response.headers["X-XSS-Protection"] = "0"

        # Cross-origin policies
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        response.headers["Cross-Origin-Opener-Policy"]   = "same-origin-allow-popups"
        response.headers["Cross-Origin-Embedder-Policy"] = "unsafe-none"

        # Content Security Policy
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' https:; "
            "script-src 'self' 'unsafe-inline' https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "img-src 'self' data: https: blob:; "
            "connect-src 'self' https: wss: ws:; "
            "font-src 'self' https: data:; "
            "media-src 'self' https: blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )

        # Never cache auth/wallet/crypto responses
        path = request.url.path
        if any(p in path for p in ['/auth/', '/wallet', '/crypto', '/admin']):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"
        elif request.method == "GET" and '/api/' in path:
            if any(p in path for p in ['/projects', '/explore', '/jobs']):
                response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=120"
            else:
                response.headers["Cache-Control"] = "private, max-age=10, stale-while-revalidate=60"

        # Ensure JSON charset
        ct = response.headers.get("content-type", "")
        if "application/json" in ct and "charset" not in ct:
            response.headers["content-type"] = "application/json; charset=utf-8"

        # Remove server fingerprint
        response.headers.pop("server", None)
        response.headers.pop("x-powered-by", None)

        return response


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Request ID
# ═══════════════════════════════════════════════════════════════════════════════

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = str(uuid.uuid4())
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Rate Limiting — per IP, sliding window
# ═══════════════════════════════════════════════════════════════════════════════

# Limits: (max_requests, window_seconds)
RATE_LIMITS = {
    "default":    (120, 60),    # 120 req/min for general endpoints
    "auth":       (10,  60),    # 10 attempts/min on auth endpoints
    "wallet":     (30,  60),    # 30 req/min on wallet/crypto
    "upload":     (10,  60),    # 10 uploads/min
    "webhook":    (200, 60),    # High limit for payment webhooks
}

def _get_limit_key(path: str) -> str:
    if "/auth/" in path:   return "auth"
    if "/wallet" in path or "/crypto" in path: return "wallet"
    if "/uploads" in path: return "upload"
    if "/webhook" in path: return "webhook"
    return "default"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding window rate limiter. Blocks repeat offenders."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        ip = _get_client_ip(request)
        now = time.time()

        # Check if IP is temporarily blocked
        with _store_lock:
            block_until = _blocked_ips.get(ip, 0)
            if now < block_until:
                logger.warning(f"RATE_BLOCKED: {ip} still blocked for {block_until-now:.0f}s")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please try again later."},
                    headers={"Retry-After": str(int(block_until - now))},
                )

        path = request.url.path
        limit_key = _get_limit_key(path)
        max_req, window = RATE_LIMITS[limit_key]
        store_key = f"{ip}:{limit_key}"

        with _store_lock:
            # Remove old timestamps outside the window
            timestamps = _rate_store[store_key]
            _rate_store[store_key] = [t for t in timestamps if now - t < window]
            current_count = len(_rate_store[store_key])

            if current_count >= max_req:
                # Escalate: block for 5 minutes on repeated abuse
                if current_count >= max_req * 2:
                    _blocked_ips[ip] = now + 300
                    logger.warning(f"RATE_ESCALATE: {ip} blocked for 5min (excessive abuse on {limit_key})")
                else:
                    logger.warning(f"RATE_LIMIT: {ip} hit {limit_key} limit ({current_count}/{max_req})")

                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please slow down."},
                    headers={"Retry-After": str(window), "X-RateLimit-Limit": str(max_req)},
                )

            _rate_store[store_key].append(now)

        response = await call_next(request)

        # Add rate limit headers to response
        remaining = max(0, max_req - current_count - 1)
        response.headers["X-RateLimit-Limit"]     = str(max_req)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"]    = str(window)

        return response


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Brute Force Protection — auth endpoint lockout
# ═══════════════════════════════════════════════════════════════════════════════

MAX_AUTH_FAILURES = 8      # lock after 8 failures
AUTH_LOCKOUT_SECS = 900    # 15 minutes
AUTH_WINDOW_SECS  = 600    # track failures over 10 min

class BruteForceMiddleware(BaseHTTPMiddleware):
    """Locks out an IP after repeated auth failures (401 responses)."""

    async def dispatch(self, request: Request, call_next):
        is_auth = "/auth/" in request.url.path and request.method == "POST"
        ip = _get_client_ip(request)
        now = time.time()

        if is_auth:
            with _store_lock:
                fails = _brute_store[ip]
                _brute_store[ip] = [t for t in fails if now - t < AUTH_WINDOW_SECS]
                if len(_brute_store[ip]) >= MAX_AUTH_FAILURES:
                    logger.warning(f"BRUTE_FORCE: {ip} locked out ({len(_brute_store[ip])} failures)")
                    # Also block at rate limit level
                    _blocked_ips[ip] = now + AUTH_LOCKOUT_SECS
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "Too many failed attempts. Try again in 15 minutes."},
                        headers={"Retry-After": str(AUTH_LOCKOUT_SECS)},
                    )

        response = await call_next(request)

        if is_auth and response.status_code == 401:
            with _store_lock:
                _brute_store[ip].append(now)
                count = len(_brute_store[ip])
                logger.warning(f"AUTH_FAIL: {ip} failure {count}/{MAX_AUTH_FAILURES} on {request.url.path}")

        return response


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Request Logging
# ═══════════════════════════════════════════════════════════════════════════════

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        ms = round((time.time() - start) * 1000, 2)

        ip = _get_client_ip(request)
        status = response.status_code

        if status in (401, 403, 429):
            logger.warning(f"SECURITY {status}: {request.method} {request.url.path} from {ip} ({ms}ms)")
        elif status >= 500:
            logger.error(f"SERVER_ERR {status}: {request.method} {request.url.path} from {ip} ({ms}ms)")
        else:
            logger.info(f"{request.method} {request.url.path} {status} {ms}ms ip={ip}")

        return response


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Input Sanitization — block scanners and injection patterns
# ═══════════════════════════════════════════════════════════════════════════════

# Compiled regex for speed
_INJECT_PATTERNS = re.compile(
    r"(\.\./|\.\.\\|/etc/passwd|/proc/|cmd\.exe|powershell|\.env|wp-admin|"
    r"phpinfo|\.git/|wp-login|xmlrpc|eval\(|exec\(|<script|javascript:|"
    r"union\s+select|drop\s+table|insert\s+into|select\s+\*|"
    r"onload=|onerror=|alert\(|document\.cookie|window\.location|"
    r"base64_decode|system\(|passthru\(|shell_exec\(|popen\(|"
    r"/etc/shadow|/etc/hosts|\.htaccess|\.htpasswd|"
    r"etc/passwd|etc/shadow|bin/bash|bin/sh)",
    re.IGNORECASE,
)

# Known bad user agents (bots, scanners, exploit tools)
_BAD_AGENTS = re.compile(
    r"(sqlmap|nikto|nmap|masscan|burpsuite|zgrab|dirbuster|gobuster|"
    r"wfuzz|hydra|medusa|havij|acunetix|nessus|openvas|w3af|skipfish|"
    r"curl/[0-6]\.|python-requests/1\.|go-http|java/1\.[0-5])",
    re.IGNORECASE,
)

class InputSanitizationMiddleware(BaseHTTPMiddleware):
    """Block path traversal, injection attempts, and scanner bots."""

    async def dispatch(self, request: Request, call_next):
        ip = _get_client_ip(request)
        path_and_query = request.url.path + "?" + str(request.url.query)

        # Block injection patterns in URL
        if _INJECT_PATTERNS.search(path_and_query):
            logger.warning(f"INJECT_BLOCKED: {ip} -> {request.url.path[:100]}")
            # Auto-block this IP for 1 hour
            with _store_lock:
                _blocked_ips[ip] = time.time() + 3600
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})

        # Block scanner user agents
        ua = request.headers.get("user-agent", "")
        if _BAD_AGENTS.search(ua):
            logger.warning(f"SCANNER_BLOCKED: {ip} UA={ua[:80]}")
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})

        # Block oversized headers (header injection / DoS)
        total_header_size = sum(len(k) + len(v) for k, v in request.headers.items())
        if total_header_size > 32_768:  # 32KB
            logger.warning(f"LARGE_HEADERS: {ip} size={total_header_size}")
            return JSONResponse(status_code=431, content={"detail": "Request Header Fields Too Large"})

        return await call_next(request)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Crypto Webhook Guard — HMAC verification helper (used in endpoints)
# ═══════════════════════════════════════════════════════════════════════════════

def verify_hmac_sha512(payload_bytes: bytes, received_sig: str, secret: str) -> bool:
    """Verify NOWPayments IPN HMAC-SHA512 signature."""
    if not secret or not received_sig:
        return False
    try:
        data = json.loads(payload_bytes)
        sorted_payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(
            secret.encode("utf-8"),
            sorted_payload.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected.lower(), received_sig.lower())
    except Exception:
        return False


def verify_paystack_hmac(payload_bytes: bytes, received_sig: str, secret: str) -> bool:
    """Verify Paystack webhook HMAC-SHA512 signature."""
    if not secret or not received_sig:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected.lower(), received_sig.lower())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting Render/Cloudflare proxy headers."""
    # Cloudflare sets CF-Connecting-IP
    if cf_ip := request.headers.get("CF-Connecting-IP"):
        return cf_ip.strip()
    # Standard proxy forwarding
    if forwarded := request.headers.get("X-Forwarded-For"):
        return forwarded.split(",")[0].strip()
    if real_ip := request.headers.get("X-Real-IP"):
        return real_ip.strip()
    return request.client.host if request.client else "unknown"
