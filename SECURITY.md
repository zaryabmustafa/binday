# Security

Binday deletes data on Mattermost servers and holds the SSH and database logins to do it, so a security bug in it matters. Please report one privately.

## Reporting a vulnerability

Use [GitHub's private vulnerability reporting](https://github.com/zaryabmustafa/binday/security/advisories/new): on the repository's **Security** tab, choose **Report a vulnerability**. Please don't open a public issue for it.

Say what an attacker can do and what they need for it (access to the computer running Binday, a position on the network, control of the Mattermost server, and so on), and how to reproduce it. You'll get an answer within a week, and a fix or a plan once the report is confirmed. Reporters are credited in the release notes unless they'd rather not be.

## Supported versions

Only the [latest release](https://github.com/zaryabmustafa/binday/releases/latest) gets fixes.

## Most worth looking at

- **What gets deleted.** Binday must only delete items the user reviewed and confirmed, and must re-check each one right before deleting it ([safety rules](docs/how-it-works.md#safety-rules)). Anything that makes it delete something else, or skip the re-check, is in scope.
- **Commands run on the server.** Binday runs `mmctl`, `du` and `docker exec` over SSH with arguments built from database values (channel and post IDs). Injection into those commands is in scope.
- **SQL.** Queries are built from table and column names read from `information_schema`, with values passed as parameters.
- **Secrets.** SSH and database passwords and `mmctl` tokens are kept in the OS keychain, never in `~/.binday/profiles.json` or the log. A leak into files, logs or screenshots is in scope.
- **The SSH tunnel** to PostgreSQL listens on `127.0.0.1` only.
- **Host keys.** Binday trusts a server's SSH host key the first time it sees it and refuses a key that changed from one in `known_hosts`.
