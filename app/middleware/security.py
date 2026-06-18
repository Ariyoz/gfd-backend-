"""Security middleware — hardened headers, rate limiting, DoS protection, injection blocking."""

import uuid
import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

logger = logging.getLogger("gfd.middleware")

# ── Request body size limits per route type ──────────────────────────────────
MAX_BODY_SIZES = {
    "/uploads/":  50 * 1024 * 1024,   # 50 MB — file uploads
    "/messages/upload": 22 * 1024 * 1024,  # 22 MB — message attachments
    "default":    1 * 1024 * 1024,    # 1 MB — all other JSON endpoints
}

# ── Auth brute-force tracking (in-memory, per IP) ────────────────────────────
_auth_attempts: dict[str, list[float]] = {}
AUTH_MAX_ATTEMPTS = 10      # attempts per window
AUTH_WINDOW_SECONDS = 300   # 5 minutes


def _is_auth_brute_forced(ip: str) -> bool:
    now = time.time()
    attempts = _auth_attempts.get(ip, [])
    # Remove stale attempts outside the window
    attempts = [t for t in attempts if now - t < AUTH_WINDOW_SECONDS]
    _auth_attempts[ip] = attempts
    return len(attempts) >= AUTH_MAX_ATTEMPTS


def _record_auth_attempt(ip: str):
    now = time.time()
    attempts = _auth_attempts.get(ip, [])
    attempts.append(now)
    _auth_attempts[ip] = attempts[-AUTH_MAX_ATTEMPTS * 2:]  # cap list size


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add comprehensive security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # XSS protection (legacy browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Force HTTPS for 2 years, include subdomains
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        # Limit referrer info
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Permissions policy — allow camera/mic for WebRTC calls
        response.headers["Permissions-Policy"] = (
            "camera=(self), microphone=(self), geolocation=(), payment=()"
        )
        # Content Security Policy
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'none'; "
            "frame-ancestors 'none';"
        )
        # Prevent caching of auth/admin responses
        if "/auth/" in request.url.path or "/admin/" in request.url.path:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        # Remove server version header leakage
        response.headers.pop("server", None)
        response.headers.pop("x-powered-by", None)
        # Cross-Origin policies
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"

        return response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to each request for tracing."""

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log request method, path, status, and duration. Flag security events."""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        client_ip = request.client.host if request.client else "unknown"

        response = await call_next(request)
        duration_ms = round((time.time() - start) * 1000, 2)

        # Log auth failures for brute-force monitoring
        if response.status_code == 401 and "/auth/" in request.url.path:
            logger.warning(f"AUTH_FAIL [{client_ip}] {request.method} {request.url.path}")
            _record_auth_attempt(client_ip)

        # Log forbidden / rate-limited hits
        if response.status_code in (403, 429):
            logger.warning(
                f"BLOCKED [{client_ip}] {request.method} {request.url.path} -> {response.status_code}"
            )

        logger.info(
            f"{request.method} {request.url.path} {response.status_code} {duration_ms}ms [{client_ip}]"
        )
        return response


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    """Block IPs that exceed auth attempt limits — works alongside slowapi."""

    async def dispatch(self, request: Request, call_next):
        if "/auth/login" in request.url.path or "/auth/register" in request.url.path:
            client_ip = request.client.host if request.client else "unknown"
            if _is_auth_brute_forced(client_ip):
                logger.warning(f"BRUTE_FORCE_BLOCKED [{client_ip}]")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many attempts. Please wait 5 minutes."},
                    headers={"Retry-After": "300"},
                )
        return await call_next(request)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds the allowed size for that route."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            size = int(content_length)
            # Choose limit based on route
            limit = MAX_BODY_SIZES["default"]
            for prefix, max_size in MAX_BODY_SIZES.items():
                if prefix != "default" and request.url.path.startswith(prefix):
                    limit = max_size
                    break
            if size > limit:
                logger.warning(
                    f"PAYLOAD_TOO_LARGE [{request.client.host if request.client else '?'}] "
                    f"{request.url.path} size={size} limit={limit}"
                )
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body too large. Maximum is {limit // (1024*1024)} MB."},
                )
        return await call_next(request)


class InputSanitizationMiddleware(BaseHTTPMiddleware):
    """Block common attack patterns in URL paths and query strings."""

    # Path traversal, shell injection, common scanner probes
    BLOCKED_PATTERNS = [
        "../", "..\\", "/etc/passwd", "/etc/shadow", "/proc/",
        "cmd.exe", "powershell", "/bin/sh", "/bin/bash",
        ".env", ".git/", "wp-admin", "wp-login", "phpinfo", "xmlrpc",
        "eval(", "exec(", "system(", "passthru(", "shell_exec(",
        "<script", "javascript:", "vbscript:", "data:text/html",
        "union select", "' or '1'='1", "\" or \"1\"=\"1",
        "onload=", "onerror=", "onclick=",
        "SLEEP(", "WAITFOR DELAY", "pg_sleep(",
    ]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path.lower()
        query = str(request.url.query).lower() if request.url.query else ""
        check = path + " " + query

        for pattern in self.BLOCKED_PATTERNS:
            if pattern.lower() in check:
                client_ip = request.client.host if request.client else "unknown"
                logger.warning(
                    f"INJECTION_BLOCKED [{client_ip}] pattern='{pattern}' url={request.url}"
                )
                return JSONResponse(status_code=403, content={"detail": "Forbidden"})

        return await call_next(request)
