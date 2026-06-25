FROM node:22-slim

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY index.js ./

USER node
EXPOSE 3000

CMD ["node", "index.js"]
