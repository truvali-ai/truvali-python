# Security Policy

## Supported versions

Security fixes are released for the latest published version of `truvali`. Older versions do not receive fixes; please upgrade to the latest release.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Older releases | No |

## Reporting a vulnerability

Please report vulnerabilities privately. **Do not open a public issue, pull request or discussion for a security problem.**

1. Open the [Security tab](https://github.com/truvali-ai/truvali-python/security) of this repository.
2. Choose "Report a vulnerability".
3. Fill in the form and submit it. The report is visible only to the maintainers and to you.

Please include as much of the following as you can:

- The version of `truvali`, the Python version and the operating system
- A description of the problem and of its impact
- The steps or a minimal script that reproduces it
- Any known workaround

Never include real API keys, API secrets, access tokens, webhook secrets or personal data in a report. Use made-up values, and if a real credential was exposed while you investigated, rotate it.

## What to expect

- We acknowledge a report as soon as we reasonably can and tell you whether we can reproduce it.
- We keep you informed while we work on a fix and may ask you for more details.
- We ask you to keep the report confidential until a fixed version is released.
- When a fix is released, we credit you in the release notes if you wish.

## Scope

This policy covers the code in this repository: the `truvali` Python package and its build and release configuration.

If you believe you have found a vulnerability in the Truvalı platform itself rather than in this SDK, you are welcome to report it through the same channel; it is passed on to the team responsible.

Bugs without a security impact belong in the public [issue tracker](https://github.com/truvali-ai/truvali-python/issues).

## Guidance for integrators

- **Keep secrets out of code and logs.** Load the API key and API secret from the environment or a secret manager (`TruvaliConfig.from_env()` reads `TRUVALI_BASE_URL`, `TRUVALI_API_KEY` and `TRUVALI_API_SECRET`). Treat webhook secrets the same way. Never commit them, print them or write them to logs. The SDK hides the API secret and the access token from `repr()` and never logs request bodies, tokens or secrets; keep your own logging just as strict.
- **Never share access tokens across hosts.** A token is bound to the IP address it was issued to. Keep one token per egress IP address and let the client obtain and renew it. A custom `TokenStore` holds a credential: keep it local to one host and protect it accordingly.
- **Always verify webhook signatures.** Pass the raw request body to `construct_event` or `verify_signature` before you act on a delivery, keep the default timestamp tolerance unless you have a reason to change it, and de-duplicate on `event_id`. Never rely on the deprecated `AUTHORIZATION` header of a delivery.
- **Use HTTPS.** Use an `https://` base URL and serve your webhook receiver over HTTPS. The SDK accepts `http://` for local sandboxes only and logs a warning on the `truvali` logger for any host other than `localhost`, `127.0.0.1` or `::1`, because credentials, tokens and records would travel unencrypted.
- **Rotate on exposure.** If an API secret or a webhook secret may have been exposed, rotate it. During a webhook secret rotation, pass both the new and the previous secret to the verification helpers.
