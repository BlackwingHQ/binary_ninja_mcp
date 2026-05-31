/*
 * Deterministic fixture binary used by the live Binary Ninja smoke
 * tests. Add new constructs (structs, switches, indirect calls, etc.)
 * AFTER the existing functions so addresses pinned by tests against
 * compute_secret and main stay stable.
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

/* ------------------------------------------------------------------
 * Symbol / type-graph fixtures, declared with extern linkage and
 * `__attribute__((used))` so the linker keeps them and BN's DWARF
 * importer surfaces both the symbols and the user-defined types.
 * Everything below is unreachable from main on purpose — its job is
 * to exist in the symbol and type tables.
 * ------------------------------------------------------------------ */

typedef enum {
    PRIORITY_LOW = 0,
    PRIORITY_NORMAL = 1,
    PRIORITY_HIGH = 7,
    PRIORITY_CRITICAL = 42,
} priority_t;

typedef struct {
    int id;
    priority_t pri;
    const char *label;
} task_t;

typedef union {
    int as_int;
    unsigned char bytes[4];
} value_view_t;

/* Named global so /hexdump, /getDataDecl, /getDataVarAt have a
 * stable, descriptive target instead of relying on Mach-O headers. */
task_t default_task = {99, PRIORITY_HIGH, "default"};

/* Touches the struct and the enum so BN records xrefs into both. */
__attribute__((used)) int task_priority(const task_t *t) {
    return t->pri == PRIORITY_CRITICAL ? -1 : t->pri;
}

/* Touches the union so it appears in the type graph. */
__attribute__((used)) int value_low_byte(const value_view_t *v) {
    return v->bytes[0];
}

/* A switch with several cases — at -O0 clang typically lays this out
 * as a chained-compare sequence rather than a jump table, which is
 * still useful for testing CFG-shape assertions in IL/disassembly. */
__attribute__((used)) int dispatch(int cmd) {
    switch (cmd) {
        case 0: return 10;
        case 1: return 20;
        case 2: return 30;
        case 3: return 40;
        case 4: return 50;
        default: return -1;
    }
}

/* Indirect call through a function pointer — covers the
 * function-pointer / unresolved-target path in BN's call-graph. */
__attribute__((used)) int call_through(int (*fn)(int), int x) {
    return fn(x);
}
