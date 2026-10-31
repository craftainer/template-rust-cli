# The release image: the static musl binary on an empty root file system.
# Built by .github/workflows/release-binaries.yml (the `image` job) from the
# attested release archives, never from source; the build context is the
# directory `release_binaries.py stage-image` writes:
#   linux-amd64/cli  linux-arm64/cli
#
#   docker buildx build -f scripts/binaries/Dockerfile --platform linux/amd64,linux/arm64 <context>
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
COPY --chmod=0555 linux-${TARGETARCH}/cli /cli
USER 65532:65532
ENTRYPOINT ["/cli"]
