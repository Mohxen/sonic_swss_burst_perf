# Runtime files

This directory records local SONiC VM runtime metadata from the benchmark environment.

- `sonic-vs.pid`: PID of the local QEMU process at capture time, useful only for reproducing the original operator context.
- `sonic-vs.monitor`: QEMU monitor socket, intentionally not committed because Git cannot store Unix socket files and the socket is only valid while the VM is running.
