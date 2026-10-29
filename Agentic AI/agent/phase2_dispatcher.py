import os
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List

from phase1_coordinator import AutoGraderCoordinator
from analyzers import ANALYZER_REGISTRY


class Phase2Dispatcher:
    def __init__(self, coordinator: AutoGraderCoordinator):
        self.coordinator = coordinator

    def _run_single_metric(self, metric_rule: Dict[str, Any]) -> Dict[str, Any]:
        """Executes a single expert analyzer and computes its weighted score contribution."""
        metric_name = metric_rule["name"]
        analyzer_key = metric_rule["analyzer"]
        weight = float(metric_rule["weight"])
        params = metric_rule.get("params", {})

        analyzer_fn = ANALYZER_REGISTRY.get(analyzer_key)
        if not analyzer_fn:
            return {
                "name": metric_name,
                "analyzer": analyzer_key,
                "score": 0.0,
                "weight": weight,
                "contribution": 0.0,
                "evidence": {"error": f"Unknown analyzer '{analyzer_key}'"},
                "feedback": f"Analyzer '{analyzer_key}' is not registered."
            }

        res = analyzer_fn(
            submission_path=self.coordinator.sandboxed_src_path,
            binary_path=self.coordinator.binary_path,
            sandbox_dir=self.coordinator.sandbox_dir,
            params=params,
            time_limit_s=int(self.coordinator.rules.get("time_limit_s", 1)),
            memory_limit_mb=int(self.coordinator.rules.get("memory_limit_mb", 64)),
            problem_description=self.coordinator.rules.get(
                "problem_description",
                "Read two integers on a single line separated by whitespace and print their sum followed by a newline."
            ),
            original_submission_path=getattr(self.coordinator, "original_submission_path", self.coordinator.submission_path),
            llm_config=self.coordinator.rules.get(
                "llm_config",
                {"model": "llama3.1", "temperature": 0, "seed": 42}
            )
        )

        raw_score = float(res["score"])
        contribution = round(raw_score * weight * 100.0, 2)

        return {
            "name": metric_name,
            "analyzer": analyzer_key,
            "score": raw_score,
            "weight": weight,
            "contribution": contribution,
            "evidence": res["evidence"],
            "feedback": res["feedback"]
        }

    def dispatch_all(self, max_workers: int = 6) -> Dict[str, Any]:
        """Runs Phase 1 and dispatches all scheduled Phase 2 analyzers concurrently."""
        first_pass = self.coordinator.run_first_pass()
        plan = self.coordinator.plan_analyzer_dispatch(first_pass)

        metric_results: List[Dict[str, Any]] = []

        # 1. Dispatch scheduled analyzers in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_metric = {
                executor.submit(self._run_single_metric, m): m
                for m in plan["scheduled"]
            }
            for future in as_completed(future_to_metric):
                metric_results.append(future.result())

        # 2. Record 0-score entries for analyzers skipped due to infinite loop or compile error
        for skipped_metric in plan["skipped"]:
            metric_results.append({
                "name": skipped_metric["name"],
                "analyzer": skipped_metric["analyzer"],
                "score": 0.0,
                "weight": float(skipped_metric["weight"]),
                "contribution": 0.0,
                "evidence": {"skipped": True, "reason": skipped_metric["skip_reason"]},
                "feedback": f"Skipped: {skipped_metric['skip_reason']}"
            })

        # Preserve original metric ordering from the rules JSON
        rule_order = {m["name"]: idx for idx, m in enumerate(self.coordinator.rules.get("metrics", []))}
        metric_results.sort(key=lambda x: rule_order.get(x["name"], 999))

        total_grade = round(sum(m["contribution"] for m in metric_results), 2)

        flags = []
        if first_pass.suspected_infinite_loop:
            flags.append("suspected_infinite_loop")
        for m in metric_results:
            if m["analyzer"] == "similarity_flag" and m["evidence"].get("flagged"):
                flags.append("plagiarism_or_llm_similarity_flagged")

        return {
            "submission": os.path.basename(self.coordinator.submission_path),
            "rules_hash": self.coordinator.rules_hash,
            "grade": total_grade,
            "first_pass": {
                "compiled": first_pass.compiled,
                "smoke_ok": first_pass.smoke_ok,
                "suspected_infinite_loop": first_pass.suspected_infinite_loop
            },
            "metrics": metric_results,
            "flags": flags
        }


if __name__ == "__main__":
    rules_path = sys.argv[1] if len(sys.argv) > 1 else "rules/add_rules.json"
    sub_path = sys.argv[2] if len(sys.argv) > 2 else "submissions/add.c"

    coord = AutoGraderCoordinator(rules_path, sub_path)
    try:
        dispatcher = Phase2Dispatcher(coord)
        full_evaluation = dispatcher.dispatch_all()
        print(json.dumps(full_evaluation, indent=2))
    finally:
        coord.cleanup()
