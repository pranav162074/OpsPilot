# Kuturl (github.com/pranav162074/Kuturl), the service OpsPilot investigates in the demo.
# Built straight from its repository, pinned to a commit, so the demo is reproducible.
FROM node:22-alpine

ARG KUTURL_REPO=https://github.com/pranav162074/Kuturl.git
ARG KUTURL_REF=a0ea1acd6d834a6e80fff403bef86e2271990b6d

RUN apk add --no-cache git curl
WORKDIR /src
RUN git init -q . \
    && git remote add origin "${KUTURL_REPO}" \
    && git fetch -q --depth 1 origin "${KUTURL_REF}" \
    && git checkout -q FETCH_HEAD \
    && rm -rf .git

WORKDIR /src/backend
# Dev dependencies too: autocannon generates the load for the traffic scenarios
RUN npm ci --no-audit --no-fund

COPY traffic.mjs /traffic/traffic.mjs

EXPOSE 3000
# Apply migrations, then serve (the worker runs inside the web process: RUN_WORKER=true)
CMD ["sh", "-c", "npm run migrate && npm start"]
