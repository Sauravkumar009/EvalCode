#define __USE_MINGW_ANSI_STDIO 1
#include <stdio.h>
#include <limits.h>

/**
 * Calculates the sum of two integers safely using 64-bit precision
 * to prevent 32-bit signed overflow.
 */
long long add_numbers(long long first, long long second) {
    return first + second;
}

int main(void) {
    long long num1 = 0;
    long long num2 = 0;

    // Read two integers separated by whitespace
    if (scanf("%lld %lld", &num1, &num2) == 2) {
        printf("%lld\n", add_numbers(num1, num2));
    }
    return 0;
}
