# Repository review guidelines

Review this repository as kairos-inteligencia-artificial. Preserve existing code conventions and flag concrete defects with file locations. Do not invent missing tests or claim static syntax checks are integration tests.

Check authentication, authorization, secret exposure and input validation where applicable. Never use production credentials, call paid AI providers or modify real databases during review.

Required validation: Validate Python and notebooks. CodeQL: python. For scaffold-only repositories, review artifacts that actually exist; do not demand an application build.
