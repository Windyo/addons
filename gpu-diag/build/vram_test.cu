// VRAM pattern test for the GPU diagnostic add-on.
// Allocates free VRAM in 128 MiB chunks (leaving `reserve` MiB for other users of the card), then
// for each pass writes an address-derived pattern to every word and checks it back on the GPU.
// Memory-bound kernels: low SM load, so a failure here points at VRAM, not at power draw.
// Prints one JSON line. Exit 0 = no mismatches, 1 = mismatches, 2 = CUDA error.
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <vector>
#include <cuda_runtime.h>

static const size_t CHUNK = 128ull << 20;

__device__ __forceinline__ uint32_t pattern(uint64_t i, uint32_t seed) {
    uint32_t x = (uint32_t)i ^ (uint32_t)(i >> 32) * 0x9E3779B9u ^ seed;
    x ^= x >> 16; x *= 0x7FEB352Du; x ^= x >> 15; x *= 0x846CA68Bu; x ^= x >> 16;
    return x;
}

__global__ void fill(uint32_t *p, size_t n, uint64_t base, uint32_t seed) {
    for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
        p[i] = pattern(base + i, seed);
}

__global__ void check(const uint32_t *p, size_t n, uint64_t base, uint32_t seed, unsigned long long *errs) {
    for (size_t i = blockIdx.x * (size_t)blockDim.x + threadIdx.x; i < n; i += (size_t)gridDim.x * blockDim.x)
        if (p[i] != pattern(base + i, seed)) atomicAdd(errs, 1ull);
}

#define CK(call) do { cudaError_t e = (call); if (e != cudaSuccess) { \
    printf("{\"ok\": false, \"error\": \"%s: %s\", \"step\": \"%s\"}\n", cudaGetErrorName(e), cudaGetErrorString(e), #call); \
    return 2; } } while (0)

int main(int argc, char **argv) {
    size_t reserve = (argc > 1 ? strtoull(argv[1], nullptr, 10) : 1536) << 20;
    int passes = argc > 2 ? atoi(argv[2]) : 4;
    size_t free_b, total_b;
    CK(cudaMemGetInfo(&free_b, &total_b));
    std::vector<uint32_t *> chunks;
    while (free_b > reserve + CHUNK) {
        uint32_t *p;
        if (cudaMalloc(&p, CHUNK) != cudaSuccess) { cudaGetLastError(); break; }
        chunks.push_back(p);
        CK(cudaMemGetInfo(&free_b, &total_b));
    }
    unsigned long long *d_errs, errs_total = 0;
    CK(cudaMalloc(&d_errs, sizeof *d_errs));
    const size_t words = CHUNK / 4;
    printf("{\"stage\": \"vram\", \"tested_mib\": %zu, \"free_left_mib\": %zu, \"total_mib\": %zu, \"passes\": [",
           chunks.size() * (CHUNK >> 20), free_b >> 20, total_b >> 20);
    for (int pass = 0; pass < passes; pass++) {
        uint32_t seed = pass % 2 ? 0xFFFFFFFFu ^ (uint32_t)pass : 0x01234567u * (pass + 1);
        for (size_t c = 0; c < chunks.size(); c++) CK((fill<<<1024, 256>>>(chunks[c], words, c * words, seed), cudaGetLastError()));
        CK(cudaDeviceSynchronize());
        CK(cudaMemset(d_errs, 0, sizeof *d_errs));
        for (size_t c = 0; c < chunks.size(); c++) CK((check<<<1024, 256>>>(chunks[c], words, c * words, seed, d_errs), cudaGetLastError()));
        unsigned long long errs;
        CK(cudaMemcpy(&errs, d_errs, sizeof errs, cudaMemcpyDeviceToHost));
        errs_total += errs;
        printf("%s%llu", pass ? ", " : "", errs);
        fflush(stdout);
    }
    printf("], \"mismatched_words\": %llu, \"ok\": %s}\n", errs_total, errs_total ? "false" : "true");
    return errs_total ? 1 : 0;
}
