# AutoGrader: A Rule-Driven Evaluator for C Programs

An autonomous, rule-driven evaluation agent designed to grade student C submissions against instructor-supplied JSON rules files unattended.

---

## Architecture Overview

The agent operates in two coordinated phases:

```
[Student C Submission] + [Instructor rules.json]
                 |
                 v
   +------------------------------+
   |   PHASE 1: SANITY COORDINATOR |
   | - Hash rules (SHA-256)       |
   | - Static sanity sweep (main) |
   | - GCC compile check          |
   | - Smoke run (timeout/memory) |
   +------------------------------+
                 |
        +--------+--------+
        |                 |
(Clean Exit)        (Infinite Loop / Crash)
        |                 |
        v                 v
[Dispatch All]     [Skip Execution Analyzers]
        |                 |
        +--------+--------+
                 |
                 v
   +------------------------------+
   |   PHASE 2: EXPERT ANALYZERS  |
   | 1. Correctness (io_tests)    |
   | 2. Syntax (compiler_warn)    |
   | 3. Structure (cyclomatic)    |
   | 4. Modularity (func_split)   |
   | 5. Logical (edge_cases)      |
   | 6. Complexity (O(1) timing)  |
   | 7. Memory Safety (ASan/LSan) |
   | 8. Readability (LLM review)  |
   | 9. Originality (similarity)  |
   +------------------------------+
                 |
                 v
   +------------------------------+
   |      COLLATION ENGINE        |
   | - Weighted score summation   |
   | - Specific failure breakdown |
   | - Actionable suggestions     |
   | - Usefulness & Limitations   |
   | - Output JSON + TXT reports  |
   +------------------------------+
```

---

## Quickstart & Usage

### 1. Requirements
- Python 3.10+
- GCC compiler (`gcc`)
- Python packages:
  ```bash
  pip install requests lizard
  ```

### 2. Run the AutoGrader
Run against the sample student submission:
```bash
python main.py rules/add_rules.json submissions/add.c
```

Run against the modular solution:
```bash
python main.py rules/add_rules.json submissions/add_modular.c
```

Run against test edge submissions (infinite loop, crash, syntax error):
```bash
python main.py rules/add_rules.json submissions/add_infinite_loop.c
python main.py rules/add_rules.json submissions/add_crash.c
python main.py rules/add_rules.json submissions/add_syntax_error.c
```

### 3. Generated Deliverables
- Machine-readable JSON report: `reports/<submission>.json`
- Human-readable text report: `reports/<submission>.txt`
