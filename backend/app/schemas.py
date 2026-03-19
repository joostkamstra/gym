"""Pydantic request/response models."""
import uuid
from datetime import datetime
from pydantic import BaseModel


# Auth
class LoginRequest(BaseModel):
    username: str
    pin: str


class LoginResponse(BaseModel):
    token: str
    user: "UserResponse"


class UserResponse(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    email: str | None


# Schemas
class SchemaResponse(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    subtitle: str
    description: str
    sort_order: int
    data: dict


class SchemaListResponse(BaseModel):
    schemas: list[SchemaResponse]


# Equipment & Exercises
class EquipmentResponse(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    brand: str | None
    location_hint: str | None


class ExerciseResponse(BaseModel):
    id: uuid.UUID
    name: str
    muscles: str
    dos: list[str]
    donts: list[str]
    equipment: EquipmentResponse | None


# Workouts
class WorkoutSetInput(BaseModel):
    exercise_name: str
    superset_key: str  # a1, b2, etc.
    set_number: int
    kg: float
    reps: int
    target_kg: float | None = None
    target_reps: int | None = None


class WorkoutCreateRequest(BaseModel):
    schema_key: str  # A, B, C, D
    date: datetime
    feedback: str | None = None
    notes: str | None = None
    sets: list[WorkoutSetInput]


class WorkoutSetResponse(BaseModel):
    exercise_name: str
    superset_key: str
    set_number: int
    kg: float
    reps: int
    target_kg: float | None
    target_reps: int | None


class TargetUpdate(BaseModel):
    exercise_name: str
    set_number: int
    old_kg: float
    new_kg: float
    old_reps: int
    new_reps: int
    reason: str  # "weight_up", "reps_up", "kg_up", "no_change"


class WorkoutResponse(BaseModel):
    id: uuid.UUID
    schema_key: str
    schema_name: str
    date: datetime
    feedback: str | None
    notes: str | None
    sets: list[WorkoutSetResponse]
    target_updates: list[TargetUpdate] | None = None
    created_at: datetime


class WorkoutListResponse(BaseModel):
    workouts: list[WorkoutResponse]
    total: int
    page: int
    page_size: int


# Progress
class ProgressPoint(BaseModel):
    date: datetime
    kg: float
    reps: int
    set_number: int


class ExerciseProgressResponse(BaseModel):
    exercise_name: str
    data_points: list[ProgressPoint]


# Evaluation
class ExerciseDelta(BaseModel):
    exercise_name: str
    previous_best_kg: float | None
    previous_best_reps: int | None
    current_best_kg: float
    current_best_reps: int
    kg_change: float | None
    reps_change: int | None
    verdict: str  # "up", "down", "stable", "new"


class WorkoutEvaluation(BaseModel):
    workout_id: uuid.UUID
    schema_name: str
    date: datetime
    previous_date: datetime | None
    deltas: list[ExerciseDelta]
    summary: str
    target_updates: list[TargetUpdate] | None = None


class SchemaTargetsResponse(BaseModel):
    schema_key: str
    schema_name: str
    exercises: list[dict]  # [{name, id, target_sets: [{kg, reps}]}]


# Bulk import
class BulkWorkoutInput(BaseModel):
    schema_key: str
    schema_name: str
    date: datetime
    feedback: str | None = None
    exercises: list[dict]  # raw format from localStorage
