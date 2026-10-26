"""Doelen-bijstelling, los van de DB. Draaien: python tests/test_target_updates.py app/routes/workouts.py (vanuit backend/)."""
import json, sys
sys.argv = ["x", sys.argv[1]]
from target_harness import run

def ladder(*xs): return [{"kg": k, "reps": r} for k, r in xs]
def ex(id, name, ts, alt=None):
    e = {"id": id, "name": name, "target_kg": max(t["kg"] for t in ts), "target_sets": ts}
    if alt: e["alt"] = {"name": alt[0], "target_kg": max(t["kg"] for t in alt[1]), "target_sets": alt[1]}
    return e
def schema(*exs): return {"supersets": [{"name": "A", "exercises": list(exs)}]}

fails = 0
def check(label, cond, info=""):
    global fails
    print(("OK  " if cond else "FOUT"), label, info)
    fails += (not cond)

# 1. Ruud vandaag, schema C slot a1: alternatief Sled Leg Press, ruim boven doel
C = schema(ex("a1", "Panatta Power Squat", ladder((40,12),(55,10),(60,10)),
              alt=("Sled Leg Press", ladder((100,10),(110,10),(110,10)))))
A = schema(ex("a1", "Sled Leg Press", ladder((115,10),(125,10),(125,10))))
B = schema(ex("a1", "Barbell Back Squat", ladder((20,10),(30,8)),
              alt=("Sled Leg Press", ladder((100,12),(110,12),(110,15)))))
ups, objs = run({"C": C, "A": A, "B": B}, "C", [("a1","Sled Leg Press",110,12),("a1","Sled Leg Press",120,12),("a1","Sled Leg Press",120,12)])
alt = objs["C"].data["supersets"][0]["exercises"][0]["alt"]["target_sets"]
prim = objs["C"].data["supersets"][0]["exercises"][0]["target_sets"]
print("  ", ups)
check("alternatief wordt bijgesteld", [t["kg"] for t in alt] == [110,120,120], alt)
check("hoofdoefening blijft ongemoeid", prim == ladder((40,12),(55,10),(60,10)))
check("A (zelfde rep-schema ×10, 125) niet omlaag/niet aangeraakt", [t["kg"] for t in objs["A"].data["supersets"][0]["exercises"][0]["target_sets"]] == [115,125,125])
check("B (×12/×15) niet meegetrokken", [t["kg"] for t in objs["B"].data["supersets"][0]["exercises"][0]["alt"]["target_sets"]] == [100,110,110])

# 2. Gewone ophoging hoofdoefening: +stap, niet meer
ups, objs = run({"C": schema(ex("c1","Seated Calf Raise", ladder((30,12),(35,12),(40,12))))}, "C",
                [("c1","Seated Calf Raise",30,12),("c1","Seated Calf Raise",35,12),("c1","Seated Calf Raise",40,12)])
ts = objs["C"].data["supersets"][0]["exercises"][0]["target_sets"]
check("precies doel gehaald → één stap omhoog", [t["kg"] for t in ts] == [35,40,45], ts)

# 3. Doorzetten naar ander schema: ladder schuift mee, wordt niet plat
X = schema(ex("b2","Lat Pulldown", ladder((40,12),(45,12),(45,12))))
Y = schema(ex("b2","Lat Pulldown", ladder((35,12),(40,12),(45,12))))
Z = schema(ex("b2","Lat Pulldown", ladder((30,15),(40,15),(45,15))))
ups, objs = run({"X": X, "Y": Y, "Z": Z}, "X", [("b2","Lat Pulldown",40,12),("b2","Lat Pulldown",45,12),("b2","Lat Pulldown",45,12)])
y = [t["kg"] for t in objs["Y"].data["supersets"][0]["exercises"][0]["target_sets"]]
z = [t["kg"] for t in objs["Z"].data["supersets"][0]["exercises"][0]["target_sets"]]
check("doorgezet als verschoven ladder (geen vlak 50)", y == [40,45,50], y)
check("ander rep-schema (×15) niet aangeraakt", z == [30,40,45], z)

# 4. Onder doel → niets
ups, objs = run({"A": schema(ex("b2","Lat Pulldown", ladder((55,12),(55,12),(55,12))))}, "A", [("b2","Lat Pulldown",50,10)]*3)
check("onder doel → geen wijziging", ups == [], ups)

# 5. Zelfde naam primair en alt: primair wint (die ziet Ruud)
S = schema(ex("b2","Lat Pulldown", ladder((50,12),(50,12),(50,12)), alt=("Lat Pulldown", ladder((40,12),(45,12),(45,12)))))
ups, objs = run({"S": S}, "S", [("b2","Lat Pulldown",50,12)]*3)
e = objs["S"].data["supersets"][0]["exercises"][0]
check("zelfde naam: hoofdoefening bijgesteld", [t["kg"] for t in e["target_sets"]] == [55,55,55], e["target_sets"])
check("zelfde naam: alt ongemoeid", [t["kg"] for t in e["alt"]["target_sets"]] == [40,45,45])

# 6. Alt via naam als slot-id niet klopt (schema herzien)
ups, objs = run({"C": C}, "C", [("zz","Sled Leg Press",100,10),("zz","Sled Leg Press",110,10),("zz","Sled Leg Press",110,10)])
alt = objs["C"].data["supersets"][0]["exercises"][0]["alt"]["target_sets"]
check("alt gevonden zonder geldig slot", [t["kg"] for t in alt] == [105,115,115], alt)
print("\nFOUTEN:", fails)
