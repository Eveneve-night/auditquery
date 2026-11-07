FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ICT_DOCKER_BIND=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements-product.txt /app/
RUN pip install --no-cache-dir -r requirements-product.txt \
    && groupadd --gid 10001 ict && useradd --uid 10001 --gid ict --no-create-home ict \
    && mkdir /instance && chown ict:ict /instance
COPY --chown=ict:ict src /app/src
COPY --chown=ict:ict configs /app/configs
COPY --chown=ict:ict domain-packs /app/domain-packs
COPY --chown=ict:ict data /app/data
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -m backend.product.server check --instance /instance --mode offline --host 0.0.0.0 || exit 1
CMD ["python", "-m", "backend.product.server", "serve", "--instance", "/instance", "--mode", "offline", "--host", "0.0.0.0"]
