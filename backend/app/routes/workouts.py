import copy
from datetime import datetime, UTC
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, attributes

from app.database import get_db
from app.models import User, Schema, WorkoutSession, WorkoutSet, Exercise
from app.auth import get_current_user
from app.schemas import (
    WorkoutCreateRequest,
    WorkoutResponse,
    WorkoutSetResponse,
    WorkoutListResponse,
    ExerciseProgressResponse,
    ProgressPoint,
    WorkoutEvaluation,
    ExerciseDelta,
    TargetUpdate,
)

router = APIRouter(prefix="/api/workouts", tags=["workouts"])


def _get_weight_increment(target_kg: float, equipment_increment: float | None = None) -> float:
    """Return weight increment from equipment data, or fallback heuristic."""
    if equipment_increment and equipment_increment > 0:
        return equipment_increment
    return 2.5 if target_kg >= 50 else 1.0


def _find_exercise_in_schema(schema_data: dict, exercise_name: str) -> tuple[dict | None, str | None]:
    """Find an exercise in the schema data by name. Returns (exercise_dict, exercise_id) or (None, None)."""
    for superset in schema_data.get("supersets", []):
        for exercise in superset.get("exercises", []):
            if exercise.get("name") == exercise_name:
                return exercise, exercise.get("id")
    return None, None


async def auto_update_targets(
    db: AsyncSession,
    schema: Schema,
    workout_sets: list[WorkoutSet],
) -> list[TargetUpdate]:
    """Apply double progression protocol to update schema targets based on workout performance.

    Rules:
    1. ALL sets hit target reps at target weight → increase weight, reset reps to minimum
    2. A specific set exceeds target reps (but not all) → update that set's target_reps
    3. A specific set exceeds target kg → update that set's target_kg (and reps)
    4. Performance below target → keep targets the same
    """
    updates: list[TargetUpdate] = []
    schema_data = copy.deepcopy(schema.data)

    # Load exercise → equipment weight_increment mapping
    exercise_names = {ws.exercise_name for ws in workout_sets}
    from sqlalchemy.orm import joinedload
    ex_result = await db.execute(
        select(Exercise).options(joinedload(Exercise.equipment)).where(Exercise.name.in_(exercise_names))
    )
    increment_map: dict[str, float] = {}
    for ex in ex_result.scalars().unique().all():
        if ex.equipment and ex.equipment.weight_increment:
            increment_map[ex.name] = ex.equipment.weight_increment

    # Group workout sets by exercise name
    sets_by_exercise: dict[str, list[WorkoutSet]] = {}
    for ws in workout_sets:
        sets_by_exercise.setdefault(ws.exercise_name, []).append(ws)

    for exercise_name, actual_sets in sets_by_exercise.items():
        exercise_def, _ = _find_exercise_in_schema(schema_data, exercise_name)
        if not exercise_def:
            continue

        target_sets = exercise_def.get("target_sets", [])
        if not target_sets:
            continue

        # Sort actual sets by set_number
        actual_sets_sorted = sorted(actual_sets, key=lambda s: s.set_number)

        # Check if ALL sets met or exceeded targets (for weight bump)
        all_sets_hit = True
        set_results = []  # (set_index, actual, target, met_target)

        for actual in actual_sets_sorted:
            set_idx = actual.set_number - 1  # set_number is 1-based
            if set_idx < 0 or set_idx >= len(target_sets):
                continue

            target = target_sets[set_idx]
            t_kg = target.get("kg", 0)
            t_reps = target.get("reps", 0)

            met = actual.kg >= t_kg and actual.reps >= t_reps
            set_results.append((set_idx, actual, target, met))

            if not met:
                all_sets_hit = False

        if not set_results:
            continue

        if all_sets_hit and len(set_results) == len(target_sets):
            # Rule 1: All sets hit → bump weight, reset reps to minimum across target reps
            increment = _get_weight_increment(target_sets[0].get("kg", 0), increment_map.get(exercise_name))
            # Find the minimum target reps (the "reset" value)
            min_reps = min(t.get("reps", 0) for t in target_sets)
            if min_reps == 0:
                min_reps = target_sets[0].get("reps", 8)

            for set_idx, actual, target, _ in set_results:
                old_kg = target.get("kg", 0)
                old_reps = target.get("reps", 0)
                new_kg = old_kg + increment
                new_reps = min_reps

                target_sets[set_idx]["kg"] = new_kg
                target_sets[set_idx]["reps"] = new_reps

                updates.append(TargetUpdate(
                    exercise_name=exercise_name,
                    set_number=set_idx + 1,
                    old_kg=old_kg,
                    new_kg=new_kg,
                    old_reps=old_reps,
                    new_reps=new_reps,
                    reason="weight_up",
                ))

            # Also update top-level target_kg if present
            if "target_kg" in exercise_def:
                exercise_def["target_kg"] = target_sets[0]["kg"]
        else:
            # Rule 2 & 3: Per-set updates
            for set_idx, actual, target, met in set_results:
                old_kg = target.get("kg", 0)
                old_reps = target.get("reps", 0)
                changed = False
                new_kg = old_kg
                new_reps = old_reps
                reason = "no_change"

                if actual.kg > old_kg:
                    # Rule 3: Actual kg exceeds target → update target kg and reps
                    new_kg = actual.kg
                    new_reps = actual.reps
                    reason = "kg_up"
                    changed = True
                elif actual.kg >= old_kg and actual.reps > old_reps:
                    # Rule 2: Reps exceeded at target weight → update reps
                    new_reps = actual.reps
                    reason = "reps_up"
                    changed = True

                if changed:
                    target_sets[set_idx]["kg"] = new_kg
                    target_sets[set_idx]["reps"] = new_reps
                    updates.append(TargetUpdate(
                        exercise_name=exercise_name,
                        set_number=set_idx + 1,
                        old_kg=old_kg,
                        new_kg=new_kg,
                        old_reps=old_reps,
                        new_reps=new_reps,
                        reason=reason,
                    ))

    # Save updated schema data back to DB
    if updates:
        schema.data = schema_data
        attributes.flag_modified(schema, "data")
        await db.flush()

    return updates


