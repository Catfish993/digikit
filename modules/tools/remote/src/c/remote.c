/* digikit/remote: press keys and turn encoders from a computer (docs/plans/remote-control.md).
 *
 *   0x38 KEY      body: code (u8), down (u8, 0 or 1)         reply: status 0 or core/input's negative result
 *   0x39 TURN     body: encoder (u8, 1..), delta (s8)        reply: one byte, the delta taken (s8, |taken| <= 30); the rest is for the host to send again
 *   0x3a RELEASE  body: none                                 reply: status 0 (every key this module holds is released)
 *   0x3e STATE    body: none                                 reply: enabled (u8), state format (u8, 1), turn limit (u8, 30)
 *
 * 0x38..0x3d is the guarded remote range of sdk/sdk.toml: while DIGIKIT > REMOTE is off the runtime answers E_DISABLED before this module sees a
 * command. 0x3e is the state range, always answered. The module knows no device: codes and encoders are the device file's.
 *
 * A turn: the firmware adds a delta to a pending count per encoder and flushes at most 30 detents every 4 ticks. The module keeps its own estimate of
 * the pending count (it drains PACE_DRAIN per PACE_BLOCKS blocks of 1/1500 s, deliberately slower than the firmware) and takes only what fits below
 * the limit, so the firmware's clamp is never the thing that drops or delays a turn. */

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
extern int remote_enabled(void);
extern u32 dkrt_blocks(void);

#define E_MSG (-50)

#define TURN_LIMIT 30
#define PACE_DRAIN 30u
#define PACE_BLOCKS 96u
#define MAX_ENC 16u

static u8 held[16]; /* a bit per key code 0..127 */
static int pending[MAX_ENC];
static u32 stamp[MAX_ENC];

static int key(u32 cmd, u32 seq, const u8 *body, u32 len, u8 *out)
{
    int r;
    (void)cmd;
    (void)seq;
    (void)out;
    if (len != 2u || body[1] > 1u)
        return E_MSG;
    if (body[0] > 127u)
        return E_MSG;
    r = input_key(body[0], body[1]);
    if (r == 0) {
        if (body[1])
            held[body[0] >> 3] |= (u8)(1u << (body[0] & 7u));
        else
            held[body[0] >> 3] &= (u8)~(1u << (body[0] & 7u));
    }
    return r;
}

static int turn(u32 cmd, u32 seq, const u8 *body, u32 len, u8 *out)
{
    u32 enc, now, steps;
    int d, room, take, r;
    (void)cmd;
    (void)seq;
    if (len != 2u)
        return E_MSG;
    enc = body[0];
    if (enc < 1u || enc > MAX_ENC)
        return E_MSG;
    now = dkrt_blocks();
    steps = (now - stamp[enc - 1u]) / PACE_BLOCKS;
    if (steps > 0u) {
        int p = pending[enc - 1u];
        u32 drain = steps * PACE_DRAIN;
        if (drain >= (u32)(p < 0 ? -p : p))
            p = 0;
        else
            p += p < 0 ? (int)drain : -(int)drain;
        pending[enc - 1u] = p;
        stamp[enc - 1u] += steps * PACE_BLOCKS;
    }
    d = (s8)body[1];
    /* the room left in the direction of the turn */
    room = d > 0 ? TURN_LIMIT - pending[enc - 1u] : TURN_LIMIT + pending[enc - 1u];
    if (room < 0)
        room = 0;
    take = d > room ? room : (d < -room ? -room : d);
    if (take == 0) {
        out[0] = 0;
        return 1;
    }
    r = input_turn(enc, take);
    if (r != 0)
        return r;
    pending[enc - 1u] += take;
    out[0] = (u8)(s8)take;
    return 1;
}

static int release(u32 cmd, u32 seq, const u8 *body, u32 len, u8 *out)
{
    u32 c;
    int worst = 0;
    (void)cmd;
    (void)seq;
    (void)body;
    (void)len;
    (void)out;
    for (c = 0; c < 128u; c++) {
        if (held[c >> 3] & (1u << (c & 7u))) {
            int r = input_key(c, 0);
            if (r == 0)
                held[c >> 3] &= (u8)~(1u << (c & 7u));
            else
                worst = r;
        }
    }
    return worst;
}

static int state(u32 cmd, u32 seq, const u8 *body, u32 len, u8 *out)
{
    (void)cmd;
    (void)seq;
    (void)body;
    (void)len;
    out[0] = (u8)(remote_enabled() ? 1 : 0);
    out[1] = 1;
    out[2] = TURN_LIMIT;
    return 3;
}

int dkm_init(const struct api *api)
{
    int a = api->register_cmds(0x38, 1, key);
    int b = api->register_cmds(0x39, 1, turn);
    int c = api->register_cmds(0x3a, 1, release);
    int d = api->register_cmds(0x3e, 1, state);
    api->log("remote registered", (u32)(a | b | c | d));
    return (a | b | c | d) ? -1 : 0;
}
