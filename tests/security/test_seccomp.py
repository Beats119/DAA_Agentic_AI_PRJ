"""D4 regression: a real seccomp allowlist is installed on every run.

Denied syscalls fail with EPERM (errno 1) — proven from inside the
sandbox — while ordinary stdio C programs and ASan-instrumented binaries
run normally under the runtime profile.
"""

from autograder.app.core.schemas import CompileConfig
from autograder.app.sandbox import compile_c
from autograder.app.sandbox.executor import ExecLimits

SOCKET_SRC = r"""
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
int main(void) {
    errno = 0;
    int s = socket(AF_INET, SOCK_STREAM, 0);
    printf("socket_errno=%d\n", errno);
    struct sockaddr_in a;
    a.sin_family = AF_INET;
    a.sin_port = htons(9);
    inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
    errno = 0;
    int r = connect(s, (struct sockaddr*)&a, sizeof(a));
    printf("connect_ret=%d errno=%d\n", r, errno);
    return 0;
}
"""

PTRACE_SRC = r"""
#include <sys/ptrace.h>
#include <errno.h>
#include <stdio.h>
int main(void) {
    errno = 0;
    long r = ptrace(PTRACE_TRACEME, 0, 0, 0);
    printf("ptrace_ret=%ld errno=%d\n", r, errno);
    return 0;
}
"""

STDIO_SRC = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void) {
    char *buf = malloc(64);
    strcpy(buf, "sandbox stdio ok");
    int a, b;
    if (scanf("%d %d", &a, &b) != 2) return 1;
    printf("%s sum=%d\n", buf, a + b);
    free(buf);
    return 0;
}
"""

UAF_SRC = r"""
#include <stdlib.h>
#include <stdio.h>
int main(void) {
    int *p = malloc(sizeof(int));
    *p = 7;
    free(p);
    /* use-after-free: ASan must catch this */
    printf("%d\n", *p);
    return 0;
}
"""

CLEAN_SRC = r"""
#include <stdio.h>
#include <stdlib.h>
int main(void) {
    int *p = malloc(sizeof(int));
    *p = 41;
    printf("clean %d\n", *p + 1);
    free(p);
    return 0;
}
"""

_SANITIZER_FLAGS = ["-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
_ASAN_ENV = {"ASAN_OPTIONS": "hard_rss_limit_mb=512:detect_leaks=0"}


def test_socket_denied_with_eperm(executor, c_binary):
    binary = c_binary("ag_sock", SOCKET_SRC)
    result = executor.run([binary], "")
    # EPERM == 1: the runtime profile denies socket() with SECCOMP_RET_ERRNO.
    assert "socket_errno=1" in result.stdout, result.stdout
    assert "connect_ret=-1" in result.stdout, result.stdout
    assert result.enforcement.seccomp_filter is True


def test_ptrace_denied_with_eperm(executor, c_binary):
    binary = c_binary("ag_ptrace", PTRACE_SRC)
    result = executor.run([binary], "")
    assert "ptrace_ret=-1 errno=1" in result.stdout, result.stdout


def test_normal_stdio_program_unaffected(executor, c_binary):
    binary = c_binary("ag_stdio", STDIO_SRC)
    result = executor.run([binary], "19 23\n")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "sandbox stdio ok sum=42\n"


def test_asan_binary_runs_and_reports_under_runtime_profile(executor, tmp_path):
    src = tmp_path / "uaf.c"
    src.write_text(UAF_SRC)
    out = tmp_path / "build_uaf"
    cres = compile_c(
        str(src), str(out), CompileConfig(flags=_SANITIZER_FLAGS),
        executor, allowed_dir=str(tmp_path),
    )
    assert cres.ok, cres.raw_stderr
    result = executor.run(
        [cres.binary_path], "",
        ExecLimits(wall_time_s=15.0, unlimited_address_space=True),
        extra_env=_ASAN_ENV,
    )
    assert result.exit_code not in (0, None), result.stdout
    assert ("AddressSanitizer" in result.stderr) or ("runtime error" in result.stderr), result.stderr[:500]
    assert result.enforcement.seccomp_filter is True


def test_asan_clean_program_exits_zero(executor, tmp_path):
    src = tmp_path / "clean.c"
    src.write_text(CLEAN_SRC)
    out = tmp_path / "build_clean"
    cres = compile_c(
        str(src), str(out), CompileConfig(flags=_SANITIZER_FLAGS),
        executor, allowed_dir=str(tmp_path),
    )
    assert cres.ok, cres.raw_stderr
    result = executor.run(
        [cres.binary_path], "",
        ExecLimits(wall_time_s=15.0, unlimited_address_space=True),
        extra_env=_ASAN_ENV,
    )
    assert result.exit_code == 0, result.stderr[:500]
    assert result.stdout == "clean 42\n"
