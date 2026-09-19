# Runs Warp's Oracle integration tests on arm64, where no macOS Oracle ODBC
# driver exists. Mirrors what CI installs on its x86-64 runner.
FROM python:3.12-slim

ARG IC=https://download.oracle.com/otn_software/linux/instantclient/2360000
# Instant Client links against libaio.so.1; trixie ships the t64 rebuild
# under a different soname, so install whichever exists and alias it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        unixodbc unixodbc-dev odbcinst curl unzip ca-certificates \
    && (apt-get install -y --no-install-recommends libaio1t64 \
        || apt-get install -y --no-install-recommends libaio1) \
    && ln -sf /usr/lib/aarch64-linux-gnu/libaio.so.1t64 \
              /usr/lib/aarch64-linux-gnu/libaio.so.1 2>/dev/null || true \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSLo /tmp/basic.zip "$IC/instantclient-basiclite-linux.arm64-23.6.0.24.10.zip" \
 && curl -sSLo /tmp/odbc.zip  "$IC/instantclient-odbc-linux.arm64-23.6.0.24.10.zip" \
 && mkdir -p /opt/oracle && unzip -q -o /tmp/basic.zip -d /opt/oracle \
 && unzip -q -o /tmp/odbc.zip -d /opt/oracle && rm /tmp/*.zip

# The driver's soname carries the client version, so discover it rather than
# pinning it to whatever the current Instant Client release happens to call it.
RUN IC_DIR="$(find /opt/oracle -maxdepth 1 -name 'instantclient_*' | head -1)" \
 && DRIVER="$(find "$IC_DIR" -name 'libsqora.so*' | head -1)" \
 && test -n "$DRIVER" \
 && echo "$IC_DIR" > /etc/ld.so.conf.d/oracle-instantclient.conf && ldconfig \
 && printf '\n[Oracle 23 ODBC driver]\nDescription=Oracle ODBC\nDriver=%s\nFileUsage=1\n' \
      "$DRIVER" >> /etc/odbcinst.ini \
 && echo "registered: $DRIVER"

WORKDIR /app
COPY requirements-it.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-it.txt
ENV PYTHONPATH=/app/src
