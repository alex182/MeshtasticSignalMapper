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

RUN mkdir -p /app/static

COPY . .

EXPOSE 5000

CMD ["python", "main.py"]
