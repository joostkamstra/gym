"""Seed database with initial data: users, equipment, exercises, schemas.

Run: cd backend && python -m seed
"""
import asyncio
import json
from pathlib import Path

from sqlalchemy import select, text
from app.database import engine, async_session, Base
from app.models import User, Equipment, Exercise, Schema
from app.auth import hash_pin


EQUIPMENT_DATA = [
    # weight_increment: minimum kg step for this equipment
    # Barbell/plates: 5kg (2.5kg per side), Dumbbells: 2.5kg, Cable stacks: 5kg, Machines: 5kg, Bodyweight: 0
    {"name": "Squat Rack", "type": "free_weight", "brand": None, "location_hint": "Free weight area", "weight_increment": 5},
    {"name": "Smith Machine", "type": "machine", "brand": None, "location_hint": "Free weight area", "weight_increment": 5},
    {"name": "Sled Leg Press", "type": "machine", "brand": None, "location_hint": "Leg area", "weight_increment": 5},
    {"name": "V-Squat Machine", "type": "machine", "brand": None, "location_hint": "Leg area", "weight_increment": 5},
    {"name": "Hack Squat", "type": "machine", "brand": None, "location_hint": "Leg area", "weight_increment": 5},
    {"name": "Leg Curl Machine (Seated)", "type": "machine", "brand": None, "location_hint": "Leg area", "weight_increment": 5},
    {"name": "Leg Curl Machine (Lying)", "type": "machine", "brand": "Panatta", "location_hint": "Leg area", "weight_increment": 5},
    {"name": "Leg Extension Machine", "type": "machine", "brand": "Technogym", "location_hint": "Leg area", "weight_increment": 5},
    {"name": "Hip Thrust Machine", "type": "machine", "brand": None, "location_hint": "Leg area", "weight_increment": 5},
    {"name": "Flat Bench", "type": "free_weight", "brand": None, "location_hint": "Bench area", "weight_increment": 5},
    {"name": "Incline Bench (Adjustable)", "type": "free_weight", "brand": None, "location_hint": "Bench area", "weight_increment": 2.5},
    {"name": "Vertical Chest Press", "type": "machine", "brand": "Panatta", "location_hint": "Machine area", "weight_increment": 5},
    {"name": "Machine Incline Press", "type": "machine", "brand": "Technogym", "location_hint": "Machine area", "weight_increment": 5},
    {"name": "Cable Crossover Station", "type": "cable", "brand": None, "location_hint": "Cable area", "weight_increment": 5},
    {"name": "Lat Pulldown Machine", "type": "cable", "brand": None, "location_hint": "Cable area", "weight_increment": 5},
    {"name": "Seated Cable Row Station", "type": "cable", "brand": None, "location_hint": "Cable area", "weight_increment": 5},
    {"name": "Plate-loaded Row", "type": "machine", "brand": "Panatta", "location_hint": "Machine area", "weight_increment": 5},
    {"name": "Shoulder Press Machine", "type": "machine", "brand": "Technogym", "location_hint": "Machine area", "weight_increment": 5},
    {"name": "Ab Machine", "type": "machine", "brand": "Technogym", "location_hint": "Core area", "weight_increment": 5},
    {"name": "Lower Back Machine", "type": "machine", "brand": None, "location_hint": "Core area", "weight_increment": 5},
    {"name": "Captain's Chair", "type": "bodyweight", "brand": None, "location_hint": "Core area", "weight_increment": 0},
    {"name": "Dips Press Machine", "type": "machine", "brand": "Panatta", "location_hint": "Machine area", "weight_increment": 5},
    {"name": "Preacher Curl Bench", "type": "free_weight", "brand": None, "location_hint": "Free weight area", "weight_increment": 2.5},
    {"name": "Dumbbell Rack (5-30 kg)", "type": "free_weight", "brand": None, "location_hint": "Dumbbell area", "weight_increment": 2.5},
    {"name": "Barbell + Plates", "type": "free_weight", "brand": None, "location_hint": "Free weight area", "weight_increment": 5},
    {"name": "EZ Curl Bar", "type": "free_weight", "brand": None, "location_hint": "Free weight area", "weight_increment": 2.5},
    {"name": "Cable Station (Multi)", "type": "cable", "brand": None, "location_hint": "Cable area", "weight_increment": 5},
    {"name": "Stationary Bike", "type": "machine", "brand": None, "location_hint": "Cardio area", "weight_increment": 0},
    {"name": "Resistance Bands", "type": "bodyweight", "brand": None, "location_hint": "Stretching area", "weight_increment": 0},
    {"name": "Pull-up Bar", "type": "bodyweight", "brand": None, "location_hint": "Free weight area", "weight_increment": 0},
]


