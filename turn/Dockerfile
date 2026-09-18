# MDD Sim Gateway media relay: the upstream coturn release, plus nftables so the relay can reach
# nothing but the engines' browser-media sockets.
#
# Build context = turn/ (install.sh: docker build -t mdd-sim-gateway/turn:<version> turn).
# The base is pinned by its multi-arch index digest, so a re-tagged upstream image cannot change
# what gets built.
ARG COTURN_IMAGE=coturn/coturn:4.17.2-debian@sha256:aa68aab64a3b929d57fc2924c98ea447bf996cf8dade2508e7b71eaf23f1f14e
FROM ${COTURN_IMAGE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends nftables \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/mdd-turn-entrypoint
RUN chmod 0755 /usr/local/bin/mdd-turn-entrypoint

ARG TURN_FP=""
LABEL io.mdd-sim-gateway.managed="true" \
      io.mdd-sim-gateway.turn-fp="${TURN_FP}" \
      org.opencontainers.image.source="https://github.com/MddIdd/mdd-sim-gateway" \
      org.opencontainers.image.title="MDD Sim Gateway media relay"
ENTRYPOINT ["/usr/local/bin/mdd-turn-entrypoint"]
