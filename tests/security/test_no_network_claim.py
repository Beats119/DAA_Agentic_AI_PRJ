"""Network isolation: MANDATORY since Phase B — never best-effort.

The executor must refuse to run when a network namespace cannot be
created; when a run does happen, ``enforcement.network_isolated`` is
always True (it comes from the launcher's own report) and external
connects fail inside the fresh namespace.
"""

from autograder.app.sandbox import probe_capabilities

NET_SRC = r"""
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdio.h>
int main() {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a;
    a.sin_family = AF_INET;
    a.sin_port = htons(53);
    inet_pton(AF_INET, "8.8.8.8", &a.sin_addr);
    int r = connect(s, (struct sockaddr*)&a, sizeof(a));
    printf("connect=%d\n", r);
    return 0;
}
"""


def test_network_isolation_mandatory_and_real(executor, c_binary):
    from autograder.app.sandbox.executor import ExecLimits

    caps = probe_capabilities()
    assert caps.network_namespace is True

    binary = c_binary("ag_net", NET_SRC)
    result = executor.run(
        [binary], "", ExecLimits(wall_time_s=8.0, cpu_time_s=10.0, memory_mb=256)
    )

    # Isolation is mandatory now: the launcher reports it, always.
    assert result.enforcement.network_isolated is True
    # In a fresh netns, the external connect must fail.
    # (Under the runtime seccomp profile socket() itself is denied too —
    # either way the connect cannot succeed.)
    assert "connect=-1" in result.stdout, (
        f"claimed network isolation but connect succeeded: {result.stdout!r}"
    )
