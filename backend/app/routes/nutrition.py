"""Nutrition tracker endpoints: parse, intake CRUD, targets, dashboard."""
import base64
from datetime import date as date_type
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, delete, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import IntakeEntry, NutritionTarget, User
from app.nutrition_ai import parse_intake
from app.schemas import (
    DayDashboardResponse,
    IntakeCreateRequest,
    IntakeEntryResponse,
    MacroTotals,
    ParseRequest,
    ParseResponse,
    ParsedFoodItem,
    TargetResponse,
    TargetUpsertRequest,
)

router = APIRouter(prefix="/api/nutrition", tags=["nutrition"])


# === helpers ===

def _sum_totals(items: list) -> MacroTotals:
    """Sum macro totals from a list of parsed items (dicts or ParsedFoodItem)."""
    def g(it, key):
        return it.get(key, 0) if isinstance(it, dict) else getattr(it, key, 0)
    return MacroTotals(
        kcal=sum(g(i, "kcal") for i in items),
        protein_g=sum(g(i, "protein_g") for i in items),
        carbs_g=sum(g(i, "carbs_g") for i in items),
        fat_g=sum(g(i, "fat_g") for i in items),
    )


def _detect_day_type(d: date_type) -> str:
    """Default day-type detection (smart version comes in Phase 2).

    For now: weekend if Sat/Sun, otherwise treat as rest (user can override per
    meal-day via the upcoming /workouts schedule check in Phase 2).
    """
    wd = d.weekday()  # 0=Mon
    if wd >= 5:
        return "weekend"
    if wd in (0, 3):  # ma + do = trainingsdagen volgens MACRO-TARGETS
        return "training"
    return "rest"


async def _get_target_for(db: AsyncSession, user_id, d: date_type, day_type: str) -> NutritionTarget | None:
    """Pick latest effective target for user × day_type. Falls back to default (None day_type)."""
    # try specific day_type first
    q = (
        select(NutritionTarget)
        .where(
            NutritionTarget.user_id == user_id,
            NutritionTarget.day_type == day_type,
            NutritionTarget.effective_from <= d,
        )
        .order_by(desc(NutritionTarget.effective_from))
        .limit(1)
    )
    result = (await db.execute(q)).scalar_one_or_none()
    if result:
        return result
    # fall back to default (day_type IS NULL)
    q2 = (
        select(NutritionTarget)
        .where(
            NutritionTarget.user_id == user_id,
            NutritionTarget.day_type.is_(None),
            NutritionTarget.effective_from <= d,
        )
        .order_by(desc(NutritionTarget.effective_from))
        .limit(1)
    )
    return (await db.execute(q2)).scalar_one_or_none()


def _entry_response(e: IntakeEntry) -> IntakeEntryResponse:
    return IntakeEntryResponse(
        id=e.id,
        client_entry_id=e.client_entry_id,
        date=e.date,
        meal_type=e.meal_type,
        raw_input=e.raw_input,
        has_photo=e.photo_blob is not None,
        parsed_foods=[ParsedFoodItem(**it) if isinstance(it, dict) else it for it in (e.parsed_foods or [])],
        totals=MacroTotals(
            kcal=e.total_kcal, protein_g=e.total_protein,
            carbs_g=e.total_carbs, fat_g=e.total_fat,
        ),
        ai_confidence=e.ai_confidence,
        user_corrected=e.user_corrected,
        created_at=e.created_at,
    )


# === endpoints ===

@router.post("/parse", response_model=ParseResponse)
async def parse(
    req: ParseRequest,
    user: User = Depends(get_current_user),
):
    """Parse text/photo input into structured macros. No DB write."""
    if not req.text and not req.image_b64:
        raise HTTPException(status_code=400, detail="Provide text or image_b64")
    try:
        result = parse_intake(text=req.text, image_b64=req.image_b64)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI parse failed: {e}")

    items = [ParsedFoodItem(**it) for it in result.get("items", [])]
    totals = _sum_totals(items)
    return ParseResponse(
        items=items,
        totals=totals,
        suggested_meal_type=result.get("suggested_meal_type"),
        overall_confidence=result.get("overall_confidence", "medium"),
        raw_text=req.text,
    )


