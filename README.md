# Flux Quantum Experiments

Experiments for [flux-quantum](https://github.com/converged-computing/flux-quantum),
quantum and classical coscheduling for Flux. One directory per vendor.

- [queues/](queues/) what each vendor's queue looks like over days, read for free
  through Braket, IonQ and IBM, with the same machine through two doors, and
  the distribution the mock draws from.
- [mock/](mock/) the mock vendor, whose queue depth is a parameter. Release
  latency, contention, core seconds, the knee, preemption and admission, with
  the data and figures.
- [forte/](forte/) the QeMCMC campaign on live devices (Rigetti Cepheus
  through Braket): the hold mechanism standalone and through flux, batch
  contention, filler sweeps, with protocol, runbook, batch scripts and the
  evidence directories. See [forte/PROTOCOL.md](forte/PROTOCOL.md).
- [probes/](probes/) the September 2026 Braket device probes the campaign's
  mechanism numbers build on (hold-open latency, queue behavior, execution
  windows, priority semantics).

The campaign's unit tests live in [tests/](tests/) and need a flux-quantum
checkout: enclosing, sibling, or named by `FLUX_QUANTUM_ROOT`.

## License

Distributed under the terms of the MIT license. All new contributions must be
made under this license. See [LICENSE](LICENSE), [COPYRIGHT](COPYRIGHT) and
[NOTICE](NOTICE) for details.

SPDX-License-Identifier: (MIT)

LLNL-CODE- 842614
