# Base image pinned by digest so builds are reproducible and a customer's
# image is exactly the one we tested. To move to a newer Node:
#   docker pull node:<version>-slim && docker image inspect node:<version>-slim \
#     | jq -r '.[0].RepoDigests[0]'
#
# node:22.23.4-slim is the October 2026 security release (10 CVEs, including
# CVE-2026-111497, an out-of-bounds write in the bundled Brotli decoder, and
# two TLS hostname-validation bypasses that matter here because this sidecar
# makes outbound TLS calls to RingCentral holding RC credentials).
# Do not pin below 22.23.4 on the Node 22 line.
FROM node:22.23.4-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d

WORKDIR /app

# npm ci installs exactly what package-lock.json pins. `npm install` without a
# lockfile let all 94 transitive packages float, so no two builds were alike
# and what a customer actually ran could not be audited.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY index.js ./

USER node
EXPOSE 3000

CMD ["node", "index.js"]
