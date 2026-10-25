# The runtime image every sandbox is built from.
#
# Built and pinned by digest, and the Sandbox Controller accepts no other —
# technical design §11.3. An Agent cannot name an image; the allowlist is the
# only source, and it holds this one.
#
# Small on purpose, with one deliberate exception (§1.27, v2.27): Python and the
# libraries office work needs — spreadsheets, documents, slides, PDFs, charts
# with a Chinese font. Everything else is surface. In particular there is no
# `curl`, no `wget`, no pip and no package manager: the container has no network
# to use them on, and a tool that can only fail invites a bug report about it.

# Stage one exists to compile the openat2 file helper (design §5.2) and is
# thrown away: the runtime image gains one static binary and no compiler.
FROM debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS build
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends gcc libc6-dev; \
    rm -rf /var/lib/apt/lists/*
COPY file_helper.c /src/file_helper.c
RUN gcc -static -O2 -Wall -Werror -o /src/tiny-hermes-file-helper /src/file_helper.c

# python-pptx has no Debian package. Fetched here, checked against its published
# hash, and unpacked without its dependencies, which the runtime stage takes from
# Debian; the downloader stays in this stage.
FROM debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS pptx
ARG PPTX_WHEEL=https://files.pythonhosted.org/packages/d9/4f/00be2196329ebbff56ce564aa94efb0fbc828d00de250b1980de1a34ab49/python_pptx-1.0.2-py3-none-any.whl
ARG PPTX_SHA256=160838e0b8565a8b1f67947675886e9fea18aa5e795db7ae531606d68e785cba
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl python3; \
    curl -fsSL -o /tmp/pptx.whl "${PPTX_WHEEL}"; \
    echo "${PPTX_SHA256}  /tmp/pptx.whl" | sha256sum -c -; \
    mkdir /pptx; \
    python3 -m zipfile -e /tmp/pptx.whl /pptx

FROM debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258

COPY --from=build /src/tiny-hermes-file-helper /usr/local/bin/tiny-hermes-file-helper

# Office work (§1.27, v2.27): spreadsheets (openpyxl, pandas), documents
# (python-docx), slides (python-pptx, below), PDFs (pypdf, pdfminer) and charts
# (matplotlib) with a Chinese font, so a chart's labels are not boxes.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 python3-openpyxl python3-pandas python3-docx python3-pypdf \
        python3-pdfminer python3-matplotlib python3-lxml python3-pil \
        python3-xlsxwriter python3-typing-extensions fonts-wqy-microhei
COPY --from=pptx /pptx/ /usr/lib/python3/dist-packages/

# Matplotlib's settings, named directly: it reads none from a config directory it
# cannot write, and falls back to Debian's. The Chinese font comes first so a
# chart needs no font settings to read, and PDFs embed TrueType so their Chinese
# can be searched and copied. Its cache goes to /tmp, not HOME — the data mount,
# where every checkpoint would carry it — and is rebuilt once per sandbox.
ENV MATPLOTLIBRC=/opt/matplotlib/matplotlibrc MPLCONFIGDIR=/tmp/matplotlib
RUN set -eu; \
    mkdir -p /opt/matplotlib; \
    printf 'font.sans-serif: WenQuanYi Micro Hei, DejaVu Sans\naxes.unicode_minus: False\nbackend: Agg\npdf.fonttype: 42\n' \
        > /opt/matplotlib/matplotlibrc; \
    python3 -c "import matplotlib.font_manager, pptx, docx, openpyxl, pandas, pypdf, pdfminer"; \
    chmod -R a+rX /opt/matplotlib

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
    # No package manager at all. With no network it could not fetch anything
    # anyway, but one present in a sandbox is an invitation to try, and its
    # lists are dead weight in every layer.
    rm -rf /var/lib/apt/lists/* /etc/apt /var/cache/apt /usr/bin/apt* /usr/bin/dpkg* /usr/lib/apt

USER ${SANDBOX_UID}:${SANDBOX_UID}
WORKDIR /workspace/data

# Overridden by the Controller, which passes `sleep infinity`. Declared so the
# image is runnable on its own for a human checking what is inside it.
CMD ["sleep", "infinity"]
