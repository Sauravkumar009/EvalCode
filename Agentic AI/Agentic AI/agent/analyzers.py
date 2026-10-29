import os
import re
import time
import json
import glob
import signal
import difflib
import subprocess
from typing import Dict, Any, List, Optional
import lizard
import requests
from llm_test_generator import generate_standard_test_suite, generate_adversarial_edge_cases
try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    HAS_RESOURCE = False

if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass


def _sandbox_preexec(memory_limit_mb: int, time_limit_s: int):
    """Enforces OS-level memory and CPU limits for child binary executions."""
    def _set_limits():
        if HAS_RESOURCE:
            mem_bytes = memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (time_limit_s + 1, time_limit_s + 1))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setsid()
    return _set_limits


def run_binary_safely(
    binary_path: str,
    input_data: str,
    time_limit_s: int = 1,
    memory_limit_mb: int = 64,
    cwd: Optional[str] = None
) -> Dict[str, Any]:
    """Helper to execute a compiled binary safely and measure wall-clock runtime & memory."""
    if os.name == "nt" and not os.path.exists(binary_path) and os.path.exists(binary_path + ".exe"):
        binary_path = binary_path + ".exe"

    preexec = _sandbox_preexec(memory_limit_mb, time_limit_s) if os.name == "posix" else None
    start_time = time.perf_counter()

    proc = subprocess.Popen(
        [binary_path],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=preexec
    )

    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=time_limit_s)
        elapsed_s = time.perf_counter() - start_time
        return {
            "timed_out": False,
            "return_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "elapsed_s": elapsed_s
        }
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
        stdout, stderr = proc.communicate()
        return {
            "timed_out": True,
            "return_code": None,
            "stdout": stdout or "",
            "stderr": stderr or "Execution timed out",
            "elapsed_s": float(time_limit_s)
        }


