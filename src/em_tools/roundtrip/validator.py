"""Integrity, range, overlap, and cycle validation for master-data roundtrip imports."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

ALLOWED_OBJEKT_TYPEN = frozenset({"gebaeude", "trinkwasser", "beleuchtung", "sonstiges"})
ALLOWED_SPARTEN = frozenset(
    {"strom", "trinkwasser", "beleuchtung", "waerme", "gas", "sonstiges"}
)
ALLOWED_METER_TYPES = frozenset({"smartmeter", "submeter", "manual", "virtual"})
ALLOWED_RULE_TYPES = frozenset({"submeter_deduction", "estimation_factor"})


class RoundtripValidationError(ValueError):
    """Raised when one or more integrity or plausibility checks fail before commit."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        bullet_list = "\n".join(f"  - {err}" for err in errors)
        super().__init__(
            f"Master-data validation failed with {len(errors)} error(s):\n{bullet_list}"
        )


def _parse_iso_date(val: Any, context: str, errors: list[str]) -> date | None:
    if val is None:
        return None
    if not isinstance(val, str):
        errors.append(f"{context}: expected ISO date YYYY-MM-DD, got {val!r}")
        return None
    try:
        return date.fromisoformat(val)
    except ValueError:
        errors.append(f"{context}: invalid ISO date {val!r} (expected YYYY-MM-DD)")
        return None


def _intervals_overlap(
    start_a: date,
    end_a: date | None,
    start_b: date,
    end_b: date | None,
) -> bool:
    """Return True if inclusive date intervals [start_a, end_a] and [start_b, end_b] overlap."""
    a_end = end_a or date.max
    b_end = end_b or date.max
    return start_a <= b_end and start_b <= a_end


