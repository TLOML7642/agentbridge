# Build only from an isolated allowlisted context, never the production tree.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE} AS runtime
ARG SOURCE_VERSION=unknown
ENV AGENTBRIDGE_BUILD_SOURCE_VERSION=${SOURCE_VERSION}
WORKDIR /app
COPY runtime/node/ /opt/node/
COPY runtime/deps/ /opt/deps/
COPY assistant.py assistant_projects.py assistant_executor.py agent_runtime.py agent_services.py process_supervisor.py agent_diagnostics.py ./
COPY agent_character.py ./
COPY computer_actions.py ./
COPY agent_driver.mjs agent_persona.mjs agent_rpc.mjs native_boundary.mjs agent_reads.py ./
COPY model_budget.mjs ./
COPY tools/check_server_runtime.py ./tools/check_server_runtime.py
COPY server/assistant/package.json server/assistant/package-lock.json server/assistant/runtime-artifacts.json ./server/assistant/
ENV PATH=/opt/node/bin:/usr/local/bin:/usr/bin:/bin \
    HOME=/tmp/home \
    AGENTBRIDGE_AGENT_NODE=/opt/node/bin/node \
    AGENTBRIDGE_AGENT_PACKAGES=/opt/deps/node_modules/@deepseek-ai \
    AGENTBRIDGE_AGENT_LEARNING=0 \
    PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/python3", "-B", "/app/assistant.py"]

# Both targets share the additive logical-project state contract. Neither
# target restores a Mac database. Freeze and retain the baseline content ID.
FROM runtime AS baseline
LABEL org.agentbridge.role=assistant-baseline org.agentbridge.state=logical-projects-v1
FROM runtime AS candidate
LABEL org.agentbridge.role=assistant-candidate org.agentbridge.state=logical-projects-v1
