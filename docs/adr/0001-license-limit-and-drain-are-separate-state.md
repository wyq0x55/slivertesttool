# License limit and drain are separate state

The worker's concurrency ceiling is operator configuration, so shutdown must
never write it. An earlier shutdown path zeroed the configured limit to force the
pool down; a crash part-way through shutdown then left the platform stuck at zero
concurrency until an administrator noticed. We instead added a transient
`license_draining` flag that blocks new license acquisitions and drives the pool
target to zero, leaving `license_limit` writable only by an explicit admin
change. The next bootstrap clears a leftover drain flag.

## Consequences

The machine-wide Silver process sweep had to become opt-in
(`SILVER_KILL_ON_EXIT=0`): on a shared host, killing by image name cannot tell
this platform's Silver processes from a colleague's manual Silver session.