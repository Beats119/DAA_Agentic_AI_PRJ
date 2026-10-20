"""Symlinks must not smuggle a sandboxed program out of its allowlist.

Landlock rules attach to the resolved inode hierarchy, so a symlink
created inside the run directory that points outside must still be
denied when followed. This test creates the symlink from inside the
sandbox and then reads through it.
"""

ESC_SRC = r"""
#include <stdio.h>
#include <unistd.h>
static void try_read(const char *label, const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) { printf("%s=FAIL\n", label); return; }
    char buf[128] = {0};
    (void)!fread(buf, 1, 100, f);
    fclose(f);
    printf("%s=OK:%s\n", label, buf);
}
int main(void) {
    int r = symlink("/etc/hostname", "esc.txt");
    printf("SYMLINK=%s\n", r == 0 ? "CREATED" : "FAILED");
    try_read("VIA_LINK", "esc.txt");
    try_read("DIRECT", "/etc/hostname");
    return 0;
}
"""


def test_symlink_escape_denied(executor, c_binary):
    binary = c_binary("ag_escape", ESC_SRC)
    result = executor.run([binary], "")
    out = result.stdout
    # Creating the symlink inside the run dir is fine...
    assert "SYMLINK=CREATED" in out, out
    # ...but following it out of the sandbox is not.
    assert "VIA_LINK=FAIL" in out, out
    assert "DIRECT=FAIL" in out, out
