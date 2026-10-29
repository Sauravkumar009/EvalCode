#include <stdio.h>

int main(void) {
    int a, b;
    scanf("%d %d", &a, &b);
    // Deliberate null-pointer dereference to test crash survival
    int *null_ptr = NULL;
    *null_ptr = 999;
    printf("%d\n", a + b);
    return 0;
}
