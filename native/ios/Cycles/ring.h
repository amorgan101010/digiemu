// A ring of stereo frames between the emulator thread (one writer) and the
// audio callback (one reader), with no lock: the callback must not wait.
#include <stdint.h>

typedef struct cf_ring cf_ring;

cf_ring *ring_new(uint32_t frames);
// -> frames written: fewer than asked when the ring is full.
uint32_t ring_write(cf_ring *ring, const float *interleaved, uint32_t frames);
// Fills left and right; -> frames read, the rest is silence.
uint32_t ring_read(cf_ring *ring, float *left, float *right, uint32_t frames);
uint32_t ring_fill(cf_ring *ring);
