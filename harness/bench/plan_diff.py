"""Do two analysis runs (harness/bench/analysis.py outputs) give the same zoom plan?

    python3 harness/bench/plan_diff.py A.json B.json
"""
import json
import sys

a, b = (json.load(open(p)) for p in sys.argv[1:3])
sa, sb = a["segments"], b["segments"]
same = sa == sb
worst = 0.0
if len(sa) == len(sb):
    for x, y in zip(sa, sb):
        for k in x:
            if isinstance(x[k], (int, float)) and isinstance(y.get(k), (int, float)):
                worst = max(worst, abs(x[k] - y[k]))
print(json.dumps({"take": a["take"], "roots": [a.get("root"), b.get("root")], "segments": [len(sa), len(sb)],
                  "identical": same, "largest_numeric_difference": round(worst, 6),
                  "samples": [a["samples"], b["samples"]], "blobs": [a["blobs"], b["blobs"]],
                  "changed_fraction_sum": [a["changed_fraction_sum"], b["changed_fraction_sum"]],
                  "analysis_wall_s": [a["wall_s"], b["wall_s"]],
                  "zooms": [[round(s.get("start", 0), 2), round(s.get("end", 0), 2), round(s.get("zoom", s.get("z", 0)), 3)] for s in sa]}))
