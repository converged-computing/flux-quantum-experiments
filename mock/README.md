# Coscheduling experiments

    ./run.sh              e1 e2 e3 e4, then analysis and figures
    ./run.sh e2 e4        just those two

## Before running

Do checks. This:

    flux module stats sched-fluxion-qmanager | grep policy

Wants easy, hybrid or conservative.
The vendor devices have to be in the graph before anything runs.

    flux python -m flux_quantum.populate mock

FLUX_QUANTUM_MOCK is exported by run.sh, but if you invoke the scripts by hand
inside a subinstance it has to be set before flux start. The CLIPlugin validate
hook is also called by the job ingest validator, a separate process that starts
with the broker.

## The experiments

    e1   what the allocation is held for, against vendor queue depth
    e2   the arms under classical contention, where the cost shows up
    e3   core seconds consumed, against allocation size
    e4   does the knee track allocation size, swept over spare cores

## The arms

    inline        an ordinary job that takes its nodes, then opens the session
                  and waits in the vendor queue while holding them
    coscheduled   the classical is held, the scout waits, the classical starts
                  once the QPU is ours
    sessionfirst  acquire priority, then submit and let it queue
    nowarmup      coscheduled without the priority check


## Reading the numbers

A vendor bills from when the first task runs, not from when the session opened,
so the queue wait is free in every arm and quantum cost comes out about equal on
an idle cluster. Coscheduling does not save quantum money. What it saves is
classical core seconds, which is e3.

Raw CSVs hold timestamps only. Everything else is derived in analyze.py and
plot.py, so the model can change without another run.
