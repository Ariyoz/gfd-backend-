"""
Security middleware — safe, async-compatible hardening for the GFD API.
"""

import hashlib
import hmac
import json
import re
import time
import uuid
import logging
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("gfd.security")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Security Headers
# ═══════════════════════════════════════════════════════════════════════════════

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to ALL responses."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), payment=(), camera=(self), microphone=(self)"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' https:; "
            "script-src 'self' 'unsafe-inline' https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "img-src 'self' data: https: blob:; "
            "connect-src 'self' https: wss: ws:; "
            "font-src 'self' https: data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none';"
        )

        # Never cache sensitive endpoints
        path = request.url.path
        if any(p in path for p in ['/auth/', '/wallet', '/crypto', '/admin', '/kyc']):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"

        # Remove server fingerprint
        if "server" in response.headers:
            del response.headers["server"]
        if "x-powered-by" in response.headers:
            del response.headers["x-powered-by"]

        ct = response.headers.get("content-type", "")
        if "application/json" in ct and "charset" not in ct:
            response.headers["content-type"] = "application/json; charset=utf-8"

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
# 3. Request Logging
# ═══════════════════════════════════════════════════════════════════════════════

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        ms = round((time.time() - start) * 1000, 2)
        ip = _get_client_ip(request)
        status = response.status_code
        if status >= 500:
            logger.error(f"SERVER_ERR {status}: {request.method} {request.url.path} {ms}ms ip={ip}")
        elif status in (401, 403, 429):
            logger.warning(f"SECURITY {status}: {request.method} {request.url.path} {ms}ms ip={ip}")
        return response


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Input Sanitization — block scanners and obvious injection in URL only
# ═══════════════════════════════════════════════════════════════════════════════

_INJECT_PATTERNS = re.compile(
    r"(\.\./|\.\.\\|/etc/passwd|cmd\.exe|powershell|wp-admin|"
    r"phpinfo|\.git/|wp-login|xmlrpc|eval\(|<script|javascript:|"
    r"union\s+select|drop\s+table|onload=|onerror=|"
    r"/etc/shadow|\.htaccess|\.htpasswd)",
    re.IGNORECASE,
)

_BAD_AGENTS = re.compile(
    r"(sqlmap|nikto|nmap|masscan|burpsuite|zgrab|dirbuster|"
    r"wfuzz|hydra|medusa|acunetix|nessus|w3af)",
    re.IGNORECASE,
)

class InputSanitizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path_and_query = request.url.path + "?" + str(request.url.query)

        if _INJECT_PATTERNS.search(path_and_query):
            ip = _get_client_ip(request)
            logger.warning(f"INJECT_BLOCKED: {ip} -> {request.url.path[:80]}")
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})

        ua = request.headers.get("user-agent", "")
        if _BAD_AGENTS.search(ua):
            ip = _get_client_ip(request)
            logger.warning(f"SCANNER_BLOCKED: {ip}")
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})

        return await call_next(request)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Brute Force Protection — simple, no threading locks, async-safe
# ═══════════════════════════════════════════════════════════════════════════════

# {ip: [timestamp, ...]} — only tracks auth failures
_auth_failures: dict[str, list] = defaultdict(list)
_auth_lockouts: dict[str, float] = {}   # ip -> unlock timestamp

MAX_AUTH_FAILS  = 10      # allow 10 failures before lockout
AUTH_WINDOW     = 600     # track over 10 min
AUTH_LOCKOUT    = 900     # lock for 15 min


class BruteForceMiddleware(BaseHTTPMiddleware):
    """Lock out IPs that repeatedly fail auth. No threading locks — async safe."""

    async def dispatch(self, request: Request, call_next):
        is_auth = (
            "/auth/login" in request.url.path or
            "/auth/register" in request.url.path
        ) and request.method == "POST"

        ip = _get_client_ip(request)
        now = time.time()

        # Check lockout
        if is_auth:
            unlock = _auth_lockouts.get(ip, 0)
            if now < unlock:
                wait = int(unlock - now)
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Too many failed attempts. Try again in {wait // 60}m {wait % 60}s."},
                    headers={"Retry-After": str(wait)},
                )

        response = await call_next(request)

        # Track failures
        if is_auth and response.status_code == 401:
            _auth_failures[ip] = [
                t for t in _auth_failures[ip] if now - t < AUTH_WINDOW
            ]
            _auth_failures[ip].append(now)
            if len(_auth_failures[ip]) >= MAX_AUTH_FAILS:
                _auth_lockouts[ip] = now + AUTH_LOCKOUT
                logger.warning(f"BRUTE_FORCE_LOCKOUT: {ip}")

        return response


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _get_client_ip(request: Request) -> str:
    if cf := request.headers.get("CF-Connecting-IP"):
        return cf.strip()
    if fwd := request.headers.get("X-Forwarded-For"):
        return fwd.split(",")[0].strip()
    if real := request.headers.get("X-Real-IP"):
        return real.strip()
    return request.client.host if request.client else "unknown"


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
    try:
        expected = hmac.new(
            secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected.lower(), received_sig.lower())
    except Exception:
        return False
