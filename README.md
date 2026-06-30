# condor_dash

<img width="500" alt="demo" src="https://github.com/user-attachments/assets/bd8d05f0-bd8d-4df3-9b95-5a397bece26c" />

An **interactive** terminal dashboard for HTCondor users. One screen answers:

- **My jobs** — what's running/idle/held, on which GPU node, CPU/GPU each, runtime
- **My priority** — your effective priority and rank among active users (lower = served first)
- **Cluster load** — CPU cores and GPUs in use vs. total, with load bars, plus a live countdown to the next negotiation cycle
- **Free GPUs** — free vs. total GPUs grouped by GPU model; expand for the per-node breakdown
- **Top users** — who is using how many CPUs/GPUs right now (you are highlighted)
- **Queued jobs** — every idle job, its requirements, and an estimate of how likely it is to match

Pure standard-library Python 3 — no `pip install`, no Python bindings, works over SSH.
It shells out to `condor_q`, `condor_status`, and `condor_userprio`.

## Install

A small bash launcher, `condor_gui`, boots the dashboard from anywhere on your
`PATH`. Symlink it into a directory already on your `PATH`:

```bash
ln -sf "$PWD/condor_gui" ~/.local/bin/condor_gui   # ~/.local/bin must be on $PATH
condor_gui                                          # now just works
```

The launcher finds `condor_dash.py` next to itself (following the symlink),
picks a Python 3 interpreter, and forwards any arguments to the dashboard.

## Usage

```bash
condor_gui                       # live dashboard, refresh every 5s
condor_gui --once                # print a single snapshot and exit
condor_gui --interval 10         # live, custom refresh interval (seconds)
condor_gui --user someone        # inspect another user's jobs & priority
condor_gui --no-color            # plain text (good for logging / piping)
```

(You can still run `./condor_dash.py` directly with the same flags.)

### Navigating the live dashboard

Every block is selectable — pick one and expand it for a full, scrollable detail view:

| Key | Action |
| --- | --- |
| `↑` / `↓`, `j` / `k`, `Tab` | move the selection between blocks |
| `1`–`6` | jump straight into a block's detail view |
| `Enter` / `→` | expand the selected block |
| `Esc` / `←` / `Backspace` | back to the overview |
| `↑` / `↓`, `PgUp` / `PgDn`, `g` / `G` | scroll inside a detail view |
| `r` | refresh now |
| `q` | quit |

The overview auto-scrolls to keep the selected block visible, so it works on short
terminals too. Each block expands to more than the overview shows:

- **My jobs** → every job with memory, full node, submit time, full command, and hold reasons
- **My priority** → the full ranked table of active users (you highlighted)
- **Cluster load** → CPU/GPU/slot breakdown, GPU-node counts (fully/partially free), queue stats, negotiator timing
- **Free GPU nodes** → every GPU node grouped by model (free first, busy dimmed), incl. per-GPU VRAM, CPU model, free CPU & memory — scroll with ↑/↓ · PgUp/PgDn · g/G
- **Top users** → every user in the queue with running CPU/GPU and idle counts, plus totals
- **Queued jobs** → every idle job with its CPU/GPU/VRAM/host requirements and a match verdict

`--once` is handy for cron/logging, e.g. `condor_dash.py --once --no-color >> load.log`.

## Notes

- "Free GPUs" is read from the unclaimed GPU count on each node's partitionable
  slot; per-user CPU/GPU usage sums `RequestCpus`/`RequestGpus` over *running* jobs.
- **Match likelihood** for queued jobs is an estimate: it compares each job's parsed
  `Requirements` (CPU/GPU count, GPU model, VRAM, host) against the pool's currently
  free slots and total capacity. It does *not* model user priority, preemption, disk,
  or rank — so treat it as a guide, and use `condor_q <id> -analyze` for the authoritative
  (but slow) answer. CPU models are decoded from CPUID family/model since the pool does
  not advertise a CPU model string.
- The **next-negotiation countdown** is an estimate: the negotiator's collector ad
  can lag real time, so the cadence is projected forward by phase from the last
  observed cycle using `NEGOTIATOR_INTERVAL`. Cycles can also fire early (e.g. via
  `condor_reschedule`), so treat it as an upper bound — idle jobs are matched to
  slots once per cycle.
- Each refresh runs a handful of quick condor queries (~1-2s total on this pool).
  Raise `--interval` if you want to query the schedd less often.
