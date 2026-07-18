#!/usr/bin/env python3
"""Summarize Locust CSV outputs from the basic-message benchmark matrix."""

from __future__ import annotations

import csv
import sys
from pathlib import Path


def read_meta(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    if not path.exists():
        return meta
    for line in path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            meta[k.strip()] = v.strip()
    return meta


def read_msg_stats(stats_csv: Path) -> dict[str, str] | None:
    if not stats_csv.exists():
        return None
    with stats_csv.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        name = row.get("Name") or row.get("name") or ""
        if "msg_client" in name:
            return row
    # Fallback to Aggregated row if present
    for row in rows:
        name = (row.get("Name") or row.get("name") or "").lower()
        if name in {"aggregated", "total"}:
            return row
    return rows[0] if rows else None


def peak_cpu(stats_csv: Path, needle: str) -> float | None:
    if not stats_csv.exists():
        return None
    peak = 0.0
    found = False
    with stats_csv.open(newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            if len(row) < 3:
                continue
            container = row[1]
            if needle not in container:
                continue
            found = True
            cpu = row[2].replace("%", "").strip()
            try:
                peak = max(peak, float(cpu))
            except ValueError:
                continue
    return peak if found else None


def fmt(v, digits=1):
    if v is None or v == "":
        return "n/a"
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "results/basicmsg")
    runs = sorted([p for p in root.iterdir() if p.is_dir()])
    print("# ACA-Py Basic Message Benchmark Summary")
    print()
    print(f"Results root: `{root}`")
    print()
    print(
        "| Run | Users | Conns/user | Mediation | msgs | fail | steady RPS | p50 ms | p95 ms | p99 ms | issuer CPU% peak | notes |"
    )
    print("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---|")

    for run in runs:
        meta = read_meta(run / "run-meta.txt")
        stats = read_msg_stats(run / "locust_stats.csv")
        if stats is None:
            print(
                f"| {run.name} | {meta.get('users','')} | {meta.get('connections_per_agent','')} | "
                f"{meta.get('mediation','')} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | no stats |"
            )
            continue
        requests = stats.get("Request Count") or stats.get("# requests") or "0"
        failures = stats.get("Failure Count") or stats.get("# failures") or "0"
        # Prefer wall-clock steady-state RPS (excludes connection setup)
        rps = meta.get("steady_state_rps") or stats.get("Requests/s") or ""
        p50 = stats.get("50%") or stats.get("Median Response Time") or ""
        p95 = stats.get("95%") or ""
        p99 = stats.get("99%") or ""
        issuer_cpu = peak_cpu(run / "docker-stats.csv", "issuer")
        notes = []
        if meta.get("acapy_log_level") == "debug":
            notes.append("debug logs")
        wallet_cfg = meta.get("wallet_storage_config", "")
        if "max_connections\":50" in wallet_cfg or "max_connections\": 50" in wallet_cfg:
            notes.append("pool=50")
        finished = ""
        for line in (run / "run-meta.txt").read_text().splitlines() if (run / "run-meta.txt").exists() else []:
            if line.startswith("finished="):
                finished = line
        if finished and "exit=0" not in finished:
            notes.append("exit=" + finished.split("exit=")[-1])
        if meta.get("steady_state_seconds"):
            notes.append(f"t={fmt(meta.get('steady_state_seconds'),1)}s")

        print(
            f"| {run.name} | {meta.get('users','')} | {meta.get('connections_per_agent','')} | "
            f"{meta.get('mediation','false')} | {requests} | {failures} | {fmt(rps,2)} | "
            f"{fmt(p50,0)} | {fmt(p95,0)} | {fmt(p99,0)} | {fmt(issuer_cpu,1)} | "
            f"{';'.join(notes) or '-'} |"
        )

    print()
    print("## Interpretation guide")
    print()
    print("- Compare `direct-1x20` vs `direct-20x1`: if serial stays near concurrent throughput, the harness or a single-threaded server path is likely limiting.")
    print("- Compare direct vs mediated: mediation/pickup overhead is the delta.")
    print("- Compare `direct-20x1` vs `direct-20x1-pool50`: pool saturation vs application-level serialization.")
    print("- Compare `direct-20x1` vs `direct-20x1-debug`: logging overhead.")
    print("- Only conclude ACA-Py event-loop serialization if concurrent load fails to scale, issuer CPU is ~1 core saturated, and Postgres/mediator/load-gen are not the limiters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
