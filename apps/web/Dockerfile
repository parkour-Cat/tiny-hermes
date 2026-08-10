FROM node:24.6.0-alpine AS build

WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.15.0 --activate
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web ./apps/web
RUN pnpm install --frozen-lockfile
RUN pnpm --filter @tiny-hermes/web build

FROM nginx:1.29.1-alpine AS runtime

COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/web/dist /usr/share/nginx/html
EXPOSE 3000
