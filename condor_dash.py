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
import threading
import time

# ---------------------------------------------------------------------------
# Terminal / ANSI helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
USE_COLOR = True

# Marks a slice of dashboard state whose collector has not finished yet. gather()
# starts every key PENDING and fills each in as its collector returns, so panels
# can paint "loading" for what isn't ready instead of blocking the whole frame.
PENDING = object()

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


def run_many(cmd_list, timeout=60):
    """Launch several commands at once; return [(stdout, err), …] in order.

    Used to fan out a per-schedd condor_q across the pool's ~12 schedds. They
    all run concurrently, so wall time is roughly the slowest single query
    rather than their sum. A command that fails or is unreachable comes back
    as (None, err) so callers can treat that schedd as count-only."""
    procs = []
    for args in cmd_list:
        try:
            procs.append((args, subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
        except Exception as exc:  # pragma: no cover - defensive
            procs.append((args, exc))
    results = []
    for args, p in procs:
        if isinstance(p, Exception):
            results.append((None, str(p)))
            continue
        try:
            out, errtxt = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.communicate(timeout=5)
            except Exception:
                pass
            results.append((None, "%s timed out" % args[0]))
            continue
        if p.returncode != 0 and not (out or "").strip():
            msg = (errtxt or "").strip().splitlines()
            results.append((None, msg[0] if msg else "exit %d" % p.returncode))
        else:
            results.append((out, None))
    return results


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
    """Per-user *actual* usage + pool-wide queue totals, from the machine side.

    We deliberately do NOT use condor_q here. This pool has ~12 separate
    schedds (its-og-login*, sugwg-login*, lhcb-login, the CE nodes, …), and
    `condor_q` without -global only sees the one local schedd, so it misses
    almost everyone. `condor_q -global` is slow (~12s), fails to authenticate
    to the CE schedds, and its RequestGpus attribute is unset on most jobs
    anyway (a job can show gpu=0 in the queue while really running 90+ GPUs).

    The execute nodes, by contrast, report the real owner and real CPU/GPU
    allocation of every claimed slot to the collector — one fast, complete,
    accurate query. Queue run/idle/held totals come from each schedd's own
    counters (TotalRunningJobs/…), also a single fast collector query.

    users: {owner: {jobs, cpu, gpu}}  actual running usage across the pool
    q:     {running, idle, held, total}  summed over every schedd
    """
    out, err = run(["condor_status", "-af:t",
                    "RemoteOwner", "Cpus", "GPUs", "State"], timeout=60)
    if err:
        return {}, {}, err
    users = {}
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or parts[3] != "Claimed":
            continue
        owner = parts[0]
        if owner in ("undefined", "", None):
            continue
        owner = owner.split("@", 1)[0]          # strip the UID domain
        u = users.setdefault(owner, {"jobs": 0, "cpu": 0, "gpu": 0})
        u["jobs"] += 1                            # one claimed slot ≈ one running job
        u["cpu"] += _num(parts[1])
        if parts[2] not in ("undefined", "", None):
            u["gpu"] += _num(parts[2])

    # pool-wide queue totals: sum each schedd's own job counters (fast, complete)
    q = {"running": 0, "idle": 0, "held": 0, "total": 0}
    sout, _se = run(["condor_status", "-schedd", "-af:t",
                     "TotalRunningJobs", "TotalIdleJobs", "TotalHeldJobs"],
                    timeout=30)
    for line in (sout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        q["running"] += _num(parts[0])
        q["idle"] += _num(parts[1])
        q["held"] += _num(parts[2])
    q["total"] = q["running"] + q["idle"] + q["held"]
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


PER_USER_CAP = 20  # keep at most this many idle jobs per user in the queue view


def collect_idle_jobs():
    """Pool-wide idle jobs, gathered by fanning out across every schedd.

    A bare `condor_q` only talks to the *local* schedd, so it misses the ~11
    other schedds in this pool (this dashboard used to show only local-schedd
    idle jobs while the CLUSTER panel reported the pool-wide count — the two
    never agreed). Instead we list the schedds from the collector and query
    each one that has idle jobs *by name, in parallel*. Schedds we can't read
    (the CE nodes reject the query, exactly as `-global` does) contribute only
    their idle count, surfaced to the user as "count only".

    When one user submits a huge array (tens of thousands of jobs is normal
    here) we keep at most PER_USER_CAP of their jobs so the panel stays legible,
    and report the remainder as a per-owner overflow.

    Returns (jobs, meta, err). meta = {total, enumerated, shown, unreachable,
    overflow{owner:n}, users} carries the headline numbers and the overflow the
    panels annotate with."""
    sout, serr = run(["condor_status", "-schedd", "-af:t",
                      "Name", "TotalIdleJobs"], timeout=30)
    if serr:
        return [], {}, serr
    schedds, total = [], 0                    # [(name, idle_count)], pool total
    for line in (sout or "").splitlines():
        p = line.split("\t")
        if len(p) < 2:
            continue
        schedds.append((p[0], _num(p[1])))
        total += _num(p[1])

    # pull idle jobs from every schedd that claims to have any, all at once
    targets = [(name, cnt) for name, cnt in schedds if cnt > 0]
    base_res = run_many([
        ["condor_q", "-name", name, "-all", "-constraint", "JobStatus==1",
         "-af:t", "ClusterId", "ProcId", "Owner", "RequestCpus", "RequestGpus",
         "RequestMemory", "QDate", "ServerTime"] for name, _ in targets
    ], timeout=60)

    by_owner = {}                             # owner -> [job dicts] (pre-cap)
    enumerated = unreachable = 0
    for (name, cnt), (out, e) in zip(targets, base_res):
        if e:
            unreachable += cnt                # rejected us (the CE nodes reject
            continue                          # this query the same way -global does)
        for line in (out or "").splitlines():
            p = line.split("\t")
            if len(p) < 8:
                continue
            srv, qd = _num(p[7]), _num(p[6])
            by_owner.setdefault(p[2], []).append({
                "id": "%s.%s" % (p[0], p[1]), "owner": p[2], "schedd": name,
                "cpu": _num(p[3]), "gpu": _num(p[4]), "mem": _num(p[5]),
                "wait": (srv - qd) if (srv and qd) else None,
                "model": None, "vram": 0, "host": None,
            })
            enumerated += 1

    # cap each user to their longest-waiting PER_USER_CAP jobs; record the rest
    kept, overflow = [], {}
    for owner, jl in by_owner.items():
        jl.sort(key=lambda x: -(x["wait"] or 0))
        if len(jl) > PER_USER_CAP:
            overflow[owner] = len(jl) - PER_USER_CAP
        kept.extend(jl[:PER_USER_CAP])

    # fetch Requirements only for the jobs we kept (≤ a few hundred), per schedd
    per_schedd = {}
    for j in kept:
        per_schedd.setdefault(j["schedd"], []).append(j["id"])
    if per_schedd:
        rmap = {}
        for out, _e in run_many([
            ["condor_q", "-name", s, "-af:tr", "ClusterId", "ProcId",
             "Requirements"] + ids for s, ids in per_schedd.items()
        ], timeout=60):
            for line in (out or "").splitlines():
                p = line.split("\t")
                if len(p) >= 3:
                    rmap["%s.%s" % (p[0], p[1])] = p[2]
        for j in kept:
            r = parse_requirements(rmap.get(j["id"], ""))
            j["model"], j["vram"], j["host"] = r["model"], r["vram"], r["host"]

    meta = {"total": total, "enumerated": enumerated, "shown": len(kept),
            "unreachable": unreachable, "overflow": overflow,
            "users": len(by_owner)}
    return kept, meta, None


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


_NEG_INTERVAL = None  # cached NEGOTIATOR_INTERVAL config value


def negotiator_interval():
    global _NEG_INTERVAL
    if _NEG_INTERVAL is None:
        out, _e = run(["condor_config_val", "NEGOTIATOR_INTERVAL"], timeout=15)
        try:
            _NEG_INTERVAL = int((out or "").strip())
        except (ValueError, AttributeError):
            _NEG_INTERVAL = 60
    return _NEG_INTERVAL


def collect_negotiator():
    """Most recent negotiation cycle timing -> dict(start, end, period, dur, interval)."""
    out, err = run(["condor_status", "-negotiator", "-af:t",
                    "LastNegotiationCycleTime0", "LastNegotiationCycleEnd0",
                    "LastNegotiationCyclePeriod0", "LastNegotiationCycleDuration0"],
                   timeout=30)
    if err:
        return {}, err
    best = None
    for line in (out or "").splitlines():
        p = line.split("\t")
        if len(p) < 2:
            continue
        start = _num(p[0])
        if start and (best is None or start > best["start"]):
            best = {"start": start, "end": _num(p[1]),
                    "period": _num(p[2]) if len(p) > 2 else 0,
                    "dur": _num(p[3]) if len(p) > 3 else 0}
    if not best:
        return {}, None
    best["interval"] = negotiator_interval()
    return best, None


def next_negotiation(neg):
    """Estimated seconds until the next negotiation cycle (a live, repeating count).

    The collector's negotiator ad can lag real time by minutes, so rather than
    `last_start + interval` (which would get stuck in the past), we project the
    cadence forward by phase: cycles recur ~every NEGOTIATOR_INTERVAL, aligned to
    the last observed cycle start. Cycles can also fire early via condor_reschedule,
    so this is an estimate / upper bound, not an exact deadline."""
    if not neg or not neg.get("start"):
        return None
    interval = neg.get("interval") or 60
    if interval <= 0:
        return None
    elapsed = time.time() - neg["start"]
    if elapsed < 0:  # clock skew between us and the negotiator
        return interval
    return interval - (elapsed % interval)


ANALYZE_CAP = 60  # bound match analysis cost when the queue is huge


# Every key a panel might read. gather() seeds them all PENDING and fills each
# in as its collector returns, so a fresh (or partial) state never KeyErrors.
_STATE_KEYS = (
    "jobs", "jsumm", "jerr", "users", "queue", "uerr",
    "cluster", "open_slots", "cap", "cerr", "gpu_nodes", "gerr",
    "idle", "idle_meta", "ierr", "neg", "nerr",
    "prows", "pme", "prank", "ptot", "perr",
)


def new_state():
    """A blank state with every collector slice marked not-yet-loaded."""
    st = {k: PENDING for k in _STATE_KEYS}
    st["ts"] = time.time()
    return st


def gather(user, publish=None):
    """Run every collector concurrently into one state dict.

    The collectors are independent and network-bound, so running them on
    separate threads makes wall time the slowest single collector (idle-job
    discovery fans condor_q across ~12 schedds and dominates at ~10s+) rather
    than their sum. As each finishes we call publish(snapshot) with a copy of
    the state so far, letting the dashboard paint the fast panels (jobs,
    priority, load, GPUs, users) within a second while the slow QUEUED panel
    still shows "loading". Without a publish callback it simply returns the
    final state, so --once and the non-interactive loop are unaffected."""
    state = new_state()
    lock = threading.Lock()

    def emit(update):
        with lock:
            state.update(update)
            snap = dict(state)
        if publish:
            publish(snap)

    def collector(fn, keys, err_key, err_defaults):
        def run_it():
            try:
                emit(dict(zip(keys, fn())))
            except Exception as exc:            # keep the panel usable on a crash
                upd = dict(err_defaults)
                upd[err_key] = str(exc)
                emit(upd)
        return run_it

    tasks = [
        collector(lambda: collect_my_jobs(user),
                  ("jobs", "jsumm", "jerr"), "jerr", {"jobs": [], "jsumm": {}}),
        collector(collect_queue_and_users,
                  ("users", "queue", "uerr"), "uerr", {"users": {}, "queue": {}}),
        collector(collect_cluster,
                  ("cluster", "open_slots", "cap", "cerr"), "cerr",
                  {"cluster": {}, "open_slots": [], "cap": []}),
        collector(collect_gpu_nodes,
                  ("gpu_nodes", "gerr"), "gerr", {"gpu_nodes": []}),
        collector(collect_idle_jobs,
                  ("idle", "idle_meta", "ierr"), "ierr", {"idle": [], "idle_meta": {}}),
        collector(collect_negotiator, ("neg", "nerr"), "nerr", {"neg": {}}),
        collector(lambda: collect_priority(user),
                  ("prows", "pme", "prank", "ptot", "perr"), "perr",
                  {"prows": [], "pme": None, "prank": None, "ptot": None}),
    ]
    threads = [threading.Thread(target=t, daemon=True) for t in tasks]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # With priority + cluster + GPU data all in, order the queue the way the
    # negotiator serves it (owner effective priority, then longest waiting) and
    # estimate each job's match. Republish so the QUEUED panel gets sorted,
    # analyzed rows. Your own jobs are highlighted in the panel regardless.
    def val(k, empty):
        v = state[k]
        return v if v is not PENDING else empty

    idle = val("idle", [])
    prio = {r["user"]: r["eff"] for r in val("prows", [])}
    idle.sort(key=lambda j: (prio.get(j["owner"], float("inf")), -(j["wait"] or 0)))
    gpu_nodes, open_slots, cap = val("gpu_nodes", []), val("open_slots", []), val("cap", [])
    for j in idle[:ANALYZE_CAP]:
        j["match"] = analyze_idle(j, gpu_nodes, open_slots, cap)
    for j in idle[ANALYZE_CAP:]:
        j["match"] = None
    emit({"idle": idle})
    return state


# ---------------------------------------------------------------------------
# Overview panels (compact)
# ---------------------------------------------------------------------------

def _loading_body():
    """Placeholder shown in a panel whose collector hasn't returned yet."""
    return [paint("  ⟳ loading…", "dim")]


def panel_jobs(st, user, width):
    if st["jobs"] is PENDING:
        return _loading_body()
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
    if s is PENDING or not s:
        return "MY JOBS"
    return "MY JOBS   %d running · %d idle · %d held   %d CPU · %d GPU in use" % (
        s.get("running", 0), s.get("idle", 0), s.get("held", 0),
        s.get("cpu", 0), s.get("gpu", 0))


def panel_priority(st, user, width):
    if st["perr"] is PENDING:
        return _loading_body()
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


def negotiation_line(st):
    """Compact, live countdown to the next negotiation cycle (idle jobs match then)."""
    if st.get("neg") is PENDING or st.get("nerr") is PENDING:
        return "   " + paint("next negotiation cycle: loading…", "dim")
    if st.get("nerr") or not st.get("neg"):
        return "   " + paint("next negotiation cycle: n/a", "dim")
    neg = st["neg"]
    rem = next_negotiation(neg)
    if rem is None:
        return "   " + paint("next negotiation cycle: n/a", "dim")
    if rem > 1:
        when = paint("in ~%s" % fmt_dur(rem), "bold", "bcyan")
    else:
        when = paint("due now — matching…", "bold", "byellow")
    detail = "every %ds" % (neg.get("interval") or 60)
    if neg.get("dur"):
        detail += ", last took %ds" % neg["dur"]
    return "   %s %s   %s" % (
        paint("next negotiation cycle", "white"), when, paint("(" + detail + ")", "dim"))


def panel_cluster(st, user, width):
    if st["cerr"] is PENDING:
        return _loading_body()
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
    body.append(negotiation_line(st))
    return body


def group_gpu_nodes(nodes):
    """Group GPU nodes by CUDA device model; return groups sorted by free GPUs."""
    groups = {}
    for n in nodes:
        g = groups.get(n[5])
        if g is None:
            g = groups[n[5]] = {"model": n[5], "vram": n[6], "nodes": [],
                                "free_g": 0, "total_g": 0, "cpus": set()}
        g["nodes"].append(n)
        g["free_g"] += n[1]
        g["total_g"] += n[2]
        g["cpus"].add(n[7])
    for g in groups.values():
        g["free_nodes"] = sum(1 for n in g["nodes"] if n[1] > 0)
        g["nodes"].sort(key=lambda x: (-x[1], x[0]))
    return sorted(groups.values(), key=lambda x: (-x["free_g"], -x["total_g"]))


def panel_freegpu(st, user, width):
    if st["gpu_nodes"] is PENDING:
        return _loading_body()
    if st["gerr"]:
        return [paint("error: " + st["gerr"], "red")]
    nodes = st["gpu_nodes"]
    if not nodes:
        return [paint("No GPU nodes found.", "dim")]
    groups = group_gpu_nodes(nodes)
    inner = width - 4
    body = [paint(fit(" %-18s %5s %6s %6s %11s %6s" % (
        "GPU MODEL", "FREE", "TOTAL", "NODES", "FREE NODES", "VRAM"), inner), "dim")]
    for g in groups:
        row = " %-18s %5d %6d %6d %11d %6s" % (
            g["model"][:18], g["free_g"], g["total_g"], len(g["nodes"]),
            g["free_nodes"], fmt_mem(g["vram"]))
        body.append(paint(fit(row, inner), "bgreen") if g["free_g"]
                     else paint(fit(row, inner), "dim"))
    tf = sum(g["free_g"] for g in groups)
    tg = sum(g["total_g"] for g in groups)
    tn = sum(len(g["nodes"]) for g in groups)
    body.append(paint(fit(" %-18s %5d %6d %6d  (Enter for per-node list)" % (
        "TOTAL", tf, tg, tn), inner), "bold"))
    return body


def panel_users(st, user, width):
    if st["users"] is PENDING:
        return _loading_body()
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
    """One overview row: plain 'head' + colored, width-bounded match cell.

    Your own jobs get a leading ▸ marker and a bright-cyan head."""
    mine = j["owner"] == user
    head = "%s%-11s %-10s %-19s %7s  " % (
        "▸" if mine else " ", j["id"], j["owner"][:10], _need_str(j)[:19],
        fmt_dur(j["wait"]) if j["wait"] is not None else "-")
    label, color, reason = j.get("match") or ("?", "dim", "")
    avail = max(6, inner - len(head))
    if len(label) + 1 <= avail:
        cell = paint(label, "bold", color) + " " + paint(reason[:avail - len(label) - 1], "dim")
    else:
        cell = paint(label[:avail], "bold", color)
    return paint(head, "bold", "bcyan") + cell if mine else head + cell


def _queue_overflow_notes(st, itemize):
    """Dim '… N more' lines explaining what the capped list can't show:
    per-user overflow (bulk arrays) and idle jobs on schedds we couldn't read.

    itemize=True lists every over-cap user (detail view); False rolls them into
    one line (compact panel)."""
    meta = st.get("idle_meta") or {}
    notes = []
    ov = sorted((meta.get("overflow") or {}).items(), key=lambda kv: -kv[1])
    if itemize:
        for owner, n in ov:
            notes.append(paint("  … %d more from %s (capped at %d/user)"
                               % (n, owner, PER_USER_CAP), "dim"))
    elif ov:
        extra = sum(n for _, n in ov)
        if len(ov) == 1:
            notes.append(paint("  … %d more from %s (capped at %d/user)"
                               % (extra, ov[0][0], PER_USER_CAP), "dim"))
        else:
            notes.append(paint("  … %d more from %d users over the %d/user cap"
                               % (extra, len(ov), PER_USER_CAP), "dim"))
    un = meta.get("unreachable", 0)
    if un:
        notes.append(paint("  … %d more on CE / unreachable schedds — count only"
                           % un, "dim"))
    return notes


def panel_queued(st, user, width):
    if st["idle"] is PENDING:
        return _loading_body()
    if st["ierr"]:
        return [paint("error: " + st["ierr"], "red")]
    idle = st["idle"]
    meta = st.get("idle_meta") or {}
    if not idle:
        if meta.get("unreachable"):
            return [paint("No readable idle jobs — %d on CE / unreachable schedds."
                          % meta["unreachable"], "dim")]
        return [paint("No queued (idle) jobs — the queue is clear.", "dim")]
    inner = width - 4
    body = [paint(fit(" %-11s %-10s %-19s %7s  %s" % (
        "JOB", "OWNER", "NEEDS", "WAIT", "WILL IT MATCH?"), inner), "dim")]
    for j in idle[:6]:
        body.append(_queued_row(j, user, inner))
    if len(idle) > 6:
        body.append(paint("  … %d more queued in detail (Enter to see all)"
                          % (len(idle) - 6), "dim"))
    body += _queue_overflow_notes(st, itemize=False)
    return body


def panel_queued_title(st):
    meta = st.get("idle_meta")
    if meta is PENDING or not meta:
        return "QUEUED JOBS"
    total = meta.get("total", 0)
    if not total:
        return "QUEUED JOBS"
    users = meta.get("users", 0)
    tail = "  ·  %d user%s waiting" % (users, "" if users == 1 else "s") if users else ""
    return "QUEUED JOBS   %d idle%s" % (total, tail)


# ---------------------------------------------------------------------------
# Detail views (full, scrollable) — return a list of content lines
# ---------------------------------------------------------------------------

def detail_jobs(st, user, width):
    if st["jobs"] is PENDING:
        return _loading_body()
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
    if st["prows"] is PENDING:
        return _loading_body()
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
    if st["cerr"] is PENDING:
        return _loading_body()
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
    out.append("")
    out.append(paint(" NEGOTIATOR", "bold", "bcyan"))
    out.append(negotiation_line(st))
    if not st.get("nerr") and st.get("neg"):
        neg = st["neg"]
        out.append("   last cycle ended %s · duration %ds · observed period %ds" % (
            fmt_when(neg.get("end")), neg.get("dur", 0), neg.get("period", 0)))
        out.append(paint("   idle jobs are matched to slots once per cycle; "
                         "early cycles can be triggered by job activity.", "dim"))
    return out


def detail_freegpu(st, user, width):
    if st["gpu_nodes"] is PENDING:
        return _loading_body()
    if st["gerr"]:
        return [paint("error: " + st["gerr"], "red")]
    nodes = st["gpu_nodes"]
    if not nodes:
        return [paint("No GPU nodes found.", "dim")]
    groups = group_gpu_nodes(nodes)
    tf = sum(g["free_g"] for g in groups)
    tg = sum(g["total_g"] for g in groups)
    out = [
        paint(" %d GPUs free / %d total   across %d nodes in %d models" % (
            tf, tg, len(nodes), len(groups)), "bold", "bgreen"),
        paint(" Grouped by GPU model — free nodes first, busy nodes dimmed."
              "  Scroll: ↑/↓ · PgUp/PgDn · g/G", "dim"),
        "",
        paint(" %-20s %8s  %-16s %8s %9s" % (
            "NODE", "GPUs", "CPU MODEL", "CPU free", "MEM free"), "bold", "dim"),
    ]
    for g in groups:
        free_g, total_g, nfree, nall = g["free_g"], g["total_g"], g["free_nodes"], len(g["nodes"])
        title = " ◆ %s   %s VRAM   —   %d/%d GPUs free · %d/%d nodes free" % (
            g["model"], fmt_mem(g["vram"]), free_g, total_g, nfree, nall)
        out.append(paint(fit(title, width), "bold", "byellow" if free_g else "dim"))
        for name, gfree, gtot, fcpu, fmem, _m, _v, cpu in g["nodes"]:
            row = "   %-20s %8s  %-16s %7dc %9s" % (
                name, "%d/%d" % (gfree, gtot), cpu[:16], fcpu, fmt_mem(fmem))
            out.append(paint(fit(row, width), "bgreen") if gfree
                       else paint(fit(row, width), "dim"))
        out.append("")
    return out


def detail_users(st, user, width):
    if st["users"] is PENDING:
        return _loading_body()
    if st["uerr"]:
        return [paint("error: " + st["uerr"], "red")]
    users = st["users"]
    if not users:
        return [paint("No running jobs.", "dim")]
    ranked = sorted(users.items(), key=lambda kv: (-kv[1]["gpu"], -kv[1]["cpu"],
                                                    -kv[1]["jobs"]))
    tcpu = sum(v["cpu"] for v in users.values())
    tgpu = sum(v["gpu"] for v in users.values())
    tjob = sum(v["jobs"] for v in users.values())
    out = [
        paint(" %d users running · %d running jobs · %d CPU · %d GPU in use" % (
            len(users), tjob, tcpu, tgpu), "bold"),
        "",
        paint(" %4s  %-18s %10s %8s %6s" % (
            "#", "USER", "RUN JOBS", "CPU", "GPU"), "bold", "dim"),
    ]
    for i, (owner, u) in enumerate(ranked, 1):
        txt = " %4d  %-18s %10d %8d %6d" % (
            i, owner, u["jobs"], u["cpu"], u["gpu"])
        if owner == user:
            out.append(paint(fit(txt + "   ◂ you", width), "bold", "bcyan"))
        else:
            out.append(fit(txt, width))
    out.append("")
    out.append(paint(" %4s  %-18s %10d %8d %6d" % (
        "", "TOTAL", tjob, tcpu, tgpu), "bold", "dim"))
    return out


def detail_queued(st, user, width):
    if st["idle"] is PENDING:
        return _loading_body()
    if st["ierr"]:
        return [paint("error: " + st["ierr"], "red")]
    idle = st["idle"]
    meta = st.get("idle_meta") or {}
    if not idle:
        if meta.get("unreachable"):
            return [paint(" %d idle jobs pool-wide, all on CE / unreachable schedds"
                          " — no per-job detail available." % meta["unreachable"],
                          "bold")]
        return [paint("No queued (idle) jobs — the queue is clear.", "dim")]
    likely = sum(1 for j in idle if j.get("match") and j["match"][0] == "LIKELY")
    waiting = sum(1 for j in idle if j.get("match") and j["match"][0] == "WAITING")
    blocked = sum(1 for j in idle if j.get("match") and j["match"][0] == "BLOCKED")
    total, shown = meta.get("total", len(idle)), meta.get("shown", len(idle))
    out = [
        paint(" %d idle jobs pool-wide across %d schedd users; showing %d"
              " (≤%d per user)." % (total, meta.get("users", 0), shown,
                                    PER_USER_CAP), "bold"),
        paint(" Of those shown:  %s likely · %s waiting · %s blocked"
              " (top %d analyzed)." % (
                  paint(str(likely), "bgreen"), paint(str(waiting), "byellow"),
                  paint(str(blocked), "bred"), ANALYZE_CAP), "dim"),
        paint(" Match is estimated from current free resources + each job's"
              " Requirements (CPU/GPU/VRAM/model/host).", "dim"),
        paint(" It ignores user priority, preemption, disk and rank — treat it as a guide.", "dim"),
        paint(" Rows are ordered by owner priority (served-first); your jobs are marked ▸.", "dim"),
        "",
        paint(" %-11s %-10s %4s %4s %-13s %7s %7s  %s" % (
            "JOB", "OWNER", "CPU", "GPU", "GPU/HOST REQ", "MEM", "WAIT",
            "WILL IT MATCH?"), "bold", "dim"),
    ]
    for j in idle:
        req = j["model"] or (("≥%s" % fmt_mem(j["vram"])) if j["vram"] else "")
        if j["host"]:
            req = (req + " @" + j["host"]).strip()
        mine = j["owner"] == user
        head = "%s%-11s %-10s %4d %4s %-13s %7s %7s  " % (
            "▸" if mine else " ", j["id"], j["owner"][:10], j["cpu"], (j["gpu"] or "-"),
            (req or "-")[:13], fmt_mem(j["mem"]),
            fmt_dur(j["wait"]) if j["wait"] is not None else "-")
        avail = max(8, width - len(head))
        label, color, reason = j.get("match") or ("(not analyzed)", "dim", "")
        if len(label) + 1 <= avail:
            cell = paint(label, "bold", color) + " " + paint(reason[:avail - len(label) - 1], "dim")
        else:
            cell = paint(label[:avail], "bold", color)
        row = paint(head, "bold", "bcyan") if mine else head
        out.append(row + cell)
    notes = _queue_overflow_notes(st, itemize=True)
    if notes:
        out.append("")
        out += notes
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

def render_overview(st, user, host, width, focus=-1, height=None, loading=False):
    width = max(60, min(width, 110))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    left = paint(" CONDOR DASHBOARD", "bold", "bcyan")
    tag = paint(" ⟳", "byellow") if loading else ""
    right = paint("%s@%s" % (user, host), "bold") + tag + paint("  " + now, "dim")
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


def render_detail(st, user, host, width, height, focus, scroll, loading=False):
    width = max(60, width)
    key, _body, detail_fn, _t = PANELS[focus]
    content = detail_fn(st, user, width)

    avail = max(3, height - 4)
    maxscroll = max(0, len(content) - avail)
    scroll = max(0, min(scroll, maxscroll))
    window = content[scroll:scroll + avail]

    title = PANEL_TITLES.get(key, key.upper())
    clock = time.strftime("%H:%M:%S")
    mark = "⟳ " if loading else ""
    crumb = (paint(" %s@%s" % (user, host), "bold", "bcyan") + paint("  ▸  ", "dim")
             + paint(title, "bold", "byellow"))
    head = (crumb + " " * max(1, width - vis_len(crumb) - len(mark) - len(clock) - 1)
            + paint(mark, "byellow") + paint(clock, "dim"))
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


class BackgroundGather:
    """Runs gather() on a worker thread so the UI never blocks on condor.

    The old loop called gather() inline, which shells out to a dozen condor
    queries and can take 10s+ — the whole TUI froze (no input, no redraw) on
    every refresh. Here the main loop reads the latest snapshot via latest()
    and asks for a refresh via request(); collection happens off the UI thread,
    so keystrokes stay responsive even mid-query. gather() also publishes each
    collector's result as it lands, so the panels fill in progressively instead
    of the whole dashboard waiting on the slowest query. The previous snapshot
    keeps showing while a refresh is in flight."""

    def __init__(self, user):
        self._user = user
        self._lock = threading.Lock()
        self._state = new_state()              # blank skeleton: panels show "loading…"
        self._loading = True
        self._req = threading.Event()
        self._stop = threading.Event()
        self._req.set()                        # kick off the first snapshot
        self._thread = threading.Thread(target=self._run, name="gather",
                                        daemon=True)
        self._thread.start()

    def _publish(self, snap):
        # Merge only the collectors that have landed onto the last displayed
        # state. On a refresh this keeps each panel showing its previous data
        # until the fresh value arrives, instead of blinking back to "loading".
        with self._lock:
            merged = dict(self._state)
            for k, v in snap.items():
                if v is not PENDING:
                    merged[k] = v
            self._state = merged

    def _run(self):
        while True:
            self._req.wait()
            if self._stop.is_set():
                return
            self._req.clear()                  # coalesce requests queued while busy
            with self._lock:
                self._loading = True
            try:
                gather(self._user, publish=self._publish)
            except Exception:                  # keep the last snapshot on error
                pass
            with self._lock:
                self._loading = False

    def request(self):
        self._req.set()

    def latest(self):
        with self._lock:
            return self._state, self._loading

    def stop(self):
        self._stop.set()
        self._req.set()


def loading_frame(user, host, width):
    """First-snapshot screen shown while the initial gather runs in the background."""
    width = max(60, min(width, 110))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    left = paint(" CONDOR DASHBOARD", "bold", "bcyan")
    right = paint("%s@%s" % (user, host), "bold") + paint("  " + now, "dim")
    header = left + " " * max(1, width - vis_len(left) - vis_len(right)) + right
    return [header, "",
            paint("  ⟳ Collecting data from the pool… (this can take a few seconds)",
                  "byellow"),
            "", paint("  q quit", "dim")]


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
    fetcher = BackgroundGather(user)   # gathers off-thread; the UI never blocks on it
    st = None
    last = time.time()
    prev_frame = None
    try:
        while True:
            size = shutil.get_terminal_size((100, 40))
            new_st, loading = fetcher.latest()
            if new_st is not None:
                st = new_st
            if st is None:
                frame = loading_frame(user, host, size.columns)
            elif mode == "overview":
                frame = render_overview(st, user, host, size.columns, focus=focus,
                                        height=size.lines, loading=loading)
            else:
                frame, scroll = render_detail(st, user, host, size.columns,
                                              size.lines, focus, scroll, loading=loading)
            if frame != prev_frame:            # only touch the terminal when it changed
                draw(frame)
                prev_frame = frame

            timeout = max(0.0, interval - (time.time() - last))
            page = max(1, size.lines - 5)
            for key in read_keys(min(timeout, 0.5) if timeout else 0.5):
                if key in ("q", "Q"):
                    return
                if key in ("r", "R"):
                    fetcher.request()
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
                fetcher.request()          # ask the worker for a fresh snapshot
                last = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        fetcher.stop()
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
