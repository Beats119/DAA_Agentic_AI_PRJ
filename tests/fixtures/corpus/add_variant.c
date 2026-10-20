#include <stdio.h>

int sum2(int p, int q) {
    return p + q;
}

int main(void) {
    int a1, b1;
    if (scanf("%d %d", &a1, &b1) != 2) {
        return 1;
    }
    printf("%d\n", sum2(a1, b1));
    return 0;
}
