"""Memory-safety analyzer: ASan/UBSan runs against fixtures."""

import pytest

from conftest import make_analyzer_ctx, trusted_case

from autograder.app.analyzers.memory_safety import analyzer as memsafe
from autograder.app.analyzers.memory_safety.analyzer import _parse_sanitizer_output


def _ctx(sandbox_executor, tmp_path, source_name, cases, **kw):
    return make_analyzer_ctx(
        sandbox_executor, tmp_path, "memory_safety", source_name,
        test_cases=cases, **kw,
    )


def test_parse_asan_output():
    stderr = (
        "==123==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1234\n"
        "    #0 0x55aa in main /tmp/x.c:12\n"
    )
    findings = _parse_sanitizer_output(stderr)
    assert len(findings) == 1
    assert findings[0]["sanitizer"] == "AddressSanitizer"
    assert "heap-buffer-overflow" in findings[0]["check"]
    assert findings[0]["stack_hint"] == "main"


def test_parse_ubsan_output():
    stderr = "x.c:5:3: runtime error: index 5 out of bounds for type 'int [4]'\n"
    findings = _parse_sanitizer_output(stderr)
    assert len(findings) == 1
    assert findings[0]["sanitizer"] == "UBSan"


def test_clean_program_scores_full(sandbox_executor, tmp_path):
    cases = [trusted_case("t1", "2 3", "5")]
    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c", cases)
    result = memsafe.analyze(ctx)
    assert result.status in ("completed", "unsupported")
    if result.status == "completed":
        assert result.score == 1.0
        assert result.measurements["finding_count"] == 0
        # D12: the result must disclose BOTH sanitizer trade-offs —
        # unlimited address space and detect_leaks=0 (leaks unchecked).
        lims = "\n".join(result.limitations)
        assert "address-space" in lims and "RLIMIT_AS" in lims
        assert "detect_leaks=0" in lims and "leaks are NOT checked" in lims
    else:
        pytest.skip(f"sanitizer unavailable: {result.diagnostics}")


def test_student_build_failure_is_failed_not_unsupported(sandbox_executor, tmp_path):
    # D12: a submission that does not compile under the sanitizer build is
    # the student's failure — it must not be mislabeled `unsupported`
    # (which claims a host/toolchain limitation).
    from conftest import make_analyzer_ctx

    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "memory_safety", "broken.c",
        source_text="int main(void) { this is not C at all; }\n",
        test_cases=[trusted_case("t1", "1", "1")],
    )
    result = memsafe.analyze(ctx)
    assert result.status == "failed"
    assert result.score is None
    assert "does not compile" in result.diagnostics["reason"]


def test_overflow_detected(sandbox_executor, tmp_path):
    cases = [trusted_case("t1", "10", "0")]
    ctx = _ctx(sandbox_executor, tmp_path, "overflow.c", cases)
    result = memsafe.analyze(ctx)
    if result.status == "unsupported":
        pytest.skip(f"sanitizer unavailable: {result.diagnostics}")
    assert result.status == "completed"
    assert result.score < 1.0
    assert result.measurements["finding_count"] >= 1
    text = "\n".join(result.findings)
    assert "heap-buffer-overflow" in text or "AddressSanitizer" in text


def test_no_trusted_tests_is_inconclusive(sandbox_executor, tmp_path):
    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c", [])
    result = memsafe.analyze(ctx)
    assert result.status in ("inconclusive", "unsupported")
    assert result.score is None
