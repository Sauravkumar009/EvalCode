import os
import re
import sys
import json
import shutil
import signal
import hashlib
import tempfile
import subprocess
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional

# resource is Unix/Linux specific (works on Linux, macOS, and Windows WSL)
try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    HAS_RESOURCE = False


# Suppress crash dialog popups on Windows so segfaults return immediately without freezing
if os.name == "nt":
    try:
        import ctypes
        # SEM_FAILCRITICALERRORS (1) | SEM_NOGPFAULTERRORBOX (2) | SEM_NOOPENFILEERRORBOX (0x8000)
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass

# Categorization of analyzers for intelligent routing[cite: 5]
EXECUTION_ANALYZERS = {
    "io_tests",
    "edge_case_runner",
    "complexity_estimator",
    "sanitizer_run"
}

SOURCE_ONLY_ANALYZERS = {
    "compiler_warnings",
    "static_analysis",
    "function_split",
    "llm_review",
    "similarity_flag"
}


@dataclass
class FirstPassResult:
    compiled: bool
    compile_warnings: List[str]
    compile_errors: str
    static_sanity_ok: bool
    static_sanity_details: Dict[str, bool]
    smoke_ok: bool
    suspected_infinite_loop: bool
    crashed: bool
    return_code: Optional[int]
    smoke_stdout: str
    smoke_stderr: str


