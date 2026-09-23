FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY skillcurrent ./skillcurrent
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin skillcurrent \
 && mkdir -p /data \
 && chown skillcurrent:skillcurrent /data
USER skillcurrent
VOLUME ["/data"]
EXPOSE 8765
ENV SKILLCURRENT_DB=/data/skillcurrent.sqlite \
    PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status == 200 else 1)"
ENTRYPOINT ["skillcurrent"]
# A team's own server. For the internet-facing waitlist host use:
#   serve --host 0.0.0.0 --port 8765 --public --trust-proxy
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
