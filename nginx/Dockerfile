FROM nginx:alpine

# netcat for the startup wait script, openssl for htpasswd generation
# nodejs/npm to download Monaco editor locally (avoids CDN @latest redirect loop)
RUN apk add --no-cache openssl netcat-openbsd nodejs npm

# Download Monaco editor at a pinned version and serve it locally.
# The pre-built benchmarker UI loads Monaco from cdn.jsdelivr.net/@latest which
# causes ERR_TOO_MANY_REDIRECTS in some browsers. We intercept the URL via
# sub_filter and serve Monaco from /monaco-cdn/ instead.
RUN npm install --prefix /tmp/monaco monaco-editor@0.52.2 --omit=dev --ignore-scripts 2>/dev/null && \
    mkdir -p /usr/share/nginx/monaco && \
    cp -r /tmp/monaco/node_modules/monaco-editor/min/vs /usr/share/nginx/monaco/ && \
    rm -rf /tmp/monaco

# Remove nodejs/npm from final image to keep it lean
RUN apk del nodejs npm

COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY pool_website /usr/share/nginx/html
COPY nginx/entrypoint.sh /entrypoint.sh
# Strip Windows CRLF line endings and make executable
RUN sed -i 's/\r//' /entrypoint.sh && chmod +x /entrypoint.sh

# .htpasswd is generated at runtime from OPERATOR_USER / OPERATOR_PASSWORD
# env vars so the password is never baked into an image layer.

ENTRYPOINT ["/entrypoint.sh"]
