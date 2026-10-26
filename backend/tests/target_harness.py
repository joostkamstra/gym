"""Draait auto_update_targets + propagate_target_increase los, met nep-DB."""
import ast, asyncio, copy, json, sys
from types import SimpleNamespace as NS

SRC = sys.argv[1]
WANTED = {"_get_weight_increment", "_round_to_increment", "_find_exercise_in_schema",
          "_find_target_def", "propagate_target_increase", "auto_update_targets"}
tree = ast.parse(open(SRC).read())
funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
code = "from __future__ import annotations\n" + "\n\n".join(ast.get_source_segment(open(SRC).read(), f) for f in funcs)

class TargetUpdate:
    def __init__(self, **kw): self.__dict__.update(kw); self.__dict__.setdefault("propagated_to", None)
    def __repr__(self): return f"{self.exercise_name} s{self.set_number}: {self.old_kg}x{self.old_reps}->{self.new_kg}x{self.new_reps} ({self.reason}) prop={self.propagated_to}"

class Chain:
    def __getattr__(self, k): return lambda *a, **kw: self
class Col:
    def __getattr__(self, k): return Col()
    def __eq__(self, o): return Chain()
    def __ne__(self, o): return Chain()
    def in_(self, *a): return Chain()

class FakeDB:
    def __init__(self, others): self.others = others; self.n = 0
    async def execute(self, q):
        self.n += 1
        items = [] if self.n == 1 else self.others  # 1e call = equipment, daarna = andere schema's
        return NS(scalars=lambda: NS(unique=lambda: NS(all=lambda: items), all=lambda: items))
    async def flush(self): pass

g = {"copy": copy, "TargetUpdate": TargetUpdate, "select": lambda *a: Chain(),
     "Schema": NS(user_id=Col(), id=Col()), "Exercise": NS(name=Col(), equipment=Col()),
     "attributes": NS(flag_modified=lambda *a: None), "WorkoutSet": object,
     "AsyncSession": object, "UUID": object}
import sqlalchemy.orm
exec(code, g)
# joinedload wordt lokaal geïmporteerd in auto_update_targets; die moet het nep-attribuut slikken
sqlalchemy.orm.joinedload = lambda *a, **kw: None

def run(schemas, key, sets):
    """schemas: {key: data}. sets: [(slot, naam, kg, reps), ...] per set in volgorde."""
    objs = {k: NS(key=k, id=k, user_id="u", data=copy.deepcopy(d)) for k, d in schemas.items()}
    src = objs[key]
    cnt = {}
    ws = []
    for slot, naam, kg, reps in sets:
        cnt[naam] = cnt.get(naam, 0) + 1
        ws.append(NS(exercise_id=None, exercise_name=naam, superset_key=slot, set_number=cnt[naam], kg=kg, reps=reps))
    others = [o for k, o in objs.items() if k != key]
    ups = asyncio.run(g["auto_update_targets"](FakeDB(others), src, ws))
    return ups, objs
