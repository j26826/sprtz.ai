FROM nginx:1.27-alpine

RUN rm -rf /usr/share/nginx/html/* /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
COPY src/app.js /usr/share/nginx/html/app.js
COPY src/app.css /usr/share/nginx/html/app.css
COPY src/i18n.js /usr/share/nginx/html/i18n.js
COPY src/settings.js /usr/share/nginx/html/settings.js
# Brand wordmark and app icon. Served from the app's own origin so they are
# covered by the same cache and CSP as everything else.
COPY src/assets/ /usr/share/nginx/html/assets/
# The Modernist design system, vendored. It is the source of truth for every
# colour, space and radius the app uses.
COPY src/ds/ /usr/share/nginx/html/ds/
COPY entrypoint.sh /entrypoint.sh

# nginx writes the generated config.js and its temp dirs at runtime.
RUN chmod +x /entrypoint.sh \
    && chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx \
    && mkdir -p /tmp/client_body && chown -R nginx:nginx /tmp

USER nginx
EXPOSE 8080

CMD ["/entrypoint.sh"]
