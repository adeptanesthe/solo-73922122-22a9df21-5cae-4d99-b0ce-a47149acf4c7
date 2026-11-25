"""Stroke *splitting* planner: one recorded stroke is really two strokes.

The student forgot to lift the pen once, so two reference strokes were
captured as a single polyline.  The teacher picks one **non-preserved**
actual stroke and asks for a trial split.

A split is a single cut at index ``k`` of the recorded point sequence::

    segment_a = points[:k]
    segment_b = points[k:]

Hard requirements (from the specification):

* ``k >= 2`` and ``len(points) - k >= 2`` -- each piece keeps at least two
  samples, and the two pieces together keep **every** sample, in the original
  order, with the original coordinates (no resampling, no reversal).
* The two pieces match two **adjacent** reference strokes: piece A matches
  slot ``j`` and piece B matches slot ``j + 1``, using the same
  writing-direction discrete Frechet distance and the same threshold.
* Together with the already preserved actual strokes, the pieces occupy
  pairwise distinct reference slots whose order follows the actual writing
  order (preserved strokes before the picked stroke first, then piece A, then
  piece B, then preserved strokes after it).  Every involved distance must be
  within the current threshold.

Among all feasible cuts we minimise the larger of the two piece distances
(minimax); ties are broken by the smaller distance sum, then the earlier cut,
so the answer is deterministic.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .frechet import MAX_POINTS_PER_STROKE, MAX_STROKES, discrete_frechet
from .planner import points_of, preserved_of, validate_payload

# Anchor = a point sequence that must occupy a fixed reference slot.
# Each entry maps to the set of compatible slots.
SlotSet = frozenset


def _compatible_slots(distances: Sequence[float], threshold: float) -> SlotSet:
    return frozenset(j for j, d in enumerate(distances) if d <= threshold + 1e-9)


def _advance(cur: Dict[int, Tuple[int, ...]],
             cset: SlotSet) -> Dict[int, Tuple[int, ...]]:
    """One DP step: choose a slot strictly greater than the previous one."""
    nxt: Dict[int, Tuple[int, ...]] = {}
    for last, assign in cur.items():
        for j in cset:
            if j > last and j not in nxt:
                nxt[j] = assign + (j,)
    return nxt


def _anchor_feasible(
    before_sets: Sequence[SlotSet],
    after_sets: Sequence[SlotSet],
    slot_a: int,
    slot_b: int,
) -> bool:
    """Check strictly increasing slots for all anchors around the split.

    Order: preserved strokes before the split, piece A (``slot_a``), piece B
    (``slot_b = slot_a + 1``), preserved strokes after it.  Every chosen slot
    is distinct because the sequence is strictly increasing.
    """
    cur: Dict[int, Tuple[int, ...]] = {-1: ()}
    for cset in list(before_sets):
        cur = _advance(cur, cset)
        if not cur:
            return False

    # Piece A must follow every preceding preserved anchor.
    if not any(slot_a > last for last in cur):
        return False
    cur = {slot_a: ()}

    # Piece B is fixed one slot after A by the adjacency requirement.
    if slot_b <= slot_a:
        return False
    cur = {slot_b: ()}

    for cset in after_sets:
        cur = _advance(cur, cset)
        if not cur:
            return False
    return True


def _any_compatible_preserved(before_sets: Sequence[SlotSet],
                              after_sets: Sequence[SlotSet]) -> bool:
    """Whether the preserved anchors alone admit an increasing assignment."""
    cur: Dict[int, Tuple[int, ...]] = {-1: ()}
    for cset in list(before_sets) + list(after_sets):
        cur = _advance(cur, cset)
        if not cur:
            return False
    return True


def _concrete_anchor_slots(
    before_sets: Sequence[SlotSet],
    after_sets: Sequence[SlotSet],
    slot_a: int,
    slot_b: int,
) -> List[int]:
    """Deterministic strictly increasing slots for the preserved anchors."""
    cur: Dict[int, Tuple[int, ...]] = {-1: ()}
    for cset in before_sets:
        cur = _advance(cur, cset)
    before_slots = cur[min(cur)] if cur else ()
    cur = {slot_b: ()}
    for cset in after_sets:
        cur = _advance(cur, cset)
    after_slots = cur[min(cur)] if cur else ()
    return list(before_slots) + list(after_slots)


def build_split_plan(
    strokes: Sequence[Dict[str, Any]],
    model: Sequence[Any],
    threshold: float,
    stroke_index: int,
) -> Dict[str, Any]:
    """Return the optimal trial split for actual stroke ``stroke_index``.

    On success the payload carries the two piece point sequences (a faithful
    partition of the original samples), their matched slots and distances.
    Domain failures come back as ``{"feasible": False, "reason": ...}`` with a
    Chinese explanation; the caller keeps the original ink untouched.
    """
    validation_error = validate_payload(strokes, model)
    if validation_error is not None:
        return {"feasible": False, "reason": validation_error}

    if not isinstance(stroke_index, int) or not (0 <= stroke_index < len(strokes)):
        return {"feasible": False, "reason": "试拆目标笔序号无效"}

    target = strokes[stroke_index]
    if preserved_of(strokes)[stroke_index]:
        return {
            "feasible": False,
            "reason": "第 {} 笔已标记保留，保留笔不能试拆；请选一笔未标保留的实写笔。".format(
                stroke_index + 1),
        }

    if len(strokes) + 1 > MAX_STROKES:
        return {
            "feasible": False,
            "reason": (
                f"当前已有 {len(strokes)} 笔，拆分后会变成 {len(strokes) + 1} 笔，"
                f"超过实写至多 {MAX_STROKES} 笔的上限；请先删除其他笔再试拆。"
            ),
        }

    raw_points = points_of(target)
    n_pts = len(raw_points)
    if n_pts < 4:
        return {
            "feasible": False,
            "reason": (
                f"第 {stroke_index + 1} 笔只有 {n_pts} 个采样点；两段各至少需要两个点，"
                "即原笔至少要有 4 个点才能试拆。"
            ),
        }

    points = [[float(p[0]), float(p[1])] for p in raw_points]
    n_slots = len(model)
    preserved = preserved_of(strokes)

    # Compatibility of every preserved stroke, split into anchors before /
    # after the picked stroke according to actual writing order.
    before_sets: List[SlotSet] = []
    after_sets: List[SlotSet] = []
    for x, stroke in enumerate(strokes):
        if not preserved[x] or x == stroke_index:
            continue
        d = [discrete_frechet(points_of(stroke), points_of(model[j]))
             for j in range(n_slots)]
        cset = _compatible_slots(d, threshold)
        if not cset:
            return {
                "feasible": False,
                "reason": (
                    f"无解：标记保留的第 {x + 1} 笔与任何范字笔画的离散 Fréchet "
                    "距离都超过上限，无法各占不同范字笔位；请调大上限或取消该笔保留。"
                ),
            }
        (before_sets if x < stroke_index else after_sets).append(cset)

    best: Optional[Dict[str, Any]] = None
    saw_adjacent_pair = False
    anchors_order_ok = _any_compatible_preserved(before_sets, after_sets)

    # cut k: piece A = points[:k], piece B = points[k:]; both need >= 2 pts.
    for k in range(2, n_pts - 1):
        seg_a = points[:k]
        seg_b = points[k:]
        dist_a = [discrete_frechet(seg_a, points_of(model[j])) for j in range(n_slots)]
        dist_b = [discrete_frechet(seg_b, points_of(model[j])) for j in range(n_slots)]
        slots_a = _compatible_slots(dist_a, threshold)
        slots_b = _compatible_slots(dist_b, threshold)

        # Enumerate every adjacent reference-slot pair both pieces fit.
        for slot_a in range(n_slots - 1):
            slot_b = slot_a + 1
            if slot_a not in slots_a or slot_b not in slots_b:
                continue
            saw_adjacent_pair = True
            if not _anchor_feasible(before_sets, after_sets, slot_a, slot_b):
                continue

            d_a = dist_a[slot_a]
            d_b = dist_b[slot_b]
            worst = max(d_a, d_b)
            total = d_a + d_b
            # Deterministic minimax: worst distance, then sum, then earliest
            # cut, then the earliest adjacent slot pair.
            key = (worst, total, k, slot_a)
            if best is None or key < best["_key"]:
                best = {
                    "_key": key,
                    "cut_index": k,
                    "slot_a": slot_a,
                    "slot_b": slot_b,
                    "d_a": d_a,
                    "d_b": d_b,
                    "seg_a": [list(p) for p in seg_a],
                    "seg_b": [list(p) for p in seg_b],
                    "preserved_slots": _concrete_anchor_slots(
                        before_sets, after_sets, slot_a, slot_b),
                }

    if best is None:
        if not saw_adjacent_pair:
            reason = (
                "无解：在当前上限 {:.0f} 下，找不到一个切点能让两段分别匹配相邻的两笔范字笔画"
                "（每段都须至少两个点，且两段距离都不超过上限）；可尝试调大 Fréchet 上限，"
                "或检查该笔是否确实并了两笔。".format(threshold)
            )
        elif not anchors_order_ok:
            reason = (
                "无解：标记保留的笔彼此之间无法按实写顺序各占不同的范字笔位"
                "（保留笔不能移动）；请取消部分保留标记后再试拆。"
            )
        else:
            reason = (
                "无解：虽然存在能对上相邻范字笔位的切法，但拆出的两段与已标记保留的笔"
                "无法按实写顺序各占不同的范字笔位（保留笔不能移动）；请取消部分保留标记"
                "或改选其他笔试拆。"
            )
        return {"feasible": False, "reason": reason}

    uid = target.get("uid") if isinstance(target, dict) else None
    return {
        "feasible": True,
        "stroke_index": stroke_index,
        "stroke_uid": uid,
        "cut_index": best["cut_index"],
        "threshold": threshold,
        "worst_distance": round(best["_key"][0], 4),
        "sum_distance": round(best["d_a"] + best["d_b"], 4),
        "resulting_stroke_count": len(strokes) + 1,
        "preserved_slots": best["preserved_slots"],
        "segments": [
            {
                "part": 0,
                "points": best["seg_a"],
                "points_count": len(best["seg_a"]),
                "slot": best["slot_a"],
                "distance": round(best["d_a"], 4),
            },
            {
                "part": 1,
                "points": best["seg_b"],
                "points_count": len(best["seg_b"]),
                "slot": best["slot_b"],
                "distance": round(best["d_b"], 4),
            },
        ],
    }
