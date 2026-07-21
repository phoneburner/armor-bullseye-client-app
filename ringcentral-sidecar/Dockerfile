# Base image: pinned to a specific version tag. For fully reproducible
# builds pin by digest — look up the current digest with:
#   docker pull node:22.11.0-slim && docker image inspect node:22.11.0-slim \
#     | jq -r '.[0].RepoDigests[0]'
# — and replace the FROM line with FROM node:22.11.0-slim@sha256:<digest>.
FROM node:22.11.0-slim

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY index.js ./

USER node
EXPOSE 3000

CMD ["node", "index.js"]
