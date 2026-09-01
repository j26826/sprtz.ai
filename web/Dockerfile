FROM nginx:1.27-alpine

RUN rm -rf /usr/share/nginx/html/* /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
COPY src/app.js /usr/share/nginx/html/app.js
COPY src/styles.css /usr/share/nginx/html/styles.css
COPY entrypoint.sh /entrypoint.sh

# nginx needs to write the generated config.js and its temp dirs at runtime.
RUN chmod +x /entrypoint.sh \
    && chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx \
    && mkdir -p /tmp/client_body && chown -R nginx:nginx /tmp

USER nginx
EXPOSE 8080

CMD ["/entrypoint.sh"]
