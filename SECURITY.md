# Security

Report a vulnerability privately to the repository owner; do not include live
credentials in public issues. Run this tool as the same account you normally use
for SSH. It does not require additional privileges.

Configuration is trusted local input: `ssh_binary`, SSH configuration, health
commands and notification commands can execute programs as the current user.
Do not run unreviewed configurations obtained from strangers. Protect private
configuration and state directories, and keep remote forwarding listeners bound
to loopback unless you intentionally authorize wider exposure.

The program does not disable host-key checking. It does not store SSH passwords
or private keys. It suppresses notification-command output and does not put
notification arguments in ordinary logs. OpenSSH error summaries and forwarding
addresses may appear in private local status/event files; review them before
sharing. External notification programs may independently collect additional
context and must be reviewed separately.