@router.post("/intake", response_model=IntakeEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_intake(
    req: IntakeCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Persist a parsed/edited intake entry. Idempotent on client_entry_id."""
    # idempotency check
    existing = (await db.execute(
        select(IntakeEntry).where(
            IntakeEntry.user_id == user.id,
            IntakeEntry.client_entry_id == req.client_entry_id,
        )
    )).scalar_one_or_none()
    if existing:
        return _entry_response(existing)

    totals = _sum_totals([i.model_dump() for i in req.parsed_foods])
    photo_bytes: bytes | None = None
    if req.photo_b64:
        b64 = req.photo_b64.split(",", 1)[1] if req.photo_b64.startswith("data:") else req.photo_b64
        try:
            photo_bytes = base64.b64decode(b64)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid photo_b64")

    entry = IntakeEntry(
        user_id=user.id,
        client_entry_id=req.client_entry_id,
        date=req.date,
        meal_type=req.meal_type,
        raw_input=req.raw_input,
        photo_blob=photo_bytes,
        parsed_foods=[i.model_dump() for i in req.parsed_foods],
        total_kcal=totals.kcal,
        total_protein=totals.protein_g,
        total_carbs=totals.carbs_g,
        total_fat=totals.fat_g,
        ai_confidence=req.ai_confidence,
        user_corrected=req.user_corrected,
    )
    db.add(entry)
    await db.flush()
    return _entry_response(entry)


@router.get("/intake", response_model=list[IntakeEntryResponse])
async def list_intake(
    date: date_type = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List entries for a specific date."""
    rows = (await db.execute(
        select(IntakeEntry)
        .where(IntakeEntry.user_id == user.id, IntakeEntry.date == date)
        .order_by(IntakeEntry.created_at)
    )).scalars().all()
    return [_entry_response(e) for e in rows]


@router.delete("/intake/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_intake(
    entry_id,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete an intake entry (user-owned only)."""
    e = (await db.execute(
        select(IntakeEntry).where(IntakeEntry.id == entry_id, IntakeEntry.user_id == user.id)
    )).scalar_one_or_none()
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    await db.delete(e)
    await db.flush()


@router.get("/dashboard", response_model=DayDashboardResponse)
async def dashboard(
    date: date_type = Query(...),
    day_type: str | None = Query(None, description="Override day-type detection"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Combined dashboard for a day: target + entries + totals + delta."""
    dt = day_type or _detect_day_type(date)
    target = await _get_target_for(db, user.id, date, dt)
    if not target:
        # No targets seeded yet — return sensible defaults so UI doesn't break
        target_response = TargetResponse(day_type=dt, kcal=2000, protein_g=180, carbs_g=170, fat_g=65, source="default")
    else:
        target_response = TargetResponse(
            day_type=target.day_type,
            kcal=target.kcal, protein_g=target.protein_g,
            carbs_g=target.carbs_g, fat_g=target.fat_g,
            source=target.source,
        )

    rows = (await db.execute(
        select(IntakeEntry)
        .where(IntakeEntry.user_id == user.id, IntakeEntry.date == date)
        .order_by(IntakeEntry.created_at)
    )).scalars().all()
    entries = [_entry_response(e) for e in rows]
    totals = MacroTotals(
        kcal=sum(e.totals.kcal for e in entries),
        protein_g=sum(e.totals.protein_g for e in entries),
        carbs_g=sum(e.totals.carbs_g for e in entries),
        fat_g=sum(e.totals.fat_g for e in entries),
    )
    delta = MacroTotals(
        kcal=target_response.kcal - totals.kcal,
        protein_g=target_response.protein_g - totals.protein_g,
        carbs_g=target_response.carbs_g - totals.carbs_g,
        fat_g=target_response.fat_g - totals.fat_g,
    )
    return DayDashboardResponse(
        date=date, day_type=dt,
        target=target_response, entries=entries,
        totals=totals, delta=delta,
    )


@router.get("/targets", response_model=list[TargetResponse])
async def list_targets(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List latest effective target per day-type for this user."""
    # Get latest per day_type — use a subquery for "latest effective_from per day_type"
    rows = (await db.execute(
        select(NutritionTarget)
        .where(NutritionTarget.user_id == user.id)
        .order_by(NutritionTarget.day_type, desc(NutritionTarget.effective_from))
    )).scalars().all()
    # Dedupe to latest per day_type
    seen: set = set()
    latest = []
    for r in rows:
        key = r.day_type  # None counts as one bucket
        if key not in seen:
            seen.add(key)
            latest.append(r)
    return [
        TargetResponse(
            day_type=r.day_type, kcal=r.kcal,
            protein_g=r.protein_g, carbs_g=r.carbs_g, fat_g=r.fat_g,
            source=r.source,
        )
        for r in latest
    ]


@router.put("/targets", response_model=TargetResponse)
async def upsert_target(
    req: TargetUpsertRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a new effective target (history preserved — never overwrite, just newer row wins)."""
    from datetime import date as _date
    target = NutritionTarget(
        user_id=user.id,
        effective_from=_date.today(),
        day_type=req.day_type,
        kcal=req.kcal, protein_g=req.protein_g,
        carbs_g=req.carbs_g, fat_g=req.fat_g,
        source=req.source,
    )
    db.add(target)
    await db.flush()
    return TargetResponse(
        day_type=target.day_type, kcal=target.kcal,
        protein_g=target.protein_g, carbs_g=target.carbs_g, fat_g=target.fat_g,
        source=target.source,
    )
