# Reviewed capture producer compatibility

The direct-capture allowlist also admits producer revision
`30f89397d183a9c39866f3ae25f46e61ce55ced0`. Its source archive has SHA-256
`a8811214fca8ea2a7c6daf2b3ab75d76a65fc14840d12b80cb080927df19a112`.
All 88 producer source files were checked against the archive's frozen qualification.

Compared with the previously admitted producer, only `env/published_capture.py`
differs: it imports `stat` and uses `stat.S_ISREG(info.st_mode)` on the existing
`lstat()` result when checking hardlinks across capture mount boundaries. This
avoids following symlinks while identifying regular files. Ownership checks,
hardlink rejection, the captured execution sources, and repair execution are
unchanged.

This admission authenticates that exact producer. It does not rewrite checkpoint
receipts, admit arbitrary source changes, or change training eligibility. Reader
qualification still covers the installed source and the frozen producer allowlist.
