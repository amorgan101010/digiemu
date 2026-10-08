// native/cfcore/src/ffi.rs
#include <stdint.h>

struct cf_chunk {
    double emulated_s;
    int64_t instructions;
    int64_t interpreted;
    int64_t idle;
    uint64_t interrupts;
    uint64_t frames;
    double level;
    uint64_t hash;
};

void *cfcore_open(const char *path, int64_t ips);
int cfcore_run(void *bench, double seconds, struct cf_chunk *out);
void cfcore_close(void *bench);
const char *cfcore_error(void);
uint64_t cfcore_blocks(void);

// To play it: one thread makes all of these calls.
void cfcore_live(void *bench);
long cfcore_advance(void *bench, long frames, float *pcm, long cap);
void cfcore_key(void *bench, int column, int bit, int down);
void cfcore_pad(void *bench, int index, int velocity);
void cfcore_turn(void *bench, int encoder, int steps);
uint64_t cfcore_leds(void *bench);
float cfcore_gain(void *bench);
int cfcore_screen(void *bench, uint32_t pointer_at, uint8_t *out);

// To keep it: the whole machine in a file cfcore_open takes.
// With wait 0 the file is written by another thread after this returns.
int cfcore_save(void *bench, const char *path, int wait);
