"""Security middleware — headers, request logging, request ID, rate limiting protection."""

import uuid
import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

logger = logging.getLogger("gfd.middleware")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        response = await call_next(request)

        # ── Core security headers ──
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), payment=(), camera=(self), microphone=(self)"
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"

        # ── CSP — frame-ancestors replaces X-Frame-Options ──
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors 'none'; "
            "default-src 'self' https:; "
            "script-src 'self' 'unsafe-inline' https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "img-src 'self' data: https: blob:; "
            "connect-src 'self' https: wss: ws:; "
            "font-src 'self' https: data:; "
            "media-src 'self' https: blob:; "
            "object-src 'none'; "
            "base-uri 'self';"
        )

        # ── Cache headers ──
        if request.url.path.startswith('/api/'):
            if '/auth/' in request.url.path or '/admin/' in request.url.path:
                response.headers["Cache-Control"] = "no-store, private"
            elif request.method == "GET":
                path = request.url.path
                if any(p in path for p in ['/projects', '/explore', '/jobs']):
                    response.headers["Cache-Control"] = "public, max-age=30, stale-while-revalidate=120"
                elif '/feed' in path or '/notifications' in path:
                    response.headers["Cache-Control"] = "private, max-age=5, stale-while-revalidate=30"
                else:
                    response.headers["Cache-Control"] = "public, max-age=10, stale-while-revalidate=60"

        # ── Ensure JSON responses include charset ──
        ct = response.headers.get("content-type", "")
        if "application/json" in ct and "charset" not in ct:
            response.headers["content-type"] = "application/json; charset=utf-8"

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
    """Log request method, path, status, and duration."""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = round((time.time() - start) * 1000, 2)

        # Log suspicious activity
        if response.status_code in (401, 403, 429):
            logger.warning(
                f"SECURITY: {request.method} {request.url.path} -> {response.status_code} from {request.client.host if request.client else 'unknown'}"
            )

        # Log brute force attempts (multiple 401s)
        if response.status_code == 401 and '/auth/' in request.url.path:
            logger.warning(
                f"AUTH_FAIL: {request.method} {request.url.path} from {request.client.host if request.client else 'unknown'}"
            )

        logger.info(
            f"{request.method} {request.url.path} -> {response.status_code} ({duration}ms)",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration,
                "ip": request.client.host if request.client else "unknown",
            },
        )
        return response


class InputSanitizationMiddleware(BaseHTTPMiddleware):
    """Block common attack patterns in URL paths only (not request bodies)."""

    BLOCKED_PATH_PATTERNS = [
        '../', '..\\', '/etc/passwd', '/proc/', 'cmd.exe', 'powershell',
        '.env', 'wp-admin', 'phpinfo', '.git/', 'wp-login', 'xmlrpc',
        'shell', 'eval(', 'exec(', '<script', 'javascript:',
    ]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path.lower()
        query = str(request.url.query).lower() if request.url.query else ""

        for pattern in self.BLOCKED_PATH_PATTERNS:
            if pattern.lower() in path or pattern.lower() in query:
                logger.warning(f"BLOCKED: Suspicious request from {request.client.host}: {request.url}")
                return JSONResponse(status_code=403, content={"detail": "Forbidden"})

        return await call_next(request)
