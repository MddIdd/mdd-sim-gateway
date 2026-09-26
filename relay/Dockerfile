# MDD Sim Gateway media relay: the upstream coturn release plus an entrypoint that places it on
# the media network. Used only in relay media mode (control/app/media.py).
#
# Release builds publish it as ghcr.io/<owner>/mdd-sim-gateway-relay:<version>; a source install
# builds it only when the relay is switched on (install.sh media relay). The base is pinned by its
# multi-arch index digest, so a re-tagged upstream image cannot change what gets built.
ARG COTURN_IMAGE=coturn/coturn:4.17.2-debian@sha256:aa68aab64a3b929d57fc2924c98ea447bf996cf8dade2508e7b71eaf23f1f14e
FROM ${COTURN_IMAGE}

USER root
COPY entrypoint.sh /usr/local/bin/mdd-relay-entrypoint
# The upstream turnserver carries a file capability (cap_net_bind_service) for binding ports below
# 1024. The relay binds none, and the control plane drops every capability, which makes the kernel
# refuse to execute a binary that asks for one. Rewriting the file drops the attribute.
RUN chmod 0755 /usr/local/bin/mdd-relay-entrypoint \
    && install -m 0755 /usr/bin/turnserver /usr/bin/turnserver.plain \
    && mv /usr/bin/turnserver.plain /usr/bin/turnserver
# The relay faces the internet. It binds unprivileged ports only and needs no capability; the
# control plane also drops them all and makes the root filesystem read-only.
USER nobody:nogroup

ARG MDD_VERSION=""
LABEL io.mdd-sim-gateway.managed="true" \
      io.mdd-sim-gateway.component="relay" \
      org.opencontainers.image.version="${MDD_VERSION}" \
      org.opencontainers.image.source="https://github.com/MddIdd/mdd-sim-gateway" \
      org.opencontainers.image.title="MDD Sim Gateway media relay"
ENTRYPOINT ["/usr/local/bin/mdd-relay-entrypoint"]
