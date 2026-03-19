from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, Schema
from app.auth import get_current_user
from app.schemas import SchemaResponse, SchemaListResponse

router = APIRouter(prefix="/api/schemas", tags=["schemas"])


@router.get("", response_model=SchemaListResponse)
async def get_schemas(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Schema)
        .where(Schema.user_id == user.id)
        .order_by(Schema.sort_order)
    )
    schemas = result.scalars().all()
    return SchemaListResponse(
        schemas=[
            SchemaResponse(
                id=s.id,
                key=s.key,
                name=s.name,
                subtitle=s.subtitle,
                description=s.description,
                sort_order=s.sort_order,
                data=s.data,
            )
            for s in schemas
        ]
    )


class SchemaImportItem(BaseModel):
    key: str
    name: str
    subtitle: str
    desc: str
    supersets: list[dict]


class SchemaImportRequest(BaseModel):
    schemas: dict[str, SchemaImportItem]


@router.post("/import")
async def import_schemas(
    req: SchemaImportRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Delete existing schemas for this user
    await db.execute(delete(Schema).where(Schema.user_id == user.id))

    sort_map = {"A": 0, "B": 1, "C": 2, "D": 3}
    created = []
    for key, s in req.schemas.items():
        schema = Schema(
            user_id=user.id,
            key=key,
            name=s.name,
            subtitle=s.subtitle,
            description=s.desc,
            sort_order=sort_map.get(key, 10),
            data={"supersets": s.supersets},
        )
        db.add(schema)
        created.append(key)

    return {"imported": created, "count": len(created)}
