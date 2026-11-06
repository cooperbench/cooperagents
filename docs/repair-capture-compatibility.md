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


## Reusing the original offline image release

The `repair-v1` offline SQFS release was built by reader `6a2d7bc2082d4901a08351acc18724ce8b60c1f0`.
Its reader qualification SHA256 is
`90db555845889be5ba02e7d17b198b48c2b0b7e605aaf3b3c77d50af0a9f1be7`.
Admit only this historical image qualification alongside the current one. Compared with that
revision, the image preparation/packing/copy functions, native inventory, namespace image audit,
and runtime measurement are unchanged; later installed-artifact changes affect checkpoint
payload access, and held-out admission changes producer metadata. The handoff validation change
only admits this exact audit provenance. Package manifests and pinned release indices remain
immutable. No qualification identity is rewritten in an old package.

Reader source validation, original image identity, sealed descriptor identity, native audit
presence and exact runtime qualification still apply. This admission does not accept historical
reader code for checkpoint execution, authorize another image producer, or certify baseline
restoration. The consumer must separately verify its selected checkpoints and official baselines.
