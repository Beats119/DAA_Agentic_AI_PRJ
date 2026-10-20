#include <stdio.h>

int main(void) {
    int x, y;
    int unused_variable = 42;
    if (scanf("%d %d", &x, &y) != 2) {
        return 1;
    }
    printf("%d\n", x + y);
}
