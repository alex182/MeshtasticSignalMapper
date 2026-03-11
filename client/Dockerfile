FROM python:3.12-alpine

RUN apk add --no-cache \
    gcc \
    musl-dev \
    linux-headers \
    libusb-dev \
    eudev-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
