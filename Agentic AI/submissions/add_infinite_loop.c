#include <stdio.h>

int main(void) {
    int a, b;
    if (scanf("%d %d", &a, &b) == 2) {
        // Deliberate infinite loop to test agent timeout and survival
        while (1) {
            a = a + 0;
        }
        printf("%d\n", a + b);
    }
    return 0;
}
