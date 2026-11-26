FROM node:22-alpine AS build
WORKDIR /web
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY index.html vite.config.js ./
COPY src ./src
COPY public ./public
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html

# Vite's copied public assets can retain restrictive source permissions (e.g. 770).
# Nginx serves files as an unprivileged worker, so normalize the static tree.
RUN find /usr/share/nginx/html -type d -exec chmod 755 {} \; \
    && find /usr/share/nginx/html -type f -exec chmod 644 {} \;

EXPOSE 80
