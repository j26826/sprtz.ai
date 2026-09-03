FROM nginx:1.27-alpine

RUN rm -rf /usr/share/nginx/html/* /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html

# The whole of src/, not a list of files. The app is ES modules served as
# written, so a module that is not copied is a bare `import` answered with 404:
# app.js never runs, and the screen is blank with nothing naming the missing
# file. A per-file list is a second place to remember, and adding moments.js
# proved it is the place that gets forgotten.
#
# This lands src/assets/ at /assets/ and src/ds/ at /ds/, which is where the
# nginx cache rules and the brand markup expect them. src/ holds nothing that
# is not meant to be served: the app has no build step, so what is written is
# what ships.
COPY src/ /usr/share/nginx/html/
COPY entrypoint.sh /entrypoint.sh

# nginx writes the generated config.js and its temp dirs at runtime.
RUN chmod +x /entrypoint.sh \
    && chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx \
    && mkdir -p /tmp/client_body && chown -R nginx:nginx /tmp

USER nginx
EXPOSE 8080

CMD ["/entrypoint.sh"]
