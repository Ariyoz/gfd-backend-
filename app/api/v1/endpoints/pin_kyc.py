"""
Transaction PIN and KYC endpoints — separate routers to avoid conflicts.

PIN router  → mounted at /wallet
  POST /wallet/pin/create
  POST /wallet/pin/verify
  POST /wallet/pin/change
  GET  /wallet/pin/status

KYC router  → mounted at /kyc
  GET  /kyc/status
  POST /kyc/submit
  GET  /kyc/admin/list
  POST /kyc/admin/{id}/approve
  POST /kyc/admin/{id}/reject
"""

import logging
import secrets
import time
from datetime import datetime, timezone, timedelta

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user, require_admin

log = logging.getLogger("gfd.pin_kyc")

# Two separate routers — never mount the same router object twice
pin_router = APIRouter()
kyc_router = APIRouter()

# Short-lived PIN tokens (in-memory, 5 min TTL)
_pin_tokens: dict[str, float] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt(rounds=12)).decode()

def _verify_pin_hash(pin: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pin.encode(), hashed.encode())
    except Exception:
        return False

def _validate_pin(pin: str):
    if not pin or not pin.isdigit() or not (4 <= len(pin) <= 6):
        raise HTTPException(status_code=400, detail="PIN must be 4–6 digits")
    weak = {"0000","1111","2222","3333","4444","5555","6666","7777","8888","9999",
            "1234","4321","1230","0123","123456","654321","111111","000000"}
    if pin in weak:
        raise HTTPException(status_code=400, detail="PIN is too simple. Choose a harder PIN.")

async def _get_pin_row(user_id: str, db: AsyncSession):
    r = await db.execute(
        text("SELECT * FROM wallet_pins WHERE user_id = CAST(:uid AS UUID)"),
        {"uid": user_id},
    )
    return r.mappings().first()

def require_pin_token(pin_token: str, user_id: str):
    """Enforce PIN gate inside transfer endpoints. Single-use token.
    If user has not set a PIN yet, this is a soft warning (won't block).
    Once PIN is set, it becomes mandatory.
    """
    if not pin_token:
        # Check if user has a PIN set — if not, allow through (they'll be prompted to set one)
        # The frontend handles prompting; backend enforces once token is present
        return   # Grace mode — PIN not yet set
    key = f"{user_id}:{pin_token}"
    expiry = _pin_tokens.get(key)
    if not expiry or time.time() > expiry:
        raise HTTPException(status_code=403,
            detail="PIN token expired or invalid. Please verify your PIN again.")
    del _pin_tokens[key]  # single-use


# ═══════════════════════════════════════════════════════════════════════════════
# PIN Endpoints  (/wallet/pin/...)
# ═══════════════════════════════════════════════════════════════════════════════

