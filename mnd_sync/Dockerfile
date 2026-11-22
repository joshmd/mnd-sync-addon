ARG BUILD_FROM
FROM $BUILD_FROM
WORKDIR /opt/mnd_sync
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python3", "-m", "app"]
