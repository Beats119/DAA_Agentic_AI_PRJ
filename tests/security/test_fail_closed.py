"""Fail closed: degraded sandbox refuses to run, and nothing executes."""

import os

import pytest

from autograder.app.sandbox import SandboxUnavailable


def _recording_binary(tmp_path: "object") -> str:
    # A program that would leave a mark if it ever ran.
    from pathlib import Path

    marker = Path(str(tmp_path)) / "i_ran"
    src = Path(str(tmp_path)) / "mark.c"
    src.write_text(
        '#include <stdio.h>\nint main(){'
        f'FILE*f=fopen("{marker}", "w"); if(f){{fputs("x",f); fclose(f);}}'
        "return 0;}\n"
    )
    return str(src)


def test_no_resource_limits_means_no_execution(executor, tmp_path, monkeypatch):
    binary_src = _recording_binary(tmp_path)
    before = set(os.listdir(executor.base_dir))

    monkeypatch.setattr(executor, "_caps",
                        executor.capabilities.model_copy(update={"resource_limits": False}))
    with pytest.raises(SandboxUnavailable):
        executor.run([binary_src], "")

    # Nothing executed: no new run dirs, and the marker was never written.
    assert set(os.listdir(executor.base_dir)) == before
    assert not (tmp_path / "i_ran").exists()


def test_no_process_group_kill_means_no_execution(executor, tmp_path, monkeypatch):
    binary_src = _recording_binary(tmp_path)

    with monkeypatch.context() as m:
        m.delattr(os, "killpg", raising=False)
        with pytest.raises(SandboxUnavailable):
            executor.run([binary_src], "")
    assert hasattr(os, "killpg")  # context manager restores on exit
    assert not (tmp_path / "i_ran").exists()
