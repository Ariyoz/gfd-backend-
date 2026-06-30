"""Wallet endpoints — Phase 2. Not yet available."""

from fastapi import APIRouter

router = APIRouter()


@router.get("")
@router.get("/")
async def wallet_phase2():
    return {"detail": "Wallet is coming in Phase 2. Stay tuned!"}


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def wallet_catch_all(path: str):
    return {"detail": "Wallet is coming in Phase 2. Stay tuned!"}
