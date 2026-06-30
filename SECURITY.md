# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| Latest stable release (currently the v1.0.x line) | ✅ |
| Current pre-release line (currently the v1.1.0-beta line) | ✅ |
| Older releases | ❌ |

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue or
discussion for them.

Use GitHub's private vulnerability reporting for this repository: open the
[Security Advisories page](https://github.com/Mikasmarthome/SmartShading/security/advisories)
and choose **"Report a vulnerability"**. This keeps the report private until a
fix is available.

When reporting, please avoid including sensitive details in any public location,
and review logs or exports for secrets before sharing them.

## Scope

Security-relevant topics for SmartShading include, for example:

- Unsafe or unintended cover command dispatch
- Unintended cover movement that could affect safety or hardware
- Exposure of diagnostics, configuration or secrets through exports or logs
- Behavior that could put a Home Assistant instance into an unsafe state

## Response

SmartShading is a community project, so responses are made on a best-effort
basis. There is no guaranteed response time or service-level agreement. Reports
are reviewed and addressed as capacity allows, with priority given to issues
that affect safety or data exposure.
