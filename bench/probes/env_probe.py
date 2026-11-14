#!/usr/bin/env python3
"""Stage 1 environment probe (BBX-ARCH-1.1 §8.11 `doctor --storage`, done by hand).

Throwaway probe, not product code. Writes bench/results/<date>-env-probe.json.
Every number below is measured in this process/container; nothing is copied from the plan.
"""
import json, mmap, os, platform, shutil, signal, statistics, subprocess, sys, tempfile, time

RESULTS = {"probe": "env", "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "python": sys.version, "executable": sys.executable, "data": {}, "notes": []}

def pct(xs, p):
    xs = sorted(xs)
    if not xs: return None
    k = min(len(xs) - 1, max(0, int(round((p / 100) * (len(xs) - 1)))))
    return xs[k]

def summarize(xs, unit):
    return {"n": len(xs), "unit": unit,
            "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99),
            "mean": round(statistics.fmean(xs), 2) if xs else None,
            "min": min(xs) if xs else None, "max": max(xs) if xs else None}

D = RESULTS["data"]

# ---------- 1. host ----------------------------------------------------------
D["host"] = {
    "system": platform.system(), "release": platform.release(), "version": platform.version(),
    "machine": platform.machine(), "processor": platform.processor() or None,
    "cpu_count_logical": os.cpu_count(),
    "page_size": mmap.PAGESIZE,
}
try:
    D["host"]["cpu_model"] = next(l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name"))
except Exception:
    D["host"]["cpu_model"] = None
try:
    mem = [l for l in open("/proc/meminfo") if l.startswith("MemTotal")]
    D["host"]["mem_total_kib"] = int(mem[0].split()[1]) if mem else None
except Exception:
    D["host"]["mem_total_kib"] = None
for cg in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
    try:
        D["host"]["cgroup_memory_limit_bytes"] = int(open(cg).read().strip())
        D["host"]["cgroup_path"] = cg
        break
    except Exception:
        continue

# ---------- 2. filesystem types ---------------------------------------------
def fs_of(path):
    rp = os.path.realpath(path)
    best, bestlen = None, -1
    try:
        for line in open("/proc/self/mountinfo"):
            left, right = line.split(" - ", 1)
            mp = left.split()[4].replace("\\040", " ")
            fstype = right.split()[0]
            if (rp == mp or rp.startswith(mp.rstrip("/") + "/")) and len(mp) > bestlen:
                best, bestlen = fstype, len(mp)
    except Exception:
        pass
    return best or "unknown"

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = os.path.expanduser("~")
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "fuse.sshfs", "fuse.rclone"}
D["filesystems"] = {
    "repo_path": REPO, "repo_fstype": fs_of(REPO),
    "home_path": HOME, "home_fstype": fs_of(HOME),
    "tmp_fstype": fs_of(tempfile.gettempdir()),
    "network_fs_denylist": sorted(NETWORK_FS),
    "repo_is_network": fs_of(REPO) in NETWORK_FS,
    "home_is_network": fs_of(HOME) in NETWORK_FS,
}
try:
    st = os.statvfs(REPO)
    D["filesystems"]["repo_free_bytes"] = st.f_bavail * st.f_frsize
    D["filesystems"]["repo_total_bytes"] = st.f_blocks * st.f_frsize
except Exception:
    pass

# ---------- 3. capability matrix --------------------------------------------
fh = tempfile.NamedTemporaryFile(dir=REPO, delete=False)
fh.close()
cap = {}
for name, fn in {
    "mmap_module": lambda: mmap.mmap,
    "os_posix_fallocate": lambda: os.posix_fallocate,
    "os_fdatasync": lambda: os.fdatasync,
    "os_fsync": lambda: os.fsync,
    "os_replace": lambda: os.replace,
    "faulthandler_register": lambda: __import__("faulthandler").register,
    "signal_SIGUSR1": lambda: signal.SIGUSR1,
    "os_register_at_fork": lambda: os.register_at_fork,
    "mmap_MAP_SHARED": lambda: mmap.MAP_SHARED,
}.items():
    try:
        fn(); cap[name] = True
    except Exception as e:
        cap[name] = f"unavailable: {type(e).__name__}"
D["capabilities"] = cap

# faulthandler round-trip (CH-09 stall stack dump)
try:
    import faulthandler
    tb = fh.name + ".tb"
    with open(tb, "w") as f:
        faulthandler.register(signal.SIGUSR1, file=f, all_threads=True)
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.05)
    cap["faulthandler_SIGUSR1_roundtrip"] = os.path.getsize(tb) > 0
    os.unlink(tb)
except Exception as e:
    cap["faulthandler_SIGUSR1_roundtrip"] = f"failed: {type(e).__name__}: {e}"

# ---------- 4. fdatasync latency (200x on a 1 MiB file) ---------------------
SYNC_N = 200
fd = os.open(fh.name, os.O_RDWR)
os.write(fd, b"\0" * (1 << 20))
os.fsync(fd)
sync_ns = []
for i in range(SYNC_N):
    os.pwrite(fd, b"x" * 4096, (i * 4096) % (1 << 19))
    t0 = time.perf_counter_ns()
    os.fdatasync(fd)
    sync_ns.append(time.perf_counter_ns() - t0)
