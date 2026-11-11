# Postmypost → VK workflow

Import `postmypost-vk-workflow.json` into n8n on the Singapore server. Keep it
inactive until the Python-side review is accepted. The webhook expects a JSON
body with `text`, public `image_url`, and optional ISO-8601 `post_at`. It waits
ten seconds, then refuses to create a post until the upload status is `1` and a
real `file_id` is present; retry a still-processing n8n execution.

Set these n8n environment variables, never workflow literals: `POSTMYPOST_API_TOKEN`,
`POSTMYPOST_PROJECT_ID`, and `POSTMYPOST_VK_ACCOUNT_ID`. The Postmypost API
uses `Authorization: Bearer …`. The workflow is a transport template; the
Python `PostmypostPublisher` is the production integration because it persists
the duplicate barrier and provider result in SQLite.

Postmypost's official n8n cookbook confirms `POST /upload/init`, polling
`GET /upload/status` until `status: 1` yields `file_id`, then
`POST /publications` with `publication_status: 4` and
`details[].publication_type: "post"`. n8n's template deliberately remains
inactive and is not deployed by this repository.
