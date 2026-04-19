FROM node:20-slim AS builder

WORKDIR /app

COPY apps/console/package.json apps/console/package-lock.json* ./
RUN npm install

COPY apps/console ./
RUN npm run build

FROM node:20-slim

WORKDIR /app

COPY --from=builder /app/build ./build
COPY --from=builder /app/package.json ./package.json

EXPOSE 5173

ENTRYPOINT ["node", "build"]
