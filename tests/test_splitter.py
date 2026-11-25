import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.frechet import discrete_frechet  # noqa: E402
from app.core.sample import make_merged_sample  # noqa: E402
from app.core.splitter import build_split_plan  # noqa: E402
from app.core.planner import build_plan  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "app", "data", "characters")


def load(char_id):
    with open(os.path.join(DATA, f"{char_id}.json"), encoding="utf-8") as f:
        return json.load(f)


def stroke(points, uid, preserved=False):
    return {"uid": uid, "points": [list(p) for p in points],
            "preserved": preserved}


def merge(a, b):
    return [list(p) for p in a] + [list(p) for p in b]


class TestSplitBasic(unittest.TestCase):
    def test_merged_mu_sample_splits_into_first_two_slots(self):
        model = load("mu")["strokes"]
        strokes = make_merged_sample(model)
        # First recorded stroke holds reference strokes 0 and 1 merged.
        self.assertEqual(len(strokes), len(model) - 1)
        plan = build_split_plan(strokes, model, 180.0, 0)
        self.assertTrue(plan["feasible"], plan.get("reason"))
        seg = plan["segments"]
        self.assertEqual([s["slot"] for s in seg], [0, 1])
        self.assertGreaterEqual(seg[0]["points_count"], 2)
        self.assertGreaterEqual(seg[1]["points_count"], 2)
        # Faithful partition: every sample kept, in order, same coordinates.
        rebuilt = seg[0]["points"] + seg[1]["points"]
        self.assertEqual(rebuilt, strokes[0]["points"])
        self.assertEqual(plan["cut_index"], seg[0]["points_count"])
        # Both distances are within the threshold and equal the reported worst.
        for s in seg:
            self.assertLessEqual(s["distance"], 180 + 1e-9)
        self.assertEqual(plan["worst_distance"],
                         round(max(seg[0]["distance"], seg[1]["distance"]), 4))
        self.assertEqual(plan["resulting_stroke_count"], len(strokes) + 1)

    def test_adopted_split_replans_with_zero_cost(self):
        model = load("shi")["strokes"]
        strokes = make_merged_sample(model)
        sp = build_split_plan(strokes, model, 180.0, 0)
        self.assertTrue(sp["feasible"])
        adopted = [
            {"uid": "p0", "points": sp["segments"][0]["points"], "preserved": True},
            {"uid": "p1", "points": sp["segments"][1]["points"], "preserved": True},
        ]
        plan = build_plan(adopted, model, 180.0)
        self.assertTrue(plan["feasible"], plan.get("reason"))
        self.assertEqual(plan["cost"], 0)
        self.assertEqual(plan["counts"], {"move": 0, "delete": 0, "add": 0})

    def test_preserved_target_is_rejected(self):
        model = load("shi")["strokes"]
        strokes = make_merged_sample(model)
        strokes[0]["preserved"] = True
        plan = build_split_plan(strokes, model, 180.0, 0)
        self.assertFalse(plan["feasible"])
        self.assertIn("保留", plan["reason"])

    def test_too_few_points(self):
        model = load("shi")["strokes"]
        strokes = [stroke([[100, 100], [200, 200], [300, 300]], "x")]
        plan = build_split_plan(strokes, model, 10 ** 9, 0)
        self.assertFalse(plan["feasible"])
        self.assertIn("4 个点", plan["reason"])

    def test_ten_stroke_cap(self):
        model = load("shi")["strokes"]
        strokes = [stroke([[10 + i, 10], [20 + i, 20], [30 + i, 30], [40 + i, 40]],
                          f"s{i}") for i in range(10)]
        plan = build_split_plan(strokes, model, 10 ** 9, 0)
        self.assertFalse(plan["feasible"])
        self.assertIn("10 笔", plan["reason"])

    def test_bad_index(self):
        model = load("shi")["strokes"]
        plan = build_split_plan([], model, 180.0, 0)
        self.assertFalse(plan["feasible"])

    def test_threshold_too_tight_no_adjacent_match(self):
        model = load("mu")["strokes"]
        strokes = make_merged_sample(model)
        plan = build_split_plan(strokes, model, 1.0, 0)
        self.assertFalse(plan["feasible"])
        self.assertIn("相邻", plan["reason"])

    def test_non_adjacent_slots_cannot_be_used(self):
        # Three-slot model: horizontals at slots 0 and 2, a vertical at 1.
        h0 = [[150, 300], [850, 300]]
        v1 = [[500, 150], [500, 850]]
        h2 = [[150, 700], [850, 700]]
        model = [h0, v1, h2]
        target = stroke(merge(h0, h2), "m")
        plan = build_split_plan([target], model, 120.0, 0)
        # The two pieces can only match slots 0 and 2, which are not adjacent.
        self.assertFalse(plan["feasible"])
        self.assertIn("相邻", plan["reason"])

    def test_preserved_stroke_without_compatible_slot(self):
        model = load("mu")["strokes"]
        strokes = make_merged_sample(model)
        far = stroke([[60, 80], [220, 120], [90, 170]], "far", preserved=True)
        strokes.insert(0, far)  # now the merged stroke sits at index 1
        plan = build_split_plan(strokes, model, 180.0, 1)
        self.assertFalse(plan["feasible"])
        self.assertIn("保留", plan["reason"])

    def test_preserved_order_conflict(self):
        model = load("mu")["strokes"]
        # Target (index 0) merges reference slots 0 and 1.
        target = stroke(merge(model[0], model[1]), "m")
        # A preserved stroke afterwards that can only occupy slot 0.
        anchor = stroke([list(p) for p in model[0]], "h", preserved=True)
        plan = build_split_plan([target, anchor], model, 180.0, 0)
        self.assertFalse(plan["feasible"])
        self.assertIn("保留", plan["reason"])

    def test_preserved_anchors_before_and_after_succeed(self):
        # Preserved strokes around the split keep distinct ordered slots.
        model = load("mu")["strokes"]
        before = stroke([list(p) for p in model[0]], "b", preserved=True)
        target = stroke(merge(model[1], model[2]), "m")
        after = stroke([list(p) for p in model[3]], "a", preserved=True)
        plan = build_split_plan([before, target, after], model, 180.0, 1)
        self.assertTrue(plan["feasible"], plan.get("reason"))
        self.assertEqual([s["slot"] for s in plan["segments"]], [1, 2])
        self.assertEqual(plan["preserved_slots"], [0, 3])


