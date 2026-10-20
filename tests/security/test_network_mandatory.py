"""D3 regression: network isolation is mandatory, not best-effort.

Connects to loopback and to an external address both fail inside the
sandbox, and the launcher-reported enforcement always shows
``network_isolated=True``. (Under the runtime seccomp profile socket()
is itself denied with EPERM, so the connect cannot even be attempted —
both layers deny, and the mandatory-netns report is what is asserted
here alongside the failed connects.)
"""

from autograder.app.sandbox.executor import ExecLimits

CONNECT_SRC = r"""
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdio.h>
static void attempt(const char *label, const char *ip, int port) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) { printf("%s=SOCKET_FAIL\n", label); return; }
    struct sockaddr_in a;
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    inet_pton(AF_INET, ip, &a.sin_addr);
    int r = connect(s, (struct sockaddr*)&a, sizeof(a));
    printf("%s=%s\n", label, r == 0 ? "CONNECTED" : "FAIL");
}
int main(void) {
    attempt("LOOPBACK", "127.0.0.1", 9);
    attempt("EXTERNAL", "1.1.1.1", 80);
    return 0;
}
"""


def test_connects_fail_and_isolation_reported(executor, c_binary):
    binary = c_binary("ag_netmand", CONNECT_SRC)
    result = executor.run(
        [binary], "", ExecLimits(wall_time_s=8.0, cpu_time_s=10.0, memory_mb=256)
    )
    assert "LOOPBACK=CONNECTED" not in result.stdout, result.stdout
    assert "EXTERNAL=CONNECTED" not in result.stdout, result.stdout
    assert "LOOPBACK=" in result.stdout and "EXTERNAL=" in result.stdout, result.stdout
    # Mandatory: the launcher itself reports the namespace was created.
    assert result.enforcement.network_isolated is True
