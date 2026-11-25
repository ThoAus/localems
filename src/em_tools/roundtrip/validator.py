"""Integrity, range, overlap, and tree-cycle validation for master-data roundtrip imports."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

ALLOWED_OBJECT_CLASSES = frozenset(
    {"building", "school", "public_lighting", "water_supply", "sports", "mobility", "other"}
)
ALLOWED_ENERGY_CARRIERS = frozenset({"electricity", "water", "gas", "heating", "other"})
ALLOWED_METER_TYPES = frozenset({"smartmeter", "submeter", "manual", "virtual"})


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


def validate_masterdata_state(state: dict[str, list[dict[str, Any]]]) -> None:
    """Validate complete target master-data state across all three tables.

    Checks:
      - Required non-null fields and allowed enum values
      - Unique primary keys
      - Foreign key references (objects <-> metering_points <-> estimation_rules)
      - Meter tree integrity: parent exists, no self-parent, matching energy_carrier & unit,
        and DFS cycle detection (object_class may differ freely across parent & children)
      - Estimation rules: factor in (0, 1], valid_from <= valid_to, and overlapping active
        estimation factors per source_metering_point_id sum <= 1.0
    """
    errors: list[str] = []

    objects = state.get("objects", [])
    metering_points = state.get("metering_points", [])
    estimation_rules = state.get("estimation_rules", [])

    # 1. Validate objects
    object_ids: set[str] = set()
    for idx, row in enumerate(objects, start=2):
        oid = row.get("object_id")
        ctx = f"objects row {idx} (object_id={oid!r})"
        if not oid or not isinstance(oid, str):
            errors.append(f"{ctx}: object_id is required")
            continue
        if oid in object_ids:
            errors.append(f"{ctx}: duplicate object_id '{oid}'")
        object_ids.add(oid)

        for req in ("name", "level_1", "level_2"):
            if not row.get(req):
                errors.append(f"{ctx}: '{req}' is required")

        oclass = row.get("object_class")
        if oclass not in ALLOWED_OBJECT_CLASSES:
            errors.append(
                f"{ctx}: invalid object_class {oclass!r} "
                f"(allowed: {sorted(ALLOWED_OBJECT_CLASSES)})"
            )

        if row.get("active") not in (0, 1):
            errors.append(f"{ctx}: active must be 0 or 1, got {row.get('active')!r}")

    # 2. Validate metering_points (first pass: scalar fields & map)
    metering_point_map: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(metering_points, start=2):
        mpid = row.get("metering_point_id")
        ctx = f"metering_points row {idx} (metering_point_id={mpid!r})"
        if not mpid or not isinstance(mpid, str):
            errors.append(f"{ctx}: metering_point_id is required")
            continue
        if mpid in metering_point_map:
            errors.append(f"{ctx}: duplicate metering_point_id '{mpid}'")
        metering_point_map[mpid] = row

        oid = row.get("object_id")
        if not oid or oid not in object_ids:
            errors.append(f"{ctx}: unknown object_id {oid!r} (FK violation)")

        carrier = row.get("energy_carrier")
        if carrier not in ALLOWED_ENERGY_CARRIERS:
            errors.append(
                f"{ctx}: invalid energy_carrier {carrier!r} "
                f"(allowed: {sorted(ALLOWED_ENERGY_CARRIERS)})"
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

    # 2b. Validate metering_points tree references (parent FK, carrier/unit match, cycles)
    adj: dict[str, list[str]] = defaultdict(list)
    for idx, row in enumerate(metering_points, start=2):
        mpid = row.get("metering_point_id")
        parent = row.get("parent_metering_point_id")
        if not mpid or not parent:
            continue
        ctx = f"metering_points row {idx} (metering_point_id={mpid!r})"
        if parent == mpid:
            errors.append(f"{ctx}: parent_metering_point_id cannot equal metering_point_id")
            continue
        if parent not in metering_point_map:
            errors.append(
                f"{ctx}: unknown parent_metering_point_id {parent!r} (FK violation)"
            )
            continue

        parent_row = metering_point_map[parent]
        if row.get("energy_carrier") != parent_row.get("energy_carrier"):
            errors.append(
                f"{ctx}: energy_carrier ({row.get('energy_carrier')!r}) must match parent "
                f"{parent!r} ({parent_row.get('energy_carrier')!r})"
            )
        if row.get("unit") != parent_row.get("unit"):
            errors.append(
                f"{ctx}: unit ({row.get('unit')!r}) must match parent "
                f"{parent!r} ({parent_row.get('unit')!r})"
            )
        adj[parent].append(mpid)

    cycle = _find_cycle(adj)
    if cycle is not None:
        errors.append(f"metering_points: cycle detected in meter tree: {' -> '.join(cycle)}")

    # 3. Validate estimation_rules
    seen_rule_ids: set[int] = set()
    active_estimations_by_source: dict[
        str, list[tuple[float, date, date | None, str]]
    ] = defaultdict(list)

    for idx, row in enumerate(estimation_rules, start=2):
        rid = row.get("rule_id")
        ctx = f"estimation_rules row {idx} (rule_id={rid!r})"

        if rid is not None:
            if not isinstance(rid, int) or rid <= 0:
                errors.append(f"{ctx}: rule_id must be a positive integer or empty for new rows")
            elif rid in seen_rule_ids:
                errors.append(f"{ctx}: duplicate rule_id {rid}")
            else:
                seen_rule_ids.add(rid)

        source_mp = row.get("source_metering_point_id")
        target_obj = row.get("target_object_id")
        factor = row.get("factor")
        active = row.get("active")

        if not source_mp or source_mp not in metering_point_map:
            errors.append(
                f"{ctx}: unknown source_metering_point_id {source_mp!r} (FK violation)"
            )

        if not target_obj or target_obj not in object_ids:
            errors.append(f"{ctx}: unknown target_object_id {target_obj!r} (FK violation)")

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

        if (
            active == 1
            and source_mp
            and vf is not None
            and (vt is None or vt >= vf)
            and isinstance(factor, (int, float))
            and 0.0 < float(factor) <= 1.0
        ):
            active_estimations_by_source[source_mp].append((float(factor), vf, vt, ctx))

    # 4. Estimation factor sum plausibility over overlapping time windows
    for source_mp, entries in active_estimations_by_source.items():
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
                    f"estimation_rules (source={source_mp!r}): overlapping active estimation "
                    f"factor sum is {total:.4f} (> 1.0) on {check_d.isoformat()}"
                )
                break

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