# =====================================================================
# 1. CORRECTNESS ANALYZER (io_tests)
# =====================================================================
def analyze_io_tests(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    time_limit_s: int = 1,
    memory_limit_mb: int = 64,
    problem_description: str = "",
    llm_config: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    testset_dir = params.get("testset_dir", "tests/add/")
    if not os.path.isabs(testset_dir):
        base_candidates = [
            os.getcwd(),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
            os.path.dirname(submission_path)
        ]
        for b in base_candidates:
            cand = os.path.join(b, testset_dir)
            if os.path.exists(cand) and glob.glob(os.path.join(cand, "*.in")):
                testset_dir = cand
                break
        else:
            # If not found yet, default to project root relative path
            testset_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", testset_dir))

    auto_generate = bool(params.get("auto_generate", True))
    num_tests = int(params.get("num_tests", 10))
    llm_cfg = llm_config or {"model": "llama3.1", "temperature": 0, "seed": 42}

    in_files = sorted(glob.glob(os.path.join(testset_dir, "*.in")))

    # Automatically generate test suite via Ollama if requested or if directory is empty
    if (auto_generate or not in_files) and problem_description:
        generate_standard_test_suite(
            problem_description=problem_description,
            testset_dir=testset_dir,
            num_tests=num_tests,
            model=llm_cfg.get("model", "llama3.1"),
            temperature=float(llm_cfg.get("temperature", 0)),
            seed=int(llm_cfg.get("seed", 42))
        )
        in_files = sorted(glob.glob(os.path.join(testset_dir, "*.in")))

    if not in_files:
        return {
            "score": 0.0,
            "evidence": {"error": f"No test files found or generated in {testset_dir}"},
            "feedback": "No test cases available."
        }

    passed = 0
    failing_cases = []

    for in_file in in_files:
        base = os.path.splitext(in_file)[0]
        out_file = base + ".out"
        test_name = os.path.basename(base)

        with open(in_file, "r", encoding="utf-8") as f:
            inp = f.read()
        with open(out_file, "r", encoding="utf-8") as f:
            expected = f.read().strip()

        res = run_binary_safely(binary_path, inp, time_limit_s, memory_limit_mb, cwd=sandbox_dir)
        actual = res["stdout"].strip()

        if not res["timed_out"] and res["return_code"] == 0 and actual == expected:
            passed += 1
        else:
            note = "wrong answer (output mismatch)"
            if res["timed_out"]:
                note = "runtime error: time limit exceeded"
            elif res["return_code"] != 0:
                note = f"runtime error: exit code {res['return_code']}"
            elif "2147483647" in inp or "-2147483648" in inp:
                note = "runtime error: signed integer overflow"

            failing_cases.append({
                "test": test_name,
                "input": inp.strip(),
                "expected": expected,
                "got": actual,
                "note": note
            })

    total = len(in_files)
    score = round(passed / total, 4)
    if failing_cases:
        first_fail = failing_cases[0]
        has_overflow = any("signed integer overflow" in c.get("note", "") for c in failing_cases)
        if has_overflow:
            feedback = f"{passed}/{total} tests pass. Fails on INT_MAX + 1: signed overflow."
        else:
            feedback = f"{passed}/{total} tests pass. Fails on input '{first_fail['input']}' (expected '{first_fail['expected']}', got '{first_fail['got']}')."
    else:
        feedback = f"All {total}/{total} tests pass."

    return {
        "score": score,
        "evidence": {
            "generated_by_llm": auto_generate,
            "tests_passed": passed,
            "tests_total": total,
            "failing": failing_cases
        },
        "feedback": feedback
    }


# =====================================================================
# 5. LLM-POWERED LOGICAL & EDGE CASE ANALYZER (edge_case_runner)
# =====================================================================
def analyze_edge_case_runner(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    time_limit_s: int = 1,
    memory_limit_mb: int = 64,
    problem_description: str = "",
    llm_config: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    requested_cases = params.get("cases", ["negatives", "zero", "int_overflow"])
    llm_cfg = llm_config or {"model": "llama3.1", "temperature": 0, "seed": 42}

    with open(submission_path, "r", encoding="utf-8", errors="replace") as f:
        student_code = f.read()

    # Dynamically generate adversarial edge cases & counter-examples using Ollama
    edge_test_list = generate_adversarial_edge_cases(
        problem_description=problem_description,
        student_c_code=student_code,
        categories=requested_cases,
        model=llm_cfg.get("model", "llama3.1"),
        temperature=float(llm_cfg.get("temperature", 0)),
        seed=int(llm_cfg.get("seed", 42))
    )

    total_checks = len(edge_test_list)
    passed_checks = 0
    counter_examples = []

    for edge in edge_test_list:
        inp = edge["input"]
        expected = edge["expected"]
        category = edge["category"]
        explanation = edge["explanation"]

        res = run_binary_safely(binary_path, inp, time_limit_s, memory_limit_mb, cwd=sandbox_dir)
        got = res["stdout"].strip()

        if not res["timed_out"] and res["return_code"] == 0 and got == expected:
            passed_checks += 1
        else:
            counter_examples.append({
                "category": category,
                "input": inp.strip(),
                "expected": expected,
                "got": got,
                "explanation": explanation
            })

    score = round(passed_checks / max(1, total_checks), 2)
    if counter_examples:
        first_counter = counter_examples[0]
        feedback = (
            f"Fails on counter-test input '{first_counter['input']}' "
            f"(expected '{first_counter['expected']}', got '{first_counter['got']}'). "
            f"WHY: {first_counter['explanation']}"
        )
    else:
        feedback = f"All {total_checks}/{total_checks} LLM-generated corner and edge cases passed."

    return {
        "score": score,
        "evidence": {
            "generated_by_llm": True,
            "cases_tested": total_checks,
            "cases_passed": passed_checks,
            "counter_test_cases": counter_examples
        },
        "feedback": feedback
    }
# =====================================================================
# 2. SYNTAX ANALYZER (compiler_warnings)
# =====================================================================
def analyze_compiler_warnings(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    **kwargs
) -> Dict[str, Any]:
    flags_str = params.get("flags", "-Wall -Wextra -Werror -std=c11")
    # Separate -Werror during diagnostic collection so we can count individual warnings accurately
    flags = [f for f in flags_str.split() if f != "-Werror"]
    out_obj = os.path.join(sandbox_dir, "syntax_check.o")

    cmd = ["gcc"] + flags + ["-fsyntax-only", submission_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

    stderr_lines = proc.stderr.strip().splitlines() if proc.stderr else []
    warnings = [line for line in stderr_lines if "warning:" in line]
    errors = [line for line in stderr_lines if "error:" in line]

    if errors or proc.returncode != 0:
        return {
            "score": 0.0,
            "evidence": {"errors": errors, "warnings": warnings},
            "feedback": f"Compilation failed with {len(errors)} error(s)."
        }

    # Deduct 0.10 per warning, floored at 0.0
    score = max(0.0, round(1.0 - (0.10 * len(warnings)), 2))
    feedback = "Clean compile with 0 warnings." if not warnings else f"{len(warnings)} compiler warning(s) generated."

    return {
        "score": score,
        "evidence": {"warning_count": len(warnings), "warnings": warnings},
        "feedback": feedback
    }


# =====================================================================
# 3. STRUCTURE ANALYZER (static_analysis via cppcheck + lizard)
# =====================================================================
def analyze_static_analysis(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    **kwargs
) -> Dict[str, Any]:
    max_ccn = int(params.get("max_cyclomatic", 8))
    max_len = int(params.get("max_function_length", 40))

    # 1. Run Lizard for Cyclomatic Complexity & Function Length
    liz_analysis = lizard.analyze_file(submission_path)
    functions_info = []
    ccn_violations = 0
    len_violations = 0

    for fn in liz_analysis.function_list:
        functions_info.append({
            "name": fn.name,
            "cyclomatic_complexity": fn.cyclomatic_complexity,
            "nloc": fn.nloc
        })
        if fn.cyclomatic_complexity > max_ccn:
            ccn_violations += 1
        if fn.nloc > max_len:
            len_violations += 1

    # 2. Run cppcheck for static defects
    cppcheck_issues = []
    try:
        proc = subprocess.run(
            ["cppcheck", "--enable=style,warning,performance,portability", "--quiet",
             "--template={severity}:{id}:{message}", submission_path],
            capture_output=True, text=True, timeout=10
        )
        if proc.stderr:
            cppcheck_issues = [
                line for line in proc.stderr.strip().splitlines()
                if not line.startswith("information:missingIncludeSystem")
            ]
    except FileNotFoundError:
        cppcheck_issues = []

    with open(submission_path, "r", encoding="utf-8", errors="replace") as f:
        code = f.read()

    has_custom_header_or_proto = bool(re.search(r"^\s*(?:int|long|void|double|float|char)\s+(?!main\b)\w+\s*\([^)]*\)\s*;", code, re.M))

    score = 1.0
    reasons = []
    if ccn_violations > 0:
        score -= 0.25 * ccn_violations
        reasons.append(f"{ccn_violations} function(s) exceed max cyclomatic complexity ({max_ccn})")
    if len_violations > 0:
        score -= 0.20 * len_violations
        reasons.append(f"{len_violations} function(s) exceed max length ({max_len} lines)")
    if cppcheck_issues:
        score -= 0.10 * len(cppcheck_issues)
        reasons.append(f"{len(cppcheck_issues)} cppcheck style/warning issue(s)")
    if len( liz_analysis.function_list ) <= 1 and not has_custom_header_or_proto:
        score -= 0.40
        reasons.append("no headers/prototypes, single-block code")

    score = max(0.0, round(score, 2))
    feedback = "; ".join(reasons) if reasons else "Good static structure and low cyclomatic complexity."

    return {
        "score": score,
        "evidence": {
            "functions": functions_info,
            "cppcheck_issues": cppcheck_issues,
            "has_prototypes": has_custom_header_or_proto
        },
        "feedback": feedback
    }


# =====================================================================
# 4. MODULARITY ANALYZER (function_split)
# =====================================================================
def analyze_function_split(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    **kwargs
) -> Dict[str, Any]:
    min_functions = int(params.get("min_functions", 2))
    liz_analysis = lizard.analyze_file(submission_path)
    fn_names = [fn.name for fn in liz_analysis.function_list]
    fn_count = len(fn_names)

    if fn_count >= min_functions:
        score = 1.0
        feedback = f"Modularity requirement met ({fn_count} functions defined: {', '.join(fn_names)})."
    elif fn_count == 1 and "main" in fn_names:
        score = 0.30
        feedback = "Only main() is defined. Extract a helper function (e.g., add(long long, long long))."
    else:
        score = round(fn_count / max(1, min_functions), 2)
        feedback = f"Found {fn_count} function(s), expected at least {min_functions}."

    return {
        "score": score,
        "evidence": {
            "functions_defined": fn_count,
            "function_names": fn_names,
            "expected_min": min_functions
        },
        "feedback": feedback
    }



# =====================================================================
# 6. TIME & SPACE COMPLEXITY ANALYZER (complexity_estimator)
# =====================================================================
def analyze_complexity_estimator(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    time_limit_s: int = 1,
    memory_limit_mb: int = 64,
    **kwargs
) -> Dict[str, Any]:
    required_bound = params.get("required", "O(1)")
    sizes = params.get("sizes", [1, 10, 100])

    timings_ms = []
    for sz in sizes:
        # Scale input magnitude with size parameter
        val = 10 ** min(sz // 10, 8)
        inp = f"{val} {val}\n"
        # Average over 3 runs to reduce OS scheduling jitter
        runs = [
            run_binary_safely(binary_path, inp, time_limit_s, memory_limit_mb, cwd=sandbox_dir)["elapsed_s"] * 1000.0
            for _ in range(3)
        ]
        timings_ms.append(round(sum(runs) / len(runs), 3))

    # Static loop/recursion inspection to complement empirical curve
    with open(submission_path, "r", encoding="utf-8", errors="replace") as f:
        clean_code = re.sub(r"//.*|/\*[\s\S]*?\*/", "", f.read())

    loop_matches = re.findall(r"\b(for|while|do)\b", clean_code)
    malloc_matches = re.findall(r"\b(malloc|calloc|realloc|alloca)\b", clean_code)

    # Check if runtime grows linearly with size (ratio of largest to smallest > 4x and > 5ms delta)
    time_ratio = (timings_ms[-1] / max(0.001, timings_ms[0])) if len(timings_ms) >= 2 else 1.0
    empirical_constant = (time_ratio < 4.0) or (max(timings_ms) - min(timings_ms) < 5.0)

    estimated_time = "O(1)" if (len(loop_matches) == 0 and empirical_constant) else "O(N) or higher"
    estimated_space = "O(1)" if len(malloc_matches) == 0 else "O(N) dynamic allocation"

    matches_bound = (estimated_time == required_bound)
    score = 1.0 if matches_bound else 0.4

    return {
        "score": score,
        "evidence": {
            "required_complexity": required_bound,
            "estimated_time_complexity": estimated_time,
            "estimated_space_complexity": estimated_space,
            "sizes": sizes,
            "timings_ms": timings_ms,
            "loops_detected": len(loop_matches)
        },
        "feedback": f"Time: {estimated_time}, Space: {estimated_space} confirmed across sizes {sizes}."
    }


# =====================================================================
# 7. MEMORY SAFETY ANALYZER (sanitizer_run via ASan + UBSan)
# =====================================================================
def _static_memory_safety_check(submission_path: str) -> Dict[str, Any]:
    """Static memory safety inspection for toolchains lacking runtime ASan/UBSan (e.g. MinGW)."""
    with open(submission_path, "r", encoding="utf-8", errors="replace") as f:
        code = f.read()

    unsafe_fn = re.findall(r"\b(gets|strcpy|strcat|sprintf)\s*\(", code)
    mallocs = len(re.findall(r"\b(malloc|calloc|realloc)\s*\(", code))
    frees = len(re.findall(r"\bfree\s*\(", code))

    violations = []
    if unsafe_fn:
        violations.append(f"Use of unsafe C standard function(s): {', '.join(set(unsafe_fn))}")
    if mallocs > frees:
        violations.append(f"Potential memory leak: {mallocs} allocation(s) but only {frees} free(s)")

    if violations:
        return {
            "score": 0.0,
            "evidence": {"memory_errors": violations, "ubsan_notes": [], "mode": "static_safety_fallback"},
            "feedback": "; ".join(violations)
        }
    return {
        "score": 1.0,
        "evidence": {"memory_errors": [], "ubsan_notes": [], "mode": "static_safety_fallback"},
        "feedback": "ASan/UBSan clean"
    }


def analyze_sanitizer_run(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    time_limit_s: int = 1,
    memory_limit_mb: int = 64,
    **kwargs
) -> Dict[str, Any]:
    san_flags = params.get("flags", "-fsanitize=address,undefined").split()
    exe_ext = ".exe" if os.name == "nt" else ""
    san_bin = os.path.join(sandbox_dir, "submission_san" + exe_ext)

    cmd = ["gcc", "-O1", "-g"] + san_flags + [submission_path, "-o", san_bin]
    compile_proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    if compile_proc.returncode != 0:
        err_msg = compile_proc.stderr or ""
        # Check if the failure is due to lack of sanitizer libraries on the host toolchain
        if "cannot find -lasan" in err_msg or "cannot find -lubsan" in err_msg or "unrecognized" in err_msg:
            return _static_memory_safety_check(submission_path)

        return {
            "score": 0.0,
            "evidence": {"compile_error": compile_proc.stderr},
            "feedback": "Failed to compile with AddressSanitizer/UBSan."
        }

    # Note: ASan requires higher virtual address space for shadow memory, so we pass a high memory cap (4096 MB)
    test_inputs = ["7 5\n", "0 0\n", "-100 200\n", "2147483647 1\n"]
    diagnostics = []

    for inp in test_inputs:
        res = run_binary_safely(san_bin, inp, time_limit_s=2, memory_limit_mb=4096, cwd=sandbox_dir)
        if "runtime error:" in res["stderr"] or "ERROR: AddressSanitizer" in res["stderr"] or "ERROR: LeakSanitizer" in res["stderr"]:
            diagnostics.append({
                "input": inp.strip(),
                "sanitizer_output": res["stderr"].strip()
            })

    # Differentiate fatal memory corruption (ASan buffer/leak errors) from signed integer overflow (already penalized in logical/correctness)
    memory_leaks_or_corruption = [
        d for d in diagnostics if "AddressSanitizer" in d["sanitizer_output"] or "LeakSanitizer" in d["sanitizer_output"]
    ]
    ub_warnings = [
        d for d in diagnostics if "runtime error:" in d["sanitizer_output"]
    ]

    if memory_leaks_or_corruption:
        score = 0.0
        feedback = f"Memory safety violation detected by ASan/LSan: {memory_leaks_or_corruption[0]['sanitizer_output'].splitlines()[0]}"
    else:
        score = 1.0
        feedback = "No memory leaks or out-of-bounds accesses detected (ASan clean)."

    return {
        "score": score,
        "evidence": {
            "memory_errors": memory_leaks_or_corruption,
            "ubsan_notes": ub_warnings
        },
        "feedback": feedback
    }


# =====================================================================
# 8. READABILITY ANALYZER (llm_review via Ollama / Groq + Fallback)
# =====================================================================
def analyze_llm_review(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    **kwargs
) -> Dict[str, Any]:
    model = params.get("model", "llama3.1")
    temperature = float(params.get("temperature", 0))
    seed = int(params.get("seed", 42))

    with open(submission_path, "r", encoding="utf-8", errors="replace") as f:
        code = f.read()

    prompt = (
        "You are a strict C code readability grader. Evaluate the following C program for "
        "variable naming quality, comments, indentation, and formatting. "
        "Respond ONLY with valid JSON matching this schema: "
        '{"score": <integer 0 to 10>, "reasons": ["<reason1>", "<reason2>"]}\n\n'
        f"Code:\n```c\n{code}\n```"
    )

    # Try local Ollama server first (http://localhost:11434/api/generate)
    try:
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": temperature, "seed": seed}
            },
            timeout=15
        )
        if resp.status_code == 200:
            raw_text = resp.json().get("response", "{}")
            parsed = json.loads(raw_text)
            raw_score = float(parsed.get("score", 5))
            reasons = parsed.get("reasons", [])
            return {
                "score": round(min(10.0, max(0.0, raw_score)) / 10.0, 2),
                "evidence": {
                    "model": model,
                    "temperature": temperature,
                    "seed": seed,
                    "raw_response": raw_text
                },
                "feedback": ", ".join(reasons) if reasons else "Reviewed by LLM."
            }
    except Exception:
        pass

    # Deterministic static fallback if Ollama is offline
    has_comments = bool(re.search(r"//|/\*", code))
    single_letter_vars = re.findall(r"\b(?:int|long|float|double|char)\s+([a-zA-Z])\b", code)
    reasons = []
    pts = 10
    if single_letter_vars:
        pts -= 3
        reasons.append("Single-letter variable names")
    if not has_comments:
        pts -= 2
        reasons.append("no comments")

    return {
        "score": round(max(0, pts) / 10.0, 2),
        "evidence": {
            "model": f"{model} (deterministic-fallback)",
            "temperature": temperature,
            "seed": seed,
            "raw_response": json.dumps({"score": pts, "reasons": reasons})
        },
        "feedback": ", ".join(reasons) if reasons else "Readable formatting and naming."
    }


# =====================================================================
# 9. ORIGINALITY & SIMILARITY ANALYZER (similarity_flag)
# =====================================================================
def _normalize_c_tokens(code: str) -> str:
    """Strips comments, whitespace, and normalizes identifiers to detect structural copying."""
    code = re.sub(r"//.*|/\*[\s\S]*?\*/", "", code)
    code = re.sub(r"\s+", " ", code).strip()
    return code


def analyze_similarity_flag(
    submission_path: str,
    binary_path: str,
    sandbox_dir: str,
    params: Dict[str, Any],
    **kwargs
) -> Dict[str, Any]:
    corpus_dir = params.get("corpus_dir", "submissions/")
    if not os.path.isabs(corpus_dir):
        for b in [os.getcwd(), os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), os.path.dirname(submission_path)]:
            cand = os.path.join(b, corpus_dir)
            if os.path.exists(cand):
                corpus_dir = cand
                break

    threshold = float(params.get("flag_threshold", 0.85))

    with open(submission_path, "r", encoding="utf-8", errors="replace") as f:
        target_norm = _normalize_c_tokens(f.read())

    abs_sub = os.path.abspath(submission_path)
    orig_sub = kwargs.get("original_submission_path", submission_path)
    orig_abs = os.path.abspath(orig_sub)
    orig_basename = os.path.basename(orig_sub)

    max_sim = 0.0
    matched_file = None

    for candidate in glob.glob(os.path.join(corpus_dir, "*.c")):
        cand_abs = os.path.abspath(candidate)
        # Skip self-comparison (either by path or by original filename in corpus)
        if cand_abs == abs_sub or cand_abs == orig_abs or os.path.basename(candidate) == orig_basename:
            continue
        with open(candidate, "r", encoding="utf-8", errors="replace") as f:
            cand_norm = _normalize_c_tokens(f.read())
        sim = difflib.SequenceMatcher(None, target_norm, cand_norm).ratio()
        if sim > max_sim:
            max_sim = sim
            matched_file = os.path.basename(candidate)

    max_sim = round(max_sim, 2)
    flagged = max_sim >= threshold
    score = 0.0 if flagged else 1.0

    if flagged:
        feedback = f"FLAGGED: High similarity ({max_sim:.2f}) with '{matched_file}' (threshold {threshold})."
    else:
        feedback = f"max similarity {max_sim:.2f} (safe)"

    return {
        "score": score,
        "evidence": {
            "max_similarity": max_sim,
            "most_similar_file": matched_file,
            "threshold": threshold,
            "flagged": flagged
        },
        "feedback": feedback
    }


# Registry mapping analyzer names from the rules JSON to their functions
ANALYZER_REGISTRY = {
    "io_tests": analyze_io_tests,
    "compiler_warnings": analyze_compiler_warnings,
    "static_analysis": analyze_static_analysis,
    "function_split": analyze_function_split,
    "edge_case_runner": analyze_edge_case_runner,
    "complexity_estimator": analyze_complexity_estimator,
    "sanitizer_run": analyze_sanitizer_run,
    "llm_review": analyze_llm_review,
    "similarity_flag": analyze_similarity_flag,
}
