# The release image: the static musl binary on an empty root file system.
# Built by .github/workflows/release-binaries.yml (the `image` job) from the
# attested release archives, never from source. This file is a template:
# `release_binaries.py stage-image` replaces @BINARY@ with the crate's binary
# name and writes the result into the build context beside the binaries:
#   Dockerfile  linux-amd64/<binary>  linux-arm64/<binary>
# (ENTRYPOINT's exec form cannot expand a build argument, hence the rendering.)
#
#   docker buildx build --platform linux/amd64,linux/arm64 <context>
#
# `scratch` has no CA certificates, shell or /tmp. A CLI that makes TLS
# connections needs a base that has them: switch to
# `gcr.io/distroless/static-debian13:nonroot@sha256:<digest>` (digest-pinned,
# Renovate keeps it current) and keep the rest.
FROM scratch
ARG TARGETARCH
ARG SOURCE_URL=""
ARG VERSION=""
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="GPL-3.0-only"
COPY --chmod=0555 linux-${TARGETARCH}/@BINARY@ /@BINARY@
USER 65532:65532
ENTRYPOINT ["/@BINARY@"]
