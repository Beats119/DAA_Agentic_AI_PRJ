"""Network isolation: honestly reported, never claimed when absent."""

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


def test_network_isolation_honestly_reported(executor, c_binary):
    from autograder.app.sandbox.executor import ExecLimits

    caps = probe_capabilities()
    assert isinstance(caps.network_namespace, bool)

    binary = c_binary("ag_net", NET_SRC)
    result = executor.run(
        [binary], "", ExecLimits(wall_time_s=8.0, cpu_time_s=10.0, memory_mb=256)
    )

    assert isinstance(result.enforcement.network_isolated, bool)
    if result.enforcement.network_isolated:
        # In a fresh netns, external connect must fail fast.
        assert "connect=-1" in result.stdout, (
            f"claimed network isolation but connect succeeded: {result.stdout!r}"
        )
    # If the kernel lacks netns, the field is False and the test still passes:
    # the sandbox never claims isolation it does not have.
