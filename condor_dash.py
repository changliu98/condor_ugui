#!/usr/bin/env python3
"""
condor_dash — an interactive terminal dashboard for HTCondor users.

Overview shows, at a glance:
  * your running/idle/held jobs (where they run, CPU/GPU, runtime)
  * your user priority and your rank among active users
  * how crowded the pool is (CPU cores and GPUs in use)
  * how many GPUs are free and which nodes have them
  * who is using how many CPUs/GPUs right now

It is navigable: select a block and press Enter to expand it into a
full, scrollable detail view with much more information.

Pure standard-library Python — no pip install needed. Works over SSH.
It shells out to condor_q, condor_status and condor_userprio.

Keys (live mode):
    ↑/↓ or j/k or 1-6   select a block
    Enter / →            expand the selected block
    Esc / ← / Backspace  back to the overview
    (in a detail view)   ↑/↓ scroll, PgUp/PgDn, g/G top/bottom
    r                    refresh now
    q                    quit

Usage:
    ./condor_dash.py                 # interactive live dashboard
    ./condor_dash.py --once          # print one snapshot and exit
    ./condor_dash.py --interval 10   # live, custom refresh seconds
    ./condor_dash.py --user someone  # inspect another user
    ./condor_dash.py --no-color      # plain text
"""

import argparse
import getpass
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Terminal / ANSI helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
USE_COLOR = True

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m", "rev": "\033[7m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m", "white": "\033[37m",
    "bgreen": "\033[92m", "byellow": "\033[93m", "bred": "\033[91m",
    "bcyan": "\033[96m", "bmagenta": "\033[95m",
}


def paint(text, *codes):
    if not USE_COLOR or not codes:
        return text
    return "".join(C[c] for c in codes) + text + C["reset"]


def vis_len(s):
    return len(_ANSI_RE.sub("", s))


def fit(s, width):
    """Truncate (with an ellipsis) or right-pad a *plain* string to `width`."""
    if width <= 0:
        return ""
    n = len(s)
    if n == width:
        return s
    if n < width:
        return s + " " * (width - n)
    if width == 1:
        return "…"
    return s[: width - 1] + "…"


def bar(frac, width):
    frac = max(0.0, min(1.0, frac))
    fill = min(width, max(0, int(round(frac * width))))
    color = "bgreen" if frac < 0.70 else ("byellow" if frac < 0.90 else "bred")
    return paint("█" * fill, color) + paint("░" * (width - fill), "dim")


# ---------------------------------------------------------------------------
# Box / panel rendering
# ---------------------------------------------------------------------------

def panel(title, lines, width, focused=False, hint=""):
    """Render a titled box. Borders brighten and a hint appears when focused."""
    border_codes = ("byellow", "bold") if focused else ()
    title_codes = ("bold", "byellow") if focused else ("bold", "bcyan")

    def b(s):
        return paint(s, *border_codes) if border_codes else s

    head = paint(("▸ " if focused else "") + title, *title_codes)
    hint_txt = paint(hint, "dim") if (hint and focused) else ""
    if hint_txt:
        dashes = max(0, width - 6 - vis_len(head) - vis_len(hint_txt))
        top = b("╭─ ") + head + " " + b("─" * dashes) + " " + hint_txt + b("╮")
    else:
        dashes = max(0, width - 5 - vis_len(head))
        top = b("╭─ ") + head + " " + b("─" * dashes) + b("╮")

    inner = width - 4
    out = [top]
    for ln in lines:
        pad = max(0, inner - vis_len(ln))
        out.append(b("│ ") + ln + " " * pad + b(" │"))
    out.append(b("╰" + "─" * (width - 2) + "╯"))
    return out


# ---------------------------------------------------------------------------
# Running condor commands
# ---------------------------------------------------------------------------

def run(args, timeout=30):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, text=True)
    except FileNotFoundError:
        return None, "%s not found" % args[0]
    except subprocess.TimeoutExpired:
        return None, "%s timed out" % args[0]
    except Exception as exc:  # pragma: no cover - defensive
        return None, str(exc)
    if p.returncode != 0 and not p.stdout.strip():
        msg = (p.stderr or "").strip().splitlines()
        return None, msg[0] if msg else "exit %d" % p.returncode
    return p.stdout, None


JOB_STATUS = {
    1: ("IDLE", "yellow"), 2: ("RUN", "bgreen"), 3: ("RM", "dim"),
    4: ("DONE", "cyan"), 5: ("HELD", "bred"), 6: ("XFER", "cyan"),
    7: ("SUSP", "magenta"),
}


def fmt_dur(secs):
    if secs is None or secs < 0:
        return "-"
    secs = int(secs)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, s = divmod(secs, 60)
    if d:
        return "%dd%dh" % (d, h)
    if h:
        return "%dh%02dm" % (h, m)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def fmt_when(ts):
    if not ts:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(ts))
    except (ValueError, OSError):
        return "-"


def fmt_mem(mb):
    if not mb:
        return "-"
    if mb >= 1024:
        return "%.0fG" % (mb / 1024.0)
    return "%dM" % mb


def short_node(remote):
    """Strip the slot prefix and the common -10-5- subnet octets."""
    if not remote:
        return "-"
    host = remote.split("@", 1)[-1]
    return re.sub(r"-10-5-(\d+-\d+)$", r"-\1", host)


def _num(v, default=0):
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Data collectors
# ---------------------------------------------------------------------------

