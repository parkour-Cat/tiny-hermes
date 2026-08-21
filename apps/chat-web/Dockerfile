FROM node:24.6.0-alpine AS build

WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.15.0 --activate
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/chat-web ./apps/chat-web
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @tiny-hermes/chat-web build

FROM nginx:1.29.1-alpine AS runtime

COPY apps/chat-web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/chat-web/dist /usr/share/nginx/html
EXPOSE 3001
