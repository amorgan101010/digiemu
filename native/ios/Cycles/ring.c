#include "ring.h"
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

struct cf_ring {
    float *data;
    uint32_t frames; // a power of two
    _Atomic uint32_t written, read; // frame counts, wrapping
};

cf_ring *ring_new(uint32_t frames) {
    uint32_t size = 1;
    while (size < frames) size <<= 1;
    cf_ring *ring = calloc(1, sizeof *ring);
    ring->data = calloc(size * 2, sizeof(float));
    ring->frames = size;
    return ring;
}

uint32_t ring_fill(cf_ring *ring) {
    return atomic_load(&ring->written) - atomic_load(&ring->read);
}

uint32_t ring_write(cf_ring *ring, const float *interleaved, uint32_t frames) {
    uint32_t at = atomic_load(&ring->written);
    uint32_t room = ring->frames - (at - atomic_load(&ring->read));
    if (frames > room) frames = room;
    for (uint32_t i = 0; i < frames; i++) {
        uint32_t slot = (at + i) & (ring->frames - 1);
        ring->data[2 * slot] = interleaved[2 * i];
        ring->data[2 * slot + 1] = interleaved[2 * i + 1];
    }
    atomic_store(&ring->written, at + frames);
    return frames;
}

uint32_t ring_read(cf_ring *ring, float *left, float *right, uint32_t frames) {
    uint32_t at = atomic_load(&ring->read);
    uint32_t have = atomic_load(&ring->written) - at;
    uint32_t n = frames < have ? frames : have;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t slot = (at + i) & (ring->frames - 1);
        left[i] = ring->data[2 * slot];
        right[i] = ring->data[2 * slot + 1];
    }
    if (n < frames) {
        memset(left + n, 0, (frames - n) * sizeof(float));
        memset(right + n, 0, (frames - n) * sizeof(float));
    }
    atomic_store(&ring->read, at + n);
    return n;
}