class AutoGraderCoordinator:
    def __init__(self, rules_path: str, submission_path: str):
        self.rules_path = rules_path
        self.submission_path = submission_path
        self.rules: Dict[str, Any] = {}
        self.rules_hash: str = ""
        self.sandbox_dir: str = tempfile.mkdtemp(prefix="autograder_sandbox_")
        self.exe_suffix: str = ".exe" if os.name == "nt" else ""
        self.binary_path: str = os.path.join(self.sandbox_dir, "submission_bin" + self.exe_suffix)
        self.original_submission_path: str = os.path.abspath(submission_path)
        self.sandboxed_src_path: str = os.path.join(
            self.sandbox_dir, os.path.basename(submission_path)
        )

    def load_and_validate_rules(self) -> None:
        """Step 1: Read external rules file, compute SHA-256 hash, and check weights[cite: 1, 2, 3, 6]."""
        if not os.path.exists(self.rules_path):
            raise FileNotFoundError(f"Rules file not found: {self.rules_path}")

        with open(self.rules_path, "rb") as f:
            raw_bytes = f.read()
            self.rules_hash = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
            self.rules = json.loads(raw_bytes.decode("utf-8"))

        # Verify weights sum to 1.00[cite: 3]
        metrics = self.rules.get("metrics", [])
        total_weight = sum(float(m.get("weight", 0.0)) for m in metrics)
        if abs(total_weight - 1.0) > 1e-6:
            raise ValueError(f"Metric weights must sum to 1.00, got {total_weight:.4f}")

        # Copy student submission into isolated sandbox directory
        if not os.path.exists(self.submission_path):
            raise FileNotFoundError(f"Submission not found: {self.submission_path}")
        shutil.copy2(self.submission_path, self.sandboxed_src_path)

    @staticmethod
    def _strip_c_comments_and_strings(code: str) -> str:
        """Removes comments and string literals so regex checks don't trigger on comments."""
        # Remove multi-line and single-line comments
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
        code = re.sub(r"//.*", "", code)
        # Replace string literals with empty quotes
        code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
        return code

    def run_static_sanity(self) -> Dict[str, bool]:
        """
        Step 2: Confirm main() is present, at least one scanf/reader is called,
        at least one printf/writer is called, and main contains a return statement[cite: 5].
        """
        with open(self.sandboxed_src_path, "r", encoding="utf-8", errors="replace") as f:
            raw_code = f.read()

        clean_code = self._strip_c_comments_and_strings(raw_code)

        has_main = bool(re.search(r"\bmain\s*\(", clean_code))
        has_reader = bool(
            re.search(r"\b(scanf|fscanf|sscanf|fgets|gets|getchar|getc|fgetc|read)\s*\(", clean_code)
        )
        has_writer = bool(
            re.search(r"\b(printf|fprintf|sprintf|snprintf|puts|fputs|putchar|putc|fputc|write)\s*\(", clean_code)
        )
        has_return = bool(re.search(r"\breturn\b[^;]*;", clean_code))

        return {
            "has_main": has_main,
            "has_reader": has_reader,
            "has_writer": has_writer,
            "has_return_in_main": has_main and has_return
        }

    def run_compile_check(self) -> tuple[bool, List[str], str]:
        """
        Step 3: Run gcc -O2 -Wall -Wextra -std=c11 in the sandbox[cite: 5].
        Returns (compiled_ok, warnings_list, error_output).
        """
        cmd = [
            "gcc",
            "-O2",
            "-Wall",
            "-Wextra",
            "-std=c11",
            self.sandboxed_src_path,
            "-o",
            self.binary_path,
            "-lm"
        ]

        proc = subprocess.run(
            cmd,
            cwd=self.sandbox_dir,
            capture_output=True,
            text=True,
            timeout=10
        )

        if os.name == "nt" and not os.path.exists(self.binary_path) and os.path.exists(self.binary_path + ".exe"):
            self.binary_path = self.binary_path + ".exe"

        compiled_ok = (proc.returncode == 0) and os.path.exists(self.binary_path)
        stderr_lines = proc.stderr.strip().splitlines() if proc.stderr else []
        warnings = [line for line in stderr_lines if "warning:" in line]
        errors = proc.stderr if not compiled_ok else ""

        return compiled_ok, warnings, errors

    def _set_sandbox_limits(self, memory_limit_mb: int, cpu_limit_s: int):
        """Pre-exec hook for Linux subprocess to enforce memory and process limits[cite: 1, 3, 5]."""
        def _limits():
            if HAS_RESOURCE:
                mem_bytes = memory_limit_mb * 1024 * 1024
                # Cap virtual address space (64 MB)[cite: 3, 5]
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
                # Cap CPU time slightly above wall-clock timeout
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit_s + 1, cpu_limit_s + 1))
                # Disable core dumps to keep sandbox clean on crashes
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # Put child in its own process group so all children can be killed on timeout
            if os.name == "posix":
                os.setsid()
        return _limits

    def run_smoke_test(self) -> tuple[bool, bool, bool, Optional[int], str, str]:
        """
        Step 4: Feed the compiled binary the sample input under fixed wall-clock
        timeout (1s) and memory cap (64 MB)[cite: 3, 5].
        Returns (smoke_ok, suspected_infinite_loop, crashed, return_code, stdout, stderr).
        """
        time_limit_s = int(self.rules.get("time_limit_s", 1))
        memory_limit_mb = int(self.rules.get("memory_limit_mb", 64))
        sample_input = self.rules.get("sample_input", "7 5\n")

        preexec = self._set_sandbox_limits(memory_limit_mb, time_limit_s) if os.name == "posix" else None

        proc = subprocess.Popen(
            [self.binary_path],
            cwd=self.sandbox_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=preexec
        )

        try:
            stdout, stderr = proc.communicate(input=sample_input, timeout=time_limit_s)
            return_code = proc.returncode

            # Negative return codes in Python subprocess indicate termination by signal (e.g., SIGSEGV, SIGABRT)
            crashed = return_code != 0
            smoke_ok = (return_code == 0)
            return smoke_ok, False, crashed, return_code, stdout, stderr

        except subprocess.TimeoutExpired:
            # Kill the entire process group if the program never halts
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
            stdout, stderr = proc.communicate()
            return False, True, False, None, stdout or "", stderr or "Execution timed out"

    def run_first_pass(self) -> FirstPassResult:
        """Executes the complete First Pass sanity sweep[cite: 5]."""
        self.load_and_validate_rules()

        # 1. Static sanity check[cite: 5]
        sanity_details = self.run_static_sanity()
        static_sanity_ok = all(sanity_details.values())

        # 2. Compile check[cite: 5]
        compiled, warnings, compile_errors = self.run_compile_check()
        if not compiled:
            return FirstPassResult(
                compiled=False,
                compile_warnings=warnings,
                compile_errors=compile_errors,
                static_sanity_ok=static_sanity_ok,
                static_sanity_details=sanity_details,
                smoke_ok=False,
                suspected_infinite_loop=False,
                crashed=False,
                return_code=None,
                smoke_stdout="",
                smoke_stderr=compile_errors
            )

        # 3. Smoke run[cite: 5]
        smoke_ok, inf_loop, crashed, ret_code, stdout, stderr = self.run_smoke_test()

        return FirstPassResult(
            compiled=True,
            compile_warnings=warnings,
            compile_errors="",
            static_sanity_ok=static_sanity_ok,
            static_sanity_details=sanity_details,
            smoke_ok=smoke_ok,
            suspected_infinite_loop=inf_loop,
            crashed=crashed,
            return_code=ret_code,
            smoke_stdout=stdout,
            smoke_stderr=stderr
        )

    def plan_analyzer_dispatch(self, first_pass: FirstPassResult) -> Dict[str, List[Dict[str, Any]]]:
        """
        Determines which analyzers to run and which to skip based on first-pass results[cite: 5].
        """
        scheduled = []
        skipped = []

        for metric in self.rules.get("metrics", []):
            analyzer_name = metric.get("analyzer")

            # If compilation failed, only source-only analyzers can run
            if not first_pass.compiled and analyzer_name in EXECUTION_ANALYZERS:
                skipped.append({**metric, "skip_reason": "Compilation failed"})
                continue

            # If smoke run timed out, skip execution-based analyzers[cite: 5]
            if first_pass.suspected_infinite_loop and analyzer_name in EXECUTION_ANALYZERS:
                skipped.append({**metric, "skip_reason": "suspected_infinite_loop in smoke run"})
                continue

            scheduled.append(metric)

        return {"scheduled": scheduled, "skipped": skipped}

    def cleanup(self) -> None:
        """Removes the temporary sandbox directory."""
        if os.path.exists(self.sandbox_dir):
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)


