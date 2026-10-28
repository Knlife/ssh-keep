# Testing real SSH tunnels

Use disposable remote ports and an independent state directory. Do not replace
production `RemoteForward` entries or kill an existing SSH/Mihomo process.

1. On the SSH-origin machine, check the upstream proxy and run `ssh -G D` /
   `ssh -G CPU`. Confirm direction, identity selection and jump hosts.
2. On each remote, reserve a free loopback port for the smoke test. Close the
   reservation immediately before starting the SSH forwarding attempt; there is
   a small race, handled by `ExitOnForwardFailure=yes` rather than port stealing.
3. Create a private temporary SSH configuration containing the effective host
   settings, replacing only forwarding declarations with one loopback-bound
   reverse forward to the existing upstream proxy. Never copy private-key data.
4. Configure the supervisor with that temporary SSH file and an application probe
   that requests HTTPS through the temporary forwarded port. Notifications must
   remain disabled or write only to a local temporary sink.
5. Wait for `HEALTHY` for both hosts. From the remote, verify the proxy exit IP and
   that the temporary listener is loopback-bound.
6. SIGTERM just the supervisor-owned SSH child PID recorded in its state. Observe
   `BACKOFF`, an incremented attempt count and a new healthy SSH child. The other
   host should remain healthy throughout.
7. SIGTERM the supervisor. Verify its owned child PIDs and temporary remote
   listeners disappear, and remove the temporary SSH config/state. Leave
   production ports and services unchanged.

The unit/integration suite uses a disposable OpenSSH-shaped subprocess peer for
deterministic timing and failure injection; it is not a replacement for the real
OpenSSH checks above. Test reports must state which of the two was run.