EXERCISE_DATA = [
    {"name": "Barbell Squat", "muscles": "Quadriceps, glutes, core", "equipment": "Squat Rack"},
    {"name": "Sled Leg Press", "muscles": "Quadriceps, glutes", "equipment": "Sled Leg Press"},
    {"name": "V-Squat", "muscles": "Quadriceps, glutes, hamstrings", "equipment": "V-Squat Machine"},
    {"name": "Bulgarian Split Squat", "muscles": "Quadriceps, glutes, core (stabilisatie)", "equipment": "Smith Machine"},
    {"name": "Hack Squat", "muscles": "Quadriceps, glutes", "equipment": "Hack Squat"},
    {"name": "Romanian Deadlift", "muscles": "Hamstrings, glutes, onderrug", "equipment": "Barbell + Plates"},
    {"name": "Leg Curl (seated)", "muscles": "Hamstrings", "equipment": "Leg Curl Machine (Seated)"},
    {"name": "Lying Leg Curl", "muscles": "Hamstrings", "equipment": "Leg Curl Machine (Lying)"},
    {"name": "Hip Thrust Machine", "muscles": "Glutes (primair), hamstrings", "equipment": "Hip Thrust Machine"},
    {"name": "Leg Extension", "muscles": "Quadriceps", "equipment": "Leg Extension Machine"},
    {"name": "Flat Bench Press", "muscles": "Borst, triceps, voorste schouder", "equipment": "Flat Bench"},
    {"name": "DB Incline Press", "muscles": "Bovenborst, schouders, triceps", "equipment": "Incline Bench (Adjustable)"},
    {"name": "DB Flat Bench Press", "muscles": "Borst, triceps, voorste schouder", "equipment": "Flat Bench"},
    {"name": "Vertical Chest Press", "muscles": "Borst, triceps, voorste schouder", "equipment": "Vertical Chest Press"},
    {"name": "Cable Fly", "muscles": "Borst (stretch + squeeze focus)", "equipment": "Cable Crossover Station"},
    {"name": "Machine Incline Press", "muscles": "Bovenborst, schouders, triceps", "equipment": "Machine Incline Press"},
    {"name": "Lat Pulldown", "muscles": "Lats, biceps, middenrug", "equipment": "Lat Pulldown Machine"},
    {"name": "Straight-arm Pulldown", "muscles": "Lats, teres major", "equipment": "Cable Station (Multi)"},
    {"name": "Seated Cable Row", "muscles": "Middenrug, lats, biceps", "equipment": "Seated Cable Row Station"},
    {"name": "Plate-loaded Row", "muscles": "Lats, middenrug, biceps", "equipment": "Plate-loaded Row"},
    {"name": "DB Shoulder Press", "muscles": "Schouders (deltoids), triceps", "equipment": "Incline Bench (Adjustable)"},
    {"name": "Smith Shoulder Press", "muscles": "Schouders (deltoids), triceps", "equipment": "Smith Machine"},
    {"name": "Arnold Press", "muscles": "Schouders (alle hoofden), triceps", "equipment": "Incline Bench (Adjustable)"},
    {"name": "DB Lateral Raise", "muscles": "Zijschouder (mediale deltoid)", "equipment": "Dumbbell Rack (5-30 kg)"},
    {"name": "Cable Lateral Raise", "muscles": "Zijschouder (mediale deltoid)", "equipment": "Cable Station (Multi)"},
    {"name": "Shoulder Press Machine", "muscles": "Schouders (deltoids), triceps", "equipment": "Shoulder Press Machine"},
    {"name": "Cable Crunch", "muscles": "Rectus abdominis (sixpack)", "equipment": "Cable Station (Multi)"},
    {"name": "Ab Machine", "muscles": "Rectus abdominis (sixpack)", "equipment": "Ab Machine"},
    {"name": "Leg Raise", "muscles": "Onderbuik, hip flexors", "equipment": "Captain's Chair"},
    {"name": "Pallof Press", "muscles": "Core (anti-rotatie), obliques", "equipment": "Cable Station (Multi)"},
    {"name": "Woodchop", "muscles": "Obliques, core (rotatie)", "equipment": "Cable Station (Multi)"},
    {"name": "Lower Back Machine", "muscles": "Erector spinae, glutes", "equipment": "Lower Back Machine"},
    {"name": "Tricep Pushdown", "muscles": "Triceps", "equipment": "Cable Station (Multi)"},
    {"name": "OH Tricep Extension", "muscles": "Triceps (lange kop)", "equipment": "Cable Station (Multi)"},
    {"name": "Dips Press", "muscles": "Triceps, borst, voorste schouder", "equipment": "Dips Press Machine"},
    {"name": "Cable Bicep Curl", "muscles": "Biceps, brachialis", "equipment": "Cable Station (Multi)"},
    {"name": "DB Hammer Curl", "muscles": "Biceps, brachialis, brachioradialis", "equipment": "Dumbbell Rack (5-30 kg)"},
    {"name": "Preacher Curl", "muscles": "Biceps (korte kop), brachialis", "equipment": "Preacher Curl Bench"},
    {"name": "Pull-ups", "muscles": "Lats, biceps, middenrug", "equipment": "Pull-up Bar"},
    {"name": "Dips", "muscles": "Triceps, borst, voorste schouder", "equipment": "Captain's Chair"},
    {"name": "DB RDL", "muscles": "Hamstrings, glutes, onderrug", "equipment": "Dumbbell Rack (5-30 kg)"},
    {"name": "Dead Bug", "muscles": "Core (TVA, rectus abdominis)", "equipment": None},
    {"name": "Plank", "muscles": "Core (rectus abdominis, obliques, TVA)", "equipment": None},
    {"name": "Seated Row Machine", "muscles": "Middenrug, lats, biceps", "equipment": None},
    {"name": "Hanging Leg Raise", "muscles": "Onderbuik, hip flexors", "equipment": "Pull-up Bar"},
]


