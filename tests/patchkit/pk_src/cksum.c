/* Freestanding ColdFire test module: a checksum, and a call into the firmware through extern symbols. */
typedef unsigned int u32;
typedef unsigned char u8;

/* Adler-32: exercises the hardware divide (divu.l / remu.l, ISA_A+) and byte loads. */
u32 pk_adler32(const u8 *p, u32 n)
{
    u32 a = 1, b = 0;
    while (n--) {
        a = (a + *p++) % 65521u;
        b = (b + a) % 65521u;
    }
    return (b << 16) | a;
}

/* Table state: .data and .bss, to see data relocations. */
static u32 counter = 5;
u32 pk_scratch[4];
u32 pk_bump(u32 by)
{
    counter += by;
    pk_scratch[counter & 3] = counter;
    return counter;
}

/* A firmware object accessor (DT2 1.17 0x40114a28, 0x40114984): returns two derived fields. */
/* The firmware sets only the low byte of D0 (clr.b), so the result type must be u8. */
extern u8 fw_flag_bit(const void *obj);
extern u32 fw_field_0c(const void *obj);
u32 pk_call_fw(const void *obj)
{
    return (fw_field_0c(obj) << 1) | fw_flag_bit(obj);
}

/* A firmware function that returns a pointer (1.17 active_kit_getter: this + 0xf4, argument on the stack). */
extern u32 active_kit_getter(void *project);
u32 pk_kit_plus(void *project, u32 add)
{
    return active_kit_getter(project) + add;
}
