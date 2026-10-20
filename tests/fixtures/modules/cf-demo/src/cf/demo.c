/* test/cf-demo: ColdFire code, read-only data, initialised data and bss placed in the expansion region.
 *
 * The harness boots a patched image to ready, calls these functions in place and reads the data back: code and
 * data must have been copied by the core/expansion loader before the reset clear, and the bss zeroed by it. */

const unsigned pk_demo_k = 0x1234;
unsigned pk_demo_table[8] = {1, 2, 3, 4, 5, 6, 7, 8};
unsigned pk_demo_bss[16];

unsigned pk_demo_add(unsigned a, unsigned b)
{
    return a + b + pk_demo_k;
}

/* table sum + bss sum + k: the bss words are zero when the loader zeroed them */
unsigned pk_demo_sum(void)
{
    unsigned s = pk_demo_k;
    for (int i = 0; i < 8; i++)
        s += pk_demo_table[i];
    for (int i = 0; i < 16; i++)
        s += pk_demo_bss[i];
    return s;
}