async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        # Check if already seeded
        result = await db.execute(select(User).limit(1))
        if result.scalar_one_or_none():
            print("Database already seeded. Skipping.")
            return

        # Equipment
        equipment_map = {}
        for eq in EQUIPMENT_DATA:
            e = Equipment(**eq)
            db.add(e)
            equipment_map[eq["name"]] = e
        await db.flush()

        # Exercises
        for ex in EXERCISE_DATA:
            equip = equipment_map.get(ex.get("equipment"))
            e = Exercise(
                name=ex["name"],
                muscles=ex["muscles"],
                equipment_id=equip.id if equip else None,
                dos=[],
                donts=[],
            )
            db.add(e)
        await db.flush()

        # Users (PIN: 1234 for both, change in production!)
        joost = User(
            username="joost",
            display_name="Joost",
            email="kamstra@gmail.com",
            pin_hash=hash_pin("1234"),
        )
        ruud = User(
            username="ruud",
            display_name="Ruud",
            email="kamstra@gmail.com",
            pin_hash=hash_pin("1234"),
        )
        db.add(joost)
        db.add(ruud)
        await db.flush()

        # Load schemas from the current index.html USERS object
        # For now, we extract the schema data structure manually
        # In production, this would be loaded from the frontend JS
        print(f"Created users: joost ({joost.id}), ruud ({ruud.id})")
        print(f"Created {len(equipment_map)} equipment items")
        print(f"Created {len(EXERCISE_DATA)} exercises")
        print("NOTE: Schema data needs to be imported separately via the bulk-import endpoint or migration script.")
        print("Default PIN for both users: 1234 — CHANGE IN PRODUCTION!")

        await db.commit()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
