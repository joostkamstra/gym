"""Open Food Facts integratie + lokale food-matching.

Verankert de macro-dichtheid aan een echte voedingsdatabase. De AI doet
identificatie + portie-schatting; de dichtheid (kcal/eiwit/KH/vet per 100g)
komt uit Open Food Facts (ODbL-open data) wanneer een product gevonden wordt.

HTTP-functies (lookup_barcode, search_by_name) zijn blocking — wrap ze in
asyncio.to_thread vanuit async endpoints. match_food/cache_food zijn async (DB).
"""
import json
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Food

_OFF_BASE = "https://world.openfoodfacts.org"
_USER_AGENT = "GymTracker/1.0 (persoonlijke voeding-app)"
_TIMEOUT = 8
_FIELDS = "product_name,brands,code,nutriments"


def _http_get_json(url: str) -> dict | None:
    """Blocking GET → geparste JSON, of None bij elke fout."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _macros_from_nutriments(n: dict) -> dict | None:
    """Normaliseer een OFF 'nutriments'-dict naar per-100g macro's. None indien onbruikbaar."""
    if not isinstance(n, dict):
        return None

    def num(*keys):
        for k in keys:
            v = n.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return None

    protein = num("proteins_100g")
    carbs = num("carbohydrates_100g")
    fat = num("fat_100g")
    fiber = num("fiber_100g")
    kcal = num("energy-kcal_100g")
    if kcal is None:
        kj = num("energy-kj_100g", "energy_100g")
        if kj is not None:
            kcal = round(kj / 4.184, 1)
    # Minimaal de drie macro's nodig; kcal afleiden indien afwezig.
    if protein is None or carbs is None or fat is None:
        return None
    if kcal is None:
        kcal = round(4 * protein + 4 * carbs + 9 * fat, 1)
    # Sanity: per-100g kcal kan niet boven puur vet (~900) uitkomen.
    if not (0 <= kcal <= 950) or protein < 0 or carbs < 0 or fat < 0:
        return None
    return {
        "kcal_per_100g": kcal,
        "protein_per_100g": protein,
        "carbs_per_100g": carbs,
        "fat_per_100g": fat,
        "fiber_per_100g": fiber,
    }


def _normalise_product(p: dict) -> dict | None:
    """OFF product-dict → genormaliseerd food-dict, of None."""
    macros = _macros_from_nutriments(p.get("nutriments", {}))
    name = (p.get("product_name") or "").strip()
    if not macros or not name:
        return None
    code = (p.get("code") or "").strip() or None
    return {
        "name": name,
        "brand": (p.get("brands") or "").split(",")[0].strip() or None,
        "barcode": code,
        "source": "open-food-facts",
        "source_ref": code,
        **macros,
    }


def lookup_barcode(code: str) -> dict | None:
    """OFF-product op barcode. Genormaliseerd food-dict of None. Blocking."""
    code = "".join(c for c in (code or "") if c.isdigit())
    if not (8 <= len(code) <= 14):
        return None
    data = _http_get_json(f"{_OFF_BASE}/api/v2/product/{code}.json?fields={_FIELDS}")
    if not data or data.get("status") != 1:
        return None
    return _normalise_product(data.get("product", {}))


def search_by_name(name: str, limit: int = 5) -> list[dict]:
    """OFF-naamzoekopdracht → lijst genormaliseerde food-dicts. Blocking."""
    q = (name or "").strip()
    if len(q) < 3:
        return []
    url = (f"{_OFF_BASE}/cgi/search.pl?search_terms={urllib.parse.quote(q)}"
           f"&json=1&page_size={limit}&fields={_FIELDS}")
    data = _http_get_json(url)
    if not data:
        return []
    out = []
    for p in data.get("products", []):
        norm = _normalise_product(p)
        if norm:
            out.append(norm)
    return out


def name_similarity(a: str, b: str) -> float:
    """0..1 gelijkenis — drempel voor het accepteren van een OFF-zoektreffer."""
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jacc = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(seq, jacc)


def best_search_match(name: str, candidates: list[dict], min_sim: float = 0.5) -> dict | None:
    """Beste OFF-zoektreffer voor `name`, of None als niets de drempel haalt."""
    best, best_sim = None, 0.0
    for c in candidates:
        s = name_similarity(name, c.get("name", ""))
        if c.get("brand"):
            s = max(s, name_similarity(name, f"{c['brand']} {c['name']}"))
        if s > best_sim:
            best, best_sim = c, s
    return best if best_sim >= min_sim else None


async def match_food(db: AsyncSession, name: str, threshold: float = 0.42) -> Food | None:
    """Beste trigram-match voor `name` in de lokale Food-tabel.

    Vereist de pg_trgm-extensie; geeft netjes None terug als die ontbreekt.
    """
    q = (name or "").strip()
    if len(q) < 3:
        return None
    try:
        sim = func.similarity(Food.name, q)
        row = (await db.execute(
            select(Food, sim.label("sim"))
            .where(sim > threshold)
            .order_by(sim.desc())
            .limit(1)
        )).first()
    except Exception:
        return None
    return row[0] if row else None


async def cache_food(db: AsyncSession, data: dict) -> Food:
    """Upsert een OFF-resultaat in de lokale Food-tabel (sleutel = barcode indien aanwezig).

    Zo matcht een volgende tekst-parse het lokaal, zonder OFF-round-trip.
    """
    if data.get("barcode"):
        existing = (await db.execute(
            select(Food).where(Food.barcode == data["barcode"])
        )).scalar_one_or_none()
        if existing:
            existing.use_count = (existing.use_count or 0) + 1
            return existing
    food = Food(
        user_id=None,
        name=data["name"],
        brand=data.get("brand"),
        source=data.get("source", "open-food-facts"),
        source_ref=data.get("source_ref"),
        barcode=data.get("barcode"),
        kcal_per_100g=data["kcal_per_100g"],
        protein_per_100g=data["protein_per_100g"],
        carbs_per_100g=data["carbs_per_100g"],
        fat_per_100g=data["fat_per_100g"],
        fiber_per_100g=data.get("fiber_per_100g"),
        use_count=1,
    )
    db.add(food)
    await db.flush()
    return food