def _build_workout_response(
    session: WorkoutSession,
    target_updates: list[TargetUpdate] | None = None,
) -> WorkoutResponse:
    return WorkoutResponse(
        id=session.id,
        schema_key=session.schema.key,
        schema_name=session.schema.name,
        date=session.date,
        feedback=session.feedback,
        notes=session.notes,
        sets=[
            WorkoutSetResponse(
                exercise_name=s.exercise_name,
                superset_key=s.superset_key,
                set_number=s.set_number,
                kg=s.kg,
                reps=s.reps,
                target_kg=s.target_kg,
                target_reps=s.target_reps,
            )
            for s in sorted(session.sets, key=lambda s: (s.superset_key, s.set_number))
        ],
        target_updates=target_updates,
        created_at=session.created_at,
    )


@router.get("", response_model=WorkoutListResponse)
async def list_workouts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    schema_key: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(WorkoutSession)
        .options(selectinload(WorkoutSession.sets), selectinload(WorkoutSession.schema))
        .where(WorkoutSession.user_id == user.id)
    )
    count_query = select(func.count()).select_from(WorkoutSession).where(WorkoutSession.user_id == user.id)

    if schema_key:
        schema_subq = select(Schema.id).where(Schema.user_id == user.id, Schema.key == schema_key)
        query = query.where(WorkoutSession.schema_id.in_(schema_subq))
        count_query = count_query.where(WorkoutSession.schema_id.in_(schema_subq))

    total = (await db.execute(count_query)).scalar() or 0
    result = await db.execute(
        query.order_by(desc(WorkoutSession.date))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    sessions = result.scalars().unique().all()

    return WorkoutListResponse(
        workouts=[_build_workout_response(s) for s in sessions],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=WorkoutResponse, status_code=status.HTTP_201_CREATED)
async def create_workout(
    req: WorkoutCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Find schema
    result = await db.execute(
        select(Schema).where(Schema.user_id == user.id, Schema.key == req.schema_key)
    )
    schema = result.scalar_one_or_none()
    if not schema:
        raise HTTPException(status_code=404, detail=f"Schema {req.schema_key} not found")

    # Resolve exercise IDs
    exercise_names = {s.exercise_name for s in req.sets}
    ex_result = await db.execute(select(Exercise).where(Exercise.name.in_(exercise_names)))
    exercise_map = {e.name: e.id for e in ex_result.scalars().all()}

    # Create session — strip timezone for naive TIMESTAMP columns
    workout_date = req.date.replace(tzinfo=None) if req.date.tzinfo else req.date
    session = WorkoutSession(
        user_id=user.id,
        schema_id=schema.id,
        date=workout_date,
        feedback=req.feedback,
        notes=req.notes,
    )
    db.add(session)
    await db.flush()

    # Create sets
    for s in req.sets:
        workout_set = WorkoutSet(
            session_id=session.id,
            exercise_id=exercise_map.get(s.exercise_name),
            exercise_name=s.exercise_name,
            superset_key=s.superset_key,
            set_number=s.set_number,
            kg=s.kg,
            reps=s.reps,
            target_kg=s.target_kg,
            target_reps=s.target_reps,
        )
        db.add(workout_set)

    await db.flush()

    # Auto-update targets using double progression protocol
    target_updates = await auto_update_targets(db, schema, [
        ws for ws in (
            await db.execute(
                select(WorkoutSet).where(WorkoutSet.session_id == session.id)
            )
        ).scalars().all()
    ])

    # Reload with relationships
    result = await db.execute(
        select(WorkoutSession)
        .options(selectinload(WorkoutSession.sets), selectinload(WorkoutSession.schema))
        .where(WorkoutSession.id == session.id)
    )
    session = result.scalar_one()
    return _build_workout_response(session, target_updates=target_updates)


@router.delete("/{workout_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workout(
    workout_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WorkoutSession).where(
            WorkoutSession.id == workout_id, WorkoutSession.user_id == user.id
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Workout not found")
    await db.delete(session)


@router.get("/{workout_id}/evaluation", response_model=WorkoutEvaluation)
async def get_evaluation(
    workout_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Load current workout
    result = await db.execute(
        select(WorkoutSession)
        .options(selectinload(WorkoutSession.sets), selectinload(WorkoutSession.schema))
        .where(WorkoutSession.id == workout_id, WorkoutSession.user_id == user.id)
    )
    current = result.scalar_one_or_none()
    if not current:
        raise HTTPException(status_code=404, detail="Workout not found")

    # Find previous session with same schema
    prev_result = await db.execute(
        select(WorkoutSession)
        .options(selectinload(WorkoutSession.sets))
        .where(
            WorkoutSession.user_id == user.id,
            WorkoutSession.schema_id == current.schema_id,
            WorkoutSession.date < current.date,
        )
        .order_by(desc(WorkoutSession.date))
        .limit(1)
    )
    previous = prev_result.scalar_one_or_none()

    # Build previous best map: exercise_name -> (max_kg, max_reps_at_max_kg)
    prev_best = {}
    if previous:
        for s in previous.sets:
            existing = prev_best.get(s.exercise_name)
            if not existing or s.kg > existing[0] or (s.kg == existing[0] and s.reps > existing[1]):
                prev_best[s.exercise_name] = (s.kg, s.reps)

    # Build current best map
    curr_best = {}
    for s in current.sets:
        existing = curr_best.get(s.exercise_name)
        if not existing or s.kg > existing[0] or (s.kg == existing[0] and s.reps > existing[1]):
            curr_best[s.exercise_name] = (s.kg, s.reps)

    # Calculate deltas
    deltas = []
    for name, (c_kg, c_reps) in curr_best.items():
        p = prev_best.get(name)
        if p:
            kg_change = c_kg - p[0]
            reps_change = c_reps - p[1]
            if kg_change > 0:
                verdict = "up"
            elif kg_change < 0:
                verdict = "down"
            elif reps_change > 0:
                verdict = "up"
            elif reps_change < 0:
                verdict = "down"
            else:
                verdict = "stable"
            deltas.append(ExerciseDelta(
                exercise_name=name,
                previous_best_kg=p[0],
                previous_best_reps=p[1],
                current_best_kg=c_kg,
                current_best_reps=c_reps,
                kg_change=kg_change,
                reps_change=reps_change,
                verdict=verdict,
            ))
        else:
            deltas.append(ExerciseDelta(
                exercise_name=name,
                previous_best_kg=None,
                previous_best_reps=None,
                current_best_kg=c_kg,
                current_best_reps=c_reps,
                kg_change=None,
                reps_change=None,
                verdict="new",
            ))

    # Summary
    ups = sum(1 for d in deltas if d.verdict == "up")
    downs = sum(1 for d in deltas if d.verdict == "down")
    stables = sum(1 for d in deltas if d.verdict == "stable")
    news = sum(1 for d in deltas if d.verdict == "new")
    parts = []
    if ups:
        parts.append(f"{ups} omhoog")
    if stables:
        parts.append(f"{stables} stabiel")
    if downs:
        parts.append(f"{downs} omlaag")
    if news:
        parts.append(f"{news} nieuw")
    summary = ", ".join(parts) if parts else "Geen data"

    return WorkoutEvaluation(
        workout_id=current.id,
        schema_name=current.schema.name,
        date=current.date,
        previous_date=previous.date if previous else None,
        deltas=deltas,
        summary=summary,
    )


@router.get("/progress/{exercise_name}", response_model=ExerciseProgressResponse)
async def get_progress(
    exercise_name: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WorkoutSet, WorkoutSession.date)
        .join(WorkoutSession)
        .where(
            WorkoutSession.user_id == user.id,
            WorkoutSet.exercise_name == exercise_name,
        )
        .order_by(WorkoutSession.date, WorkoutSet.set_number)
    )
    rows = result.all()

    return ExerciseProgressResponse(
        exercise_name=exercise_name,
        data_points=[
            ProgressPoint(date=date, kg=ws.kg, reps=ws.reps, set_number=ws.set_number)
            for ws, date in rows
        ],
    )
