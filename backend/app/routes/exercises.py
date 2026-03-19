from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models import Exercise, Equipment
from app.auth import get_current_user, User
from app.schemas import ExerciseResponse, EquipmentResponse

router = APIRouter(prefix="/api", tags=["exercises"])


@router.get("/exercises", response_model=list[ExerciseResponse])
async def get_exercises(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Exercise).options(joinedload(Exercise.equipment)).order_by(Exercise.name)
    )
    exercises = result.scalars().unique().all()
    return [
        ExerciseResponse(
            id=e.id,
            name=e.name,
            muscles=e.muscles,
            dos=e.dos or [],
            donts=e.donts or [],
            equipment=EquipmentResponse(
                id=e.equipment.id,
                name=e.equipment.name,
                type=e.equipment.type,
                brand=e.equipment.brand,
                location_hint=e.equipment.location_hint,
            ) if e.equipment else None,
        )
        for e in exercises
    ]


@router.get("/equipment", response_model=list[EquipmentResponse])
async def get_equipment(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Equipment).order_by(Equipment.name))
    equipment = result.scalars().all()
    return [
        EquipmentResponse(
            id=e.id, name=e.name, type=e.type, brand=e.brand, location_hint=e.location_hint,
        )
        for e in equipment
    ]
