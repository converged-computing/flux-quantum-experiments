# Braket device probes (September 2026)

Raw records from `tests/probe_hold.py` runs against live Braket devices,
kept because the C2/C3 mechanism numbers in `forte/PROTOCOL.md`
build on what these established (hold-open latency, queue behavior,
execution windows, priority semantics).

- `probe-runs.jsonl` - hold probes against Rigetti Cepheus-1-108Q with
  filler contention (shots, fillers, calibration snapshot per run).
- `priority-race.jsonl` - the token-vs-filler priority race record.
- `probe-hold.log` - the `--survey` transcripts: device status, queue
  depths and execution windows across regions.
- `specs.jsonl` - a device specs dump (two-qubit gate fidelities);
  `tests/plot_specs.py` renders it, `gate-error.png` is one such render.
- `probe-hold-settled-depth.diff` - an UNLANDED change to
  `tests/probe_hold.py` (wait for queue counters to settle before
  reading). It no longer applies to the current tree (it references a
  test file that does not exist here); kept for reference until it is
  reimplemented or discarded.
