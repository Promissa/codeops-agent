FROM node:22-bookworm-slim

ARG CODEGRAPH_VERSION
RUN test -n "$CODEGRAPH_VERSION" \
  && npm install --global "@colbymchenry/codegraph@$CODEGRAPH_VERSION"

WORKDIR /workspace
ENTRYPOINT ["codegraph"]
