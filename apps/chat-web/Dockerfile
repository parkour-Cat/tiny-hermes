FROM node:24.6.0-alpine AS build

WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.15.0 --activate
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/chat-web ./apps/chat-web
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @tiny-hermes/chat-web build

FROM nginx:1.29.1-alpine AS runtime

# `/etc/nginx/templates/*.template` -> `/etc/nginx/conf.d/*.conf`, run
# through `envsubst` by the base image's own entrypoint before nginx starts
# (task-9 review finding B) — this is what lets `CHAT_WEB_FRAME_ANCESTORS`
# be deploy-time configuration rather than a value baked into the image.
# The `ENV` below is what keeps that substitution from ever hitting an
# *unset* shell variable: nginx's own `$request_id`/`$host`/`$scheme` in
# this same file are untouched by envsubst precisely because they are not
# environment variables, but `$CHAT_WEB_FRAME_ANCESTORS` is one, and an
# unset one would leave nginx trying to parse an nginx variable it never
# declared rather than the closed-by-default policy this is meant to be.
ENV CHAT_WEB_FRAME_ANCESTORS="'none'"
COPY apps/chat-web/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/apps/chat-web/dist /usr/share/nginx/html
EXPOSE 3001
