"""D1 regression: sandboxed code must not READ the host filesystem.

The old Landlock policy granted read+execute on ``/`` — there was no read
isolation at all. The Phase-B policy is default-deny: runtime libraries,
the run directory, /dev/null, and explicitly granted paths only. These
tests run a real program that attempts the reads; every host read must
fail while a file inside its own run directory stays readable.
"""

import os
from pathlib import Path

READER_SRC = r"""
#include <stdio.h>
static void try_read(const char *label, const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) { printf("%s=FAIL\n", label); return; }
    char buf[160] = {0};
    (void)!fread(buf, 1, 120, f);
    fclose(f);
    printf("%s=OK:%s\n", label, buf);
}
int main(int argc, char **argv) {
    FILE *w = fopen("mine.txt", "w");
    fputs("run-dir-secret-42", w);
    fclose(w);
    try_read("RUNDIR", "mine.txt");
    try_read("SHADOW", "/etc/shadow");
    /* /etc/hostname is world-readable by DAC: only Landlock can deny it. */
    try_read("HOSTNAME", "/etc/hostname");
    for (int i = 1; i < argc; i++) {
        char label[16];
        snprintf(label, sizeof label, "ARG%d", i);
        try_read(label, argv[i]);
    }
    return 0;
}
"""


def test_host_reads_denied_run_dir_allowed(executor, c_binary, tmp_path):
    planted = Path(__file__).parent / ".planted_secret"
    planted_env = Path(__file__).parent / ".env"
    planted.write_text("PLANTED-SECRET-VALUE-12345")
    planted_env.write_text("API_KEY=PLANTED-ENV-SECRET-67890\n")
    try:
        ssh_key = os.path.expanduser("~/.ssh/id_ed25519")
        binary = c_binary("ag_reader", READER_SRC)
        result = executor.run(
            [binary, ssh_key, str(planted), str(planted_env)], ""
        )
        out = result.stdout
        # Positive control: its own run directory is fully usable.
        assert "RUNDIR=OK:run-dir-secret-42" in out, out
        # Every host read must fail.
        assert "SHADOW=FAIL" in out, out
        assert "HOSTNAME=FAIL" in out, out
        assert "ARG1=FAIL" in out, out  # ~/.ssh/id_ed25519
        assert "ARG2=FAIL" in out, out  # planted secret in the app tree
        assert "ARG3=FAIL" in out, out  # planted .env in the app tree
        # And no secret content leaked anywhere.
        assert "PLANTED-SECRET-VALUE-12345" not in out
        assert "PLANTED-ENV-SECRET-67890" not in out
        assert result.enforcement.landlock_fs is True
    finally:
        planted.unlink(missing_ok=True)
        planted_env.unlink(missing_ok=True)
