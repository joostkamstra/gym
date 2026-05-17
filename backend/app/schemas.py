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
    client_workout_id: uuid.UUID | None = None  # client-generated UUID for idempotent retries


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
    reason: str  # "weight_up", "reps_up", "kg_up", "no_change", "propagated"
    propagated_to: list[str] | None = None  # schema keys where target was also bumped


class WorkoutResponse(BaseModel):
    id: uuid.UUID
    schema_key: str
    schema_name: str
    date: datetime
    feedback: str | None
    notes: str | None
    sets: list[WorkoutSetResponse]
    target_updates: list[TargetUpdate] | None = None
    client_workout_id: uuid.UUID | None = None
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


# === NUTRITION ===
from datetime import date as date_type


class ParsedFoodItem(BaseModel):
    name: str
    quantity_g: float
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    confidence: str | None = None  # 'high', 'medium', 'low'
    notes: str | None = None
    food_id: uuid.UUID | None = None  # if matched to known food


class ParseRequest(BaseModel):
    text: str | None = None
    image_b64: str | None = None  # JPEG/PNG base64 (no data: prefix)


class MacroTotals(BaseModel):
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float


class ParseResponse(BaseModel):
    items: list[ParsedFoodItem]
    totals: MacroTotals
    suggested_meal_type: str | None = None
    overall_confidence: str = "medium"
    raw_text: str | None = None  # echo back so frontend has source-of-truth


class IntakeCreateRequest(BaseModel):
    client_entry_id: uuid.UUID
    date: date_type
    meal_type: str | None = None
    raw_input: str | None = None
    photo_b64: str | None = None  # optional, will be stored as bytea
    parsed_foods: list[ParsedFoodItem]
    ai_confidence: str | None = None
    user_corrected: bool = False


class IntakeEntryResponse(BaseModel):
    id: uuid.UUID
    client_entry_id: uuid.UUID | None
    date: date_type
    meal_type: str | None
    raw_input: str | None
    has_photo: bool
    parsed_foods: list[ParsedFoodItem]
    totals: MacroTotals
    ai_confidence: str | None
    user_corrected: bool
    created_at: datetime


class TargetResponse(BaseModel):
    day_type: str | None  # None = default
    kcal: int
    protein_g: int
    carbs_g: int
    fat_g: int
    source: str | None


class TargetUpsertRequest(BaseModel):
    day_type: str | None = None  # None means default
    kcal: int
    protein_g: int
    carbs_g: int
    fat_g: int
    source: str = "manual"


class DayDashboardResponse(BaseModel):
    date: date_type
    day_type: str | None
    target: TargetResponse
    target_basis: dict | None = None  # only set when smart target used
    entries: list[IntakeEntryResponse]
    totals: MacroTotals
    delta: MacroTotals  # target - totals (positive = nog te eten)


# === BODY MEASUREMENTS ===

class BodyMeasurementCreate(BaseModel):
    date: date_type | None = None  # defaults to today
    weight_kg: float
    body_fat_pct: float | None = None
    lean_mass_kg: float | None = None
    bmr: int | None = None
    bmi: float | None = None
    spiermassa_kg: float | None = None
    skeletspier_pct: float | None = None
    spiersnelheid_pct: float | None = None
    eiwit_pct: float | None = None
    water_pct: float | None = None
    watergewicht_kg: float | None = None
    onderhuids_vet_pct: float | None = None
    visceraal_vet: float | None = None
    notes: str | None = None
    source: str = "manual"


class BodyMeasurementResponse(BaseModel):
    id: uuid.UUID
    date: date_type
    weight_kg: float
    body_fat_pct: float | None
    lean_mass_kg: float | None
    bmr: int | None
    bmi: float | None
    spiermassa_kg: float | None
    skeletspier_pct: float | None
    spiersnelheid_pct: float | None
    eiwit_pct: float | None
    water_pct: float | None
    watergewicht_kg: float | None
    onderhuids_vet_pct: float | None
    visceraal_vet: float | None
    notes: str | None
    source: str
    created_at: datetime


class MeasurementParseRequest(BaseModel):
    image_b64: str


class MeasurementParseResponse(BaseModel):
    parsed: BodyMeasurementCreate
    confidence: str  # 'high', 'medium', 'low'
    raw_extracted: dict | None = None  # debug: alle velden die Claude eruit haalde


# === DAILY ACTIVITY CORRECTION ===

class ActivityCorrectionRequest(BaseModel):
    image_b64: str
    date: date_type | None = None  # defaults today


class ActivityCorrectionResponse(BaseModel):
    date: date_type
    active_kcal: int
    exercise_min: int | None = None
    standing_hours: float | None = None
    bmr_used: int
    deficit_used: int
    baseline_target_kcal: int  # what smart-target would have been (activity-factor based)
    adjusted_target_kcal: int  # new target = BMR + active_kcal - deficit
    extra_kcal: int  # adjusted - baseline (positive = je mag meer eten)
    target: TargetResponse  # full macro split met aangepaste KH
    confidence: str