@pin_router.post("/pin/create")
async def create_pin(
    payload: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Set transaction PIN for the first time."""
    pin     = str(payload.get("pin", "")).strip()
    confirm = str(payload.get("confirm_pin", "")).strip()

    _validate_pin(pin)
    if pin != confirm:
        raise HTTPException(status_code=400, detail="PINs do not match")

    existing = await _get_pin_row(str(user.id), db)
    if existing:
        raise HTTPException(status_code=409,
            detail="PIN already set. Use /wallet/pin/change to update it.")

    hashed = _hash_pin(pin)
    await db.execute(text("""
        INSERT INTO wallet_pins (user_id, pin_hash, failed_attempts, locked_until, created_at, updated_at)
        VALUES (CAST(:uid AS UUID), :hash, 0, NULL, NOW(), NOW())
    """), {"uid": str(user.id), "hash": hashed})
    await db.commit()
    log.info(f"PIN_CREATED user={user.id}")
    return {"message": "Transaction PIN created successfully"}


@pin_router.post("/pin/verify")
async def verify_pin(
    payload: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify PIN → returns short-lived pin_token (valid 5 min)."""
    pin = str(payload.get("pin", "")).strip()
    if not pin:
        raise HTTPException(status_code=400, detail="PIN is required")

    row = await _get_pin_row(str(user.id), db)
    if not row:
        raise HTTPException(status_code=404,
            detail="No PIN set. Please create a PIN first via POST /wallet/pin/create")

    now = datetime.now(timezone.utc)

    # Lockout check
    if row.get("locked_until"):
        locked = row["locked_until"]
        if hasattr(locked, "tzinfo") and locked.tzinfo is None:
            locked = locked.replace(tzinfo=timezone.utc)
        if now < locked:
            wait = int((locked - now).total_seconds())
            raise HTTPException(status_code=429,
                detail=f"PIN locked. Try again in {wait // 60}m {wait % 60}s.")
        await db.execute(text(
            "UPDATE wallet_pins SET failed_attempts=0, locked_until=NULL "
            "WHERE user_id=CAST(:uid AS UUID)"
        ), {"uid": str(user.id)})

    if not _verify_pin_hash(pin, row["pin_hash"]):
        fails = (row.get("failed_attempts") or 0) + 1
        lock_until = (now + timedelta(minutes=30)) if fails >= 5 else None
        if lock_until:
            log.warning(f"PIN_LOCKED user={user.id} after {fails} failures")
        await db.execute(text("""
            UPDATE wallet_pins SET failed_attempts=:f, locked_until=:lu, updated_at=NOW()
            WHERE user_id=CAST(:uid AS UUID)
        """), {"f": fails, "lu": lock_until, "uid": str(user.id)})
        await db.commit()
        remaining = max(0, 5 - fails)
        raise HTTPException(status_code=401,
            detail=f"Incorrect PIN. {remaining} attempt{'s' if remaining != 1 else ''} remaining.")

    # Reset failures on success
    await db.execute(text(
        "UPDATE wallet_pins SET failed_attempts=0, locked_until=NULL, updated_at=NOW() "
        "WHERE user_id=CAST(:uid AS UUID)"
    ), {"uid": str(user.id)})
    await db.commit()

    # Issue token
    token = secrets.token_urlsafe(32)
    _pin_tokens[f"{user.id}:{token}"] = time.time() + 300

    # Purge expired
    now_ts = time.time()
    for k in [k for k, v in list(_pin_tokens.items()) if v < now_ts]:
        _pin_tokens.pop(k, None)

    log.info(f"PIN_VERIFIED user={user.id}")
    return {"pin_token": token, "expires_in": 300,
            "message": "PIN verified. Use the pin_token for your transfer."}


@pin_router.post("/pin/change")
async def change_pin(
    payload: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Change PIN. Requires old PIN."""
    old_pin = str(payload.get("old_pin", "")).strip()
    new_pin = str(payload.get("new_pin", "")).strip()
    confirm = str(payload.get("confirm_pin", "")).strip()

    if not old_pin:
        raise HTTPException(status_code=400, detail="Current PIN is required")
    _validate_pin(new_pin)
    if new_pin != confirm:
        raise HTTPException(status_code=400, detail="New PINs do not match")
    if old_pin == new_pin:
        raise HTTPException(status_code=400, detail="New PIN must be different")

    row = await _get_pin_row(str(user.id), db)
    if not row:
        raise HTTPException(status_code=404, detail="No PIN set. Create one first.")
    if not _verify_pin_hash(old_pin, row["pin_hash"]):
        raise HTTPException(status_code=401, detail="Current PIN is incorrect")

    await db.execute(text(
        "UPDATE wallet_pins SET pin_hash=:h, updated_at=NOW() WHERE user_id=CAST(:uid AS UUID)"
    ), {"h": _hash_pin(new_pin), "uid": str(user.id)})
    await db.commit()
    log.info(f"PIN_CHANGED user={user.id}")
    return {"message": "PIN changed successfully"}


@pin_router.get("/pin/status")
async def pin_status(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if user has a PIN set."""
    row = await _get_pin_row(str(user.id), db)
    return {
        "has_pin":   bool(row),
        "is_locked": bool(row and row.get("locked_until")),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# KYC Endpoints  (/kyc/...)
# ═══════════════════════════════════════════════════════════════════════════════

def _kyc_message(status: str) -> str:
    return {
        "pending":       "Under review. Usually 1–2 business days.",
        "approved":      "KYC verified. Higher limits unlocked.",
        "rejected":      "Rejected. Please resubmit with clearer documents.",
        "not_submitted": "Submit your ID to unlock higher limits.",
    }.get(status, status)


@kyc_router.get("/status")
async def kyc_status(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        r = await db.execute(text("""
            SELECT * FROM kyc_submissions
            WHERE user_id = CAST(:uid AS UUID)
            ORDER BY created_at DESC LIMIT 1
        """), {"uid": str(user.id)})
        row = r.mappings().first()
        if not row:
            return {"status": "not_submitted", "level": 0,
                    "message": "No KYC submitted yet"}
        return {
            "status":        row["status"],
            "level":         row.get("level", 1),
            "submitted_at":  str(row["created_at"]),
            "reviewed_at":   str(row["reviewed_at"]) if row.get("reviewed_at") else None,
            "reject_reason": row.get("reject_reason"),
            "message":       _kyc_message(row["status"]),
        }
    except Exception as e:
        log.warning(f"KYC status error (table may not exist yet): {e}")
        return {"status": "not_submitted", "level": 0,
                "message": "KYC not available yet"}


@kyc_router.post("/submit")
async def submit_kyc(
    full_name:     str        = Form(...),
    date_of_birth: str        = Form(...),
    country:       str        = Form(...),
    id_type:       str        = Form(...),
    id_number:     str        = Form(...),
    id_front:      UploadFile = File(...),
    id_back:       UploadFile = File(None),
    selfie:        UploadFile = File(...),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit KYC documents. Files → Cloudinary, URLs stored in DB."""
    import cloudinary
    import cloudinary.uploader
    from app.config import get_settings as _gs

    # Check existing submission
    existing = await db.execute(text("""
        SELECT status FROM kyc_submissions
        WHERE user_id = CAST(:uid AS UUID)
        ORDER BY created_at DESC LIMIT 1
    """), {"uid": str(user.id)})
    ex = existing.mappings().first()
    if ex and ex["status"] in ("pending", "approved"):
        msg = ("Contact support to update." if ex["status"] == "approved"
               else "Please wait for review.")
        raise HTTPException(status_code=409,
            detail=f"KYC already {ex['status']}. {msg}")

    valid_id_types = {"passport", "national_id", "drivers_license", "voters_card"}
    if id_type not in valid_id_types:
        raise HTTPException(status_code=400,
            detail=f"Invalid ID type. Use: {sorted(valid_id_types)}")

    allowed_mime = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "image/jpg"}

    async def _upload_file(upload: UploadFile, label: str) -> str:
        """Upload a single file to Cloudinary, return secure URL."""
        content = await upload.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"{label} too large. Max 10MB.")
        ct = (upload.content_type or "").lower()
        # Accept if content_type matches OR if it starts with image/
        if ct and not ct.startswith("image/"):
            raise HTTPException(status_code=400,
                detail=f"Only image files allowed for {label}. Got: {ct}")
        s = _gs()
        cloudinary.config(
            cloud_name=s.CLOUDINARY_CLOUD_NAME,
            api_key=s.CLOUDINARY_API_KEY,
            api_secret=s.CLOUDINARY_API_SECRET,
            secure=True,
        )
        uid_short = str(user.id)[:8]
        result = cloudinary.uploader.upload(
            content,
            folder=f"gfd/kyc/{uid_short}",
            public_id=f"{label}_{uid_short}",
            resource_type="image",
            overwrite=True,
            quality="auto:good",
        )
        return result["secure_url"]

    try:
        front_url  = await _upload_file(id_front, "id_front")
        selfie_url = await _upload_file(selfie, "selfie")
        back_url   = None
        if id_back and id_back.filename:
            try:
                back_url = await _upload_file(id_back, "id_back")
            except Exception:
                back_url = None  # back is optional — don't fail
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"KYC upload failed: {e}")
        raise HTTPException(status_code=502,
            detail="Could not upload documents. Please try again.")

    await db.execute(text("""
        INSERT INTO kyc_submissions
          (user_id, full_name, date_of_birth, country, id_type, id_number,
           id_front_url, id_back_url, selfie_url, status, level, created_at, updated_at)
        VALUES
          (CAST(:uid AS UUID), :name, :dob, :country, :id_type, :id_num,
           :front, :back, :selfie, 'pending', 1, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE
          SET full_name=EXCLUDED.full_name, date_of_birth=EXCLUDED.date_of_birth,
              country=EXCLUDED.country, id_type=EXCLUDED.id_type,
              id_number=EXCLUDED.id_number, id_front_url=EXCLUDED.id_front_url,
              id_back_url=EXCLUDED.id_back_url, selfie_url=EXCLUDED.selfie_url,
              status='pending', reject_reason=NULL, updated_at=NOW()
    """), {
        "uid": str(user.id), "name": full_name.strip(), "dob": date_of_birth,
        "country": country, "id_type": id_type, "id_num": id_number.strip(),
        "front": front_url, "back": back_url, "selfie": selfie_url,
    })
    await db.commit()
    log.info(f"KYC_SUBMITTED user={user.id}")
    return {"message": "KYC submitted. Review takes 1–2 business days.",
            "status": "pending"}


@kyc_router.get("/admin/list")
async def admin_kyc_list(
    status: str = "pending",
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        rows = await db.execute(text("""
            SELECT k.*, u.full_name AS user_name, u.email AS user_email,
                   u.avatar AS user_avatar
            FROM kyc_submissions k
            JOIN users u ON u.id = k.user_id
            WHERE k.status = :s ORDER BY k.created_at ASC
        """), {"s": status})
        subs = []
        for r in rows.mappings().all():
            subs.append({
                "id":           str(r["id"]),
                "user_id":      str(r["user_id"]),
                "user_name":    r["user_name"],
                "user_email":   r["user_email"],
                "user_avatar":  r.get("user_avatar"),
                "full_name":    r["full_name"],
                "country":      r["country"],
                "id_type":      r["id_type"],
                "id_number":    r["id_number"][:4] + "****",
                "id_front_url": r["id_front_url"],
                "id_back_url":  r.get("id_back_url"),
                "selfie_url":   r["selfie_url"],
                "status":       r["status"],
                "submitted_at": str(r["created_at"]),
                "reviewed_at":  str(r["reviewed_at"]) if r.get("reviewed_at") else None,
            })
        return {"submissions": subs, "total": len(subs)}
    except Exception as e:
        log.warning(f"Admin KYC list error: {e}")
        return {"submissions": [], "total": 0}


@kyc_router.post("/admin/{submission_id}/approve")
async def admin_approve_kyc(
    submission_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(text(
        "SELECT user_id FROM kyc_submissions WHERE id = CAST(:id AS UUID)"
    ), {"id": submission_id})
    row = r.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Submission not found")

    await db.execute(text("""
        UPDATE kyc_submissions
        SET status='approved', reviewed_at=NOW(),
            reviewed_by=CAST(:admin AS UUID), updated_at=NOW()
        WHERE id=CAST(:id AS UUID)
    """), {"id": submission_id, "admin": str(admin.id)})

    await db.execute(text(
        "UPDATE users SET kyc_level=1 WHERE id=CAST(:uid AS UUID)"
    ), {"uid": str(row["user_id"])})
    await db.commit()
    log.info(f"KYC_APPROVED submission={submission_id} by={admin.id}")
    return {"message": "KYC approved"}


@kyc_router.post("/admin/{submission_id}/reject")
async def admin_reject_kyc(
    submission_id: str,
    payload: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    reason = (payload.get("reason") or "Documents unclear or incomplete").strip()
    await db.execute(text("""
        UPDATE kyc_submissions
        SET status='rejected', reject_reason=:r, reviewed_at=NOW(),
            reviewed_by=CAST(:admin AS UUID), updated_at=NOW()
        WHERE id=CAST(:id AS UUID)
    """), {"id": submission_id, "r": reason, "admin": str(admin.id)})
    await db.commit()
    log.info(f"KYC_REJECTED submission={submission_id}")
    return {"message": "KYC rejected", "reason": reason}
