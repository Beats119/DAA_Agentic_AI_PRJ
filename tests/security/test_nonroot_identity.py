"""Non-root sandbox identity regression (sandbox-2.1 rootless strategy).

The production deployment is NOT root: the app runs as the instructor's
own unprivileged account (e.g. on WSL). In that configuration a bare
``unshare(CLONE_NEWNET)`` fails with EPERM, and — more importantly — the
sandboxed child shares the app user's host uid, so DAC alone cannot
protect the app user's own files: only Landlock + the user namespace
can. This test runs the REAL executor as a non-root identity against a
planted secret in a directory owned by that identity:

- pytest running as root re-executes the probe as uid 65534 via
  ``setpriv`` (failing loudly if setpriv is unavailable — a silent skip
  would hide exactly the regression this test exists to catch). Because
  uid 65534 cannot traverse the developer's home directory on this
  host, the ``autograder`` package and its pydantic dependency are
  staged into a world-readable copy and run with the system python;
  the staged bytes are a copy of the current source tree.
- pytest running unprivileged (CI, WSL) runs the probe directly.

The probe constructs the executor as the app user, calls
``ensure_available()``, compiles and runs a program that tries to read
``$HOME/secret.txt`` and to connect to 127.0.0.1:9. Two outcomes are
secure, and the test asserts the invariant for BOTH:

- ``EXECUTED``: the read was DENIED (Landlock — DAC would have allowed
  it: the secret is owned by, and mode-600 to, the very uid the child
  shares), the connect/socket was denied, ``network_isolated`` is true
  and the strategy is ``rootless-userns``; a hello-world control runs.
- ``REFUSED``: the host forbids unprivileged user namespaces (e.g.
  Ubuntu's AppArmor restriction — the case on THIS VM for uid 65534),
  ``ensure_available`` raised, and nothing executed at all.

In neither case may the secret marker appear in any output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_UID = 65534  # nobody — the non-root identity used when pytest is root
TARGET_GID = 65534

PROBE_SRC = r'''
import json
import os
import sys
from pathlib import Path


def emit(obj):
    print("AGPROBE " + json.dumps(obj), flush=True)


home = Path(os.environ["AG_PROBE_HOME"])
work = Path(os.environ["AG_PROBE_WORK"])

from autograder.app.core.schemas import CompileConfig
from autograder.app.sandbox.compiler import compile_c
from autograder.app.sandbox.executor import (
    ExecLimits,
    SandboxError,
    SandboxExecutor,
)

ATTACK = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
int main(void) {
    const char *home = getenv("HOME");
    char path[4096];
    snprintf(path, sizeof path, "%s/secret.txt", home ? home : "");
    FILE *f = fopen(path, "r");
    if (f) {
        char buf[512];
        size_t n = fread(buf, 1, sizeof buf - 1, f);
        buf[n] = 0;
        fclose(f);
        printf("READ-OK %s\n", buf);
    } else {
        printf("READ-DENIED errno=%d\n", errno);
    }
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) {
        printf("SOCKET-DENIED errno=%d\n", errno);
    } else {
        struct sockaddr_in a;
        memset(&a, 0, sizeof a);
        a.sin_family = AF_INET;
        a.sin_port = htons(9);
        a.sin_addr.s_addr = htonl(0x7f000001);
        int rc = connect(s, (struct sockaddr *)&a, sizeof a);
        printf(rc == 0 ? "CONNECT-OK\n" : "CONNECT-FAILED errno=%d\n", errno);
    }
    return 0;
}
"""

HELLO = '#include <stdio.h>\nint main(void){ printf("hello-nonroot\\n"); return 0; }\n'

out = {"app_uid": os.getuid(), "app_gid": os.getgid()}
try:
    ex = SandboxExecutor(base_dir=work / "sb")
    ex.ensure_available()
    src = work / "attack.c"
    src.write_text(ATTACK)
    cres = compile_c(src, work / "cout", CompileConfig(), ex, allowed_dir=work)
    if not cres.ok:
        out.update(outcome="COMPILE_FAILED", detail=cres.raw_stderr[-800:])
        emit(out)
        sys.exit(0)
    r = ex.run(
        [cres.binary_path],
        "",
        ExecLimits(wall_time_s=10.0, cpu_time_s=15.0),
        extra_env={"HOME": str(home)},
    )
    out.update(
        outcome="EXECUTED",
        stdout=r.stdout,
        stderr=r.stderr[-800:],
        enforcement=r.enforcement.model_dump(),
    )
    hsrc = work / "hello.c"
    hsrc.write_text(HELLO)
    hres = compile_c(hsrc, work / "hout", CompileConfig(), ex, allowed_dir=work)
    if hres.ok:
        hr = ex.run(
            [hres.binary_path], "", ExecLimits(wall_time_s=10.0, cpu_time_s=15.0)
        )
        out["hello_stdout"] = hr.stdout
except SandboxError as exc:
    # SandboxUnavailable is a subclass: the sandbox refused to run
    # anything. That is a secure outcome — nothing executed.
    out.update(outcome="REFUSED", reason=f"{type(exc).__name__}: {exc}")
