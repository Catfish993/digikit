/* test/input-probe: SysEx commands that press keys and turn encoders through core/input (docs/plans/remote-control.md). The proof of the input adapter:
 * what these commands do is compared with the emulator's own input.button and input.turn. digikit/remote is the module for players.
 *
 *   0x38  key:  body = code (u8), down (u8)       reply: status 0, or core/input's negative result
 *   0x39  turn: body = encoder (u8), delta (i8)   reply: status 0, or the negative result
 *
 * input_key and input_turn are imports of the runtime's table (the names core/input provides); a runtime without core/input does not find them and the
 * module does not load. */

typedef unsigned int u32;
typedef unsigned char u8;
typedef signed char s8;

typedef int (*svc_handler)(u32 cmd, u32 seq, const u8 *body, u32 len, u8 *out);

struct api {
    u32 version, size, device, runtime;
    u32 (*import)(const char *name);
    void *(*alloc)(u32 size);
    void (*free)(void *p);
    int (*store_read)(const char *path, void *dst, u32 cap);
    void (*log)(const char *msg, u32 value);
    u32 memcpy_, memset_, memcmp_, muldi3_, udivdi3_, umoddi3_;
    void (*log_lv)(u32 level, const char *msg, u32 value);
    int (*register_cmds)(u32 first, u32 count, svc_handler handler);
};

extern int input_key(u32 code, u32 down);
extern int input_turn(u32 encoder, int delta);

static int key(u32 cmd, u32 seq, const u8 *body, u32 len, u8 *out)
{
    (void)cmd;
    (void)seq;
    (void)out;
    if (len != 2u)
        return -50; /* E_MSG */
    return input_key(body[0], body[1]);
}

static int turn(u32 cmd, u32 seq, const u8 *body, u32 len, u8 *out)
{
    (void)cmd;
    (void)seq;
    (void)out;
    if (len != 2u)
        return -50;
    return input_turn(body[0], (s8)body[1]);
}

int dkm_init(const struct api *api)
{
    int a = api->register_cmds(0x38, 1, key);
    int b = api->register_cmds(0x39, 1, turn);
    api->log("input-probe registered", (u32)(a | b));
    return 0;
}
