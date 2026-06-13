# Session-affinity gateway: HAProxy with consistent-hash routing on
# the X-Session-Id header, in front of the agent ECS service.
#
# Why HAProxy: simplest mainstream L7 LB that supports consistent
# hash on a header. ~30 lines of config, one binary, well-known
# operationally. Alternatives (Envoy, ECS Service Connect) are more
# powerful but pull in more moving parts than this gateway needs.
FROM haproxy:3.0-alpine

USER root

# HAProxy needs `socat` for the runtime socket if we want to add
# admin / stats access via TCP. Skipping for now — keep image minimal.

# Healthcheck endpoint runs on :8404 (stats listener defined in
# haproxy.cfg). Container's main listener is :80.
EXPOSE 80 8404

# deploy.sh builds from the repo root, so this path is rooted there.
COPY gateway/haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg

# HAProxy refuses to start if the config can't be parsed; doing the
# check at build time catches typos before the container ever ships.
# Real values are injected at runtime via ECS task env; stub them
# here so the parser is happy.
RUN AGENT_SERVICE_DNS=agent.example.local \
    AGENT_SERVICE_PORT=8080 \
    haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg

USER haproxy
