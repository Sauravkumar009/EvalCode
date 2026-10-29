import os
import sys

# Add agent folder to path
agent_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent")
if agent_dir not in sys.path:
    sys.path.insert(0, agent_dir)

from autograder import run_autograder

if __name__ == "__main__":
    rules_file = sys.argv[1] if len(sys.argv) > 1 else "rules/add_rules.json"
    submission_file = sys.argv[2] if len(sys.argv) > 2 else "submissions/add.c"
    output_directory = sys.argv[3] if len(sys.argv) > 3 else "reports"

    print(f"=== AutoGrader: Rule-Driven Evaluator for C Programs ===")
    print(f"Rules:      {rules_file}")
    print(f"Submission: {submission_file}\n")

    res = run_autograder(rules_file, submission_file, output_directory)
    print(res["text_report"])
    print(f"\n[Generated Reports]\n  JSON: {res['json_path']}\n  TXT:  {res['txt_path']}")
