# Policy System Agent Skills

One of the client types users use to access Policy System is Agent Skills. The following Agent Skills are officially supported:

- ps-qna. This skill is used to ask policy related Questions and retrieve Answers (QnA) from the Company Policy Knowledge Graph.

## Development of Policy System Skills

Agent Skills are shipped as a single git-hosted Claude plugin, not a separate dev/dist split. `ps-skills/policy-system/` is the plugin directory itself — its `skills/` subfolder and `.mcp.json` connector are exactly what gets installed, so there is no packaging or build step between editing the skill and using it.

Policy System Agent Skills should not be part of the Policy System repo skill structure as they are not used across the development team to develop Policy System, rather they are part of the product so they should be installed into the local user Agent Skill structure if needed for testing the skills.

To install: add this repo as a marketplace and run `/plugin install policy-system` (see `docs/artifacts/user-guide.md`).
