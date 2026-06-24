"""Security middleware — headers, request logging, request ID, rate limiting protection."""

import uuid
import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

logger = logging.getLogger("gfd.middleware")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add comprehensive security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        # Let preflight OPTIONS pass straight through — CORS middleware handles it
        if request.method == "OPTIONS":
            return await call_next(request)

        response = await call_next(request)
        # Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # XSS protection
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Force HTTPS
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        # Control referrer info
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Restrict browser features (allow camera/mic for calls)
        response.headers["Permissions-Policy"] = "geolocation=(), payment=()"
        # Content Security Policy - permissive for API
        if not request.url.path.startswith('/api/'):
            response.headers["Content-Security-Policy"] = "default-src 'self' https:; script-src 'self' 'unsafe-inline' https:; style-src 'self' 'unsafe-inline' https:; img-src 'self' data: https: blob:; connect-src 'self' https: wss:; font-src 'self' https: data:; media-src 'self' https: blob:;"
        # Prevent caching of sensitive data
        if '/auth/' in request.url.path or '/admin/' in request.url.path:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"
        # Cross-Origin policies — allow cross-origin fetches from our frontend
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
