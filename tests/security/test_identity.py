"""D2 regression: sandboxed code must not run as the invoking user.

When the application runs as root, the launcher drops the child to the
policy's dedicated sandbox identity (default nobody/65534) before exec —
which is also what makes RLIMIT_NPROC enforceable. When the application
runs unprivileged (sandbox-2.1 rootless strategy), the child is uid 0
inside its OWN user namespace — an identity that exists only inside
that namespace and maps back to the unprivileged app uid on the host;
the enforcement report carries the OUTER uid and
``isolation_strategy == "rootless-userns"`` (and ``uid_dropped=False``:
no setuid happened, or was needed).
"""

import os
import re

UID_SRC = r"""
#include <stdio.h>
#include <unistd.h>
int main(void) {
    printf("uid=%d euid=%d gid=%d\n", getuid(), geteuid(), getgid());
    return 0;
}
"""


def test_child_identity(executor, c_binary):
    binary = c_binary("ag_uid", UID_SRC)
    result = executor.run([binary], "")
    m = re.search(r"uid=(\d+) euid=(\d+) gid=(\d+)", result.stdout)
    assert m, f"no uid line in output: {result.stdout!r} {result.stderr!r}"
    uid, euid, gid = (int(g) for g in m.groups())
    if os.geteuid() == 0:
        assert executor.policy.drop_privileges_when_root is True
        assert uid == executor.policy.sandbox_uid == 65534
        assert euid == 65534
        assert gid == executor.policy.sandbox_gid == 65534
        assert uid != 0
        # The enforcement report is the launcher's actual: same story.
        assert result.enforcement.uid == 65534
        assert result.enforcement.uid_dropped is True
        assert result.enforcement.isolation_strategy == "root-setuid"
    else:
        # Rootless strategy: the program observes its namespace-INNER
        # uid (0), while the enforcement report carries the OUTER uid —
        # the app user's real, unprivileged host identity.
        assert result.enforcement.isolation_strategy == "rootless-userns"
        assert uid == 0
        assert result.enforcement.uid == os.getuid()
        assert result.enforcement.uid_dropped is False
