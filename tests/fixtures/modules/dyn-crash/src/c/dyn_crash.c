/* test/dyn-crash: a module that raises a CPU exception some time into a session (docs/findings/46-digikit-debug.md).
 *
 * The delay and the kind come from the store file `cfg/crash-after.bin` (5 bytes: u32 big endian blocks, then 0 = illegal instruction,
 * 1 = division by zero); no file: never crashes. Just before it does, it logs `crash-pc` with the address of the illegal instruction (mode 0) or
 * of the divide (mode 1), which is the program counter the crash record must show. (A read of an unmapped address is no good as a second
 * kind: the emulator reports it as a bus fault of the run, not as an exception the guest can handle.) */

typedef unsigned int u32;

struct api {
    u32 version, size, device, runtime;
    u32 (*import)(const char *name);
    void *(*alloc)(u32 size);
    void (*free)(void *p);
    int (*store_read)(const char *path, void *dst, u32 cap);
    void (*log)(const char *msg, u32 value);
};

/* the runtime's 1500 Hz block clock, an import of the module table */
extern u32 dkrt_blocks(void);

static u32 after = 0xffffffffu;
static u32 mode;
static u32 start;
static u32 started;

int dkm_init(const struct api *api)
{
    unsigned char cfg[16];
    int n = api->store_read("cfg/crash-after.bin", cfg, sizeof cfg);
    if (n >= 5) {
        after = ((u32)cfg[0] << 24) | ((u32)cfg[1] << 16) | ((u32)cfg[2] << 8) | cfg[3];
        mode = cfg[4];
    }
    api->log("crash-after", after);
    return 0;
}

void dkm_slot_0(const struct api *api)
{
    u32 now = dkrt_blocks();
    register u32 pc;
    if (!started) {
        started = 1;
        start = now;
        return;
    }
    if (now - start < after) {
        return;
    }
    if (mode == 0) {
        __asm__ volatile("lea 2f(%%pc),%0" : "=a"(pc));
        api->log("crash-pc", pc);
        __asm__ volatile("2: .short 0x4afc");
    } else {
        /* a division by zero; the divisor is read through a volatile so that the compiler cannot see it */
        volatile u32 zero = 0;
        register u32 x = 1234;
        __asm__ volatile("lea 3f(%%pc),%0" : "=a"(pc));
        api->log("crash-pc", pc);
        __asm__ volatile("3: divu.l %1,%0" : "+d"(x) : "d"(zero));
    }
    for (;;) {
    }
}
