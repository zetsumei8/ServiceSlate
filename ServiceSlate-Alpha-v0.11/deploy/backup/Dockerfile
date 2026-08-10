FROM postgres:16-alpine
RUN apk add --no-cache curl ca-certificates \
    && curl -fsSL https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/local/bin/mc \
    && chmod +x /usr/local/bin/mc
COPY backup.sh /usr/local/bin/serviceslate-backup
RUN chmod +x /usr/local/bin/serviceslate-backup
ENTRYPOINT ["/usr/local/bin/serviceslate-backup"]
