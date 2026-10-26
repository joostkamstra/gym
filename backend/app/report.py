"""Sessieverslag — deterministische feedback op een opgeslagen workout.

Ontwerpregels (in deze volgorde):

1. **Feitelijk juist boven volledig.** Elk getal komt uit gelogde sets of uit het
   schema zoals dat bij het opslaan gold. Er wordt niets geschat, geïnterpoleerd
   of ingevuld. Geen AI in dit pad — hetzelfde verslag komt er elke keer uit.
2. **Elke uitspraak draagt zijn onderbouwing mee** (`basis`), zodat de app kan
   tonen waaróm er iets staat en het narekenbaar is.
3. **Niet-vergelijkbaar wordt uitgesproken, niet weggelaten.** Een oefening
   zonder historie is "eerste keer", niet stilzwijgend afwezig. Een sessie waar
   de doelen-per-set niet meer te achterhalen zijn, zegt dat.
4. **Compleet betekent: ook wat er níet gebeurde.** Overgeslagen oefeningen,
   vervangers en extra sets staan in het verslag.

Bekende valkuilen die hier expliciet zijn afgevangen:

- *Zwaarder is niet automatisch beter.* 40 kg × 5 tegenover 35 kg × 12 is minder
  werk. Het oordeel per oefening rust op volume én geschat 1RM; spreken die
  elkaar tegen, dan is het oordeel "gemengd" met beide getallen erbij.
- *Lichaamsgewicht- en tijdoefeningen hebben volume 0* (Plank: 0 kg × 60). Die
  worden op reps/seconden vergeleken, niet op volume.
- *Doelen per set zijn een ladder*, geen vlak getal. `workout_sets.target_kg`
  bevat het topgewicht van de oefening, niet het doel van díe set. Bij het
  opslaan snapshotten we de ladder; voor oudere sessies is die niet bekend en
  zegt het verslag dat.
- *De vorige keer dat je een oefening deed staat niet per se in hetzelfde
  schema.* Er wordt over alle schema's heen teruggekeken.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Schema, WorkoutSession, WorkoutSet

REPORT_VERSION = 1

# Een oefening telt als lichaamsgewicht/tijd zodra álle gelogde sets 0 kg zijn.
# Volume (kg × reps) is dan per definitie 0 en zegt niets; we vergelijken reps.
_BODYWEIGHT = "bodyweight_of_tijd"
_LOADED = "gewicht"

# Drempels. Bewust ruim: onder deze waarden is het ruis, geen signaal.
_VOLUME_NOISE_PCT = 5.0      # verschil kleiner dan dit heet "gelijk"
_LANGE_PAUZE_DAGEN = 14      # daarboven krijgt de sessie een pauze-context
_STILSTAND_SESSIES = 4       # zelfde topgewicht N sessies achter elkaar
_ONDER_DOEL_SESSIES = 3      # N sessies achtereen onder doel


# --------------------------------------------------------------------------
# rekenhulpjes (puur — geen DB, makkelijk te controleren)
# --------------------------------------------------------------------------

def _e1rm(kg: float, reps: int) -> float | None:
    """Geschat 1RM volgens Epley. Alleen binnen het bereik waar de formule
    betekenis heeft: boven ~15 reps loopt de schatting hard weg van de
    werkelijkheid, dus geven we liever niets dan een verzonnen getal."""
    if kg <= 0 or reps < 1 or reps > 15:
        return None
    return round(kg * (1 + reps / 30), 1)


def _volume(sets: list[dict]) -> float:
    return round(sum((s["kg"] or 0) * (s["reps"] or 0) for s in sets), 1)


def _totaal_reps(sets: list[dict]) -> int:
    return sum(s["reps"] or 0 for s in sets)


def _beste_set(sets: list[dict], soort: str) -> dict | None:
    """Zwaarste set; bij gelijk gewicht die met de meeste reps.
    Bij lichaamsgewicht/tijd: de set met de meeste reps."""
    werkend = [s for s in sets if (s["reps"] or 0) > 0]
    if not werkend:
        return None
    if soort == _BODYWEIGHT:
        best = max(werkend, key=lambda s: s["reps"])
    else:
        best = max(werkend, key=lambda s: ((s["kg"] or 0), s["reps"]))
    return {
        "kg": best["kg"],
        "reps": best["reps"],
        "set_nr": best["set_nr"],
        "e1rm": _e1rm(best["kg"] or 0, best["reps"] or 0),
    }


def _pct(nieuw: float, oud: float) -> float | None:
    if not oud:
        return None
    return round((nieuw - oud) / oud * 100, 1)


def _soort(sets: list[dict]) -> str:
    return _BODYWEIGHT if all((s["kg"] or 0) == 0 for s in sets) else _LOADED


def _richting(delta: float | None, ruis: float = 0.0) -> str:
    if delta is None:
        return "onbekend"
    if delta > ruis:
        return "omhoog"
    if delta < -ruis:
        return "omlaag"
    return "gelijk"


# --------------------------------------------------------------------------
# schema-snapshot
# --------------------------------------------------------------------------

def build_slot_plan(schema_data: dict) -> list[dict]:
    """Zet de schema-JSON om naar een platte lijst slots met hun doelen.

    Roep dit aan vóór `auto_update_targets`, anders snapshot je de ladder van
    ná de ophoging en meet je de sessie langs een doel dat tijdens die sessie
    nog niet bestond.
    """
    plan: list[dict] = []
    for ss in schema_data.get("supersets", []):
        for ex in ss.get("exercises", []):
            alt = ex.get("alt") or {}
            plan.append({
                "slot": (ex.get("id") or "").lower(),
                "superset": ss.get("name"),
                "naam": ex.get("name"),
                "alt_naam": alt.get("name"),
                "geplande_sets": len(ex.get("target_sets") or []) or ss.get("sets") or 0,
                "doelen": {
                    ex.get("name"): ex.get("target_sets") or None,
                    # Heet het alternatief hetzelfde als de hoofdoefening, dan wint de
                    # hoofdoefening: die doelen ziet de gebruiker en die worden bijgesteld.
                    **({alt["name"]: alt.get("target_sets") or None}
                       if alt.get("name") and alt.get("name") != ex.get("name") else {}),
                },
                "doel_kg": ex.get("target_kg"),
            })
    return plan


def _slot_van(plan: list[dict], slot: str) -> dict | None:
    for p in plan:
        if p["slot"] == slot:
            return p
    return None


def _naam_van(plan: list[dict], naam: str) -> dict | None:
    """Zoek het slot waar deze oefening staat, als hoofdoefening of alternatief.

    Gebruikt voor sessies waarvan de eigen schema-snapshot ontbreekt: daar is de
    naam het enige identificerende dat een schema-herziening overleeft. Slot-ids
    niet -- die worden hergebruikt voor een andere oefening.
    """
    for p in plan:
        if p["naam"] == naam:
            return p
    for p in plan:
        if p.get("alt_naam") == naam:
            return p
    return None


# --------------------------------------------------------------------------
# per-set oordeel
# --------------------------------------------------------------------------

def _beoordeel_sets(sets: list[dict], ladder: list[dict] | None) -> tuple[list[dict], str]:
    """Vergelijk elke set met zijn doel. Retourneert (sets_met_oordeel, doel_basis).

    `ladder` = de target_sets uit het schema zoals dat bij het opslaan gold.
    Ontbreekt die (oudere sessies), dan valt hij terug op het vlakke doelgewicht
    dat bij de set is opgeslagen; het reps-doel is dan niet bekend en dat wordt
    zo gerapporteerd — niet gemaskeerd als "gehaald".
    """
    uit: list[dict] = []
    basis = "ladder" if ladder else "vlak"

    for s in sets:
        idx = (s["set_nr"] or 1) - 1
        doel_kg = doel_reps = None
        if ladder and 0 <= idx < len(ladder):
            doel_kg = ladder[idx].get("kg")
            doel_reps = ladder[idx].get("reps")
        elif s.get("doel_kg_gelogd") is not None:
            doel_kg = s["doel_kg_gelogd"]

        if doel_kg is None and doel_reps is None:
            oordeel = "geen doel"
        elif doel_reps is None:
            # Alleen het TOPgewicht van de oefening is bekend, niet het doel van
            # deze set. Een ladder begint bewust lichter, dus set 1 langs het
            # topgewicht leggen zou stelselmatig "onder doel" opleveren voor een
            # set die precies deed wat er stond. Dus: geen oordeel per set; het
            # oordeel gaat verderop over de zwaarste set tegen het topgewicht.
            oordeel = "doel per set onbekend"
        elif (s["kg"] or 0) >= (doel_kg or 0) and (s["reps"] or 0) >= doel_reps:
            boven = (s["kg"] or 0) > (doel_kg or 0) or (s["reps"] or 0) > doel_reps
            oordeel = "boven doel" if boven else "gehaald"
        elif (s["kg"] or 0) >= (doel_kg or 0):
            oordeel = "gewicht gehaald, reps niet"
        else:
            oordeel = "onder doel"

        uit.append({**s, "doel_kg": doel_kg, "doel_reps": doel_reps, "oordeel": oordeel})

    return uit, basis


def _samenvat_doel(sets_met_oordeel: list[dict], doel_basis: str,
                   beste_set: dict | None = None) -> dict:
    gehaald = sum(1 for s in sets_met_oordeel if s["oordeel"] in ("gehaald", "boven doel"))
    met_doel = sum(1 for s in sets_met_oordeel if s["oordeel"] != "geen doel")
    if doel_basis == "vlak":
        top_doel = max((s["doel_kg"] for s in sets_met_oordeel
                        if s["doel_kg"] is not None), default=None)
        top_gedaan = (beste_set or {}).get("kg")
        if top_doel is None or top_gedaan is None:
            return {"label": "geen doel bekend", "gehaald": 0, "van": 0, "volledig": False,
                    "let_op": "voor deze sessie is geen doel bewaard"}
        gehaald_top = top_gedaan >= top_doel
        return {
            "label": (f"zwaarste set {top_gedaan:g} kg · doelgewicht {top_doel:g} kg"),
            "gehaald": 1 if gehaald_top else 0,
            "van": 1,
            "volledig": gehaald_top,
            "onzeker": True,
            "let_op": ("de doelen per set zijn voor deze sessie niet bewaard; alleen het "
                       "doelgewicht van de oefening is bekend, dus alleen de zwaarste set "
                       "is te beoordelen"),
        }
    if met_doel == 0:
        return {"label": "geen doel in het schema", "gehaald": 0, "van": 0, "volledig": False}
    if gehaald == met_doel:
        return {"label": "doel gehaald", "gehaald": gehaald, "van": met_doel, "volledig": True}
    if gehaald == 0:
        return {"label": "onder doel", "gehaald": 0, "van": met_doel, "volledig": False}
    return {"label": f"deels gehaald ({gehaald}/{met_doel})", "gehaald": gehaald, "van": met_doel, "volledig": False}


# --------------------------------------------------------------------------
# vergelijking met de vorige keer
# --------------------------------------------------------------------------

def _vergelijk(nu: dict, toen: dict | None, soort: str) -> dict:
    """Vergelijk deze oefening met de vorige keer dat hij gedaan is.

    Het eindoordeel rust op twee onafhankelijke maten — totaal werk (volume) en
    de zwaarste set (geschat 1RM). Zijn ze het eens, dan is het oordeel hard.
    Zijn ze het oneens (zwaarder maar minder werk), dan heet dat "gemengd" en
    krijg je beide getallen; er wordt niet één maat gekozen om er een mooier
    verhaal van te maken.
    """
    if not toen:
        return {
            "vergelijkbaar": False,
            "reden": "eerste keer dat deze oefening gelogd is",
            "oordeel": "nieuw",
        }

    if soort == _BODYWEIGHT:
        d_reps = nu["totaal_reps"] - toen["totaal_reps"]
        pct = _pct(nu["totaal_reps"], toen["totaal_reps"])
        return {
            "vergelijkbaar": True,
            "vorige_datum": toen["datum"],
            "vorige_schema": toen["schema_key"],
            "soort": _BODYWEIGHT,
            "reps_toen": toen["totaal_reps"],
            "reps_nu": nu["totaal_reps"],
            "reps_delta": d_reps,
            "reps_pct": pct,
            "oordeel": {"omhoog": "vooruit", "omlaag": "achteruit", "gelijk": "gelijk"}.get(
                _richting(pct, _VOLUME_NOISE_PCT), "onbekend"),
            "basis": (f"{nu['totaal_reps']} reps nu tegen {toen['totaal_reps']} op "
                      f"{toen['datum']} (kg = 0, dus op reps vergeleken)"),
        }

    vol_pct = _pct(nu["volume_kg"], toen["volume_kg"])
    e_nu = (nu.get("beste_set") or {}).get("e1rm")
    e_toen = (toen.get("beste_set") or {}).get("e1rm")
    e_pct = _pct(e_nu, e_toen) if (e_nu and e_toen) else None

    r_vol = _richting(vol_pct, _VOLUME_NOISE_PCT)
    r_e1 = _richting(e_pct, _VOLUME_NOISE_PCT)

    # "Gemengd" is alleen op zijn plaats bij ECHTE tegenspraak: de een omhoog en
    # de ander omlaag. Staat een van de twee op "gelijk" of "onbekend", dan is er
    # geen conflict en telt de maat die wél iets zegt -- anders krijg je een
    # waarschuwing over een tegenstrijdigheid die er niet is.
    _woord = {"omhoog": "vooruit", "omlaag": "achteruit", "gelijk": "gelijk"}
    tegenspraak = {r_vol, r_e1} == {"omhoog", "omlaag"}
    if tegenspraak:
        oordeel = "gemengd"
    else:
        sprekend = [r for r in (r_vol, r_e1) if r in ("omhoog", "omlaag")]
        oordeel = _woord.get(sprekend[0], "onbekend") if sprekend else _woord.get(r_vol, "onbekend")

    bs_nu, bs_toen = nu.get("beste_set") or {}, toen.get("beste_set") or {}
    basis = (f"{nu['volume_kg']:g} kg werk nu tegen {toen['volume_kg']:g} kg op {toen['datum']}"
             f" · zwaarste set {bs_nu.get('kg', '—')}×{bs_nu.get('reps', '—')}"
             f" tegen {bs_toen.get('kg', '—')}×{bs_toen.get('reps', '—')}")

    return {
        "vergelijkbaar": True,
        "vorige_datum": toen["datum"],
        "vorige_schema": toen["schema_key"],
        "soort": _LOADED,
        "volume_toen": toen["volume_kg"],
        "volume_nu": nu["volume_kg"],
        "volume_pct": vol_pct,
        "e1rm_toen": e_toen,
        "e1rm_nu": e_nu,
        "e1rm_pct": e_pct,
        "beste_set_toen": bs_toen or None,
        "oordeel": oordeel,
        "basis": basis,
    }


# --------------------------------------------------------------------------
# hoofdroutine
# --------------------------------------------------------------------------

async def generate_report(
    db: AsyncSession,
    session: WorkoutSession,
    *,
    slot_plan: list[dict] | None = None,
    target_updates: list | None = None,
    schema_plan_basis: str = "zoals opgeslagen bij de sessie",
) -> dict:
    """Bouw het sessieverslag. Leest alleen; schrijven doet de aanroeper.

    `slot_plan` is de snapshot van het schema zoals het gold tijdens de sessie
    (zie `build_slot_plan`). Zonder snapshot valt het verslag terug op wat er
    per set is opgeslagen en zegt dat in `basis`.
    """
    huidige_sets = sorted(session.sets, key=lambda s: (s.superset_key or "", s.set_number))
    datum = session.date

    # Is dit plan de snapshot van deze sessie, of het schema zoals het nu is?
    # Dat verschil bepaalt wat je nog mag beweren -- zie hieronder.
    plan_is_snapshot = slot_plan is not None

    if slot_plan is None:
        schema_obj = await db.get(Schema, session.schema_id)
        slot_plan = build_slot_plan(schema_obj.data) if schema_obj else []
        # De ladder van nú is niet de ladder van toen — targets worden na elke
        # sessie opgehoogd. Ook de slot-indeling is niet houdbaar: een schema
        # wordt herschreven en dan wijst slot 'b2' naar een andere oefening dan
        # tijdens die sessie. Op slot vergelijken levert dan zelfverzekerde
        # onzin op ("je deed X in plaats van Y") die nergens op slaat. Dus:
        # doelen weggooien, en verderop matchen op NAAM in plaats van op slot.
        for p in slot_plan:
            p["doelen"] = {k: None for k in p["doelen"]}
        schema_plan_basis = (
            "het schema zoals het nu is -- de indeling en doelen van tijdens deze "
            "sessie zijn niet bewaard, dus er wordt op oefeningsnaam gematcht"
        )

    # ---- 1. alles wat deze sessie is gelogd, gegroepeerd per slot+oefening ----
    groepen: dict[tuple[str, str], list[dict]] = {}
    for s in huidige_sets:
        if (s.reps or 0) <= 0 and (s.kg or 0) <= 0:
            continue
        key = ((s.superset_key or "").lower(), s.exercise_name)
        groepen.setdefault(key, []).append({
            "set_nr": s.set_number,
            "kg": s.kg,
            "reps": s.reps,
            "doel_kg_gelogd": s.target_kg,
        })

    namen = sorted({naam for _, naam in groepen})

    # ---- 2. historie van deze oefeningen, over álle schema's heen ----
    historie = await _laad_historie(db, session, namen)

    # ---- 3. per oefening ----
    oefeningen: list[dict] = []
    for (slot, naam), sets in sorted(groepen.items()):
        sets.sort(key=lambda s: s["set_nr"])
        soort = _soort(sets)

        if plan_is_snapshot:
            plan = _slot_van(slot_plan, slot)
            if plan is None:
                rol, ladder = "extra", None
            elif naam == plan["naam"]:
                rol, ladder = "volgens schema", (plan["doelen"] or {}).get(naam)
            elif naam == plan.get("alt_naam"):
                # Alternatieven hebben lang niet altijd een eigen ladder.
                rol, ladder = "alternatief uit schema", (plan["doelen"] or {}).get(naam)
            else:
                rol, ladder = "afwijking van schema", None
        else:
            plan = _naam_van(slot_plan, naam)
            ladder = None
            if plan is None:
                rol = "niet in het huidige schema"
            elif naam == plan["naam"]:
                rol = "volgens schema"
            else:
                rol = "alternatief uit schema"

        sets_beoordeeld, doel_basis = _beoordeel_sets(sets, ladder)
        nu = {
            "volume_kg": _volume(sets),
            "totaal_reps": _totaal_reps(sets),
            "beste_set": _beste_set(sets, soort),
        }
        vorige = historie.get(naam, {}).get("vorige")
        oefeningen.append({
            "slot": slot,
            "naam": naam,
            "rol": rol,
            "soort": soort,
            "schema_oefening": plan["naam"] if plan else None,
            "sets": sets_beoordeeld,
            "doel_basis": doel_basis,
            "doel": _samenvat_doel(sets_beoordeeld, doel_basis, nu["beste_set"]),
            "volume_kg": nu["volume_kg"],
            "totaal_reps": nu["totaal_reps"],
            "beste_set": nu["beste_set"],
            "geplande_sets": plan["geplande_sets"] if plan else None,
            "gelogde_sets": len(sets),
            "vergelijking": _vergelijk(nu, vorige, soort),
            "record": None,  # ingevuld door _bepaal_records
        })

    _bepaal_records(oefeningen, historie)

    # ---- 4. dekking: ook wat er niet gebeurd is ----
    dekking = _dekking(slot_plan, groepen, oefeningen, plan_is_snapshot)

    # ---- 5. totalen + sessievergelijking ----
    alle_sets = [s for grp in groepen.values() for s in grp]
    totalen = {
        "sets": len(alle_sets),
        "reps": _totaal_reps(alle_sets),
        "volume_kg": _volume(alle_sets),
        "oefeningen": len(groepen),
    }
    vorige_sessie = await _vorige_sessie(db, session, zelfde_schema=True)
    laatste_training = await _vorige_sessie(db, session, zelfde_schema=False)

    sessie_vgl = None
    if vorige_sessie:
        v_sets = [
            {"kg": s.kg, "reps": s.reps}
            for s in vorige_sessie.sets if (s.reps or 0) > 0 or (s.kg or 0) > 0
        ]
        v_vol = _volume([{**s, "set_nr": 0} for s in v_sets])
        sessie_vgl = {
            "vorige_datum": vorige_sessie.date.date().isoformat(),
            "volume_toen": v_vol,
            "volume_nu": totalen["volume_kg"],
            "volume_pct": _pct(totalen["volume_kg"], v_vol),
            "sets_toen": len(v_sets),
            "dagen_ertussen": (datum.date() - vorige_sessie.date.date()).days,
            "basis": (f"{totalen['volume_kg']:g} kg over {totalen['sets']} sets nu, tegen "
                      f"{v_vol:g} kg over {len(v_sets)} sets op "
                      f"{vorige_sessie.date.date().isoformat()} (zelfde schema)"),
        }

    rust = {
        "dagen_sinds_laatste_training": (
            (datum.date() - laatste_training.date.date()).days if laatste_training else None
        ),
        "laatste_training": laatste_training.date.date().isoformat() if laatste_training else None,
        "dagen_sinds_dit_schema": sessie_vgl["dagen_ertussen"] if sessie_vgl else None,
    }

    # ---- 6. records ----
    records = [o["record"] for o in oefeningen if o.get("record") and o["record"].get("nieuw")]

    # ---- 7. signalen ----
    signalen = _signalen(oefeningen, dekking, rust, totalen, sessie_vgl, historie)

    return {
        "versie": REPORT_VERSION,
        "gegenereerd_op": datetime.utcnow().isoformat(timespec="seconds"),
        "workout_id": str(session.id),
        "datum": datum.isoformat(),
        "schema": {"key": session.schema.key, "naam": session.schema.name},
        "totalen": totalen,
        "rust": rust,
        "sessie_vergelijking": sessie_vgl,
        "dekking": dekking,
        "oefeningen": oefeningen,
        "records": records,
        "targets_bijgewerkt": [
            {
                "oefening": u.exercise_name,
                "set": u.set_number,
                "van_kg": u.old_kg, "naar_kg": u.new_kg,
                "van_reps": u.old_reps, "naar_reps": u.new_reps,
                "reden": u.reason,
                "ook_in_schemas": u.propagated_to or [],
            }
            for u in (target_updates or [])
        ],
        "eigen_opmerking": session.feedback or None,
        "signalen": signalen,
        "basis": {
            "doelen_per_set": (
                "vastgelegd bij het opslaan van deze sessie"
                if any(o["doel_basis"] == "ladder" for o in oefeningen)
                else "niet vastgelegd — alleen het doelgewicht van die dag is bekend"
            ),
            "schema_indeling": schema_plan_basis,
            "volume": "volume = kg × reps zoals gelogd; bij dumbbells is dat het gewicht per dumbbell",
            "e1rm": "geschat 1RM volgens Epley (kg × (1 + reps/30)), alleen bij 1–15 reps",
            "vergelijking": "vorige keer = de meest recente eerdere sessie waarin die oefening voorkomt, ongeacht schema",
        },
    }


# --------------------------------------------------------------------------
# DB-helpers
# --------------------------------------------------------------------------

async def _vorige_sessie(
    db: AsyncSession, session: WorkoutSession, *, zelfde_schema: bool
) -> WorkoutSession | None:
    from sqlalchemy.orm import selectinload

    q = (
        select(WorkoutSession)
        .options(selectinload(WorkoutSession.sets))
        .where(
            WorkoutSession.user_id == session.user_id,
            WorkoutSession.date < session.date,
            WorkoutSession.id != session.id,
        )
        .order_by(WorkoutSession.date.desc())
        .limit(1)
    )
    if zelfde_schema:
        q = q.where(WorkoutSession.schema_id == session.schema_id)
    return (await db.execute(q)).scalars().first()


async def _laad_historie(
    db: AsyncSession, session: WorkoutSession, namen: list[str]
) -> dict[str, dict]:
    """Per oefeningsnaam: de vorige keer (over alle schema's) en het record.

    Eén query voor alle oefeningen samen — anders krijg je N+1 en, erger, per
    oefening een andere afkapdatum.
    """
    if not namen:
        return {}

    rows = (await db.execute(
        select(WorkoutSet, WorkoutSession.date, WorkoutSession.id, Schema.key)
        .join(WorkoutSession, WorkoutSet.session_id == WorkoutSession.id)
        .join(Schema, WorkoutSession.schema_id == Schema.id)
        .where(
            WorkoutSession.user_id == session.user_id,
            WorkoutSession.date < session.date,
            WorkoutSession.id != session.id,
            WorkoutSet.exercise_name.in_(namen),
        )
        .order_by(WorkoutSession.date.desc())
    )).all()

    # groepeer: naam -> sessie-id -> sets
    per_naam: dict[str, dict] = {}
    for ws, ses_datum, ses_id, schema_key in rows:
        if (ws.reps or 0) <= 0 and (ws.kg or 0) <= 0:
            continue
        naam_blok = per_naam.setdefault(ws.exercise_name, {"sessies": {}})
        blok = naam_blok["sessies"].setdefault(
            ses_id, {"datum": ses_datum, "schema_key": schema_key, "sets": []}
        )
        blok["sets"].append({"set_nr": ws.set_number, "kg": ws.kg, "reps": ws.reps})

    uit: dict[str, dict] = {}
    for naam in namen:
        blok = per_naam.get(naam)
        if not blok or not blok["sessies"]:
            uit[naam] = {"vorige": None, "record": None, "reeks": []}
            continue

        reeks = sorted(blok["sessies"].values(), key=lambda b: b["datum"], reverse=True)
        for b in reeks:
            soort = _soort(b["sets"])
            b["volume_kg"] = _volume(b["sets"])
            b["totaal_reps"] = _totaal_reps(b["sets"])
            b["beste_set"] = _beste_set(b["sets"], soort)
            b["datum"] = b["datum"].date().isoformat()

        uit[naam] = {"vorige": reeks[0], "record": None, "reeks": reeks}

    return uit


def _bepaal_records(oefeningen: list[dict], historie: dict[str, dict]) -> None:
    """Zet per oefening een record-blok. Een record is pas een record als er
    iets is om mee te vergelijken — de eerste keer is geen PR."""
    for o in oefeningen:
        blok = historie.get(o["naam"]) or {}
        reeks = blok.get("reeks") or []
        bs = o.get("beste_set")
        if not bs or not reeks:
            o["record"] = None
            continue

        if o["soort"] == _BODYWEIGHT:
            oud = max((b["beste_set"]["reps"] for b in reeks if b.get("beste_set")), default=None)
            nieuw = oud is not None and bs["reps"] > oud
            o["record"] = {
                "soort": "reps",
                "nu": bs["reps"], "vorig_record": oud, "nieuw": nieuw,
                "oefening": o["naam"],
                "basis": f"{bs['reps']} reps nu, beste eerder {oud}",
            } if oud is not None else None
            continue

        oud_kg = max((b["beste_set"]["kg"] for b in reeks if b.get("beste_set")), default=None)
        if oud_kg is None:
            o["record"] = None
            continue
        nieuw = bs["kg"] > oud_kg
        o["record"] = {
            "soort": "gewicht",
            "oefening": o["naam"],
            "nu": bs["kg"], "nu_reps": bs["reps"],
            "vorig_record": oud_kg, "nieuw": nieuw,
            "basis": f"{bs['kg']} kg × {bs['reps']} nu, zwaarste eerder {oud_kg} kg",
        }


# --------------------------------------------------------------------------
# dekking + signalen
# --------------------------------------------------------------------------

def _dekking(slot_plan: list[dict], groepen: dict, oefeningen: list[dict],
             plan_is_snapshot: bool = True) -> dict:
    if plan_is_snapshot:
        gelogde_slots = {slot for slot, _ in groepen}
        gedekt = [p for p in slot_plan if p["slot"] and p["slot"] in gelogde_slots]
        overgeslagen = [
            {"slot": p["slot"], "oefening": p["naam"], "superset": p["superset"],
             "geplande_sets": p["geplande_sets"]}
            for p in slot_plan if p["slot"] and p["slot"] not in gelogde_slots
        ]
        # Alleen zinnig als het plan de snapshot van deze sessie is. Zonder die
        # snapshot zegt "slot b2 hoorde X te zijn" niets over toen.
        vervangen = [
            {"slot": o["slot"], "schema_vroeg": o["schema_oefening"], "gedaan": o["naam"],
             "soort": o["rol"]}
            for o in oefeningen
            if o["rol"] in ("alternatief uit schema", "afwijking van schema") and o["schema_oefening"]
        ]
    else:
        gelogde_namen = {naam for _, naam in groepen}
        gedekt = [p for p in slot_plan
                  if p["naam"] in gelogde_namen or p.get("alt_naam") in gelogde_namen]
        overgeslagen = [
            {"slot": p["slot"], "oefening": p["naam"], "superset": p["superset"],
             "geplande_sets": p["geplande_sets"]}
            for p in slot_plan
            if p["naam"] not in gelogde_namen and p.get("alt_naam") not in gelogde_namen
        ]
        vervangen = []
    extra = [
        {"slot": o["slot"], "oefening": o["naam"], "sets": o["gelogde_sets"]}
        for o in oefeningen if o["rol"] in ("extra", "niet in het huidige schema")
    ]
    afwijkende_setaantallen = [
        {"oefening": o["naam"], "gepland": o["geplande_sets"], "gelogd": o["gelogde_sets"]}
        for o in oefeningen
        if o["geplande_sets"] and o["gelogde_sets"] != o["geplande_sets"]
    ]
    gepland_sets = sum(p["geplande_sets"] or 0 for p in slot_plan)
    gelogd_sets = sum(o["gelogde_sets"] for o in oefeningen)

    return {
        "op_naam_gematcht": not plan_is_snapshot,
        "geplande_oefeningen": len(slot_plan),
        "gelogde_oefeningen": len(gedekt),
        "geplande_sets": gepland_sets,
        "gelogde_sets": gelogd_sets,
        "volledig": len(overgeslagen) == 0,
        "overgeslagen": overgeslagen,
        "vervangen": vervangen,
        "extra": extra,
        "afwijkende_setaantallen": afwijkende_setaantallen,
        "basis": (f"{len(gedekt)} van {len(slot_plan)} schema-oefeningen gelogd, "
                  f"{gelogd_sets} sets tegenover {gepland_sets} geplande"
                  + ("" if plan_is_snapshot
                     else " -- gematcht op oefeningsnaam tegen het schema zoals het nu is")),
    }


def _signalen(oefeningen, dekking, rust, totalen, sessie_vgl, historie) -> list[dict]:
    """Alleen observaties die rechtstreeks uit de getallen volgen. Elk signaal
    draagt zijn onderbouwing. Geen blessure-, voedings- of medisch advies."""
    uit: list[dict] = []
    # Per-oefening signalen apart: die worden hieronder afgetopt. Een verslag met
    # twintig meldingen leest niemand, en dan verdwijnt het ene punt dat er wel
    # toe doet tussen negentien die al als chip bij de oefening staan.
    per_oefening: list[dict] = []

    # Pauze-context. Twee verschillende gaten die allebei uitmaken: sinds je
    # laatste training uberhaupt, en sinds je dit schema voor het laatst deed --
    # want dat laatste is waar de sessievergelijking tegenaan ligt.
    dagen = rust.get("dagen_sinds_laatste_training")
    dagen_schema = rust.get("dagen_sinds_dit_schema")
    if dagen is not None and dagen >= _LANGE_PAUZE_DAGEN:
        uit.append({
            "code": "lange_pauze",
            "soort": "context",
            "tekst": f"{dagen} dagen sinds je vorige training. Lagere cijfers dan de "
                     f"vorige keer zijn na zo'n pauze normaal.",
            "basis": f"vorige training {rust.get('laatste_training')}, deze sessie {dagen} dagen later",
        })
    elif dagen_schema is not None and dagen_schema >= _LANGE_PAUZE_DAGEN:
        uit.append({
            "code": "schema_gat",
            "soort": "context",
            "tekst": f"Dit schema deed je {dagen_schema} dagen geleden voor het laatst. "
                     f"De vergelijking hieronder overbrugt die hele periode.",
            "basis": (sessie_vgl or {}).get("basis"),
        })

    op_naam = dekking.get("op_naam_gematcht")

    if dekking["overgeslagen"]:
        namen = ", ".join(o["oefening"] for o in dekking["overgeslagen"])
        uit.append({
            "code": "overgeslagen",
            "soort": "dekking",
            "tekst": (f"Staat wel in het schema zoals het nu is, niet in deze sessie: {namen}. "
                      f"Het schema kan sinds die dag zijn aangepast."
                      if op_naam else f"Niet gelogd deze sessie: {namen}."),
            "basis": dekking["basis"],
        })

    if dekking["extra"]:
        namen = ", ".join(f"{e['oefening']} ({e['sets']} sets)" for e in dekking["extra"])
        uit.append({
            "code": "extra_werk",
            "soort": "dekking",
            "tekst": (f"Komt niet voor in het schema zoals het nu is: {namen}."
                      if op_naam else f"Buiten het schema gelogd: {namen}."),
            "basis": ("gematcht op naam tegen het huidige schema" if op_naam
                      else "deze sets horen bij geen enkel slot in het schema"),
        })

    for v in dekking["vervangen"]:
        if v["soort"] == "afwijking van schema":
            uit.append({
                "code": "afwijking",
                "soort": "dekking",
                "tekst": f"Slot {v['slot'].upper()}: je deed {v['gedaan']} in plaats van "
                         f"{v['schema_vroeg']}. Die staat niet als alternatief in het schema, "
                         f"dus er is geen doel om langs te leggen.",
                "basis": f"schema-slot {v['slot']} = {v['schema_vroeg']}",
            })

    for o in oefeningen:
        vg = o["vergelijking"]
        if vg.get("oordeel") == "gemengd":
            per_oefening.append({
                "code": "gemengd",
                "soort": "oefening",
                "oefening": o["naam"],
                "tekst": f"{o['naam']}: zwaarder óf lichter is hier niet hetzelfde als beter — "
                         f"totaal werk en zwaarste set wijzen verschillende kanten op.",
                "basis": vg.get("basis"),
            })
        elif vg.get("oordeel") == "achteruit" and vg.get("volume_pct") is not None \
                and vg["volume_pct"] <= -15:
            per_oefening.append({
                "code": "daling",
                "soort": "oefening",
                "_pct": vg["volume_pct"],
                "oefening": o["naam"],
                "tekst": f"{o['naam']}: {abs(vg['volume_pct'])}% minder werk dan de vorige keer.",
                "basis": vg.get("basis"),
            })

        # Structureel onder doel: alleen melden als het doel echt bekend is.
        if o["doel_basis"] == "ladder" and o["doel"]["van"] and o["doel"]["gehaald"] == 0:
            reeks = (historie.get(o["naam"]) or {}).get("reeks") or []
            if len(reeks) >= _ONDER_DOEL_SESSIES - 1:
                per_oefening.append({
                    "code": "onder_doel",
                    "soort": "doel",
                    "oefening": o["naam"],
                    "tekst": f"{o['naam']}: geen enkele set haalde het doel "
                             f"({o['sets'][0].get('doel_kg')} kg × {o['sets'][0].get('doel_reps')}).",
                    "basis": "; ".join(
                        f"S{s['set_nr']} {s['kg']}×{s['reps']} (doel {s['doel_kg']}×{s['doel_reps']})"
                        for s in o["sets"] if s["doel_kg"] is not None
                    ),
                })

        # Stilstand: N sessies op hetzelfde topgewicht.
        reeks = (historie.get(o["naam"]) or {}).get("reeks") or []
        bs = o.get("beste_set")
        if bs and o["soort"] == _LOADED and len(reeks) >= _STILSTAND_SESSIES - 1:
            vorige_kgs = [b["beste_set"]["kg"] for b in reeks[:_STILSTAND_SESSIES - 1]
                          if b.get("beste_set")]
            if len(vorige_kgs) == _STILSTAND_SESSIES - 1 and all(k == bs["kg"] for k in vorige_kgs):
                datums = ", ".join(b["datum"] for b in reeks[:_STILSTAND_SESSIES - 1])
                per_oefening.append({
                    "code": "stilstand",
                    "soort": "oefening",
                    "oefening": o["naam"],
                    "tekst": f"{o['naam']}: {_STILSTAND_SESSIES} sessies op rij zwaarste set "
                             f"{bs['kg']} kg.",
                    "basis": f"zelfde topgewicht op {datums} en nu",
                })

    # Aftoppen per soort, sterkste eerst. De volledige stand per oefening staat
    # altijd nog in `oefeningen` -- hier gaat het om wat opvalt, niet om alles.
    _MAX = {"onder_doel": 99, "stilstand": 3, "gemengd": 2, "daling": 3}
    per_oefening.sort(key=lambda x: (
        {"onder_doel": 0, "stilstand": 1, "gemengd": 2, "daling": 3}.get(x["code"], 9),
        -abs((x.get("_pct") or 0)),
    ))
    geteld: dict[str, int] = {}
    for x in per_oefening:
        n = geteld.get(x["code"], 0)
        if n >= _MAX.get(x["code"], 3):
            continue
        geteld[x["code"]] = n + 1
        x.pop("_pct", None)
        uit.append(x)

    return uit
