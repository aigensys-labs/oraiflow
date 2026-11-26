FROM node:22-alpine

WORKDIR /runtime

COPY --chown=node:node package.json ./
RUN npm install --no-audit --no-fund

COPY --chown=node:node server.js ./

ENV NODE_ENV=production PORT=3001
EXPOSE 3001

USER node
CMD ["npm", "start"]
