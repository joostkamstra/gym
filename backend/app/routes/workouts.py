from datetime import datetime, UTC
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
)

router = APIRouter(prefix="/api/workouts", tags=["workouts"])


def _build_workout_response(session: WorkoutSession) -> WorkoutResponse:
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

    # Reload with relationships
    result = await db.execute(
        select(WorkoutSession)
        .options(selectinload(WorkoutSession.sets), selectinload(WorkoutSession.schema))
        .where(WorkoutSession.id == session.id)
    )
    session = result.scalar_one()
    return _build_workout_response(session)


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
