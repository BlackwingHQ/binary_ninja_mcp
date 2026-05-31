/*
 * Deterministic fixture binary used by the live Binary Ninja smoke
 * tests. Keep this file small and add new constructs (structs, switches,
 * indirect calls, etc.) on demand as tests grow.
 *
 * Build with: tests/integration/fixtures/build.sh
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int compute_secret(int n) {
    int acc = 0;
    for (int i = 1; i <= n; i++) {
        acc += i * 7;
    }
    return acc;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        printf("usage: %s <n>\n", argv[0]);
        return 1;
    }
    int n = atoi(argv[1]);
    int result = compute_secret(n);
    printf("result = %d\n", result);
    return 0;
}
