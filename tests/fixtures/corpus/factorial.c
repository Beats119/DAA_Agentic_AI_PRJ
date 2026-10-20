#include <stdio.h>

long fact(int n) {
    long r = 1;
    for (int i = 2; i <= n; i++) {
        r *= i;
    }
    return r;
}

int main(void) {
    int n;
    if (scanf("%d", &n) != 1) {
        return 1;
    }
    printf("%ld\n", fact(n));
    return 0;
}
