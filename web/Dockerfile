# WenQu web 生产镜像(Vue SPA 构建 + nginx 非 root 托管,SSE 反代配置为生死线)。
# 构建(上下文 = 本目录):docker build -t wenqu-web ./web
# VITE_API_BASE_URL 不写死进仓库:代码默认值即 /api/v1(src/api/http.ts),ARG 仅显式覆盖用。
FROM node:22-bookworm-slim AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY --from=build /web/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
