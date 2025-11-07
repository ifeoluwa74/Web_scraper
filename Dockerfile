FROM alpine:latest
WORKDIR /app
COPY Flight_result.json .
CMD ["cat", "Flight_result.json"]
