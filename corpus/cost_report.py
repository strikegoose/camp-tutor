#!/usr/bin/env python3
"""成本报告:聚合 data/token-ledger.jsonl → data/cost-report.json(逐部件 token 与折算金额)。
单价读 config/pricing.yaml;缓存命中不计费但单列。"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import common  # noqa: E402

import yaml  # noqa: E402


def model_family(model):
    m = (model or "").split("[")[0]
    return m


def main():
    pricing = yaml.safe_load((common.CONFIG / "pricing.yaml").read_text(encoding="utf-8"))["prices"]
    ledger = common.DATA / "token-ledger.jsonl"
    stats = defaultdict(lambda: {"calls": 0, "cached_calls": 0, "input_tokens": 0,
                                 "output_tokens": 0, "cost_yuan": 0.0, "models": set()})
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = __import__("json").loads(line)
            part = r.get("part", "unknown")
            s = stats[part]
            fam = model_family(r.get("model"))
            s["models"].add(r.get("model", ""))
            if r.get("cached"):
                s["cached_calls"] += 1
                continue
            s["calls"] += 1
            s["input_tokens"] += r.get("input_tokens", 0)
            s["output_tokens"] += r.get("output_tokens", 0)
            price = pricing.get(fam, {"input": 0, "output": 0, "source": "缺"})
            s["cost_yuan"] += (r.get("input_tokens", 0) * price["input"]
                               + r.get("output_tokens", 0) * price["output"]) / 1e6
    total = {"calls": 0, "cached_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_yuan": 0.0}
    parts = {}
    for part, s in sorted(stats.items()):
        parts[part] = {k: (round(v, 4) if k == "cost_yuan" else v) for k, v in s.items() if k != "models"}
        parts[part]["models"] = sorted(s["models"])
        for k in ("calls", "cached_calls", "input_tokens", "output_tokens", "cost_yuan"):
            total[k] += parts[part][k]
    total["cost_yuan"] = round(total["cost_yuan"], 4)
    report = {
        "generated_by": "corpus/cost_report.py",
        "pricing_config": "config/pricing.yaml",
        "note": "cached_calls 为幂等重跑的缓存命中,不计费;标注(估)的单价见 pricing.yaml",
        "parts": parts,
        "total": total,
    }
    common.write_json(common.DATA / "cost-report.json", report)
    print(f"cost-report.json 已生成: 总调用 {total['calls']} 次(缓存命中 {total['cached_calls']}),"
          f" 输入 {total['input_tokens']}, 输出 {total['output_tokens']}, 折算 ¥{total['cost_yuan']}")


if __name__ == "__main__":
    main()