D["fdatasync"] = summarize([n / 1000 for n in sync_ns], "us") | {"file_bytes": 1 << 20, "n_iterations": SYNC_N, "command": "pwrite 4 KiB then os.fdatasync, 200 iterations"}
os.close(fd)

# ---------- 5. os.replace atomicity (500x, concurrent reader) ---------------
import threading
dst = fh.name + ".replace-target"
open(dst, "w").write("old")
stop = threading.Event()
seen = {"missing": 0, "torn": 0, "ok": 0}
def reader():
    while not stop.is_set():
        try:
            v = open(dst).read()
        except FileNotFoundError:
            seen["missing"] += 1
            continue
        if v in ("old", "new"):
            seen["ok"] += 1
        else:
            seen["torn"] += 1
th = threading.Thread(target=reader, daemon=True); th.start()
for i in range(500):
    tmp = fh.name + ".tmp"
    with open(tmp, "w") as f:
        f.write("new" if i % 2 else "old"); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, dst)   # the only way the target changes: no in-place truncation
stop.set(); th.join(timeout=2)
D["os_replace"] = {"iterations": 500, "reader_observations": seen,
                   "command": "write tmp + fsync + os.replace while a reader thread reads the target"}
for p in (dst, fh.name):
    try: os.unlink(p)
    except FileNotFoundError: pass

# ---------- 6. mmap cross-process visibility --------------------------------
mp_path = os.path.join(REPO, ".probe-mmap.bin")
SZ = 1 << 20
with open(mp_path, "wb") as f:
    f.write(b"\0" * SZ)
mm = mmap.mmap(os.open(mp_path, os.O_RDWR), SZ, mmap.MAP_SHARED)
nonce = str(time.time_ns()).encode()[:16].ljust(16, b"0")
mm[0:16] = nonce
mm.flush()
child = subprocess.run([sys.executable, "-c",
    "import mmap,sys;f=open(sys.argv[1],'r+b');m=mmap.mmap(f.fileno(),%d);print(m[0:16].decode());m.close()" % SZ,
    mp_path], capture_output=True, text=True, timeout=30)
D["mmap_cross_process"] = {
    "parent_wrote": nonce.decode(), "child_read": child.stdout.strip(),
    "visible": child.stdout.strip() == nonce.decode(), "stderr": child.stderr[-300:] if child.stderr else "",
}

# ---------- 7. preallocation vs sparse (CH-10 / F-evidence) -----------------
pre = os.path.join(REPO, ".probe-prealloc.bin")
spa = os.path.join(REPO, ".probe-sparse.bin")
S16 = 16 << 20
t0 = time.perf_counter_ns()
fd = os.open(pre, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
if hasattr(os, "posix_fallocate"):
    os.posix_fallocate(fd, 0, S16); method = "posix_fallocate"
else:
    os.write(fd, b"\0" * S16); method = "write-zeros"
os.close(fd)
fallocate_ns = time.perf_counter_ns() - t0
with open(spa, "wb") as f:
    f.truncate(S16)
def du_kib(p):
    return int(subprocess.run(["du", "-k", p], capture_output=True, text=True).stdout.split()[0])
D["preallocation"] = {"size_bytes": S16, "method": method,
                      "fallocate_16MiB_ms": round(fallocate_ns / 1e6, 2),
                      "du_kib_preallocated": du_kib(pre), "du_kib_sparse_truncate": du_kib(spa),
                      "verdict": "preallocation allocates blocks; truncate() leaves a hole (the SIGBUS hazard class)"}
for p in (pre, spa, mp_path):
    try: os.unlink(p)
    except FileNotFoundError: pass

# ---------- 8. two costs that feed the Stage 2 estimate ---------------------
import hashlib
buf = os.urandom(1024)
t = []
for _ in range(20000):
    t0 = time.perf_counter_ns(); hashlib.blake2b(buf, digest_size=32).digest(); t.append(time.perf_counter_ns() - t0)
D["blake2b_1KiB"] = summarize([x / 1000 for x in t], "us")
big = os.urandom(1 << 20)
t = []
for _ in range(200):
    t0 = time.perf_counter_ns(); hashlib.blake2b(big, digest_size=32).digest(); t.append(time.perf_counter_ns() - t0)
D["blake2b_1MiB"] = summarize([x / 1e6 for x in t], "ms")

# ---------- 9. python versions available ------------------------------------
vers = {}
for v in ("3.10", "3.11", "3.12", "3.13", "3.14"):
    p = shutil.which("python" + v)
    if p:
        r = subprocess.run([p, "-c", "import sys;print(sys.version.split()[0])"], capture_output=True, text=True)
        vers[v] = r.stdout.strip() or "?"
D["pythons_found"] = vers
D["pythons_requested"] = ["3.10", "3.11", "3.12", "3.13", "3.14"]

out = os.path.join(REPO, "bench", "results", time.strftime("%Y-%m-%d") + "-env-probe.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump(RESULTS, f, indent=2, sort_keys=True)
print(json.dumps({k: RESULTS["data"][k] for k in
                  ("host", "filesystems", "capabilities", "fdatasync", "os_replace",
                   "mmap_cross_process", "preallocation", "blake2b_1KiB", "pythons_found")}, indent=2))
print("\nwritten:", out)
