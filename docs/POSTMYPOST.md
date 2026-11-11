# Postmypost → VK

The integration uses the official Postmypost v4.1 REST API as the only VK
publisher. It never calls the VK API. Telegram and Telegram → Telegraph remain
the primary, unchanged publication path.

## Configuration

Set these values only on the server `.env` file:

```dotenv
POSTMYPOST_ENABLED=1
POSTMYPOST_API_TOKEN=...
POSTMYPOST_PROJECT_ID=...
POSTMYPOST_VK_ACCOUNT_ID=...
```

Create the token in Postmypost, then use `GET /projects` and `GET /accounts`
with that token to identify the project and the connected VK account. Do not
put the token in Git, an n8n node, logs, or a chat transcript. The feature is
off unless `POSTMYPOST_ENABLED=1` and all three identifiers are present.

## Behaviour and recovery

After Telegram is sent successfully, the optional VK step imports the public
hero image or uploads a local file, waits for a Postmypost `file_id`, and
creates a single-account publication. Immediate and scheduled publications use
the same documented `POST /publications` shape; a scheduled call includes
`post_at`.

`social_publications` is keyed by `(article_id, social_network)`. It records
the provider publication ID, lifecycle status, destination URL, time and an
error summary. A timeout or 5xx while creating the publication becomes
`unknown`; automatic retries stop so a second VK post cannot be created.
`created` and `scheduled` rows are polled every 15 minutes without DeepSeek;
a real HTTP(S) destination URL is the condition for `published`.

Set `POSTMYPOST_ENABLED=0` to stop only this destination. Existing database
rows are retained for audit/reconciliation. Reverting this feature's commit or
returning to `main` restores the previous Telegram-only route.

## Confirmed official v4.1 contract

- bearer authentication: `Authorization: Bearer <token>`;
- `GET /projects`, `GET /accounts`;
- `POST /upload/init`, `POST /upload/complete`, `GET /upload/status`;
- `POST /publications`, `GET /publications/:id`;
- image URL import uses `project_id` and `url`, then completion; local file
  uses `project_id`, `name`, `size`, object-store upload, then completion;
- upload status `1` with `file_id` is required before post creation;
- creation body has `account_ids`, `publication_status: 4`, and
  `details: [{publication_type: "post", content, file_ids}]`.

Sources: [Postmypost API v4.1](https://help.postmypost.io/docs/api/) and the
[official n8n cookbook](https://help.postmypost.io/docs/cookbook/n8n).
