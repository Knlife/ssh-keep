# Validation record — 2026-10-28

No production forwarding configuration or SSH daemon setting was modified during
these tests. All newly created forwarding listeners used disposable high ports,
and the supervisors and their owned SSH children were stopped after testing. No
external Feishu notification was sent.

## Automated tests

- macOS, Python 3.14: all 11 tests passed.
- PJK Linux, Python 3.10.12: the same 11 tests passed in a private temporary
  checkout. Test notification commands wrote local temporary files only.
- Offline zipapp installation and executable backup were tested without fetching
  dependencies.
- Terminal rendering was tested at widths 1, 12, 40, 64, 80 and 160, including
  narrow-height terminals and Chinese error text.

## Real OpenSSH tests from PJK

Both configured targets, D and CPU, were reachable. Both established independent
real SSH connections and passed an application probe through their temporary
reverse-forwarded HTTP proxy ports to PJK's loopback Mihomo API. An unauthenticated
401 from that API is an expected successful connectivity result; no management
secret was needed.

Both targets were also verified to reach a public HTTPS trace through their
temporary proxy and to use the expected ISP exit. Live exit addresses, node
credentials and private hostnames are intentionally omitted from this public
record.

For D, the test additionally verified all of the following:

1. The temporary reverse listener was bound only to loopback.
2. SIGTERM of just the owned SSH child caused an automatic reconnect, a larger
   attempt count and a different child PID.
3. The application probe returned to healthy after reconnection.
4. Graceful supervisor termination removed the temporary remote listener.

Independent multi-host operation was covered by the automated subprocess tests.
The live disconnect/reconnect injection was completed on D; CPU was not subjected
to further TCP forwarding tests after the binding-policy issue below was found.

## CPU binding policy requiring attention

CPU's temporary reverse listener appeared as `0.0.0.0:<test-port>`, even though the
client requested `127.0.0.1`. CPU's pre-existing production listener on port 11020
also appeared as a wildcard IPv4 listener. This means potential accessibility to
other hosts on reachable networks; it does not by itself prove public Internet
reachability.

The active sshd process points to `/kubebrain/sshd_config`, and that file explicitly
contains `GatewayPorts yes`. Editing the ordinary
`/etc/ssh/sshd_config` without checking the active process would not reliably
change its behavior. That server configuration and its reload/persistence policy
should be reviewed by its maintainer before formally enabling the CPU tunnel.
The tool did not modify it or close the user's pre-existing connection.

OpenSSH documents that `GatewayPorts=yes` forces wildcard binding, `no` forces
loopback, and `clientspecified` respects the client's requested address:
[sshd_config(5)](https://man.openbsd.org/sshd_config.5#GatewayPorts).

## Why the default check uses a loopback API

Initial live checks used a public HTTPS address with a 3-second connection limit.
They timed out despite SSH being established; PJK's own public HTTPS requests also
showed slow or intermittent TLS setup. This is a poor default for deciding when
to rebuild a tunnel.

The shipped PJK example instead probes the complete reverse-forward/proxy path to
PJK's loopback management port. Its successful state therefore measures that
configured path, not general Internet health or Claude account/risk acceptance.
An optional public HTTPS probe is documented separately with longer timeouts.