def collect_my_jobs(user):
    out, err = run([
        "condor_q", user, "-json", "-attributes",
        "ClusterId,ProcId,JobStatus,RequestCpus,RequestGpus,RequestMemory,"
        "RemoteHost,JobStartDate,QDate,ServerTime,Cmd,HoldReason",
    ], timeout=30)
    if err:
        return [], {}, err
    out = (out or "").strip()
    summ = {"running": 0, "idle": 0, "held": 0, "cpu": 0, "gpu": 0}
    if not out:
        return [], summ, None
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        return [], {}, "parse error: %s" % exc

    now = max((j.get("ServerTime", 0) for j in raw), default=0)
    jobs = []
    for j in raw:
        st = _num(j.get("JobStatus"))
        cpu = _num(j.get("RequestCpus"))
        gpu = _num(j.get("RequestGpus"))
        srv = j.get("ServerTime") or now
        if st == 2:
            summ["running"] += 1
            summ["cpu"] += cpu
            summ["gpu"] += gpu
            when = (srv - j["JobStartDate"]) if (srv and j.get("JobStartDate")) else None
        else:
            if st == 1:
                summ["idle"] += 1
            elif st == 5:
                summ["held"] += 1
            when = (srv - j["QDate"]) if (srv and j.get("QDate")) else None
        cmd = str(j.get("Cmd", "") or "")
        jobs.append({
            "id": "%s.%s" % (j.get("ClusterId", "?"), j.get("ProcId", 0)),
            "status": st, "cpu": cpu, "gpu": gpu,
            "mem": _num(j.get("RequestMemory")),
            "node": short_node(j.get("RemoteHost")) if st == 2 else "-",
            "host_full": (j.get("RemoteHost") or "-") if st == 2 else "-",
            "dur": when, "submit": j.get("QDate"),
            "cmd": os.path.basename(cmd), "cmd_full": cmd,
            "hold": str(j.get("HoldReason", "") or ""),
        })
    jobs.sort(key=lambda x: (x["status"] != 2, x["id"]))
    return jobs, summ, None


def collect_queue_and_users():
    out, err = run(["condor_q", "-all", "-af:t", "Owner", "JobStatus",
                    "RequestCpus", "RequestGpus"], timeout=60)
    if err:
        return {}, {}, err
    users = {}
    q = {"running": 0, "idle": 0, "held": 0, "other": 0, "total": 0}
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        owner, st = parts[0], _num(parts[1], -1)
        if st < 0:
            continue
        q["total"] += 1
        u = users.setdefault(owner, {"jobs": 0, "cpu": 0, "gpu": 0, "idle": 0})
        if st == 2:
            q["running"] += 1
            u["jobs"] += 1
            u["cpu"] += _num(parts[2])
            u["gpu"] += _num(parts[3])
        elif st == 1:
            q["idle"] += 1
            u["idle"] += 1
        elif st == 5:
            q["held"] += 1
        else:
            q["other"] += 1
    users = {k: v for k, v in users.items() if v["jobs"] or v["idle"]}
    return users, q, None


def collect_cluster():
    """One full-pool scan -> (aggregates, open_slots, capacity, err).

    open_slots: unclaimed slots available now -> (machine, fcpu, fmem, fgpu, model, vram)
    capacity:   one row per machine (its total resources) -> (machine, tcpu, tmem, tgpu, model, vram)
    """
    out, err = run([
        "condor_status", "-af:t", "Machine", "State", "Cpus", "Memory", "GPUs",
        "TotalSlotCpus", "TotalSlotMemory", "TotalSlotGpus",
        "CUDADeviceName", "CUDAGlobalMemoryMb",
    ], timeout=90)
    if err:
        return {}, [], [], err
    d = dict(cores_total=0, cores_used=0, gpu_total=0, gpu_used=0, gpu_free=0,
             slots=0, claimed=0, unclaimed=0)
    open_slots = []
    cap = {}
    for line in (out or "").splitlines():
        p = line.split("\t")
        if len(p) < 5:
            continue
        machine, state, cpus, mem, gpus = p[0], p[1], p[2], p[3], p[4]
        model = short_gpu(p[8]) if len(p) > 8 else "-"
        vram = _num(p[9]) if len(p) > 9 else 0
        c = _num(cpus)
        d["cores_total"] += c
        d["slots"] += 1
        claimed = (state == "Claimed")
        gnum = _num(gpus) if gpus not in ("undefined", "", None) else 0
        if claimed:
            d["cores_used"] += c
            d["claimed"] += 1
            d["gpu_used"] += gnum
        else:
            d["unclaimed"] += 1
            d["gpu_free"] += gnum
            open_slots.append((machine, c, _num(mem), gnum, model, vram))
        if gpus not in ("undefined", "", None):
            d["gpu_total"] += gnum
        if machine not in cap and len(p) > 7:
            cap[machine] = (machine, _num(p[5]), _num(p[6]), _num(p[7]), model, vram)
    return d, open_slots, list(cap.values()), None


def short_gpu(name):
    """'NVIDIA A100 80GB PCIe' -> 'A100 80GB PCIe'; '' -> '-'."""
    if not name or name == "undefined":
        return "-"
    return re.sub(r"^(NVIDIA|Tesla)\s+", "", name).strip()


# x86 CPUID (family, model) -> compact microarchitecture label. Condor does not
# advertise a CPU model string on this pool, so we decode the microarch instead.
_CPU_UARCH = {
    (6, 63): "Xeon Haswell", (6, 79): "Xeon Broadwell",
    (6, 85): "Xeon Skylake-SP", (6, 106): "Xeon Ice Lake-SP",
    (6, 143): "Xeon SapphireRap", (6, 207): "Xeon EmeraldRap",
    (23, 1): "EPYC Naples Zen", (23, 49): "EPYC Rome Zen2",
    (23, 113): "AMD Zen2", (25, 1): "EPYC Milan Zen3",
    (25, 17): "EPYC Genoa Zen4", (26, 17): "EPYC Turin Zen5",
}


def cpu_model(family, model, microarch):
    """Best-effort human-readable CPU label from CPUID family/model."""
    name = _CPU_UARCH.get((family, model))
    if name:
        return name
    vendor = "Intel" if family == 6 else ("AMD" if family >= 16 else "")
    if microarch and microarch != "undefined":
        return ("%s %s" % (vendor, microarch)).strip()
    if family:
        return ("%s fam%d/m%d" % (vendor, family, model)).strip()
    return "-"


