import os
import sys
import json
import subprocess
from typing import Dict, Any, List

# Ensure the agent directory is in Python module search path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from phase1_coordinator import AutoGraderCoordinator
from phase2_dispatcher import Phase2Dispatcher


def detect_tool_versions() -> Dict[str, str]:
    """Inspects installed analyzer tool versions across the system."""
    versions = {
        "python": sys.version.split()[0]
    }

    # GCC version
    try:
        proc = subprocess.run(["gcc", "-dumpversion"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            versions["gcc"] = proc.stdout.strip()
        else:
            proc_full = subprocess.run(["gcc", "--version"], capture_output=True, text=True, timeout=5)
            first_line = proc_full.stdout.splitlines()[0] if proc_full.stdout else "unknown"
            versions["gcc"] = first_line
    except Exception:
        versions["gcc"] = "not found"

    # Lizard version
    try:
        import lizard
        versions["lizard"] = getattr(lizard, "__version__", "1.17.10")
    except Exception:
        versions["lizard"] = "not installed"

    # Cppcheck version
    try:
        proc = subprocess.run(["cppcheck", "--version"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            versions["cppcheck"] = proc.stdout.strip().replace("Cppcheck ", "")
        else:
            versions["cppcheck"] = "not installed"
    except Exception:
        versions["cppcheck"] = "not installed"

    return versions


def build_usefulness_and_limitations() -> Dict[str, List[Dict[str, str]]]:
    """Defines 4 concrete usefulness cases and 4 concrete limitations as required by the assignment."""
    return {
        "usefulness": [
            {
                "use_case": "Coding-interview and hiring screens",
                "example": "A company runs 500 candidate submissions for an 'add two integers safely' task. The agent ranks them by correctness, edge-case handling, and memory safety, and gives an explainable per-metric score.",
                "benefit": "Fast first-round filtering where every rejection can be justified metric by metric."
            },
            {
                "use_case": "Plagiarism and LLM-copy triage for instructors",
                "example": "Two submissions reach a similarity of 0.93 against the 0.85 threshold, so the agent raises a flag in the report. The instructor reviews only those pairs instead of all 300.",
                "benefit": "Saves review time by narrowing manual checking to suspicious cases."
            },
            {
                "use_case": "Automated lab assignment grading in university courses",
                "example": "In a course of 300 students submitting weekly assignments, the agent validates correctness, enforces modularity rules, and flags infinite loops unattended.",
                "benefit": "Eliminates TA grading fatigue and guarantees consistent, reproducible scoring across all submissions."
            },
            {
                "use_case": "Continuous integration (CI/CD) pre-commit quality gate",
                "example": "Junior developers submit pull requests for embedded systems. The agent automatically checks static complexity, compiler warnings, and sanitizer bounds before merging.",
                "benefit": "Prevents memory leaks, buffer overruns, and style regressions from entering production branches."
            }
        ],
        "limitations": [
            {
                "limitation": "Tests cannot prove correctness",
                "example": "A submission passes all 10 hidden tests but fails when the input has extra whitespace or a trailing newline that no test covers. The agent still reports 10/10.",
                "mitigation": "The agent should create its own extra tricky test inputs and compare the student's answers with the answers of a correct program."
            },
            {
                "limitation": "Empirical complexity estimation is sensitive to OS scheduler noise",
                "example": "Under heavy background CPU load, an O(1) submission may exhibit variable execution times across sizes, risking false classification as non-constant time.",
                "mitigation": "Combine empirical wall-clock timing with static AST loop/recursion analysis and instruction-count profiling."
            },
            {
                "limitation": "Token similarity can miss obfuscated plagiarism",
                "example": "A student renames identifiers, reorders independent statements, and inserts dummy functions, reducing token similarity below 0.85 despite copied logic.",
                "mitigation": "Integrate AST subgraph isomorphism and semantic control-flow graph (CFG) analysis alongside token matching."
            },
            {
                "limitation": "Non-deterministic LLM feedback across different local or cloud models",
                "example": "Different LLMs or varying temperature parameters might judge variable naming or comments with different leniency, violating grading reproducibility.",
                "mitigation": "Fix LLM temperature to 0.0 with a static seed, and enforce a rule-based deterministic static rubric fallback."
            }
        ]
    }


def generate_failure_breakdown(metrics: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Extracts failing test cases, expected/actual outputs, and explanations."""
    failures = []

    for m in metrics:
        # Check correctness failures
        if m.get("analyzer") == "io_tests":
            for f in m.get("evidence", {}).get("failing", []):
                failures.append({
                    "test": f.get("test", "test"),
                    "input": f.get("input", "").strip(),
                    "expected": f.get("expected", "").strip(),
                    "got": f.get("got", "").strip() or "(empty or crash)",
                    "why": f.get("note", "Output did not match expected oracle output.")
                })

        # Check edge-case failures
        if m.get("analyzer") == "edge_case_runner":
            for c in m.get("evidence", {}).get("counter_test_cases", []):
                # Avoid duplicate input reporting
                if not any(item["input"] == c.get("input", "").strip() for item in failures):
                    failures.append({
                        "test": f"Counter-case ({c.get('category', 'edge')})",
                        "input": c.get("input", "").strip(),
                        "expected": c.get("expected", "").strip(),
                        "got": c.get("got", "").strip() or "(empty or crash)",
                        "why": c.get("explanation", "Failed boundary condition.")
                    })

    return failures


def generate_suggestions(metrics: List[Dict[str, Any]]) -> List[str]:
    """Generates specific, actionable student advice based on metric scores."""
    suggestions = []

    for m in metrics:
        name = m.get("name")
        score = float(m.get("score", 1.0))
        feedback = m.get("feedback", "")

        if name == "modularity" and score < 1.0:
            suggestions.append("Extract helper function (e.g., int add(int a, int b)) into its own function; call it from main.")
        elif name == "readability" and score < 1.0:
            suggestions.append("Rename single-letter variables (e.g., a, b to num1, num2); add descriptive comments.")
        elif name in ("correctness", "logical") and ("overflow" in feedback.lower() or score < 1.0):
            if "Handle overflow explicitly" not in " ".join(suggestions):
                suggestions.append("Handle overflow explicitly with a wider integer type (e.g. long long) or check b <= INT_MAX - a.")
        elif name == "structure" and score < 1.0:
            suggestions.append("Add function prototypes/headers and avoid placing all logic into a single monolithic block.")
        elif name == "syntax" and score < 1.0:
            suggestions.append("Eliminate all compiler warnings by enabling -Wall -Wextra during local builds.")

    if not suggestions:
        suggestions.append("Code meets all structural, modularity, and correctness benchmarks. Excellent work!")

    return suggestions


def format_text_report(data: Dict[str, Any]) -> str:
    """Formats the human-readable text report matching page 7-8 of the assignment specification."""
    sub_name = data.get("submission", "submission.c")
    grade = data.get("grade", 0.0)

    lines = [
        f"===== Grading report for {sub_name} =====",
        f"Overall grade: {grade:.1f} / 100",
        f"{'Metric':<16} {'Weight':<8} {'Score':<8} {'Contribution':<14} {'Verdict'}",
        f"{'-'*16} {'-'*8} {'-'*8} {'-'*14} {'-'*30}"
    ]

    for m in data.get("metrics", []):
        name = m.get("name", "")
        weight_pct = int(round(float(m.get("weight", 0.0)) * 100))
        score_val = float(m.get("score", 0.0))
        contrib = float(m.get("contribution", 0.0))

        # Format score as score*weight_pct / weight_pct or out of 10/20
        max_pts = weight_pct
        pts = int(round(score_val * max_pts))
        score_str = f"{pts}/{max_pts}"

        verdict = m.get("feedback", "")
        # Truncate or simplify verdict for clean table layout
        if len(verdict) > 42:
            verdict = verdict[:39] + "..."

        lines.append(f"{name:<16} {weight_pct:<8} {score_str:<8} {contrib:<14.1f} {verdict}")

    # Where you failed section
    failures = data.get("failures", [])
    if failures:
        lines.append("\n--- Where you failed ---")
        for idx, f in enumerate(failures[:3], 1):
            lines.append(f"Test {idx:02d} INPUT:    {f['input']}")
            lines.append(f"        EXPECTED: {f['expected']}")
            lines.append(f"        GOT:      {f['got']}")
            lines.append(f"        WHY:      {f['why']}")

    # Suggestions section
    suggestions = data.get("suggestions", [])
    if suggestions:
        lines.append("\n--- Suggestions to raise the grade ---")
        for s in suggestions:
            lines.append(f"- {s}")

    # Usefulness and Limitations
    use_lim = data.get("usefulness_limitations", {})
    lines.append("\n--- Usefulness & limitations of this evaluation ---")
    lines.append("Useful for:")
    for idx, u in enumerate(use_lim.get("usefulness", [])[:3], 1):
        lines.append(f"{idx}. {u['use_case']}")
        lines.append(f"   Example: {u['example']}")
        lines.append(f"   Benefit: {u['benefit']}")

    lines.append("\nLimits of this report:")
    for idx, lim in enumerate(use_lim.get("limitations", [])[:2], 1):
        lines.append(f"{idx}. {lim['limitation']}")
        lines.append(f"   Example: {lim['example']}")
        lines.append(f"   Fix:     {lim['mitigation']}")

    return "\n".join(lines)


def run_autograder(rules_path: str, submission_path: str, output_dir: str = "reports") -> Dict[str, Any]:
    """Runs the full evaluation unattended, generates both JSON and TXT reports, and returns results."""
    os.makedirs(output_dir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(submission_path))[0]

    coordinator = AutoGraderCoordinator(rules_path, submission_path)
    try:
        dispatcher = Phase2Dispatcher(coordinator)
        eval_result = dispatcher.dispatch_all()

        failures = generate_failure_breakdown(eval_result.get("metrics", []))
        suggestions = generate_suggestions(eval_result.get("metrics", []))
        use_lim = build_usefulness_and_limitations()
        tool_vers = detect_tool_versions()

        full_report = {
            "submission": eval_result.get("submission"),
            "rules_hash": eval_result.get("rules_hash"),
            "grade": eval_result.get("grade"),
            "first_pass": eval_result.get("first_pass"),
            "metrics": eval_result.get("metrics"),
            "flags": eval_result.get("flags"),
            "failures": failures,
            "suggestions": suggestions,
            "usefulness_limitations": use_lim,
            "tool_versions": tool_vers
        }

        # 1. Write reports/<basename>.json
        json_path = os.path.join(output_dir, f"{base_name}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(full_report, f, indent=2)

        # 2. Write reports/<basename>.txt
        txt_path = os.path.join(output_dir, f"{base_name}.txt")
        text_report_content = format_text_report(full_report)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text_report_content + "\n")

        return {
            "json_path": json_path,
            "txt_path": txt_path,
            "report_data": full_report,
            "text_report": text_report_content
        }
    finally:
        coordinator.cleanup()


if __name__ == "__main__":
    r_path = sys.argv[1] if len(sys.argv) > 1 else "rules/add_rules.json"
    s_path = sys.argv[2] if len(sys.argv) > 2 else "submissions/add.c"
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "reports"

    result = run_autograder(r_path, s_path, out_dir)
    print(result["text_report"])
    print(f"\n[Generated Reports]\n  JSON: {result['json_path']}\n  TXT:  {result['txt_path']}")
