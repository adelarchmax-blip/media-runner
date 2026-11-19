# media-runner

Ephemeral GitHub Actions worker: pulls download jobs from a private controller API,
fetches the media with yt-dlp, normalizes a 2:3 poster, uploads to an rclone remote and
reports progress back. All endpoints, paths and credentials come from repository secrets;
logs contain job IDs only.

Required secrets: `RUNNER_API_URL`, `RUNNER_API_TOKEN`, `RCLONE_CONFIG_BASE64`,
`RCLONE_REMOTE`, `MEDIA_BASE_PATH`.
