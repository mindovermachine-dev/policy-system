# Policy System Agent Skills

One of the client types users use to access Policy System is Agent Skills. The following Agent Skills are officially supported:

- ps-qna. This skill is used to ask policy related Questions and retrieve Answers (QnA) from the Company Policy Knowledge Graph.

## Development of Policy System Skills

Agent Skills are structured in subfolders to the ps-skills folder. Each Agent Skill folder has two sub-folders: `dist` is used for distribution of the packaged Agent Skill and `dev` is used to develop and maintain the Agent SKill.

Policy System Agent Skills should not be part of the Policy System repo skill structure as they are not used across the development team to develop Polciy System, rather they are part of the product so they should be installed into the local user Agent Skill structure if needed for testing the skills.

There is a `install-ps-skills.sh` script in the `scripts` folder that can be used gh cli to install the Policy System Agent skills into your preferred agent