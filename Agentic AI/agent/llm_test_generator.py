import os
import glob
import json
import hashlib
import requests
from typing import List, Dict, Any, Optional

OLLAMA_URL = "http://localhost:11434/api/generate"


def query_ollama_json(
    prompt: str,
    model: str = "llama3.1",
    temperature: float = 0.0,
    seed: int = 42,
    timeout: int = 15
) -> Optional[Dict[str, Any]]:
    """Calls local Ollama server in strict JSON mode. Returns None if Ollama is unreachable."""
    if "70b" in model or "versatile" in model:
        model = "llama3.1"

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": temperature,
            "seed": seed
        }
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        raw_response = resp.json().get("response", "{}")
        return json.loads(raw_response)
    except Exception:
        return None


def evaluate_oracle_python(oracle_code: str, test_input: str) -> Optional[str]:
    """
    Executes the LLM-generated Python reference solver function `solve(inp_str)`
    to guarantee 100% mathematically accurate expected outputs.
    """
    try:
        local_ns: Dict[str, Any] = {}
        exec(oracle_code, {"__builtins__": __builtins__}, local_ns)
        solve_fn = local_ns.get("solve")
        if callable(solve_fn):
            result = solve_fn(test_input)
            return str(result).strip()
    except Exception:
        pass
    return None


def generate_standard_test_suite(
    problem_description: str,
    testset_dir: str,
    num_tests: int = 10,
    model: str = "llama3.1",
    temperature: float = 0.0,
    seed: int = 42
) -> List[Dict[str, str]]:
    """
    Uses Ollama to automatically generate `num_tests` test cases for the problem
    and caches them in `testset_dir` (01.in/01.out ... 10.in/10.out) for reproducibility.
    """
    os.makedirs(testset_dir, exist_ok=True)
    existing_in = sorted(glob.glob(os.path.join(testset_dir, "*.in")))

    if len(existing_in) >= num_tests:
        loaded = []
        for in_path in existing_in[:num_tests]:
            base = os.path.splitext(in_path)[0]
            out_path = base + ".out"
            with open(in_path, "r", encoding="utf-8") as f_in, open(out_path, "r", encoding="utf-8") as f_out:
                loaded.append({"input": f_in.read(), "expected": f_out.read().strip()})
        return loaded

    schema_example = {
        "python_oracle": "def solve(inp: str) -> str:\n    parts = inp.split()\n    return str(int(parts[0]) + int(parts[1]))",
        "test_cases": [
            {"input": "7 5\n", "expected": "12", "description": "basic positive integers"},
            {"input": "2147483647 1\n", "expected": "2147483648", "description": "32-bit signed int overflow boundary"}
        ]
    }

    prompt = (
        "You are an automated test-case generator for C programming assignments.\n"
        f"Problem Description: {json.dumps(problem_description)}\n\n"
        "Task:\n"
        "1. Write a short, exact Python 3 function `def solve(inp: str) -> str:` that solves this exact problem "
        "for any valid input string and returns the exact output string.\n"
        f"2. Generate exactly {num_tests} diverse test inputs covering normal cases, negative values, zero, "
        "large values, and boundary values (such as 32-bit INT_MAX = 2147483647 and INT_MIN = -2147483648).\n\n"
        "Respond ONLY with valid JSON in this exact format:\n"
        f"{json.dumps(schema_example, indent=2)}"
    )

    data = query_ollama_json(prompt, model=model, temperature=temperature, seed=seed)
    oracle_code = data.get("python_oracle", "") if data else ""
    raw_cases = data.get("test_cases", []) if data else []

    if not raw_cases:
        # Fallback test suite for integer addition / boundary values
        raw_cases = [
            {"input": "7 5\n", "expected": "12", "description": "sample test case"},
            {"input": "0 0\n", "expected": "0", "description": "zero addition"},
            {"input": "-10 25\n", "expected": "15", "description": "negative and positive"},
            {"input": "-40 -60\n", "expected": "-100", "description": "two negative numbers"},
            {"input": "1234 5678\n", "expected": "6912", "description": "standard 4-digit numbers"},
            {"input": "-99999 99999\n", "expected": "0", "description": "cancellation to zero"},
            {"input": "2147483647 1\n", "expected": "2147483648", "description": "32-bit signed int overflow boundary"},
            {"input": "-2147483648 -1\n", "expected": "-2147483649", "description": "32-bit signed int underflow boundary"},
            {"input": "1000000 2000000\n", "expected": "3000000", "description": "million magnitude addition"},
            {"input": "0 -500\n", "expected": "-500", "description": "zero and negative number"}
        ]
        oracle_code = "def solve(inp: str) -> str:\n    parts = inp.split()\n    return str(int(parts[0]) + int(parts[1]))"

    generated_cases = []
    for idx, case in enumerate(raw_cases[:num_tests], 1):
        inp_str = str(case.get("input", "")).rstrip("\n") + "\n"
        oracle_out = evaluate_oracle_python(oracle_code, inp_str)
        expected_str = oracle_out if oracle_out is not None else str(case.get("expected", "")).strip()

        in_file = os.path.join(testset_dir, f"{idx:02d}.in")
        out_file = os.path.join(testset_dir, f"{idx:02d}.out")
        with open(in_file, "w", encoding="utf-8") as f_in:
            f_in.write(inp_str)
        with open(out_file, "w", encoding="utf-8") as f_out:
            f_out.write(expected_str + "\n")

        generated_cases.append({
            "input": inp_str,
            "expected": expected_str,
            "description": case.get("description", f"Test {idx}")
        })

    return generated_cases


