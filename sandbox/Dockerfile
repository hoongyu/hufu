# The box that model-written code runs in. One fresh container per run, destroyed afterwards.
# Nothing here is secret: no keys, no app code, only an interpreter, three libraries and the harness.
# Runtime limits (no network, read-only root, no capabilities, pids/memory/CPU/time caps, gVisor
# when available) are set by app/sandbox.py at `docker run`, not here.
FROM python:3.12-slim

RUN pip install --no-cache-dir pandas==2.2.3 numpy==2.1.3 matplotlib==3.9.2 \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin runner

# Build the matplotlib font cache once, so a run does not spend seconds rebuilding it.
ENV MPLBACKEND=Agg MPLCONFIGDIR=/opt/mplconfig
RUN mkdir -p /opt/mplconfig && python -c "import matplotlib.pyplot" && chmod -R a+rX /opt/mplconfig

COPY harness.py /opt/harness.py
USER 10001
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/harness.py"]