emit(out)
'''


def _make_traversable(path: Path) -> None:
    """Add o+x (traverse ONLY — never read/write) to path and ancestors."""
    for anc in [path, *path.parents]:
        try:
            st = anc.stat()
        except OSError:
            continue
        if not (st.st_mode & 0o001):
            try:
                os.chmod(anc, st.st_mode | 0o001)
            except OSError:
                pass


def _stage_tree(base: Path) -> Path:
    """World-readable copy of the package + pydantic closure.

    Needed only for the root->65534 mode: the sandbox uid cannot
    traverse the developer's home directory, so neither the venv
    interpreter nor the package under it is reachable. The staged copy
    is made from the CURRENT source tree at test time.
    """
    import pydantic

    stage = base / "stage"
    stage.mkdir()
    shutil.copytree(
        REPO_ROOT / "autograder",
        stage / "autograder",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    site_packages = Path(pydantic.__file__).resolve().parents[1]
    for pkg in ("pydantic", "pydantic_core", "annotated_types", "typing_inspection"):
        shutil.copytree(site_packages / pkg, stage / pkg)
    shutil.copy(site_packages / "typing_extensions.py", stage / "typing_extensions.py")
    for p in sorted(stage.rglob("*")):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    os.chmod(stage, 0o755)
    return stage


def test_nonroot_sandbox_never_leaks_app_user_secret(tmp_path):
    marker = "AG-NONROOT-SECRET-" + uuid.uuid4().hex
    running_as_root = os.geteuid() == 0
    app_uid = TARGET_UID if running_as_root else os.getuid()
    app_gid = TARGET_GID if running_as_root else os.getgid()

    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    secret = home / "secret.txt"
    secret.write_text(marker + "\n")
    os.chmod(secret, 0o600)  # readable ONLY by its owner: the app uid
    if running_as_root:
        for d in (home, work):
            os.chown(d, app_uid, app_gid)
        os.chown(secret, app_uid, app_gid)
        _make_traversable(tmp_path)

    env = {
        "PATH": "/usr/bin:/bin",
        "AG_PROBE_HOME": str(home),
        "AG_PROBE_WORK": str(work),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if running_as_root:
        setpriv = shutil.which("setpriv")
        assert setpriv, (
            "setpriv is required to re-execute the probe as uid 65534; "
            "without it this regression test cannot run — failing loudly "
            "instead of skipping the non-root identity check"
        )
        pyname = f"python{sys.version_info.major}.{sys.version_info.minor}"
        system_python = shutil.which(pyname, path="/usr/bin:/bin")
        assert system_python, f"{pyname} not found in /usr/bin:/bin for the staged probe"
        stage = _stage_tree(tmp_path)
        probe = stage / "probe.py"
        probe.write_text(PROBE_SRC)
        os.chmod(probe, 0o644)
        env["PYTHONPATH"] = str(stage)
        cmd = [
            setpriv,
            f"--reuid={TARGET_UID}",
            f"--regid={TARGET_GID}",
            "--clear-groups",
            system_python,
            str(probe),
        ]
    else:
        probe = tmp_path / "probe.py"
        probe.write_text(PROBE_SRC)
        env["PYTHONPATH"] = str(REPO_ROOT)
        cmd = [sys.executable, str(probe)]

    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=600, env=env, cwd=str(work)
    )
    # The invariant, unconditionally: the secret never leaks anywhere.
    assert marker not in proc.stdout, f"secret leaked into probe output:\n{proc.stdout}"
    assert marker not in proc.stderr, f"secret leaked into probe stderr:\n{proc.stderr}"

    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("AGPROBE ")]
    assert lines, (
        f"probe produced no result (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    data = json.loads(lines[-1][len("AGPROBE "):])
    # Prove the probe really ran as the intended non-root identity.
    assert data["app_uid"] == app_uid != 0, data

    outcome = data["outcome"]
    print(f"[test_nonroot_identity] branch taken: {outcome} ({data.get('reason', '')[:160]})")
    if outcome == "REFUSED":
        # Secure: the host forbids the rootless path and the sandbox
        # refused to execute anything at all (fail closed).
        assert data.get("reason"), data
        return
    assert outcome == "EXECUTED", f"unexpected probe outcome: {data}"
    stdout = data["stdout"]
    # DAC would have ALLOWED this read (the child shares the app uid and
    # the secret is owner-readable): the denial must come from Landlock.
    assert "READ-DENIED" in stdout, f"home secret was not denied: {data}"
    assert "READ-OK" not in stdout, f"home secret read succeeded: {data}"
    assert "CONNECT-OK" not in stdout, f"network connect succeeded: {data}"
    assert "SOCKET-DENIED" in stdout or "CONNECT-FAILED" in stdout, data
    enf = data["enforcement"]
    assert enf["network_isolated"] is True, enf
    assert enf["landlock_fs"] is True, enf
    assert enf["seccomp_filter"] is True, enf
    assert enf["isolation_strategy"] == "rootless-userns", enf
    assert enf["uid"] == app_uid, enf  # outer (host) identity reported
    assert enf["uid_dropped"] is False, enf
    # Positive control: the sandbox genuinely works in this branch —
    # refusal is not masking a path that claims success vacuously.
    assert data.get("hello_stdout", "").strip() == "hello-nonroot", data