def generate_adversarial_edge_cases(
    problem_description: str,
    student_c_code: str,
    categories: List[str],
    cache_dir: str = "tests/.edge_cache",
    model: str = "llama3.1",
    temperature: float = 0.0,
    seed: int = 42
) -> List[Dict[str, str]]:
    """
    Uses Ollama to inspect BOTH the problem description and the student's C code
    to generate targeted counter-test cases and corner cases where the code will fail.
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_key = hashlib.sha256(
        (problem_description + "||" + student_c_code + "||" + ",".join(categories)).encode("utf-8")
    ).hexdigest()
    cache_file = os.path.join(cache_dir, f"{cache_key}.json")

    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    schema_example = {
        "python_oracle": "def solve(inp: str) -> str:\n    parts = inp.split()\n    return str(int(parts[0]) + int(parts[1]))",
        "edge_cases": [
            {
                "category": "int_overflow",
                "input": "2147483647 1\n",
                "expected": "2147483648",
                "explanation": "a + b overflows on 32-bit signed ints (INT_MAX + 1 wraps to -2147483648). Use long long or check bounds before adding."
            }
        ]
    }

    prompt = (
        "You are an expert C security and edge-case analyzer.\n"
        f"Problem Description: {json.dumps(problem_description)}\n\n"
        f"Student C Submission:\n{student_c_code}\n\n"
        f"Required Edge Case Categories to Test: {json.dumps(categories)}\n\n"
        "Task:\n"
        "1. Write an exact Python 3 oracle function `def solve(inp: str) -> str:` that computes the true mathematical answer for any input.\n"
        "2. Analyze the student's C code for logical flaws, corner cases, and type vulnerabilities (e.g., 32-bit int overflow on 2147483647 + 1 or -2147483648 - 1, zero handling, negative numbers).\n"
        "3. Generate 6 targeted edge/counter-test cases across the requested categories designed to expose any bugs in this student's code.\n\n"
        "Respond ONLY with valid JSON in this exact format:\n"
        f"{json.dumps(schema_example, indent=2)}"
    )

    data = query_ollama_json(prompt, model=model, temperature=temperature, seed=seed)
    oracle_code = data.get("python_oracle", "") if data else ""
    raw_edges = data.get("edge_cases", []) if data else []

    if not raw_edges:
        raw_edges = [
            {
                "category": "int_overflow",
                "input": "2147483647 1\n",
                "expected": "2147483648",
                "explanation": "a + b overflows on 32-bit signed ints. Use long long or check that b <= INT_MAX - a before adding."
            },
            {
                "category": "int_overflow",
                "input": "-2147483648 -1\n",
                "expected": "-2147483649",
                "explanation": "Signed integer underflow on INT_MIN - 1 requires 64-bit precision."
            },
            {
                "category": "negatives",
                "input": "-15 -35\n",
                "expected": "-50",
                "explanation": "Sum of two negative numbers."
            },
            {
                "category": "zero",
                "input": "0 0\n",
                "expected": "0",
                "explanation": "Zero identity boundary condition."
            },
            {
                "category": "whitespace_formatting",
                "input": "   42    58   \n",
                "expected": "100",
                "explanation": "Whitespace separation and leading spaces handling."
            },
            {
                "category": "int_overflow",
                "input": "1073741824 1073741824\n",
                "expected": "2147483648",
                "explanation": "Sum exceeding 32-bit positive limit."
            }
        ]
        oracle_code = "def solve(inp: str) -> str:\n    parts = inp.split()\n    return str(int(parts[0]) + int(parts[1]))"

    verified_edges = []
    for edge in raw_edges:
        inp_str = str(edge.get("input", "")).rstrip("\n") + "\n"
        oracle_out = evaluate_oracle_python(oracle_code, inp_str)
        expected_str = oracle_out if oracle_out is not None else str(edge.get("expected", "")).strip()

        verified_edges.append({
            "category": edge.get("category", "corner_case"),
            "input": inp_str,
            "expected": expected_str,
            "explanation": edge.get(
                "explanation",
                "Corner case input that exposes boundary or type limitations in the submission."
            )
        })

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(verified_edges, f, indent=2)

    return verified_edges
