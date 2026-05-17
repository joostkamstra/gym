"""Nutrition tracker endpoints: parse, intake CRUD, targets, dashboard, trends, favorites, body measurements, reminders."""
import base64
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date as date_type, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select, delete, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

from app.auth import get_current_user
from app.database import get_db
from app.models import BodyMeasurement, IntakeEntry, NutritionTarget, User
from app.nutrition_ai import parse_intake, parse_measurement
from app.schemas import (
    BodyMeasurementCreate,
    BodyMeasurementResponse,
    DayDashboardResponse,
    IntakeCreateRequest,
    IntakeEntryResponse,
    MacroTotals,
    MeasurementParseRequest,
    MeasurementParseResponse,
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


async def _latest_measurement(db: AsyncSession, user_id) -> BodyMeasurement | None:
    """Most recent body measurement for the user (None if never logged)."""
    return (await db.execute(
        select(BodyMeasurement)
        .where(BodyMeasurement.user_id == user_id)
        .order_by(desc(BodyMeasurement.date), desc(BodyMeasurement.created_at))
        .limit(1)
    )).scalar_one_or_none()


def _smart_target(measurement: BodyMeasurement, day_type: str, cut_deficit_kcal: int = 400) -> tuple[dict, dict]:
    """Compute daily macro target from latest measurement + dagtype.

    Returns (target_dict, basis_dict). basis is opaque metadata for UI display.

    Approach:
    - BMR × 1.3 (light active baseline) = TDEE estimate
    - Apply cut deficit (kcal/day average across week)
    - Day-type adjustment: training +100 kcal, rest -100, weekend = base
    - Eiwit: 2.3 g/kg body weight (training/rest), 2.2 (weekend) — matches MACRO-TARGETS.md
    - Vet: 0.7 g/kg (training, hogere KH compenseert) of 0.8 g/kg (rest/weekend)
    - KH: residual after eiwit + vet
    """
    bmr = measurement.bmr
    weight = measurement.weight_kg
    if not bmr:
        # Mifflin-St Jeor fallback (men): BMR = 10*kg + 6.25*cm - 5*age + 5
        # Use Joost's defaults: 183cm, 39y → easily 50% wrong for general use, so prefer measured BMR
        bmr = round(10 * weight + 6.25 * 183 - 5 * 39 + 5)

    tdee = round(bmr * 1.3)
    base_kcal = tdee - cut_deficit_kcal  # weekly average

    if day_type == "training":
        kcal = base_kcal + 100
        protein_per_kg = 2.3
        fat_per_kg = 0.7
    elif day_type == "rest":
        kcal = base_kcal - 100
        protein_per_kg = 2.3
        fat_per_kg = 0.8
    else:  # weekend (and any unknown)
        kcal = base_kcal
        protein_per_kg = 2.2
        fat_per_kg = 0.8

    protein_g = round(weight * protein_per_kg)
    fat_g = round(weight * fat_per_kg)
    p_kcal = protein_g * 4
    f_kcal = fat_g * 9
    carbs_g = max(0, round((kcal - p_kcal - f_kcal) / 4))

    target = {"kcal": kcal, "protein_g": protein_g, "carbs_g": carbs_g, "fat_g": fat_g}
    basis = {
        "bmr": bmr,
        "tdee": tdee,
        "weight_kg": weight,
        "body_fat_pct": measurement.body_fat_pct,
        "lean_mass_kg": measurement.lean_mass_kg,
        "measurement_date": str(measurement.date),
        "cut_deficit_kcal": cut_deficit_kcal,
        "activity_factor": 1.3,
        "protein_per_kg": protein_per_kg,
        "fat_per_kg": fat_per_kg,
    }
    return target, basis


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
    mode: str = Query("smart", description="'smart' = use latest body measurement, 'manual' = seeded targets"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Combined dashboard for a day: target + entries + totals + delta.

    Default: smart-target from latest body measurement. Falls back to manual seed
    if no measurement exists, or if mode=manual is passed.
    """
    dt = day_type or _detect_day_type(date)
    target_basis = None
    target_response: TargetResponse | None = None

    if mode == "smart":
        m = await _latest_measurement(db, user.id)
        if m:
            tgt, basis = _smart_target(m, dt)
            target_response = TargetResponse(
                day_type=dt, kcal=tgt["kcal"], protein_g=tgt["protein_g"],
                carbs_g=tgt["carbs_g"], fat_g=tgt["fat_g"],
                source="smart-bmr-{}-{}".format(basis["bmr"], basis["measurement_date"]),
            )
            target_basis = basis

    if not target_response:
        target = await _get_target_for(db, user.id, date, dt)
        if not target:
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
        target=target_response, target_basis=target_basis,
        entries=entries, totals=totals, delta=delta,
    )


# === BODY MEASUREMENTS ===

def _bm_response(m: BodyMeasurement) -> BodyMeasurementResponse:
    return BodyMeasurementResponse(
        id=m.id, date=m.date, weight_kg=m.weight_kg, body_fat_pct=m.body_fat_pct,
        lean_mass_kg=m.lean_mass_kg, bmr=m.bmr, bmi=m.bmi,
        spiermassa_kg=m.spiermassa_kg, skeletspier_pct=m.skeletspier_pct,
        spiersnelheid_pct=m.spiersnelheid_pct, eiwit_pct=m.eiwit_pct,
        water_pct=m.water_pct, watergewicht_kg=m.watergewicht_kg,
        onderhuids_vet_pct=m.onderhuids_vet_pct, visceraal_vet=m.visceraal_vet,
        notes=m.notes, source=m.source, created_at=m.created_at,
    )


@router.post("/measurements", response_model=BodyMeasurementResponse, status_code=status.HTTP_201_CREATED)
async def create_measurement(
    req: BodyMeasurementCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Log a new body measurement. Drives smart-target calc on next dashboard load."""
    from datetime import date as _date
    # Auto-compute lean mass from weight + bf% if not provided
    lean = req.lean_mass_kg
    if lean is None and req.body_fat_pct is not None:
        lean = round(req.weight_kg * (1 - req.body_fat_pct / 100), 1)
    m = BodyMeasurement(
        user_id=user.id,
        date=req.date or _date.today(),
        weight_kg=req.weight_kg,
        body_fat_pct=req.body_fat_pct,
        lean_mass_kg=lean,
        bmr=req.bmr,
        bmi=req.bmi,
        spiermassa_kg=req.spiermassa_kg,
        skeletspier_pct=req.skeletspier_pct,
        spiersnelheid_pct=req.spiersnelheid_pct,
        eiwit_pct=req.eiwit_pct,
        water_pct=req.water_pct,
        watergewicht_kg=req.watergewicht_kg,
        onderhuids_vet_pct=req.onderhuids_vet_pct,
        visceraal_vet=req.visceraal_vet,
        notes=req.notes,
        source=req.source,
    )
    db.add(m)
    await db.flush()
    return _bm_response(m)


@router.post("/measurements/parse", response_model=MeasurementParseResponse)
async def parse_measurement_photo(
    req: MeasurementParseRequest,
    user: User = Depends(get_current_user),
):
    """Parse a Fitdays screenshot into a structured measurement. No DB write — frontend
    previews, user reviews, then POSTs to /measurements to save."""
    try:
        raw = parse_measurement(req.image_b64)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI parse failed: {e}")

    # Drop confidence from the BodyMeasurementCreate (it's not a column)
    confidence = raw.pop("confidence", "medium")
    # Map to BodyMeasurementCreate (auto-source = fitdays-vision)
    parsed = BodyMeasurementCreate(source="fitdays-vision", **raw)
    return MeasurementParseResponse(parsed=parsed, confidence=confidence, raw_extracted=raw)


# === TRENDS ===

@router.get("/trends")
async def nutrition_trends(
    days: int = Query(7, ge=1, le=90),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daily macro totals for the last N days, plus compliance % vs smart target.

    Returns: [{date, kcal, protein, carbs, fat, target_kcal, target_protein,
               kcal_pct, protein_pct, entries_count}, ...] newest first.
    """
    from datetime import date as _date
    since = _date.today() - timedelta(days=days - 1)
    rows = (await db.execute(
        select(IntakeEntry)
        .where(IntakeEntry.user_id == user.id, IntakeEntry.date >= since)
        .order_by(IntakeEntry.date)
    )).scalars().all()

    # Latest measurement drives target (snapshot in time would be more accurate
    # but for short windows latest is good enough)
    m = await _latest_measurement(db, user.id)

    by_date: dict = defaultdict(lambda: {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "n": 0})
    for e in rows:
        d = by_date[e.date]
        d["kcal"] += e.total_kcal
        d["protein_g"] += e.total_protein
        d["carbs_g"] += e.total_carbs
        d["fat_g"] += e.total_fat
        d["n"] += 1

    out = []
    for i in range(days):
        d = _date.today() - timedelta(days=i)
        bucket = by_date.get(d, {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0, "n": 0})
        dt = _detect_day_type(d)
        target = None
        if m:
            tgt, _ = _smart_target(m, dt)
            target = tgt
        else:
            t = await _get_target_for(db, user.id, d, dt)
            target = {"kcal": t.kcal, "protein_g": t.protein_g, "carbs_g": t.carbs_g, "fat_g": t.fat_g} if t else {"kcal": 2000, "protein_g": 180, "carbs_g": 170, "fat_g": 65}
        out.append({
            "date": str(d),
            "day_type": dt,
            "kcal": round(bucket["kcal"]),
            "protein_g": round(bucket["protein_g"]),
            "carbs_g": round(bucket["carbs_g"]),
            "fat_g": round(bucket["fat_g"]),
            "target_kcal": target["kcal"],
            "target_protein_g": target["protein_g"],
            "kcal_pct": round(bucket["kcal"] / target["kcal"] * 100, 1) if target["kcal"] else 0,
            "protein_pct": round(bucket["protein_g"] / target["protein_g"] * 100, 1) if target["protein_g"] else 0,
            "entries_count": bucket["n"],
        })
    return out  # newest-first because i=0 is today


# === FAVORITES ===

@router.get("/favorites")
async def favorites(
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(8, ge=1, le=30),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Most-frequent intake entries in last N days, deduplicated by item-name signature.

    Useful for 1-click "log opnieuw" buttons. Returns top-N with use_count and a
    representative recent entry's macro data.
    """
    from datetime import date as _date
    since = _date.today() - timedelta(days=days)
    rows = (await db.execute(
        select(IntakeEntry)
        .where(IntakeEntry.user_id == user.id, IntakeEntry.date >= since)
        .order_by(desc(IntakeEntry.created_at))
        .limit(300)
    )).scalars().all()

    by_sig: dict = {}
    for e in rows:
        names = tuple(sorted(((it.get("name", "") or "").lower().strip() for it in (e.parsed_foods or []))))
        if not names:
            continue
        sig = "|".join(names)
        if sig not in by_sig:
            by_sig[sig] = {"count": 0, "latest": e}
        by_sig[sig]["count"] += 1

    top = sorted(by_sig.items(), key=lambda kv: (-kv[1]["count"], -kv[1]["latest"].created_at.timestamp()))[:limit]
    return [
        {
            "signature": sig,
            "use_count": g["count"],
            "meal_type": g["latest"].meal_type,
            "raw_input": g["latest"].raw_input,
            "parsed_foods": g["latest"].parsed_foods,
            "totals": {
                "kcal": g["latest"].total_kcal,
                "protein_g": g["latest"].total_protein,
                "carbs_g": g["latest"].total_carbs,
                "fat_g": g["latest"].total_fat,
            },
            "last_used": str(g["latest"].date),
        }
        for sig, g in top
    ]


# === REMINDERS (Phase 5 — Telegram-based, HTTPS-free MVP) ===

# Hour-based rules (CEST). Each (hour, condition_fn, message_fn) is checked once
# per hour by the cron-trigger. condition_fn returns True if reminder should fire.
def _reminder_rules():
    return [
        # hour, label, condition (entries, totals, target, hour), message-template
        (10, "ontbijt-missing",
         lambda entries, t, tgt: not any(e.meal_type == "ontbijt" for e in entries),
         "🍽️ Vergeten ontbijt te loggen? Open de Voeding-tab om even bij te werken."),
        (14, "lunch-missing",
         lambda entries, t, tgt: not any(e.meal_type == "lunch" for e in entries),
         "🥗 Lunch nog niet gelogd?"),
        (19, "diner-missing",
         lambda entries, t, tgt: not any(e.meal_type == "diner" for e in entries),
         "🍝 Diner-tijd — vergeet niet te loggen."),
        (21, "eiwit-deficit",
         lambda entries, t, tgt: (tgt["protein_g"] - t["protein_g"]) > 40,
         lambda entries, t, tgt: f"⚠️ Eiwit-target nog {round(tgt['protein_g'] - t['protein_g'])}g te gaan vandaag (gegeten: {round(t['protein_g'])}g / {tgt['protein_g']}g)."),
    ]


async def _send_telegram(chat_id: str, text: str) -> bool:
    """Synchronous-ish via urllib (no aiohttp dep). Run in thread to avoid blocking."""
    import asyncio
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()

    def _post():
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status == 200
        except Exception:
            return False
    return await asyncio.get_event_loop().run_in_executor(None, _post)


@router.post("/check-reminders")
async def check_reminders(
    hour: int | None = Query(None, ge=0, le=23, description="Force hour (testing). Default: now CEST"),
    dry_run: bool = Query(False),
    x_reminder_secret: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint called by cron (RemoteTrigger). Secret-protected so it can run
    without JWT. For each registered user, evaluates today's state vs reminder rules
    for the current hour and sends Telegram notifications. Idempotent per-hour by
    design (cron runs once per hour, so each rule fires at most once)."""
    settings = get_settings()
    if not settings.REMINDER_SECRET or x_reminder_secret != settings.REMINDER_SECRET:
        raise HTTPException(status_code=403, detail="Bad reminder secret")

    # Determine current hour in CEST
    now_cest = datetime.now(ZoneInfo("Europe/Amsterdam"))
    current_hour = hour if hour is not None else now_cest.hour
    today = now_cest.date()

    # Hardcoded user-to-telegram mapping for MVP — Joost only
    # (Future: user_settings.telegram_chat_id column)
    from app.models import User
    user_telegram: dict[str, str] = {}
    if settings.TELEGRAM_CHAT_ID_JOOST:
        joost_q = await db.execute(select(User).where(User.username == "joost"))
        joost = joost_q.scalar_one_or_none()
        if joost:
            user_telegram[str(joost.id)] = settings.TELEGRAM_CHAT_ID_JOOST

    results: list[dict] = []
    rules = _reminder_rules()
    for user_id, chat_id in user_telegram.items():
        import uuid as _uuid
        uid = _uuid.UUID(user_id)

        # Today's state
        dt = _detect_day_type(today)
        m = await _latest_measurement(db, uid)
        target = None
        if m:
            tgt, _ = _smart_target(m, dt)
            target = tgt
        else:
            t = await _get_target_for(db, uid, today, dt)
            target = {"kcal": t.kcal, "protein_g": t.protein_g, "carbs_g": t.carbs_g, "fat_g": t.fat_g} if t else {"kcal": 2000, "protein_g": 180, "carbs_g": 170, "fat_g": 65}

        entries = (await db.execute(
            select(IntakeEntry).where(IntakeEntry.user_id == uid, IntakeEntry.date == today)
        )).scalars().all()
        totals = {
            "kcal": sum(e.total_kcal for e in entries),
            "protein_g": sum(e.total_protein for e in entries),
            "carbs_g": sum(e.total_carbs for e in entries),
            "fat_g": sum(e.total_fat for e in entries),
        }

        for rule_hour, label, cond, msg in rules:
            if rule_hour != current_hour:
                continue
            if cond(entries, totals, target):
                text = msg(entries, totals, target) if callable(msg) else msg
                sent = False
                if not dry_run:
                    sent = await _send_telegram(chat_id, text)
                results.append({
                    "user_id": user_id, "rule": label, "hour": rule_hour,
                    "message": text, "sent": sent,
                })

    return {"checked_hour": current_hour, "today": str(today), "results": results}


@router.get("/measurements", response_model=list[BodyMeasurementResponse])
async def list_measurements(
    days: int = Query(60, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Recent body measurements, newest first."""
    from datetime import date as _date, timedelta
    since = _date.today() - timedelta(days=days)
    rows = (await db.execute(
        select(BodyMeasurement)
        .where(BodyMeasurement.user_id == user.id, BodyMeasurement.date >= since)
        .order_by(desc(BodyMeasurement.date), desc(BodyMeasurement.created_at))
    )).scalars().all()
    return [_bm_response(m) for m in rows]


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
