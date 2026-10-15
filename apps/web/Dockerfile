# Build context is the REPO ROOT (see compose.yaml: `context: .`,
# `dockerfile: apps/web/Dockerfile`) so this stage can see the
# workspace-level pnpm-lock.yaml + pnpm-workspace.yaml. Installing with
# pnpm against a committed lockfile is both faster (content-addressed
# store + BuildKit cache mount survive across builds) and deterministic
# (no npm re-resolving the whole dependency tree from scratch every
# time package.json changes) compared to the previous lockfile-less
# `npm install`.
FROM public.ecr.aws/docker/library/node:22.14.0-alpine AS build

ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    NODE_ENV=development \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH

RUN corepack enable

WORKDIR /app

COPY pnpm-lock.yaml pnpm-workspace.yaml package.json ./
COPY apps/web/package.json ./apps/web/package.json
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --filter @quberi/web... --frozen-lockfile

COPY apps/web ./apps/web
WORKDIR /app/apps/web
RUN pnpm run build

FROM public.ecr.aws/docker/library/nginx:1.27.5-alpine
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/web/dist /usr/share/nginx/html
EXPOSE 80
