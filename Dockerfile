# Ceramic Stamp Maker — Cloud Run image.
# Packages the Python toolchain plus the native converters the pipeline shells out to:
# potrace (tracing), Ghostscript (EPS/PS/AI via Pillow), librsvg (SVG previews),
# and OpenSCAD (geometry + .3mf rendering).
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3.13 \
        python3.13-venv \
        python3-pip \
        potrace \
        ghostscript \
        librsvg2-bin \
        openscad \
        fonts-dejavu-core \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Debian's OpenSCAD is the 2021.01 release; the project is verified against newer
# snapshot builds, so prefer a snapshot AppImage when one is bundled, else PATH.
ENV OPENSCAD=/usr/bin/openscad

WORKDIR /app

# Python dependencies first so image layers cache across code edits.
COPY processor/requirements.txt /app/processor/requirements.txt
RUN python3.13 -m venv /app/processor/.venv \
    && /app/processor/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/processor/.venv/bin/pip install --no-cache-dir -r /app/processor/requirements.txt

COPY png2stamp.sh /app/png2stamp.sh
COPY processor/ /app/processor/
RUN chmod +x /app/png2stamp.sh /app/processor/start.sh

# Fail the build if a host (macOS) virtualenv leaked into the context: its
# interpreter symlink points at Homebrew and its wheels are Mach-O, so the
# server would not start. .dockerignore should prevent this.
RUN test -x /app/processor/.venv/bin/python3.13 \
    && /app/processor/.venv/bin/python -c 'import sys; print("venv python", sys.version.split()[0])' \
    && /app/processor/.venv/bin/python -c 'import cv2, numpy, PIL, pillow_heif; print("deps ok")'

# png2stamp.sh reads STAMP_PYTHON when the server launches it.
ENV STAMP_PYTHON=/app/processor/.venv/bin/python \
    STAMP_HOST=0.0.0.0 \
    PORT=8080

EXPOSE 8080

# Cloud Run sets PORT; the server binds STAMP_HOST and reads PORT as its default.
CMD ["/app/processor/.venv/bin/python", "/app/processor/stamp_tool.py"]