def collect_gpu_nodes():
    """All GPU nodes (partitionable parents):
    (node, free, total, free_cpus, free_mem_mb, gpu_model, gpu_vram_mb, cpu_model)."""
    out, err = run([
        "condor_status", "-constraint", "PartitionableSlot=?=true && TotalGpus > 0",
        "-af:t", "Machine", "TotalGpus", "GPUs", "Cpus", "Memory",
        "CUDADeviceName", "CUDAGlobalMemoryMb", "CpuFamily", "CpuModelNumber",
        "Microarch",
    ], timeout=60)
    if err:
        return [], err
    nodes = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        g = lambda i: parts[i] if len(parts) > i else ""
        model = short_gpu(g(5))
        vram = _num(g(6))
        cpu = cpu_model(_num(g(7)), _num(g(8)), g(9))
        nodes.append((short_node(parts[0]), _num(parts[2]), _num(parts[1]),
                      _num(parts[3]), _num(parts[4]), model, vram, cpu))
    nodes.sort(key=lambda x: (-x[1], x[0]))
    return nodes, None


# --- queued (idle) job analysis ------------------------------------------

_RE_GPU_MODEL = re.compile(r'regexp\(\s*"([^"]+)"\s*,\s*(?:TARGET\.)?CUDADeviceName', re.I)
_RE_GPU_VRAM = re.compile(r'CUDAGlobalMemoryMb\s*>=?\s*(\d+)', re.I)
_RE_HOST = re.compile(r'regexp\(\s*"([^"]+)"\s*,\s*(?:TARGET\.)?(?:Machine|Name)\b', re.I)


def parse_requirements(req):
    """Pull the constraints we can match on out of a raw Requirements string."""
    req = req or ""
    m = _RE_GPU_MODEL.search(req)
    v = _RE_GPU_VRAM.search(req)
    h = _RE_HOST.search(req)
    return {"model": m.group(1) if m else None,
            "vram": int(v.group(1)) if v else 0,
            "host": h.group(1) if h else None}


def collect_idle_jobs():
    """Pool-wide idle jobs with their resource requests and parsed Requirements.

    Two queries: numeric fields *evaluated* (-af:t) for correct numbers, and the
    Requirements expression *raw* (-af:tr); merged by job id."""
    base, err = run([
        "condor_q", "-all", "-constraint", "JobStatus==1", "-af:t",
        "ClusterId", "ProcId", "Owner", "RequestCpus", "RequestGpus",
        "RequestMemory", "QDate", "ServerTime",
    ], timeout=60)
    if err:
        return [], err
    reqs, _e = run([
        "condor_q", "-all", "-constraint", "JobStatus==1", "-af:tr",
        "ClusterId", "ProcId", "Requirements",
    ], timeout=60)
    rmap = {}
    for line in (reqs or "").splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            rmap["%s.%s" % (p[0], p[1])] = p[2]
    jobs = []
    for line in (base or "").splitlines():
        p = line.split("\t")
        if len(p) < 8:
            continue
        jid = "%s.%s" % (p[0], p[1])
        srv, qd = _num(p[7]), _num(p[6])
        req = parse_requirements(rmap.get(jid, ""))
        jobs.append({
            "id": jid, "owner": p[2],
            "cpu": _num(p[3]), "gpu": _num(p[4]), "mem": _num(p[5]),
            "wait": (srv - qd) if (srv and qd) else None,
            "model": req["model"], "vram": req["vram"], "host": req["host"],
        })
    return jobs, None


def analyze_idle(job, gpu_nodes, open_slots, cap):
    """Estimate match likelihood -> (label, color, reason).

    Honest heuristic from current free resources + each job's Requirements.
    It does NOT model user priority, preemption, disk, or rank."""
    rc, rg, rm = job["cpu"], job["gpu"], job["mem"]
    model, vram, host = job["model"], job["vram"], job["host"]

    def host_ok(name):
        return (not host) or (host.lower() in name.lower())

    if rg > 0:  # GPU job — judge against GPU nodes (free + total + model + vram)
        able = [n for n in gpu_nodes if n[2] >= rg and host_ok(n[0])
                and (not model or model.lower() in n[5].lower())
                and (not vram or n[6] >= vram)]
        free = [n for n in able if n[1] >= rg and n[3] >= rc and n[4] >= rm]
        if free:
            return ("LIKELY", "bgreen", "%d node%s free now" %
                    (len(free), "" if len(free) == 1 else "s"))
        if able:
            tag = model or ("≥%s" % fmt_mem(vram) if vram else "GPU")
            return ("WAITING", "byellow", "%d %s node%s busy" %
                    (len(able), tag, "" if len(able) == 1 else "s"))
        if model:
            return ("BLOCKED", "bred", "no %s GPUs in pool" % model)
        if vram:
            return ("BLOCKED", "bred", "no GPU has ≥%s VRAM" % fmt_mem(vram))
        return ("BLOCKED", "bred", "no node has %d GPUs" % rg)

    # CPU/general job — judge against unclaimed slots (now) and machine capacity
    now = sum(1 for s in open_slots if s[1] >= rc and s[2] >= rm and host_ok(s[0]))
    if now:
        return ("LIKELY", "bgreen", "%d slot%s free now" %
                (now, "" if now == 1 else "s"))
    able = sum(1 for m in cap if m[1] >= rc and m[2] >= rm and host_ok(m[0]))
    if able:
        return ("WAITING", "byellow", "fits %d machine%s (busy)" %
                (able, "" if able == 1 else "s"))
    if host:
        return ("BLOCKED", "bred", 'no "%s" host fits %s' % (host, fmt_mem(rm)))
    return ("BLOCKED", "bred", "needs %s mem / %d cpu" % (fmt_mem(rm), rc))


