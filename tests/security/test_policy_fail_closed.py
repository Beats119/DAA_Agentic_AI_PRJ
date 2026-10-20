"""Fail-closed policy behavior: unknown mandates and broken setups refuse.

- A policy mandating a capability the sandbox does not know makes
  ``ensure_available`` raise — never a silent weaker run.
- A run whose launcher cannot install the mandated seccomp profile is
  refused with :class:`SandboxError`, and the target binary NEVER
  executes (proven by the marker file it would have written).
- A policy listing a nonexistent ``fs_read_paths`` entry is tolerated:
  missing optional directories are skipped, the run still works.
"""

import pytest

from autograder.app.sandbox import (
    SandboxError,
    SandboxExecutor,
    SandboxUnavailable,
)
from autograder.app.sandbox.policy import SandboxPolicy

MARKER_SRC = r"""
#include <stdio.h>
int main(int argc, char **argv) {
    FILE *f = fopen(argv[1], "w");
    fputs("EXECUTED", f);
    fclose(f);
    printf("EXECUTED\n");
    return 0;
}
"""


def test_bogus_mandatory_capability_refused(tmp_path):
    policy = SandboxPolicy(mandatory=["resource_limits", "teleportation"])
    executor = SandboxExecutor(base_dir=tmp_path / "sb", policy=policy)
    with pytest.raises(SandboxUnavailable):
        executor.ensure_available()


def test_bad_seccomp_profile_refuses_and_never_executes(executor, c_binary, tmp_path):
    marker = tmp_path / "marker.txt"
    binary = c_binary("ag_marker", MARKER_SRC)
    with pytest.raises(SandboxError):
        executor.run([binary, str(marker)], "", seccomp_profile="nonexistent-profile")
    assert not marker.exists(), "target executed despite the refused run"


def test_launcher_level_seccomp_failure_never_executes(tmp_path, c_binary):
    """Drive the REAL launcher directly with a spec naming a seccomp
    profile that does not exist: the launcher must report
    ``failed_step == "seccomp_filter"``, exit 125, and never exec the
    target (which would have written a marker file — its directory IS
    Landlock-granted in the spec, so a run would have succeeded)."""
    import json
    import os
    import shutil
    import subprocess
    import sys

    from autograder.app.sandbox.landlock import FS_READ_EXEC, fs_all_rights

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if os.geteuid() == 0:
        os.chown(run_dir, 65534, 65534)  # writable by the sandbox identity
    binary = c_binary("ag_marker2", MARKER_SRC)
    staged = run_dir / "prog"
    shutil.copyfile(binary, staged)
    staged.chmod(0o755)
    marker = run_dir / "marker2.txt"

    spec = {
        "argv": ["./prog", str(marker)],
        "cwd": str(run_dir),
        "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        "limits": {
            "cpu_time_s": 10, "memory_mb": 256, "fsize_mb": 16,
            "stack_mb": 8, "nproc": 32, "unlimited_address_space": False,
        },
        "policy": {
            "mandatory": ["resource_limits", "landlock_fs", "network_namespace",
                          "seccomp_filter", "no_new_privs"],
            "sandbox_uid": 65534, "sandbox_gid": 65534,
            "drop_privileges_when_root": True,
            "seccomp_profile": "nonexistent-profile",
        },
        "landlock_rules": [
            [str(run_dir), fs_all_rights()],
            ["/usr/lib", FS_READ_EXEC],
            ["/lib", FS_READ_EXEC],
            ["/lib64", FS_READ_EXEC],
            ["/dev/null", 6],
        ],
        "status_fd": 3,
        "want_netns": True,
        "want_landlock": True,
    }
    spec_path = tmp_path / "spec.json"
    status_r, status_w = os.pipe()
    spec["status_fd"] = status_w  # pass_fds preserves the fd number
    spec_path.write_text(json.dumps(spec))
    proc = subprocess.Popen(
        [sys.executable, "-m", "autograder.app.sandbox.launcher", str(spec_path)],
        cwd=str(run_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(status_w,),
    )
    os.close(status_w)
    with os.fdopen(status_r) as fh:
        status = json.loads(fh.readline())
    proc.wait(timeout=30)
    assert status["ok"] is False
    assert status["failed_step"] == "seccomp_filter"
    assert proc.returncode == 125
    assert not marker.exists(), "target executed despite the launcher failure"


def test_missing_fs_read_path_skipped_gracefully(tmp_path):
    policy = SandboxPolicy(fs_read_paths=["/definitely/not/a/real/dir", "/usr/lib"])
    executor = SandboxExecutor(base_dir=tmp_path / "sb", policy=policy)
    result = executor.run(["/bin/cat"], "still works", None)
    assert result.stdout == "still works"
    assert result.enforcement.landlock_fs is True
