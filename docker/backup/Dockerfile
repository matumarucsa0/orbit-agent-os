# Backup sidecar: pg_dump (same major version as the db service) + age + rclone.
FROM postgres:16-alpine
RUN apk add --no-cache age rclone curl tzdata     && mkdir /config && touch /config/rclone.conf
# Remotes come from the environment (RCLONE_CONFIG_<NAME>_...); the file only stays empty.
ENV RCLONE_CONFIG=/config/rclone.conf
COPY lib.sh /usr/local/lib/orbit-backup/lib.sh
COPY backup.sh restore.sh scheduler.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh /usr/local/bin/scheduler.sh
ENTRYPOINT []
CMD ["scheduler.sh"]
