#include <stdio.h>
#include <stdlib.h>

int main(void) {
    int n;
    if (scanf("%d", &n) != 1) {
        return 1;
    }
    int *arr = malloc(4 * sizeof(int));
    if (!arr) {
        return 1;
    }
    for (int i = 0; i < n; i++) {
        arr[i] = i; /* heap-buffer-overflow when n > 4 */
    }
    printf("%d\n", arr[0]);
    free(arr);
    return 0;
}
