FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY skillcurrent ./skillcurrent
RUN pip install --no-cache-dir .
VOLUME ["/data"]
EXPOSE 8765
ENV SKILLCURRENT_DB=/data/skillcurrent.sqlite
ENTRYPOINT ["skillcurrent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
