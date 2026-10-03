"""Measure the agent's real CPU/RAM footprint during enforcement work.

Runs the actual hot path — enumerate processes, safety-gate, whitelist-match,
read the foreground window, and capture a screenshot — in a dry-run loop so
nothing is ever closed. Reports RSS and CPU% so the NFR targets (CPU < 2% avg,
RAM < 100 MB) can be checked honestly on real hardware.

    python scripts/measure_agent.py [seconds] [interval_s]
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in ("shared", "agent", "dashboard"):
    sys.path.insert(0, str(_ROOT / _p))

import psutil  # noqa: E402

from labsentinel_agent.capture.screenshot import MssScreenshotCapturer  # noqa: E402
from labsentinel_agent.enforce.matcher import WhitelistMatcher  # noqa: E402
from labsentinel_agent.enforce.safety import SafetyPolicy  # noqa: E402
from labsentinel_agent.platform.win_process import collect_processes  # noqa: E402
from labsentinel_agent.platform.win_window import active_window_title  # noqa: E402
from labsentinel_shared.rules import AppRule, RuleSet  # noqa: E402


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    proc = psutil.Process(os.getpid())
    proc.cpu_percent(None)  # prime

    policy = SafetyPolicy(own_pids=frozenset({os.getpid()}))
    matcher = WhitelistMatcher()
    ruleset = RuleSet(
        revision=1,
        dry_run=True,
        app_whitelist=(AppRule(process_name="code.exe"), AppRule(process_name="python.exe")),
    )
    capturer = MssScreenshotCapturer()

    peak_rss = 0.0
    cpu_samples: list[float] = []
    sweeps = 0
    evaluable = 0
    violations = 0
    t_end = time.time() + duration

    while time.time() < t_end:
        tick = time.time()
        procs = collect_processes(own_pids=frozenset({os.getpid()}))
        for p in procs:
            if policy.evaluate(p).evaluable:
                evaluable += 1
                if matcher.check(p, ruleset).is_violation:
                    violations += 1
        _ = active_window_title()
        _ = capturer.capture_jpeg(max_width=320, quality=40)  # grid-thumbnail size
        sweeps += 1

        cpu_samples.append(proc.cpu_percent(None))
        peak_rss = max(peak_rss, proc.memory_info().rss / (1024 * 1024))

        sleep_left = interval - (time.time() - tick)
        if sleep_left > 0:
            time.sleep(sleep_left)

    n_cores = psutil.cpu_count() or 1
    avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0
    # cpu_percent is relative to one core; normalize to whole-machine percent.
    avg_cpu_machine = avg_cpu / n_cores

    print("=== Lab Sentinel agent footprint (dry-run, real processes) ===")
    print(f"duration           : {duration:.0f}s @ {interval:.1f}s interval ({sweeps} sweeps)")
    print(f"processes/sweep    : {len(procs)} total, {evaluable // max(1, sweeps)} evaluable/sweep")
    print(f"violations/sweep   : {violations // max(1, sweeps)} (dry-run, nothing closed)")
    print(f"peak RSS           : {peak_rss:.1f} MB      (target < 100 MB)  "
          f"{'OK' if peak_rss < 100 else 'OVER'}")
    print(f"avg CPU (1 core)   : {avg_cpu:.2f}%")
    print(f"avg CPU (machine)  : {avg_cpu_machine:.2f}%   (target < 2%)     "
          f"{'OK' if avg_cpu_machine < 2 else 'OVER'}")
    print(f"cores              : {n_cores}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
