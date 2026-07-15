FROM node:23-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    ipmitool nmap sshpass jq bash iputils-ping \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
EXPOSE 8448
CMD ["node", "dist/index.js"]
