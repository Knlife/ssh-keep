# Contributing

Use Python 3.9+ and the standard library for runtime functionality. Keep OpenSSH
responsible for authentication and forwarding. Do not change host-key verification
to make an unreliable connection appear fixed.

Run `python3 -m unittest discover -s tests -v` before submitting changes. Changes to
the supervisor should exercise the public CLI using a real subprocess, including
failure and cancellation. Tests must not send external notifications, touch a
user's SSH configuration, or reuse production forwarding ports.

Keep configuration examples free of credentials and personal infrastructure. Do
not add real logs, notification recipient details, SSH private keys, proxy
credentials, or complete environment dumps to commits or test artifacts.

For protocol/lifecycle changes, verify on macOS and Linux and explain whether
"connected" refers to SSH transport or an actual end-to-end probe. Treat stdout
from a remote machine as untrusted terminal input. All owned subprocess groups
must be terminated and reaped on graceful cancellation.
