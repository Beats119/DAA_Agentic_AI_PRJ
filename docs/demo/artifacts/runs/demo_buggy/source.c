#include <stdio.h>

int add(int a, int b) {
    return a + b + 1; /* off-by-one bug */
}

int main(void) {
    int x, y;
    if (scanf("%d %d", &x, &y) != 2) {
        return 1;
    }
    printf("%d\n", add(x, y));
    return 0;
}
