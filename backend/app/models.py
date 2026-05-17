import uuid
from datetime import datetime, UTC, date as date_type
from sqlalchemy import String, Integer, Float, Boolean, ForeignKey, Text, DateTime, Date, LargeBinary
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=True)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    schemas: Mapped[list["Schema"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    workout_sessions: Mapped[list["WorkoutSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Equipment(Base):
    __tablename__ = "equipment"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)  # machine, cable, free_weight, bodyweight
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    location_hint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    weight_increment: Mapped[float] = mapped_column(Float, default=2.5)  # minimum kg step for this equipment

    exercises: Mapped[list["Exercise"]] = relationship(back_populates="equipment")


class Exercise(Base):
    __tablename__ = "exercises"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    aliases: Mapped[list] = mapped_column(JSONB, default=list)  # alternative names for matching
    equipment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("equipment.id"), nullable=True)
    muscles: Mapped[str] = mapped_column(String(255), nullable=False)
    dos: Mapped[dict] = mapped_column(JSONB, default=list)
    donts: Mapped[dict] = mapped_column(JSONB, default=list)

    equipment: Mapped[Equipment | None] = relationship(back_populates="exercises")
    workout_sets: Mapped[list["WorkoutSet"]] = relationship(back_populates="exercise")


class Schema(Base):
    __tablename__ = "schemas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(10), nullable=False)  # A, B, C, D
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    subtitle: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)  # full superset/exercise tree
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="schemas")
    workout_sessions: Mapped[list["WorkoutSession"]] = relationship(back_populates="schema")


class WorkoutSession(Base):
    __tablename__ = "workout_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    schema_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("schemas.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_from_offline: Mapped[bool] = mapped_column(Boolean, default=False)
    client_workout_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="workout_sessions")
    schema: Mapped[Schema] = relationship(back_populates="workout_sessions")
    sets: Mapped[list["WorkoutSet"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class WorkoutSet(Base):
    __tablename__ = "workout_sets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workout_sessions.id"), nullable=False)
    exercise_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("exercises.id"), nullable=True)
    exercise_name: Mapped[str] = mapped_column(String(100), nullable=False)  # denormalized for convenience
    superset_key: Mapped[str] = mapped_column(String(10), nullable=False)  # a1, b2, etc.
    set_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kg: Mapped[float] = mapped_column(Float, nullable=False)
    reps: Mapped[int] = mapped_column(Integer, nullable=False)
    target_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_reps: Mapped[int | None] = mapped_column(Integer, nullable=True)

    session: Mapped[WorkoutSession] = relationship(back_populates="sets")
    exercise: Mapped[Exercise | None] = relationship(back_populates="workout_sets")


# === NUTRITION TRACKER ===

class Food(Base):
    """Master food database. user_id NULL = shared/seed, else user-custom."""
    __tablename__ = "foods"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # 'manual', 'ai-estimated', 'open-food-facts'
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kcal_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    protein_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    carbs_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    fat_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    fiber_per_100g: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IntakeEntry(Base):
    """One logged meal/snack. parsed_foods is the source of truth for macros."""
    __tablename__ = "intake_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    client_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    meal_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ontbijt/lunch/diner/snack
    raw_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    parsed_foods: Mapped[list] = mapped_column(JSONB, nullable=False)
    total_kcal: Mapped[float] = mapped_column(Float, nullable=False)
    total_protein: Mapped[float] = mapped_column(Float, nullable=False)
    total_carbs: Mapped[float] = mapped_column(Float, nullable=False)
    total_fat: Mapped[float] = mapped_column(Float, nullable=False)
    ai_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NutritionTarget(Base):
    """Daily macro target per user, per day-type. Latest effective_from wins."""
    __tablename__ = "nutrition_targets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    effective_from: Mapped[date_type] = mapped_column(Date, nullable=False)
    day_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # training/rest/weekend/None=default
    kcal: Mapped[int] = mapped_column(Integer, nullable=False)
    protein_g: Mapped[int] = mapped_column(Integer, nullable=False)
    carbs_g: Mapped[int] = mapped_column(Integer, nullable=False)
    fat_g: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
