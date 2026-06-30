# Contributing to SmartShading

Thanks for your interest in SmartShading. This is a community project and
contributions, testing and feedback are all welcome.

## Reporting bugs

Please open a [GitHub Issue](https://github.com/Mikasmarthome/SmartShading/issues)
and include as much of the following as you can:

- Home Assistant version
- SmartShading version
- The relevant part of your configuration (cover type, window orientation,
  selected sensors, behavior mode)
- Relevant log lines (`Settings → System → Logs → smartshading`)
- A Support Export, if possible

Please do **not** post secrets, tokens, access credentials or other private
data. Review logs and exports before sharing them.

## Questions and feedback

For setup or behavior questions, please use
[GitHub Discussions](https://github.com/Mikasmarthome/SmartShading/discussions)
rather than issues. Describe what you expected SmartShading to do and what
happened instead.

## Pull requests

Pull requests are welcome. For anything beyond a small fix, please open an issue
or discussion first so the approach can be agreed before you invest time.

Please make sure changes:

- Keep all control logic local — no external services or cloud dependencies.
- Follow Home Assistant integration conventions.
- Do not break existing entities, configuration or backward compatibility.
- Stay focused: one logical change per pull request.

## Especially welcome

Testing with different cover types, facade orientations, solar and rain sensors,
window-contact setups, and translations into additional languages.