if __name__ == "__main__":
    rules_file = sys.argv[1] if len(sys.argv) > 1 else "rules/add_rules.json"
    submission_file = sys.argv[2] if len(sys.argv) > 2 else "submissions/add.c"

    coordinator = AutoGraderCoordinator(rules_file, submission_file)
    try:
        first_pass_res = coordinator.run_first_pass()
        dispatch_plan = coordinator.plan_analyzer_dispatch(first_pass_res)

        flags = []
        if first_pass_res.suspected_infinite_loop:
            flags.append("suspected_infinite_loop")
        if first_pass_res.crashed:
            flags.append(f"smoke_run_crashed_rc_{first_pass_res.return_code}")
        if not first_pass_res.static_sanity_ok:
            flags.append("failed_static_sanity")

        # Partial Phase 1 output matching the assignment's JSON schema[cite: 6, 7]
        phase1_report = {
            "submission": os.path.basename(submission_file),
            "rules_hash": coordinator.rules_hash,
            "first_pass": {
                "compiled": first_pass_res.compiled,
                "smoke_ok": first_pass_res.smoke_ok,
                "suspected_infinite_loop": first_pass_res.suspected_infinite_loop,
                "static_sanity_ok": first_pass_res.static_sanity_ok,
                "static_sanity_details": first_pass_res.static_sanity_details,
                "compile_warnings": first_pass_res.compile_warnings,
                "smoke_stdout": first_pass_res.smoke_stdout.strip()
            },
            "flags": flags,
            "dispatch_plan": {
                "analyzers_to_run": [m["name"] for m in dispatch_plan["scheduled"]],
                "analyzers_skipped": [
                    {"name": m["name"], "reason": m["skip_reason"]}
                    for m in dispatch_plan["skipped"]
                ]
            }
        }

        print(json.dumps(phase1_report, indent=2))
    finally:
        coordinator.cleanup()
