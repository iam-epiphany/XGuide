# 基础镜像说明：优先使用本地可离线获取的 tag（node:22-bookworm-slim / nginx:alpine），
# 避免国内网络拉取 Docker Hub（node:22-alpine / nginx:1.27-alpine）超时；
# 有外网环境同样可正常构建，功能无差异。
FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine@sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752 AS runtime
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
