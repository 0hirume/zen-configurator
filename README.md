# zen-configurator

Declarative Zen Browser profiles from TOML.

## Commands

```text
uv run zen-configurator import -d <zen-directory>
uv run zen-configurator render <profile>
uv run zen-configurator render --all
uv run zen-configurator apply <profile> --zen-profile <Zen profile>
uv run zen-configurator apply --all -m <profile>=<Zen profile> -i <Zen installation>
```

`render` writes `user.js`, `policies.json`, and `theme.json` under `generated/` without touching Zen. `apply` writes the profile files and installation-wide policies; it also installs profile-only addons directly into the mapped profile.

## Configuration

`base` is shared. Profile keys are arbitrary and do not need to match Zen profile names.

```toml
[[base.addons]]
id = "uBlock0@raymondhill.net"
install_url = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"

[profiles.main]

[[profiles.main.addons]]
id = "example@example.com"
install_url = "https://addons.mozilla.org/firefox/downloads/latest/example/latest.xpi"
```

`policies.json` is installation-wide. Profile mappings are supplied to `apply` with `--zen-profile` or repeated `--map` options.
