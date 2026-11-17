# The web console's image: the runner's own package with the console's web packages, on the same Ubuntu as the
# VM, so the console and the runner open runner.db with the same SQLite. setup.sh builds it from runner/ and extra/
# (the build context holds those two only) and tags it with the hash of what went in.
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# tzdata: schedule.timezone in config.yaml is checked against the system's zone files.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY runner /src/runner
# The install's own packages (setup.conf RUNNER_EXTRA_PACKAGES), as the wheels setup built for the runner; usually
# none. The console reads config.yaml, so it imports every provider config.yaml names, as the runner does.
COPY extra /src/extra
RUN python3 -m venv /opt/console \
 && /opt/console/bin/pip install --no-cache-dir --disable-pip-version-check "/src/runner[console]" \
 && if [ -n "$(ls /src/extra)" ]; then \
      /opt/console/bin/pip install --no-cache-dir --disable-pip-version-check --no-deps /src/extra/*.whl; fi \
 && rm -rf /src

LABEL arceus.keep="true"
ENTRYPOINT ["/opt/console/bin/arceus-runner"]
CMD ["console", "serve", "--host", "127.0.0.1", "--port", "8180"]
