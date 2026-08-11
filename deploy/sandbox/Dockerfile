# The runtime image every sandbox is built from.
#
# Built and pinned by digest, and the Sandbox Controller accepts no other —
# technical design §11.3. An Agent cannot name an image; the allowlist is the
# only source, and it holds this one.
#
# It contains almost nothing on purpose. `shell.exec` needs a shell and the
# ordinary file utilities; everything else is surface. In particular there is no
# `curl` and no `wget`: the container is created with `network_mode=none`, so a
# network tool could only ever fail, and a failing `curl` invites a bug report
# about `curl` when the answer is that there is no network at all.
FROM debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258

# Numeric and fixed, matching SANDBOX_UID in the container policy. The platform
# passes `--user 10001:10001` rather than a name, so the container's own
# /etc/passwd cannot decide who the platform meant.
ARG SANDBOX_UID=10001

RUN set -eu; \
    groupadd --gid "${SANDBOX_UID}" sandbox; \
    useradd --uid "${SANDBOX_UID}" --gid "${SANDBOX_UID}" --home-dir /workspace/data \
            --no-create-home --shell /bin/bash sandbox; \
    # Both writable paths are mounted as named volumes. They exist here so the
    # volume inherits this ownership on first use — a mount point owned by root
    # is a workspace the sandbox user cannot write to.
    mkdir -p /workspace/data /workspace/cache; \
    chown -R "${SANDBOX_UID}:${SANDBOX_UID}" /workspace; \
    # apt is left unusable rather than merely unused. With no network it could
    # not fetch anything anyway, but a package manager present in a sandbox is
    # an invitation to try, and its lists are dead weight in every layer.
    rm -rf /var/lib/apt/lists/* /etc/apt/sources.list.d/*

USER ${SANDBOX_UID}:${SANDBOX_UID}
WORKDIR /workspace/data

# Overridden by the Controller, which passes `sleep infinity`. Declared so the
# image is runnable on its own for a human checking what is inside it.
CMD ["sleep", "infinity"]
