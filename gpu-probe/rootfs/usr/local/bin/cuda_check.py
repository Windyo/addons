#!/usr/bin/env python3
"""A small CUDA job through the driver API (libcuda via ctypes), for the GPU probe.

Device query, the primary context (the one the CUDA runtime uses), device memory, a PTX kernel
JIT-compiled by the driver, and managed memory (the /dev/nvidia-uvm path). Every element of the
result is checked. Prints one JSON line; exit status 0 only if everything passed.
"""
import ctypes
import json
import sys
import time
from array import array

PTX = "/usr/local/share/gpu-probe/saxpy.ptx"
N = 1 << 20  # 4 MiB per buffer
A = 2.0
BLOCK = 256
CC_MAJOR, CC_MINOR = 75, 76  # CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR/_MINOR
JIT_ERROR_LOG, JIT_ERROR_LOG_SIZE = 5, 6  # CU_JIT_ERROR_LOG_BUFFER, ..._SIZE_BYTES
ATTACH_GLOBAL = 1  # CU_MEM_ATTACH_GLOBAL


class CudaError(Exception):
    pass


def mismatches(raw):
    got = array("f")
    got.frombytes(raw)
    # x[i] = i % 1024, y[i] = 1, a = 2: every result is exact in float32.
    return sum(1 for i, v in enumerate(got) if v != A * (i % 1024) + 1.0)


def main(out):
    cu = ctypes.CDLL("libcuda.so.1")
    timings = out["timings_ms"] = {}

    def call(fn, *args, label=None, on_error=None):
        t = time.monotonic()
        rc = getattr(cu, fn)(*args)
        if label:
            timings[label] = round((time.monotonic() - t) * 1000, 2)
        if rc:
            name = ctypes.c_char_p()
            cu.cuGetErrorName(rc, ctypes.byref(name))
            out["failed_step"] = fn
            out["error"] = f"{rc} {(name.value or b'?').decode()}"
            if on_error:
                out.update(on_error())
            raise CudaError

    call("cuInit", 0, label="init")
    version, count, dev = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    call("cuDriverGetVersion", ctypes.byref(version))
    call("cuDeviceGetCount", ctypes.byref(count))
    call("cuDeviceGet", ctypes.byref(dev), 0)
    name = ctypes.create_string_buffer(256)
    call("cuDeviceGetName", name, 256, dev)
    major, minor = ctypes.c_int(), ctypes.c_int()
    call("cuDeviceGetAttribute", ctypes.byref(major), CC_MAJOR, dev)
    call("cuDeviceGetAttribute", ctypes.byref(minor), CC_MINOR, dev)
    out["device"] = {"name": name.value.decode(), "count": count.value,
                     "compute_capability": f"{major.value}.{minor.value}", "driver_api": version.value}

    ctx = ctypes.c_void_p()
    call("cuDevicePrimaryCtxRetain", ctypes.byref(ctx), dev, label="context")
    try:
        call("cuCtxSetCurrent", ctx)
        free, total = ctypes.c_size_t(), ctypes.c_size_t()
        call("cuMemGetInfo_v2", ctypes.byref(free), ctypes.byref(total))
        out["vram_mib"] = {"total": total.value >> 20, "free": free.value >> 20}

        with open(PTX, "rb") as f:
            image = ctypes.create_string_buffer(f.read())
        jit_log = ctypes.create_string_buffer(4096)
        opts = (ctypes.c_int * 2)(JIT_ERROR_LOG, JIT_ERROR_LOG_SIZE)
        vals = (ctypes.c_void_p * 2)(ctypes.addressof(jit_log), ctypes.sizeof(jit_log))
        mod, fn = ctypes.c_void_p(), ctypes.c_void_p()
        call("cuModuleLoadDataEx", ctypes.byref(mod), image, 2, opts, vals, label="ptx_jit",
             on_error=lambda: {"jit_log": jit_log.value.decode(errors="replace")[-1000:]})
        try:
            call("cuModuleGetFunction", ctypes.byref(fn), mod, b"saxpy")
            run_jobs(call, cu, fn, out)
        finally:
            cu.cuModuleUnload(mod)
    finally:
        cu.cuDevicePrimaryCtxRelease_v2(dev)

    out["ok"] = out["device_memory_mismatches"] == 0 and out["managed_memory_mismatches"] == 0


def run_jobs(call, cu, fn, out):
    nbytes = 4 * N
    size = ctypes.c_size_t(nbytes)
    x = array("f", (float(i % 1024) for i in range(N)))
    y = array("f", [1.0]) * N
    hx, hy = ctypes.c_void_p(x.buffer_info()[0]), ctypes.c_void_p(y.buffer_info()[0])

    def saxpy(px, py, label):
        params = [ctypes.c_uint64(px.value), ctypes.c_uint64(py.value), ctypes.c_float(A), ctypes.c_uint32(N)]
        ptrs = (ctypes.c_void_p * len(params))(*(ctypes.addressof(p) for p in params))
        t = time.monotonic()
        call("cuLaunchKernel", fn, (N + BLOCK - 1) // BLOCK, 1, 1, BLOCK, 1, 1, 0, None, ptrs, None)
        call("cuCtxSynchronize")
        out["timings_ms"][label] = round((time.monotonic() - t) * 1000, 2)

    # Device memory: copy in, run, copy out.
    dx, dy = ctypes.c_uint64(), ctypes.c_uint64()
    try:
        call("cuMemAlloc_v2", ctypes.byref(dx), size)
        call("cuMemAlloc_v2", ctypes.byref(dy), size)
        call("cuMemcpyHtoD_v2", dx, hx, size)
        call("cuMemcpyHtoD_v2", dy, hy, size)
        saxpy(dx, dy, "kernel")
        res = ctypes.create_string_buffer(nbytes)
        call("cuMemcpyDtoH_v2", res, dy, size)
        out["device_memory_mismatches"] = mismatches(res.raw)
    finally:
        for p in (dx, dy):
            if p.value:
                cu.cuMemFree_v2(p)

    # Managed memory: the host writes and reads it directly; pages migrate through nvidia-uvm.
    mx, my = ctypes.c_uint64(), ctypes.c_uint64()
    try:
        call("cuMemAllocManaged", ctypes.byref(mx), size, ctypes.c_uint(ATTACH_GLOBAL), label="managed_alloc")
        call("cuMemAllocManaged", ctypes.byref(my), size, ctypes.c_uint(ATTACH_GLOBAL))
        ctypes.memmove(mx.value, hx, nbytes)
        ctypes.memmove(my.value, hy, nbytes)
        saxpy(mx, my, "managed_kernel")
        out["managed_memory_mismatches"] = mismatches(ctypes.string_at(my.value, nbytes))
    finally:
        for p in (mx, my):
            if p.value:
                cu.cuMemFree_v2(p)


if __name__ == "__main__":
    result = {"ok": False}
    try:
        main(result)
    except CudaError:
        pass
    except Exception as e:  # reported, not raised: the probe reads this one line
        result["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)
