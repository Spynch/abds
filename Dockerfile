FROM python:3.14-slim
WORKDIR /service
COPY app ./app
CMD ["python", "-m", "app.server"]
