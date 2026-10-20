#include <stdio.h>
#include <string.h>

int main(void) {
    char buf[16];
    char dst[16];
    gets(buf);
    strcpy(dst, buf);
    printf("%s\n", dst);
    return 0;
}
