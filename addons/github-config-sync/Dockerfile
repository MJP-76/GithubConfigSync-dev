ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

COPY rootfs /

RUN chmod a+x /usr/local/bin/start.sh \
    && pip3 install --no-cache-dir flask==3.0.3

EXPOSE 8099

CMD ["/usr/local/bin/start.sh"]
