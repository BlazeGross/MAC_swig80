FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 backtest \
    && useradd --uid 10001 --gid backtest --no-create-home backtest \
    && mkdir -p /app/output \
    && chown backtest:backtest /app/output

COPY swig80_backtest.py ./
USER 10001:10001
ENTRYPOINT ["python", "swig80_backtest.py"]
CMD ["--out", "/app/output"]