def collect_priority(user):
    out, err = run(["condor_userprio"], timeout=30)
    if err:
        return [], None, None, None, err
    rows = []
    seen_sep = False
    for line in (out or "").splitlines():
        stripped = line.strip()
        # the header/body separator is a row of dash-groups joined by spaces
        if "---" in stripped and set(stripped) <= {"-", " "}:
            seen_sep = True
            continue
        if not seen_sep:
            continue
        parts = line.split()
        if len(parts) < 5 or "@" not in parts[0]:
            continue
        try:
            row = {"user": parts[0].split("@", 1)[0], "eff": float(parts[1]),
                   "factor": float(parts[2]), "inuse": int(parts[3]),
                   "usage": float(parts[4])}
        except ValueError:
            continue
        rows.append(row)
    rows.sort(key=lambda r: r["eff"])  # lower effective priority == served first
    me = rank = None
    for i, r in enumerate(rows, 1):
        if r["user"] == user:
            me, rank = r, i
            break
    return rows, me, rank, len(rows), None


ANALYZE_CAP = 60  # bound match analysis cost when the queue is huge


def gather(user):
    """Run every collector once; return a single cached state dict."""
    jobs, jsumm, jerr = collect_my_jobs(user)
    users, queue, uerr = collect_queue_and_users()
    cluster, open_slots, cap, cerr = collect_cluster()
    gpu_nodes, gerr = collect_gpu_nodes()
    idle, ierr = collect_idle_jobs()
    prows, pme, prank, ptot, perr = collect_priority(user)

    # estimate match likelihood for each idle job (user's first, then longest waiting)
    idle.sort(key=lambda j: (j["owner"] != user, -(j["wait"] or 0)))
    for j in idle[:ANALYZE_CAP]:
        j["match"] = analyze_idle(j, gpu_nodes, open_slots, cap)
    for j in idle[ANALYZE_CAP:]:
        j["match"] = None

    return {
        "jobs": jobs, "jsumm": jsumm, "jerr": jerr,
        "users": users, "queue": queue, "uerr": uerr,
        "cluster": cluster, "open_slots": open_slots, "cap": cap, "cerr": cerr,
        "gpu_nodes": gpu_nodes, "gerr": gerr,
        "idle": idle, "ierr": ierr,
        "prows": prows, "pme": pme, "prank": prank, "ptot": ptot, "perr": perr,
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# Overview panels (compact)
# ---------------------------------------------------------------------------

def panel_jobs(st, user, width):
    body = []
    if st["jerr"]:
        return [paint("error: " + st["jerr"], "red")]
    jobs, summ = st["jobs"], st["jsumm"]
    if not jobs:
        return [paint("No jobs in the queue.", "dim")]
    hdr = " %-11s %-5s %4s %3s  %-15s %8s  %s" % (
        "JOB", "STAT", "CPU", "GPU", "NODE", "TIME", "CMD")
    body.append(paint(fit(hdr, width - 4), "dim"))
    for j in jobs[:10]:
        label = JOB_STATUS.get(j["status"], ("?", "white"))[0]
        row = fit(" %-11s %-5s %3dc %2dg  %-15s %8s  %s" % (
            j["id"], label, j["cpu"], j["gpu"], j["node"], fmt_dur(j["dur"]),
            j["cmd"]), width - 4)
        if j["status"] == 5:
            row = paint(row, "bred")
        elif j["status"] == 1:
            row = paint(row, "yellow")
        body.append(row)
    if len(jobs) > 10:
        body.append(paint("  … and %d more (Enter to see all)" % (len(jobs) - 10), "dim"))
    return body


def panel_jobs_title(st):
    s = st["jsumm"]
    if not s:
        return "MY JOBS"
    return "MY JOBS   %d running · %d idle · %d held   %d CPU · %d GPU in use" % (
        s.get("running", 0), s.get("idle", 0), s.get("held", 0),
        s.get("cpu", 0), s.get("gpu", 0))


def panel_priority(st, user, width):
    if st["perr"]:
        return [paint("error: " + st["perr"], "red")]
    me, rank, total = st["pme"], st["prank"], st["ptot"]
    if me is None:
        return [paint("Not currently an active submitter (no recent usage).", "dim")]
    if rank <= max(1, total // 3):
        rc = "bgreen"
    elif rank <= 2 * total // 3:
        rc = "byellow"
    else:
        rc = "bred"
    return [
        " effective priority %s   (lower = served first)   %s" % (
            paint("%.1f" % me["eff"], "bold"),
            paint("rank %d of %d active users" % (rank, total), rc)),
        paint(" priority factor %.0f · resources in use %d · weighted usage %.0f hrs" % (
            me["factor"], me["inuse"], me["usage"]), "dim"),
    ]


def panel_cluster(st, user, width):
    if st["cerr"]:
        return [paint("error: " + st["cerr"], "red")]
    cl, q = st["cluster"], st["queue"]
    ct, cu = cl["cores_total"], cl["cores_used"]
    gt, gu, gf = cl["gpu_total"], cl["gpu_used"], cl["gpu_free"]
    cfrac = (cu / ct) if ct else 0
    gfrac = (gu / gt) if gt else 0
    blen = max(10, width - 42)
    body = [
        " CPU cores  %s %3d%%   %s used / %s" % (
            bar(cfrac, blen), int(cfrac * 100), paint("%d" % cu, "bold"), ct),
        " GPUs       %s %3d%%   %s used / %s" % (
            bar(gfrac, blen), int(gfrac * 100), paint("%d" % gu, "bold"), gt),
    ]
    free = "   %s   %s" % (
        paint("FREE GPUs: %d" % gf, "bold", "bgreen" if gf else "bred"),
        paint("free CPU cores: %d" % (ct - cu), "bold"))
    if not st["uerr"]:
        free += paint("      queue: %d run · %d idle · %d held" % (
            q["running"], q["idle"], q["held"]), "dim")
    body.append(free)
    return body


def panel_freegpu(st, user, width):
    if st["gerr"]:
        return [paint("error: " + st["gerr"], "red")]
    free = [n for n in st["gpu_nodes"] if n[1] > 0]
    if not free:
        return [paint("No GPUs free right now.", "bred")]
    cells = [paint("%-15s %d/%d gpu %dc free" % (n[0], n[1], n[2], n[3]), "bgreen")
             for n in free[:10]]
    half = (len(cells) + 1) // 2
    colw = (width - 5) // 2
    body = []
    for i in range(half):
        l = cells[i]
        r = cells[i + half] if i + half < len(cells) else ""
        body.append(" " + l + " " * max(0, colw - vis_len(l)) + " " + r)
    if len(free) > 10:
        body.append(paint("  … %d more nodes with free GPUs (Enter to see all)" % (
            len(free) - 10), "dim"))
    return body


def panel_users(st, user, width):
    if st["uerr"]:
        return [paint("error: " + st["uerr"], "red")]
    users = st["users"]
    runners = {k: v for k, v in users.items() if v["jobs"]}
    if not runners:
        return [paint("No running jobs.", "dim")]
    ranked = sorted(runners.items(), key=lambda kv: (-kv[1]["gpu"], -kv[1]["cpu"]))
    body = [paint(fit(" %-14s %6s %8s %6s" % ("USER", "JOBS", "CPU", "GPU"),
                      width - 4), "dim")]
    for owner, u in ranked[:8]:
        txt = " %-14s %6d %8d %6d" % (owner, u["jobs"], u["cpu"], u["gpu"])
        if owner == user:
            body.append(paint(fit(txt + "   ◂ you", width - 4), "bold", "bcyan"))
        else:
            body.append(fit(txt, width - 4))
    if len(ranked) > 8:
        body.append(paint("  … %d more (Enter to see all)" % (len(ranked) - 8), "dim"))
    return body


def _need_str(j):
    """Compact resource-requirement string for a queued job."""
    s = "%dc" % j["cpu"]
    if j["gpu"]:
        s += " %dg" % j["gpu"]
        if j["model"]:
            s += " " + j["model"]
        elif j["vram"]:
            s += " ≥%s" % fmt_mem(j["vram"])
    if j["host"]:
        s += " @%s" % j["host"]
    s += " %s" % fmt_mem(j["mem"])
    return s


def _queued_row(j, user, inner):
    """One overview row: plain 'head' + colored, width-bounded match cell."""
    head = " %-11s %-10s %-19s %7s  " % (
        j["id"], j["owner"][:10], _need_str(j)[:19],
        fmt_dur(j["wait"]) if j["wait"] is not None else "-")
    label, color, reason = j.get("match") or ("?", "dim", "")
    avail = max(6, inner - len(head))
    if len(label) + 1 <= avail:
        cell = paint(label, "bold", color) + " " + paint(reason[:avail - len(label) - 1], "dim")
    else:
        cell = paint(label[:avail], "bold", color)
    return paint(head, "bold", "bcyan") + cell if j["owner"] == user else head + cell


def panel_queued(st, user, width):
    if st["ierr"]:
        return [paint("error: " + st["ierr"], "red")]
    idle = st["idle"]
    if not idle:
        return [paint("No queued (idle) jobs — the queue is clear.", "dim")]
    inner = width - 4
    body = [paint(fit(" %-11s %-10s %-19s %7s  %s" % (
        "JOB", "OWNER", "NEEDS", "WAIT", "WILL IT MATCH?"), inner), "dim")]
    for j in idle[:6]:
        body.append(_queued_row(j, user, inner))
    if len(idle) > 6:
        body.append(paint("  … %d more queued (Enter to see all)" % (len(idle) - 6), "dim"))
    return body


def panel_queued_title(st):
    idle = st.get("idle") or []
    if not idle:
        return "QUEUED JOBS"
    waiting = sum(1 for j in idle if j.get("match") and j["match"][0] != "LIKELY")
    blocked = sum(1 for j in idle if j.get("match") and j["match"][0] == "BLOCKED")
    extra = "   %d need resources" % waiting if waiting else "   all matchable"
    if blocked:
        extra += " · %d blocked" % blocked
    return "QUEUED JOBS   %d idle%s" % (len(idle), extra)


# ---------------------------------------------------------------------------
# Detail views (full, scrollable) — return a list of content lines
# ---------------------------------------------------------------------------

def detail_jobs(st, user, width):
    if st["jerr"]:
        return [paint("error: " + st["jerr"], "red")]
    jobs = st["jobs"]
    if not jobs:
        return [paint("No jobs in the queue.", "dim")]
    out = [paint(" %-11s %-5s %4s %3s %6s  %-20s %9s  %-12s %s" % (
        "JOB", "STAT", "CPU", "GPU", "MEM", "NODE", "RUNTIME", "SUBMITTED", "COMMAND"),
        "bold", "dim")]
    for j in jobs:
        label = JOB_STATUS.get(j["status"], ("?", "white"))[0]
        runtime = fmt_dur(j["dur"]) if j["status"] == 2 else (
            "wait " + fmt_dur(j["dur"]) if j["status"] == 1 else "-")
        node = j["node"] if j["status"] == 2 else "-"
        row = fit(" %-11s %-5s %3dc %2dg %6s  %-20s %9s  %-12s %s" % (
            j["id"], label, j["cpu"], j["gpu"], fmt_mem(j["mem"]), node,
            runtime, fmt_when(j["submit"]), j["cmd_full"]), width)
        if j["status"] == 5:
            row = paint(row, "bred")
        elif j["status"] == 1:
            row = paint(row, "yellow")
        out.append(row)
        if j["status"] == 5 and j["hold"]:
            out.append(paint(fit("       ↳ held: " + j["hold"], width), "dim", "red"))
    return out


def detail_priority(st, user, width):
    if st["perr"]:
        return [paint("error: " + st["perr"], "red")]
    rows = st["prows"]
    if not rows:
        return [paint("No active submitters reported.", "dim")]
    out = [
        paint(" Effective priority decides who is matched first — LOWER is better.", "dim"),
        paint(" Your share ∝ 1 / priority; priority grows with recent usage and"
              " your priority factor.", "dim"),
        "",
        paint(" %4s  %-16s %12s %9s %6s %14s" % (
            "RANK", "USER", "EFF.PRIORITY", "FACTOR", "INUSE", "USAGE(hrs)"),
            "bold", "dim"),
    ]
    for i, r in enumerate(rows, 1):
        txt = " %4d  %-16s %12.1f %9.0f %6d %14.0f" % (
            i, r["user"], r["eff"], r["factor"], r["inuse"], r["usage"])
        if r["user"] == user:
            out.append(paint(fit(txt + "   ◂ you", width), "bold", "bcyan"))
        else:
            out.append(fit(txt, width))
    return out


def detail_cluster(st, user, width):
    out = []
    if st["cerr"]:
        return [paint("error: " + st["cerr"], "red")]
    cl, q = st["cluster"], st["queue"]
    ct, cu = cl["cores_total"], cl["cores_used"]
    gt, gu, gf = cl["gpu_total"], cl["gpu_used"], cl["gpu_free"]
    cfrac = (cu / ct) if ct else 0
    gfrac = (gu / gt) if gt else 0
    blen = max(12, width - 48)
    out.append(paint(" RESOURCES", "bold", "bcyan"))
    out.append("   CPU cores  %s %3d%%   %d used · %d free · %d total" % (
        bar(cfrac, blen), int(cfrac * 100), cu, ct - cu, ct))
    out.append("   GPUs       %s %3d%%   %d used · %d free · %d total" % (
        bar(gfrac, blen), int(gfrac * 100), gu, gf, gt))
    out.append("   slots      %d claimed · %d unclaimed · %d total" % (
        cl["claimed"], cl["unclaimed"], cl["slots"]))
    out.append("")
    nodes = st["gpu_nodes"]
    if not st["gerr"] and nodes:
        fully = sum(1 for n in nodes if n[1] == n[2] and n[2] > 0)
        partial = sum(1 for n in nodes if 0 < n[1] < n[2])
        none_free = sum(1 for n in nodes if n[1] == 0)
        out.append(paint(" GPU NODES", "bold", "bcyan"))
        out.append("   %d GPU nodes: %s fully free · %s partially free · %s full" % (
            len(nodes), paint(str(fully), "bgreen"), paint(str(partial), "byellow"),
            paint(str(none_free), "bred")))
        out.append("   %s GPUs free across the pool" % paint(str(gf), "bold", "bgreen"))
        out.append("")
    if not st["uerr"]:
        runners = sum(1 for v in st["users"].values() if v["jobs"])
        out.append(paint(" QUEUE", "bold", "bcyan"))
        out.append("   %d jobs total: %s running · %s idle · %s held" % (
            q["total"], paint(str(q["running"]), "bgreen"),
            paint(str(q["idle"]), "yellow"), paint(str(q["held"]), "bred")))
        out.append("   %d users with running jobs" % runners)
    return out


def detail_freegpu(st, user, width):
    if st["gerr"]:
        return [paint("error: " + st["gerr"], "red")]
    free = [n for n in st["gpu_nodes"] if n[1] > 0]
    if not free:
        return [paint("No GPUs free right now.", "bred")]
    # free GPUs per model, most plentiful first
    by_model = {}
    for n in free:
        by_model[n[5]] = by_model.get(n[5], 0) + n[1]
    summary = " · ".join("%s ×%d" % (m, c) for m, c in
                         sorted(by_model.items(), key=lambda kv: -kv[1]))
    out = [
        paint(" %d nodes have free GPUs (%d GPUs free total)" % (
            len(free), sum(n[1] for n in free)), "bgreen"),
        paint(fit(" free by model:  " + summary, width), "dim"),
        "",
        paint(" %-18s %8s  %-15s %6s  %-16s %8s %8s" % (
            "NODE", "GPU free", "CUDA DEVICE", "VRAM", "CPU MODEL",
            "CPU free", "MEM free"), "bold", "dim"),
    ]
    for name, gfree, gtot, cpus, mem, model, vram, cpu in free:
        out.append(fit(" %-18s %8s  %-15s %6s  %-16s %7dc %8s" % (
            name, "%d/%d" % (gfree, gtot), model[:15], fmt_mem(vram),
            cpu[:16], cpus, fmt_mem(mem)), width))
    return out


def detail_users(st, user, width):
    if st["uerr"]:
        return [paint("error: " + st["uerr"], "red")]
    users = st["users"]
    if not users:
        return [paint("No jobs in the queue.", "dim")]
    ranked = sorted(users.items(), key=lambda kv: (-kv[1]["gpu"], -kv[1]["cpu"],
                                                    -kv[1]["idle"]))
    tcpu = sum(v["cpu"] for v in users.values())
    tgpu = sum(v["gpu"] for v in users.values())
    tjob = sum(v["jobs"] for v in users.values())
    tidle = sum(v["idle"] for v in users.values())
    out = [
        paint(" %d users in the queue · %d running jobs · %d CPU · %d GPU in use" % (
            len(users), tjob, tcpu, tgpu), "bold"),
        "",
        paint(" %4s  %-16s %8s %8s %6s %8s" % (
            "#", "USER", "RUN JOBS", "CPU", "GPU", "IDLE"), "bold", "dim"),
    ]
    for i, (owner, u) in enumerate(ranked, 1):
        txt = " %4d  %-16s %8d %8d %6d %8d" % (
            i, owner, u["jobs"], u["cpu"], u["gpu"], u["idle"])
        if owner == user:
            out.append(paint(fit(txt + "   ◂ you", width), "bold", "bcyan"))
        else:
            out.append(fit(txt, width))
    out.append("")
    out.append(paint(" %4s  %-16s %8d %8d %6d %8d" % (
        "", "TOTAL", tjob, tcpu, tgpu, tidle), "bold", "dim"))
    return out


def detail_queued(st, user, width):
    if st["ierr"]:
        return [paint("error: " + st["ierr"], "red")]
    idle = st["idle"]
    if not idle:
        return [paint("No queued (idle) jobs — the queue is clear.", "dim")]
    likely = sum(1 for j in idle if j.get("match") and j["match"][0] == "LIKELY")
    waiting = sum(1 for j in idle if j.get("match") and j["match"][0] == "WAITING")
    blocked = sum(1 for j in idle if j.get("match") and j["match"][0] == "BLOCKED")
    out = [
        paint(" %d queued (idle) jobs:  %s likely · %s waiting · %s blocked" % (
            len(idle), paint(str(likely), "bgreen"), paint(str(waiting), "byellow"),
            paint(str(blocked), "bred")), "bold"),
        paint(" Estimate from current free resources + each job's Requirements"
              " (CPU/GPU/VRAM/model/host).", "dim"),
        paint(" It ignores user priority, preemption, disk and rank — treat it as a guide.", "dim"),
        "",
        paint(" %-11s %-10s %4s %4s %-13s %7s %7s  %s" % (
            "JOB", "OWNER", "CPU", "GPU", "GPU/HOST REQ", "MEM", "WAIT",
            "WILL IT MATCH?"), "bold", "dim"),
    ]
    for j in idle:
        req = j["model"] or (("≥%s" % fmt_mem(j["vram"])) if j["vram"] else "")
        if j["host"]:
            req = (req + " @" + j["host"]).strip()
        head = " %-11s %-10s %4d %4s %-13s %7s %7s  " % (
            j["id"], j["owner"][:10], j["cpu"], (j["gpu"] or "-"),
            (req or "-")[:13], fmt_mem(j["mem"]),
            fmt_dur(j["wait"]) if j["wait"] is not None else "-")
        avail = max(8, width - len(head))
        label, color, reason = j.get("match") or ("(not analyzed)", "dim", "")
        if len(label) + 1 <= avail:
            cell = paint(label, "bold", color) + " " + paint(reason[:avail - len(label) - 1], "dim")
        else:
            cell = paint(label[:avail], "bold", color)
        row = paint(head, "bold", "bcyan") if j["owner"] == user else head
        out.append(row + cell)
    return out


# Panel registry: (key, overview-body-fn, detail-fn, static-title)
PANELS = [
    ("jobs", panel_jobs, detail_jobs, None),
    ("priority", panel_priority, detail_priority, "MY PRIORITY"),
    ("cluster", panel_cluster, detail_cluster, "CLUSTER LOAD"),
    ("freegpu", panel_freegpu, detail_freegpu, "FREE GPU NODES"),
    ("users", panel_users, detail_users, "TOP USERS"),
    ("queued", panel_queued, detail_queued, "QUEUED JOBS"),
]
PANEL_TITLES = {
    "jobs": "MY JOBS", "priority": "MY PRIORITY", "cluster": "CLUSTER LOAD",
    "freegpu": "FREE GPU NODES", "users": "TOP USERS (RUNNING)",
    "queued": "QUEUED JOBS — MATCH ESTIMATE",
}


# ---------------------------------------------------------------------------
# Frame composition
# ---------------------------------------------------------------------------

def render_overview(st, user, host, width, focus=-1, height=None):
    width = max(60, min(width, 110))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    left = paint(" CONDOR DASHBOARD", "bold", "bcyan")
    right = paint("%s@%s" % (user, host), "bold") + paint("  " + now, "dim")
    header = [left + " " * max(1, width - vis_len(left) - vis_len(right)) + right]
    header.append(paint("  ↑/↓ or 1-6 select · Enter expand · r refresh · q quit", "dim")
                  if focus >= 0 else "")
    header.append("")

    body_lines, spans = [], []  # spans[i] = (start, end) of panel i within body_lines
    for i, (key, body_fn, _detail, title) in enumerate(PANELS):
        if key == "jobs":
            ttl = panel_jobs_title(st)
        elif key == "queued":
            ttl = panel_queued_title(st)
        else:
            ttl = title
        block = panel(ttl, body_fn(st, user, width), width,
                      focused=(i == focus), hint="↵ expand")
        spans.append((len(body_lines), len(body_lines) + len(block)))
        body_lines += block

    total = len(header) + len(body_lines)
    if not height or focus < 0 or total <= height:
        return header + body_lines

    # Auto-scroll the panel region so the selected panel stays visible.
    # Reserve 2 lines for the "more panels" indicators.
    avail = max(3, height - len(header) - 2)
    fs, fe = spans[focus]
    off = 0
    if fe > avail:
        off = fe - avail
    if off > fs:
        off = fs
    off = max(0, min(off, max(0, len(body_lines) - avail)))
    out = list(header)
    if off > 0:
        out.append(paint("  ↑ more panels above", "dim"))
    out += body_lines[off:off + avail]
    if off + avail < len(body_lines):
        out.append(paint("  ↓ more panels below", "dim"))
    return out


def render_detail(st, user, host, width, height, focus, scroll):
    width = max(60, width)
    key, _body, detail_fn, _t = PANELS[focus]
    content = detail_fn(st, user, width)

    avail = max(3, height - 4)
    maxscroll = max(0, len(content) - avail)
    scroll = max(0, min(scroll, maxscroll))
    window = content[scroll:scroll + avail]

    title = PANEL_TITLES.get(key, key.upper())
    clock = time.strftime("%H:%M:%S")
    crumb = (paint(" %s@%s" % (user, host), "bold", "bcyan") + paint("  ▸  ", "dim")
             + paint(title, "bold", "byellow"))
    head = crumb + " " * max(1, width - vis_len(crumb) - len(clock) - 1) + paint(clock, "dim")
    out = [head, paint("─" * width, "dim")]
    out += window
    # pad so the footer sits near the bottom
    out += [""] * max(0, avail - len(window))

    pos = "" if not content else "showing %d–%d of %d" % (
        scroll + 1, min(scroll + avail, len(content)), len(content))
    up = "↑" if scroll > 0 else " "
    dn = "↓" if scroll < maxscroll else " "
    foot = paint(" %s%s scroll · PgUp/PgDn · g/G · Esc back · r refresh · q quit    %s" % (
        up, dn, pos), "dim")
    out.append(paint("─" * width, "dim"))
    out.append(foot)
    return out, scroll


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def parse_key(data):
    if not data:
        return None
    if data in (b"\r", b"\n"):
        return "ENTER"
    if data == b"\t":
        return "TAB"
    if data in (b"\x7f", b"\x08"):
        return "BACK"
    if data == b" ":
        return "SPACE"
    if data == b"\x1b":
        return "ESC"
    if data[:2] in (b"\x1b[", b"\x1bO"):
        return {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT",
                b"5~": "PGUP", b"6~": "PGDN", b"H": "HOME", b"F": "END",
                b"Z": "SHTAB"}.get(data[2:])
    try:
        ch = data.decode("utf-8", "ignore")
    except Exception:
        return None
    return ch[:1] if ch else None


def tokenize(data):
    """Split a raw input buffer into a list of canonical key tokens.

    Handles several keystrokes arriving in one read (key auto-repeat or fast
    typing) and multi-byte escape sequences (arrows, PgUp/PgDn)."""
    keys, i, n = [], 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x1b:  # ESC — possibly the start of a CSI/SS3 sequence
            if data[i + 1:i + 2] == b"[":
                j = i + 2
                while j < n and 0x30 <= data[j] <= 0x3f:  # CSI parameter bytes
                    j += 1
                if j < n:
                    keys.append(parse_key(data[i:j + 1]))
                    i = j + 1
                    continue
                keys.append("ESC")
                break
            if data[i + 1:i + 2] == b"O" and i + 2 < n:
                keys.append(parse_key(data[i:i + 3]))
                i += 3
                continue
            keys.append("ESC")
            i += 1
            continue
        ln = 1 if b < 0x80 else (4 if b >= 0xf0 else (3 if b >= 0xe0 else 2))
        keys.append(parse_key(data[i:i + ln]))
        i += ln
    return [k for k in keys if k]


def read_keys(timeout):
    """Wait up to `timeout`s for input; return a list of key tokens."""
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return []
    try:
        data = os.read(sys.stdin.fileno(), 64)
    except OSError:
        return []
    return tokenize(data)


# ---------------------------------------------------------------------------
# Rendering loop
# ---------------------------------------------------------------------------

def configure_color(no_color):
    global USE_COLOR
    USE_COLOR = (not no_color) and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def draw(frame_lines):
    sys.stdout.write("\033[H" + "\033[K\n".join(frame_lines) + "\033[K\033[0J")
    sys.stdout.flush()


def print_once(user, host):
    width = shutil.get_terminal_size((100, 40)).columns
    st = gather(user)
    for ln in render_overview(st, user, host, width, focus=-1):
        sys.stdout.write(ln + "\n")
    sys.stdout.flush()


def interactive_loop(user, host, interval):
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\033[?1049h\033[?25l")
    mode = "overview"      # or "detail"
    focus = 0
    scroll = 0
    st = gather(user)
    last = time.time()
    try:
        while True:
            size = shutil.get_terminal_size((100, 40))
            if mode == "overview":
                frame = render_overview(st, user, host, size.columns, focus=focus,
                                        height=size.lines)
            else:
                frame, scroll = render_detail(st, user, host, size.columns,
                                              size.lines, focus, scroll)
            draw(frame)

            timeout = max(0.0, interval - (time.time() - last))
            page = max(1, size.lines - 5)
            for key in read_keys(min(timeout, 0.5) if timeout else 0.5):
                if key in ("q", "Q"):
                    return
                if key in ("r", "R"):
                    st = gather(user)
                    last = time.time()
                elif mode == "overview":
                    if key in ("DOWN", "j", "TAB"):
                        focus = (focus + 1) % len(PANELS)
                    elif key in ("UP", "k", "SHTAB"):
                        focus = (focus - 1) % len(PANELS)
                    elif key in ("ENTER", "RIGHT", "l", "SPACE"):
                        mode, scroll = "detail", 0
                    elif key in tuple("123456")[:len(PANELS)]:
                        focus, mode, scroll = int(key) - 1, "detail", 0
                else:  # detail mode
                    if key in ("ESC", "BACK", "LEFT", "h"):
                        mode = "overview"
                    elif key in ("DOWN", "j"):
                        scroll += 1
                    elif key in ("UP", "k"):
                        scroll = max(0, scroll - 1)
                    elif key == "PGDN":
                        scroll += page
                    elif key == "PGUP":
                        scroll = max(0, scroll - page)
                    elif key in ("g", "HOME"):
                        scroll = 0
                    elif key in ("G", "END"):
                        scroll = 10 ** 9  # clamped on render
                    elif key == "TAB":
                        focus, scroll = (focus + 1) % len(PANELS), 0

            if time.time() - last >= interval:
                st = gather(user)
                last = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def noninteractive_loop(user, host, interval):
    """Fallback for when stdin is not a TTY: just redraw the overview."""
    sys.stdout.write("\033[?1049h\033[?25l")
    try:
        while True:
            size = shutil.get_terminal_size((100, 40))
            st = gather(user)
            frame = render_overview(st, user, host, size.columns, focus=-1)
            draw(frame)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description="Interactive HTCondor user dashboard.")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--interval", type=int, default=5, help="refresh seconds (default 5)")
    ap.add_argument("--user", default=None, help="user to inspect (default: you)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    args = ap.parse_args()

    user = args.user or os.environ.get("USER") or getpass.getuser()
    host = socket.gethostname().split(".")[0]
    configure_color(args.no_color)
    interval = max(2, args.interval)

    if args.once:
        print_once(user, host)
    elif sys.stdin.isatty():
        interactive_loop(user, host, interval)
    else:
        noninteractive_loop(user, host, interval)


if __name__ == "__main__":
    main()