class TestMinimaxOptimality(unittest.TestCase):
    def _brute_force_worst(self, points, model, threshold):
        """Minimax over every cut and adjacent slot pair (no anchors)."""
        best = None
        n = len(points)
        for k in range(2, n - 1):
            a, b = points[:k], points[k:]
            for j in range(len(model) - 1):
                da = discrete_frechet(a, model[j])
                db = discrete_frechet(b, model[j + 1])
                if da <= threshold + 1e-9 and db <= threshold + 1e-9:
                    key = (max(da, db), da + db, k)
                    if best is None or key < best:
                        best = key
        return best

    def test_returned_worst_equals_brute_force(self):
        rng = random.Random(77)
        checked = 0
        for _ in range(60):
            n_slots = rng.randrange(2, 5)
            model = [[(rng.randrange(100, 900), rng.randrange(100, 900))
                      for _ in range(rng.randrange(4, 10))]
                     for _ in range(n_slots)]
            j = rng.randrange(n_slots - 1)
            a = [[x + rng.randrange(-60, 61), y + rng.randrange(-60, 61)]
                 for x, y in model[j]]
            b = [[x + rng.randrange(-60, 61), y + rng.randrange(-60, 61)]
                 for x, y in model[j + 1]]
            merged = a + b
            threshold = rng.choice([160.0, 260.0, 10 ** 9])
            plan = build_split_plan([stroke(merged, "m")], model, threshold, 0)
            brute = self._brute_force_worst(merged, model, threshold)
            if brute is None:
                self.assertFalse(plan["feasible"])
                continue
            self.assertTrue(plan["feasible"], plan.get("reason"))
            checked += 1
            self.assertAlmostEqual(plan["worst_distance"], round(brute[0], 4),
                                   places=3)
            self.assertEqual(plan["cut_index"], brute[2])
        self.assertGreater(checked, 20)

    def test_deterministic_across_calls(self):
        model = load("mu")["strokes"]
        strokes = make_merged_sample(model)
        p1 = build_split_plan(strokes, model, 180.0, 0)
        p2 = build_split_plan(strokes, model, 180.0, 0)
        self.assertEqual(p1["cut_index"], p2["cut_index"])
        self.assertEqual(p1["segments"], p2["segments"])


if __name__ == "__main__":
    unittest.main()
