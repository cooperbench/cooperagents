# Relocating Apptainer base images

An incremental checkpoint still requires the exact base SIF SHA256. Its location
may change: pass the new location as `ApptainerEnv.from_checkpoint(...,
base_image=...)`. The original image path remains checkpoint provenance, not a
requirement that the original file still exists.

Apptainer generates `.singularity.d/Singularity` from the input path, adds
source-path and build-time labels, and maintains `bootstrap_history`. New
Apptainer checkpoints always include the contents of those generated files in
the delta payload, even when they match the expanded base. Other files retain
ordinary delta compression. The format is unchanged.

Older checkpoints may omit the generated `Singularity` definition. Restoration
can reconstruct the known `bootstrap: localimage` definition from the recorded
original image path only when both its length and SHA256 exactly match the
stored file record. It uses an in-memory stream and never modifies the base
cache or checkpoint. Custom definitions still use the normal payload/base path.
Unknown historical generated content is not guessed: any remaining mismatch
fails restoration. In particular, an old omitted build-time label file cannot
be recovered from an unavailable timestamp merely by ignoring its hash.

SIF identity, payload digest, individual file hashes, exact reconstructed tar
hash, and subsequent Git/patch checks remain enforced. This does not make
checkpoints portable across arbitrary Apptainer versions or missing bind data.

The generated-file audit used Apptainer v1.5.5's
[metadata implementation](https://github.com/apptainer/apptainer/blob/v1.5.5/internal/pkg/build/metadata.go).
For a local-SIF build without recipe sections, scripts are not regenerated from
the host path; previous definitions moved into history originate in the SIF.
Real-node validation must also compare metadata from separate expansion paths.