def validate_masterdata_state(state: dict[str, list[dict[str, Any]]]) -> None:
    """Validate complete target master-data state across all three tables.

    Checks:
      - Required non-null fields and allowed enum values
      - Unique primary keys
      - Foreign key references (objekte <-> messpunkte <-> hierarchie_regeln)
      - Date format and valid_from <= valid_to
      - Rule-type semantics, factor range (0, 1], and same-sparte sub-meter check
      - Estimation factor sums <= 1.0 across any overlapping validity interval
      - Cycle detection on active submeter_deduction graphs (including overlapping periods)
    """
    errors: list[str] = []

    objekte = state.get("objekte", [])
    messpunkte = state.get("messpunkte", [])
    regeln = state.get("hierarchie_regeln", [])

    # 1. Validate objekte
    objekt_ids: set[str] = set()
    for idx, row in enumerate(objekte, start=2):
        oid = row.get("objekt_id")
        ctx = f"objekte row {idx} (objekt_id={oid!r})"
        if not oid or not isinstance(oid, str):
            errors.append(f"{ctx}: objekt_id is required")
            continue
        if oid in objekt_ids:
            errors.append(f"{ctx}: duplicate objekt_id '{oid}'")
        objekt_ids.add(oid)

        for req in ("name", "ebene_1", "ebene_2"):
            if not row.get(req):
                errors.append(f"{ctx}: '{req}' is required")

        otyp = row.get("objekt_typ")
        if otyp not in ALLOWED_OBJEKT_TYPEN:
            errors.append(
                f"{ctx}: invalid objekt_typ {otyp!r} (allowed: {sorted(ALLOWED_OBJEKT_TYPEN)})"
            )

        if row.get("active") not in (0, 1):
            errors.append(f"{ctx}: active must be 0 or 1, got {row.get('active')!r}")

    # 2. Validate messpunkte
    messpunkt_map: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(messpunkte, start=2):
        mpid = row.get("messpunkt_id")
        ctx = f"messpunkte row {idx} (messpunkt_id={mpid!r})"
        if not mpid or not isinstance(mpid, str):
            errors.append(f"{ctx}: messpunkt_id is required")
            continue
        if mpid in messpunkt_map:
            errors.append(f"{ctx}: duplicate messpunkt_id '{mpid}'")
        messpunkt_map[mpid] = row

        oid = row.get("objekt_id")
        if not oid or oid not in objekt_ids:
            errors.append(f"{ctx}: unknown objekt_id {oid!r} (FK violation)")

        sparte = row.get("sparte")
        if sparte not in ALLOWED_SPARTEN:
            errors.append(
                f"{ctx}: invalid sparte {sparte!r} (allowed: {sorted(ALLOWED_SPARTEN)})"
            )

        for req in ("name", "unit"):
            if not row.get(req):
                errors.append(f"{ctx}: '{req}' is required")

        mtype = row.get("meter_type")
        if mtype not in ALLOWED_METER_TYPES:
            errors.append(
                f"{ctx}: invalid meter_type {mtype!r} (allowed: {sorted(ALLOWED_METER_TYPES)})"
            )

        if row.get("active") not in (0, 1):
            errors.append(f"{ctx}: active must be 0 or 1, got {row.get('active')!r}")

        vf = _parse_iso_date(row.get("valid_from"), f"{ctx}.valid_from", errors)
        vt = _parse_iso_date(row.get("valid_to"), f"{ctx}.valid_to", errors)
        if vf and vt and vt < vf:
            errors.append(f"{ctx}: valid_to ({vt}) is earlier than valid_from ({vf})")

    # 3. Validate hierarchie_regeln
    seen_rule_ids: set[int] = set()
    active_submeter_edges: list[tuple[str, str, date, date | None, str]] = []
    active_estimations_by_parent: dict[
        str, list[tuple[float, date, date | None, str]]
    ] = defaultdict(list)

    for idx, row in enumerate(regeln, start=2):
        rid = row.get("rule_id")
        ctx = f"hierarchie_regeln row {idx} (rule_id={rid!r})"

        if rid is not None:
            if not isinstance(rid, int) or rid <= 0:
                errors.append(f"{ctx}: rule_id must be a positive integer or empty for new rows")
            elif rid in seen_rule_ids:
                errors.append(f"{ctx}: duplicate rule_id {rid}")
            else:
                seen_rule_ids.add(rid)

        parent = row.get("parent_messpunkt_id")
        child = row.get("child_messpunkt_id")
        target_obj = row.get("target_objekt_id")
        rtype = row.get("rule_type")
        factor = row.get("factor")
        active = row.get("active")

        if not parent or parent not in messpunkt_map:
            errors.append(f"{ctx}: unknown parent_messpunkt_id {parent!r} (FK violation)")

        if child is not None and child not in messpunkt_map:
            errors.append(f"{ctx}: unknown child_messpunkt_id {child!r} (FK violation)")

        if target_obj is not None and target_obj not in objekt_ids:
            errors.append(f"{ctx}: unknown target_objekt_id {target_obj!r} (FK violation)")

        if rtype not in ALLOWED_RULE_TYPES:
            errors.append(
                f"{ctx}: invalid rule_type {rtype!r} (allowed: {sorted(ALLOWED_RULE_TYPES)})"
            )
        elif rtype == "submeter_deduction":
            if not child:
                errors.append(f"{ctx}: submeter_deduction requires child_messpunkt_id")
            elif child == parent:
                errors.append(f"{ctx}: parent_messpunkt_id and child_messpunkt_id cannot be equal")
            elif parent in messpunkt_map and child in messpunkt_map:
                p_sparte = messpunkt_map[parent].get("sparte")
                c_sparte = messpunkt_map[child].get("sparte")
                if p_sparte != c_sparte:
                    errors.append(
                        f"{ctx}: parent ({parent}, {p_sparte}) and child ({child}, {c_sparte}) "
                        f"must have the same sparte"
                    )
        elif rtype == "estimation_factor":
            if not target_obj:
                errors.append(f"{ctx}: estimation_factor requires target_objekt_id")

        if not isinstance(factor, (int, float)) or not (0.0 < float(factor) <= 1.0):
            errors.append(f"{ctx}: factor must be in (0, 1], got {factor!r}")

        if active not in (0, 1):
            errors.append(f"{ctx}: active must be 0 or 1, got {active!r}")

        if row.get("valid_from") is None:
            errors.append(f"{ctx}: valid_from is required")
            vf = None
        else:
            vf = _parse_iso_date(row.get("valid_from"), f"{ctx}.valid_from", errors)
        vt = _parse_iso_date(row.get("valid_to"), f"{ctx}.valid_to", errors)
        if vf and vt and vt < vf:
            errors.append(f"{ctx}: valid_to ({vt}) is earlier than valid_from ({vf})")

        if active == 1 and vf is not None and (vt is None or vt >= vf):
            if rtype == "submeter_deduction" and parent and child:
                active_submeter_edges.append((parent, child, vf, vt, ctx))
            elif (
                rtype == "estimation_factor"
                and parent
                and isinstance(factor, (int, float))
                and 0.0 < float(factor) <= 1.0
            ):
                active_estimations_by_parent[parent].append((float(factor), vf, vt, ctx))

    # 4. Estimation factor sum plausibility over overlapping time windows
    for parent, entries in active_estimations_by_parent.items():
        # Evaluate total factor at every interval start boundary
        check_dates = sorted({vf for _, vf, _, _ in entries})
        for check_d in check_dates:
            active_on_d = [
                fac
                for fac, vf, vt, _ in entries
                if vf <= check_d and (vt is None or check_d <= vt)
            ]
            total = sum(active_on_d)
            if total > 1.0 + 1e-9:
                errors.append(
                    f"hierarchie_regeln (parent={parent!r}): overlapping active estimation_factor "
                    f"sum is {total:.4f} (> 1.0) on {check_d.isoformat()}"
                )
                break

    # 5. Cycle detection on active submeter_deduction graph (per time-slice)
    if active_submeter_edges:
        check_dates = sorted({vf for _, _, vf, _, _ in active_submeter_edges})
        reported_cycles: set[tuple[str, ...]] = set()
        for check_d in check_dates:
            adj: dict[str, list[str]] = defaultdict(list)
            for parent, child, vf, vt, _ in active_submeter_edges:
                if vf <= check_d and (vt is None or check_d <= vt):
                    adj[parent].append(child)

            cycle = _find_cycle(adj)
            if cycle is not None:
                key = tuple(sorted(cycle))
                if key not in reported_cycles:
                    reported_cycles.add(key)
                    errors.append(
                        f"hierarchie_regeln: cycle detected on {check_d.isoformat()}: "
                        f"{' -> '.join(cycle)}"
                    )

    if errors:
        raise RoundtripValidationError(errors)


def _find_cycle(adj: dict[str, list[str]]) -> list[str] | None:
    """Return a cycle path [a, b, ..., a] if directed graph `adj` has a cycle, else None."""
    state: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=visited
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in adj.get(node, ()):
            nxt_state = state.get(nxt, 0)
            if nxt_state == 1:
                start_idx = stack.index(nxt)
                return stack[start_idx:] + [nxt]
            if nxt_state == 0:
                found = dfs(nxt)
                if found is not None:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in list(adj.keys()):
        if state.get(node, 0) == 0:
            found = dfs(node)
            if found is not None:
                return found
    return None
