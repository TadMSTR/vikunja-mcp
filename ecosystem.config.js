// Reference PM2 process definition. The container is the supported artefact
// (see docs/docker.md); this is here for anyone supervising the installed
// package directly. Paths and URL are examples — change them.
// The run.sh wrapper sources an env file (never committed) and execs the
// installed entry point. Vikunja API tokens are NOT set here — this server is stateless and
// reads each caller's token from the incoming request (see SECURITY.md, token passthrough).
module.exports = {
  apps: [{
    name: "vikunja-mcp",
    script: "/opt/vikunja-mcp/run.sh",
    interpreter: "bash",
    env: {
      LOG_LEVEL: "INFO",
      VIKUNJA_URL: "https://vikunja.example.com",
      VIKUNJA_HOST: "127.0.0.1",
      VIKUNJA_PORT: "8501",
    },
    restart_delay: 5000,
    max_restarts: 10,
    watch: false,
  }]
};
