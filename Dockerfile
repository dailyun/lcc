FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY configs ./configs
COPY examples ./examples
COPY eval ./eval
COPY docker/entrypoint.sh /usr/local/bin/lcc-router-entrypoint

RUN python -m pip install --no-cache-dir -e . \
    && useradd --create-home --uid 10001 lcc \
    && mkdir -p eval/reports \
    && chown -R lcc:lcc eval/reports

# Run unprivileged: the eval only needs to write eval/reports.
USER lcc

ENTRYPOINT ["lcc-router-entrypoint"]
CMD ["lcc", "route", "eval", "--cases", "examples/tasks", "--output", "eval/reports/docker_eval.json"]
