from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from difflib import get_close_matches
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from urllib.request import urlopen

import lz4.block
import tomli_w
from mozprofile.addons import AddonManager

PREFERENCE_PATTERN = re.compile(r'^user_pref\("((?:\\.|[^"])*)",\s*(.*?)\);$')
RENDERED_FILES = ("user.js", "policies.json", "theme.json")

SEPARATE_PREFERENCES = {
    "browser.uiCustomization.state",
}

IGNORED_PREFERENCE_PATTERNS = {
    "*attempt*",
    "*backup*",
    "*build*",
    "*check*",
    "*chosen",
    "*converter*",
    "*count*",
    "*date*",
    "*ever*",
    "*first*",
    "*has*",
    "*impression*",
    "*last*",
    "*migrat*",
    "*mstone*",
    "*next*",
    "*pending*",
    "*prompt*",
    "*schema*",
    "*seen*",
    "*storage*",
    "*totalSearches*",
    "*update*",
    "*used*",
    "*version*",
    "accessibility.typeaheadfind.flashBar",
    "app.*",
    "browser.bookmarks.*",
    "browser.download.panel.shown",
    "browser.engagement.*",
    "browser.laterrun.*",
    "browser.newtabpage.*",
    "browser.pageActions.persistedActions",
    "browser.pagethumbnails.*",
    "browser.privacySegmentation.*",
    "browser.proton.*",
    "browser.region.*",
    "browser.rights.*",
    "browser.safebrowsing.*",
    "browser.search.region",
    "browser.sessionstore.*",
    "browser.shell.*",
    "browser.termsofuse.*",
    "browser.translations.*",
    "browser.urlbar.placeholder*",
    "*typewasregistered*",
    "captchadetection.*",
    "datareporting.*",
    "distribution.*",
    "doh-rollout.*",
    "dom.push.userAgentID",
    "extensions.blocklist.*",
    "extensions.colorway-*",
    "extensions.dnr.*",
    "extensions.quarantinedDomains.*",
    "extensions.signature*",
    "extensions.systemAddonSet",
    "extensions.ui.*",
    "extensions.webextensions.*migrated*",
    "extensions.webextensions.uuids",
    "gecko.handlerService.*",
    "gfx*",
    "idle.*",
    "media.gmp*",
    "media.hardware-*",
    "media.videocontrols.*",
    "nimbus.*",
    "pdfjs.*",
    "places.*",
    "sanity-test.*",
    "screenshots.browser.component.last-*",
    "services.settings.*",
    "services.sync.*last*",
    "services.sync.*next*",
    "services.sync.clients.*",
    "services.sync.globalScore",
    "sidebar.backup*",
    "signon.*migration*",
    "signon.storage.rust.*",
    "storage.vacuum.*",
    "toolkit.profiles.*",
    "toolkit.startup.*",
    "toolkit.telemetry.*",
    "ui.osk.debug.keyboardDisplayReason",
    "zen.keyboard.shortcuts.version",
    "zen.mods.*last*",
    "zen.mods.*milestone",
    "zen.mods.*observer",
    "zen.session-store.*build*",
    "zen.ui.migration.*",
    "zen.urlbar.*suggestions-learner",
    "zen.welcome-screen.*seen",
    "zen.workspaces.active",
}


def zen_directory_candidates(
    platform_name: str = sys.platform,
    home: Path | None = None,
    app_data: Path | None = None,
) -> tuple[Path, ...]:
    home = home or Path.home()

    if platform_name == "win32":
        app_data = app_data or Path(
            os.environ.get("APPDATA", home / "AppData" / "Roaming")
        )

        return (app_data / "zen",)

    if platform_name == "darwin":
        return (home / "Library" / "Application Support" / "zen",)

    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")

    return (
        xdg_config_home / "zen",
        home / ".zen",
        home / ".var" / "app" / "app.zen_browser.zen" / ".zen",
    )


def default_zen_directory() -> Path:
    candidates = zen_directory_candidates()

    return next(
        (
            candidate
            for candidate in candidates
            if (candidate / "profiles.ini").is_file()
        ),
        candidates[0],
    )


def zen_installation_candidates(
    platform_name: str = sys.platform,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    home = home if home is not None else Path.home()
    environment = environment if environment is not None else os.environ
    candidates: list[Path] = []

    if platform_name == "win32":
        local_app_data = Path(
            environment.get("LOCALAPPDATA", home / "AppData" / "Local")
        )

        program_files = Path(environment.get("ProgramFiles", "C:/Program Files"))

        candidates.extend(
            (
                local_app_data / "Programs" / "Zen Browser",
                program_files / "Zen Browser",
            )
        )
    elif platform_name == "darwin":
        candidates.extend(
            (
                home / "Applications" / "Zen Browser.app" / "Contents" / "Resources",
                Path("/Applications/Zen Browser.app/Contents/Resources"),
            )
        )
    else:
        executable = shutil.which("zen-browser") or shutil.which("zen")

        if executable:
            candidates.append(Path(executable).resolve().parent)

        candidates.extend(
            (
                Path("/usr/lib/zen-browser"),
                Path("/usr/local/lib/zen-browser"),
                Path("/opt/zen-browser"),
                home / ".local" / "share" / "zen-browser",
            )
        )

    return tuple(dict.fromkeys(candidates))


def default_zen_installation() -> Path | None:
    for candidate in zen_installation_candidates():
        if any(
            (candidate / name).exists()
            for name in ("zen.exe", "zen", "zen-bin", "application.ini")
        ):
            return candidate

    return None


def discover_profiles(zen_directory: Path) -> dict[str, Path]:
    profiles_file = zen_directory / "profiles.ini"
    parser = configparser.ConfigParser()
    parser.read(profiles_file, encoding="utf-8")
    profiles: dict[str, Path] = {}

    for section in parser.sections():
        if not section.startswith("Profile"):
            continue

        name = parser.get(section, "Name", fallback=section.removeprefix("Profile"))
        profile_path = Path(parser.get(section, "Path"))

        if parser.getboolean(section, "IsRelative", fallback=False):
            profile_path = zen_directory / profile_path

        profiles[name] = profile_path

    return profiles


def read_preferences(profile_path: Path) -> dict[str, Any]:
    preferences: dict[str, Any] = {}
    preferences_file = profile_path / "prefs.js"

    for line in preferences_file.read_text(encoding="utf-8").splitlines():
        match = PREFERENCE_PATTERN.match(line.strip())

        if match is None:
            continue

        name = json.loads(f'"{match.group(1)}"')

        if name in SEPARATE_PREFERENCES or any(
            fnmatchcase(name.casefold(), pattern.casefold())
            for pattern in IGNORED_PREFERENCE_PATTERNS
        ):
            continue

        try:
            preferences[name] = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue

    return preferences


def addon_install_url(addon: dict[str, Any]) -> str | None:
    source_url = addon.get("installTelemetryInfo", {}).get("sourceURL")

    if source_url:
        parts = [part for part in urlsplit(source_url).path.split("/") if part]

        if "addon" in parts:
            slug_index = parts.index("addon") + 1

            if slug_index < len(parts):
                slug = parts[slug_index]

                return (
                    "https://addons.mozilla.org/firefox/downloads/latest/"
                    f"{slug}/latest.xpi"
                )

    return addon.get("sourceURI")


def read_addons(profile_path: Path) -> list[dict[str, str]]:
    addons_file = profile_path / "extensions.json"

    if not addons_file.exists():
        return []

    addons = json.loads(addons_file.read_text(encoding="utf-8")).get("addons", [])
    installed_addons = []

    for addon in addons:
        if (
            addon.get("type") != "extension"
            or addon.get("location") != "app-profile"
            or not addon.get("active")
            or addon.get("userDisabled")
        ):
            continue

        record = {"id": addon["id"]}
        install_url = addon_install_url(addon)

        if install_url:
            record["install_url"] = install_url

        installed_addons.append(record)

    return sorted(installed_addons, key=lambda addon: addon["id"])


def read_toolbar(profile_path: Path) -> dict[str, Any]:
    preferences_file = profile_path / "prefs.js"

    for line in preferences_file.read_text(encoding="utf-8").splitlines():
        match = PREFERENCE_PATTERN.match(line.strip())

        if match is None or match.group(1) != "browser.uiCustomization.state":
            continue

        state = json.loads(json.loads(match.group(2)))

        return {
            "current_version": state.get("currentVersion"),
            "placements": state.get("placements", {}),
        }

    return {}


def decompress_session(profile_path: Path) -> dict[str, Any]:
    session_file = profile_path / "zen-sessions.jsonlz4"

    if not session_file.exists():
        return {}

    compressed = session_file.read_bytes()

    if compressed[:8] != b"mozLz40\0":
        raise ValueError(f"Unsupported Zen session header: {session_file}")

    size = int.from_bytes(compressed[8:12], "little")
    data = cast(Any, lz4.block).decompress(compressed[12:], uncompressed_size=size)

    return json.loads(data)


def read_theme(profile_path: Path) -> dict[str, Any]:
    spaces = decompress_session(profile_path).get("spaces", [])

    if not spaces:
        return {}

    theme = spaces[0].get("theme", {})
    gradient_colors = theme.get("gradientColors", [])

    colors = [
        "#" + "".join(f"{component:02x}" for component in color["c"])
        for color in gradient_colors
        if isinstance(color.get("c"), list) and len(color["c"]) == 3
    ]

    return {
        "type": theme.get("type"),
        "opacity": theme.get("opacity"),
        "texture": theme.get("texture"),
        "colors": colors,
        "gradient_colors": gradient_colors,
    }


def compress_session(session: dict[str, Any]) -> bytes:
    data = json.dumps(session, ensure_ascii=False, separators=(",", ":")).encode()
    compressed = cast(Any, lz4.block).compress(data, store_size=False)

    return b"mozLz40" + bytes([0]) + len(data).to_bytes(4, "little") + compressed


def load_configuration(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def merge_addons(*addon_lists: list[dict[str, str] | str]) -> list[dict[str, str]]:
    addons: dict[str, dict[str, str]] = {}

    for addon_list in addon_lists:
        for addon in addon_list:
            if isinstance(addon, str):
                record = {"id": addon}
            else:
                record = dict(addon)

            addons.setdefault(record["id"], record).update(record)

    return [addons[addon_id] for addon_id in sorted(addons)]


def merge_profile(configuration: dict[str, Any], profile_name: str) -> dict[str, Any]:
    base = configuration.get("base", {})
    profile = configuration["profiles"][profile_name]
    preferences = dict(base.get("preferences", {}))
    preferences.update(profile.get("preferences", {}))
    toolbar = dict(base.get("toolbar", {}))
    toolbar.update(profile.get("toolbar", {}))

    return {
        "preferences": preferences,
        "addons": merge_addons(base.get("addons", [])),
        "profile_addons": merge_addons(profile.get("addons", [])),
        "toolbar": toolbar,
        "theme": profile.get("theme", base.get("theme", {})),
    }


def toolbar_state(toolbar: dict[str, Any]) -> str | None:
    placements = toolbar.get("placements")

    if placements is None:
        return None

    state: dict[str, Any] = {"placements": placements}

    if "current_version" in toolbar:
        state["currentVersion"] = toolbar["current_version"]

    return json.dumps(state, separators=(",", ":"))


def render_user_js(preferences: dict[str, Any], toolbar: dict[str, Any]) -> str:
    values = dict(preferences)
    state = toolbar_state(toolbar)

    if state is not None:
        values["browser.uiCustomization.state"] = state

    return chr(10).join(
        f"user_pref({json.dumps(name)}, {json.dumps(values[name])});"
        for name in sorted(values)
    ) + chr(10)


def parse_color(value: str) -> list[int]:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"Expected a six-digit hexadecimal color: {value}")

    return [int(value[index : index + 2], 16) for index in (1, 3, 5)]


def apply_theme(session: dict[str, Any], theme: dict[str, Any]) -> dict[str, Any]:
    spaces = session.get("spaces", [])

    if not spaces:
        raise ValueError("Zen session has no default Space")

    session_theme = dict(spaces[0].get("theme", {}))

    for source_name, target_name in (
        ("type", "type"),
        ("opacity", "opacity"),
        ("texture", "texture"),
    ):
        if source_name in theme:
            session_theme[target_name] = theme[source_name]

    gradient_colors = json.loads(json.dumps(theme.get("gradient_colors", [])))

    for index, color in enumerate(theme.get("colors", [])):
        if index < len(gradient_colors):
            gradient_colors[index]["c"] = parse_color(color)
        else:
            gradient_colors.append({"c": parse_color(color)})

    if "colors" in theme or "gradient_colors" in theme:
        session_theme["gradientColors"] = gradient_colors

    spaces[0]["theme"] = session_theme

    return session


def shared_values(values: dict[str, Any]) -> dict[str, Any]:
    profiles = list(values.values())

    if not profiles:
        return {}

    first = profiles[0]

    return {
        key: value
        for key, value in first.items()
        if all(profile.get(key) == value for profile in profiles[1:])
    }


def import_configuration(zen_directory: Path) -> dict[str, Any]:
    profiles = discover_profiles(zen_directory)

    imported: dict[str, dict[str, Any]] = {
        name: {
            "preferences": read_preferences(path),
            "addons": read_addons(path),
            "toolbar": read_toolbar(path),
            "theme": read_theme(path),
        }
        for name, path in profiles.items()
    }

    base_preferences = shared_values(
        {name: data["preferences"] for name, data in imported.items()}
    )

    base_addons = (
        merge_addons(*(data["addons"] for data in imported.values()))
        if imported
        else []
    )

    base_addon_ids = {addon["id"] for addon in base_addons}

    base_toolbar = shared_values(
        {name: data["toolbar"] for name, data in imported.items()}
    )

    configuration: dict[str, Any] = {
        "base": {
            "preferences": base_preferences,
            "addons": base_addons,
            "toolbar": base_toolbar,
        },
        "profiles": {},
    }

    for name, data in imported.items():
        configuration["profiles"][name] = {
            "preferences": {
                key: value
                for key, value in data["preferences"].items()
                if key not in base_preferences
            },
            "addons": [
                addon for addon in data["addons"] if addon["id"] not in base_addon_ids
            ],
            "toolbar": {
                key: value
                for key, value in data["toolbar"].items()
                if base_toolbar.get(key) != value
            },
            "theme": data["theme"],
        }

    return configuration


def import_command(zen_directory: Path, output: Path) -> None:
    configuration = import_configuration(zen_directory)
    output.write_text(tomli_w.dumps(configuration), encoding="utf-8")
    print(f"Wrote {output}")


def render_policies(addons: list[dict[str, str] | str]) -> str:
    extension_settings: dict[str, dict[str, str]] = {}

    for addon in addons:
        record = {"id": addon} if isinstance(addon, str) else addon
        install_url = record.get("install_url")

        if install_url is None:
            continue

        extension_settings[record["id"]] = {
            "installation_mode": "force_installed",
            "install_url": install_url,
        }

    policies = {"policies": {"ExtensionSettings": extension_settings}}

    return json.dumps(policies, indent=2) + "\n"


def install_profile_addons(
    profile_path: Path,
    addons: list[dict[str, str] | str],
) -> None:
    current_addon_ids = {addon["id"] for addon in read_addons(profile_path)}
    addon_manager = AddonManager(str(profile_path), restore=False)

    for addon in addons:
        record = {"id": addon} if isinstance(addon, str) else addon
        addon_id = record["id"]

        if addon_id in current_addon_ids:
            continue

        install_url = record.get("install_url")

        if install_url is None:
            raise ValueError(f"Addon {addon_id!r} has no install_url")

        temporary_path = ""

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".xpi", delete=False
            ) as temporary_file:
                temporary_path = temporary_file.name

                with urlopen(install_url) as response:
                    shutil.copyfileobj(response, temporary_file)

            addon_manager.install(temporary_path)
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

        current_addon_ids.add(addon_id)
        print(f"Installed {addon_id} in {profile_path}")


def render_profile(
    configuration: dict[str, Any],
    profile_name: str,
    output: Path,
) -> Path:
    merged = merge_profile(configuration, profile_name)
    profile_output = output / profile_name
    profile_output.mkdir(parents=True, exist_ok=True)

    (profile_output / "user.js").write_text(
        render_user_js(merged["preferences"], merged["toolbar"]),
        encoding="utf-8",
    )

    (profile_output / "policies.json").write_text(
        render_policies(merged["addons"]),
        encoding="utf-8",
    )

    (profile_output / "theme.json").write_text(
        json.dumps(merged["theme"], indent=2) + chr(10),
        encoding="utf-8",
    )

    return profile_output


def resolve_profile_name(profiles: dict[str, Path], query: str) -> str:
    normalized_query = query.casefold()
    exact_matches = {name.casefold(): name for name in profiles}

    if normalized_query in exact_matches:
        return exact_matches[normalized_query]

    glob_matches = [
        name for name in profiles if fnmatchcase(name.casefold(), normalized_query)
    ]

    if len(glob_matches) == 1:
        return glob_matches[0]

    if len(glob_matches) > 1:
        raise ValueError(
            f"Profile pattern {query!r} matches: {', '.join(sorted(glob_matches))}"
        )

    names = list(profiles)

    fuzzy_matches = get_close_matches(
        normalized_query,
        [name.casefold() for name in names],
        n=2,
    )

    normalized_names = [name.casefold() for name in names]

    if len(fuzzy_matches) == 1:
        return names[normalized_names.index(fuzzy_matches[0])]

    if len(fuzzy_matches) > 1:
        matches = [names[normalized_names.index(match)] for match in fuzzy_matches]

        raise ValueError(
            f"Profile query {query!r} is ambiguous: {', '.join(sorted(matches))}"
        )

    raise ValueError(f"Unknown profile: {query}")


def atomic_write(path: Path, data: bytes) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_bytes(data)
    os.replace(temporary_path, path)


def resolve_owner(owner: str) -> tuple[int, int]:
    if getattr(os, "chown", None) is None:
        raise RuntimeError("--owner requires a POSIX platform")

    get_password_entry = __import__("pwd").getpwnam  # ty: ignore[unresolved-attribute]
    account = get_password_entry(owner)

    return account.pw_uid, account.pw_gid


def restore_owner(path: Path, owner: tuple[int, int]) -> None:
    user_id, group_id = owner
    change_owner = getattr(os, "chown", None)

    if change_owner is None:
        raise RuntimeError("--owner requires a POSIX platform")

    for current_path in (path, *path.rglob("*")):
        change_owner(current_path, user_id, group_id, follow_symlinks=False)


def parse_profile_mappings(values: list[str]) -> dict[str, str]:
    mappings: dict[str, str] = {}

    for value in values:
        logical_name, separator, zen_name = value.partition("=")

        if not separator or not logical_name or not zen_name:
            raise ValueError(f"Expected PROFILE=ZEN_PROFILE, got {value!r}")

        mappings[logical_name] = zen_name

    return mappings


def apply_command(
    configuration_file: Path,
    profile_name: str | None,
    apply_all: bool,
    zen_profile_name: str | None,
    profile_mappings: list[str],
    zen_directory: Path,
    zen_installation: Path | None,
    output: Path,
    owner: str | None,
) -> None:
    configuration = load_configuration(configuration_file)
    profiles = discover_profiles(zen_directory)
    zen_installation = zen_installation or default_zen_installation()
    owner_ids = resolve_owner(owner) if owner is not None else None

    if zen_installation is None:
        raise ValueError("Could not find the Zen installation; pass --zen-installation")

    configured_profiles = configuration.get("profiles", {})
    mappings = parse_profile_mappings(profile_mappings)

    if apply_all:
        if profile_name is not None or zen_profile_name is not None:
            raise ValueError("--all cannot be combined with a single profile")

        missing = sorted(set(configured_profiles) - set(mappings))

        if missing:
            raise ValueError(f"Missing --map values for: {', '.join(missing)}")

        profile_pairs = [
            (name, resolve_profile_name(profiles, mappings[name]))
            for name in sorted(configured_profiles)
        ]
    else:
        if profile_name is None or zen_profile_name is None:
            raise ValueError("apply requires PROFILE and --zen-profile")

        if profile_mappings:
            raise ValueError("--map is only valid with --all")

        if profile_name not in configured_profiles:
            raise ValueError(
                f"Profile {profile_name!r} is missing from the configuration"
            )

        profile_pairs = [
            (profile_name, resolve_profile_name(profiles, zen_profile_name))
        ]

    rendered_profiles: list[tuple[str, Path, Path]] = []

    for logical_name, zen_name in profile_pairs:
        profile_path = profiles[zen_name]
        rendered = render_profile(configuration, logical_name, output)
        rendered_profiles.append((logical_name, rendered, profile_path))

    policy_contents = {
        (rendered / "policies.json").read_bytes()
        for _, rendered, _ in rendered_profiles
    }

    if len(policy_contents) != 1:
        raise ValueError("Profiles produced different policies.json files")

    for logical_name, rendered, profile_path in rendered_profiles:
        session = decompress_session(profile_path)
        theme = json.loads((rendered / "theme.json").read_text(encoding="utf-8"))
        atomic_write(profile_path / "user.js", (rendered / "user.js").read_bytes())

        atomic_write(
            profile_path / "zen-sessions.jsonlz4",
            compress_session(apply_theme(session, theme)),
        )

        install_profile_addons(
            profile_path,
            merge_profile(configuration, logical_name)["profile_addons"],
        )

        if owner_ids is not None:
            restore_owner(profile_path, owner_ids)

        print(f"Applied {logical_name} to {profile_path}")

    policies_directory = zen_installation / "distribution"
    policies_directory.mkdir(parents=True, exist_ok=True)
    atomic_write(policies_directory / "policies.json", policy_contents.pop())
    print(f"Applied policies to {policies_directory}")


def confirm_render_overwrite(profile_names: list[str], output: Path) -> bool:
    existing_profiles = [
        name
        for name in profile_names
        if any((output / name / filename).is_file() for filename in RENDERED_FILES)
    ]

    if not existing_profiles:
        return True

    names = ", ".join(sorted(existing_profiles))

    try:
        answer = input(f"Overwrite rendered files for {names}? [Y/n] ")
    except EOFError as error:
        raise RuntimeError("Cannot confirm overwriting rendered files") from error

    return answer.strip().casefold() not in {"n", "no"}


def render_command(
    configuration_file: Path,
    profile_name: str | None,
    render_all: bool,
    output: Path,
) -> None:
    configuration = load_configuration(configuration_file)
    configured_profiles = configuration.get("profiles", {})

    if render_all:
        profile_names = sorted(configured_profiles)
    else:
        if profile_name is None:
            raise ValueError("A profile or --all is required")

        profile_names = [profile_name]

        if profile_name not in configured_profiles:
            raise ValueError(
                f"Profile {profile_name!r} is missing from the configuration"
            )

    if not confirm_render_overwrite(profile_names, output):
        print("Rendering cancelled")

        return

    for name in profile_names:
        profile_output = render_profile(configuration, name, output)
        print(f"Wrote {profile_output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zen-configurator")
    commands = parser.add_subparsers(dest="command", required=True)
    import_parser = commands.add_parser("import", help="Import existing Zen profiles")

    import_parser.add_argument(
        "-d", "--zen-directory", type=Path, default=default_zen_directory()
    )

    import_parser.add_argument(
        "-o", "--output", type=Path, default=Path("zen-config.toml")
    )

    render_parser = commands.add_parser("render", help="Render a Zen profile")
    render_parser.add_argument("profile", nargs="?")
    render_parser.add_argument("-a", "--all", action="store_true")

    render_parser.add_argument(
        "-c", "--config", type=Path, default=Path("zen-config.toml")
    )

    render_parser.add_argument("-o", "--output", type=Path, default=Path("generated"))

    apply_parser = commands.add_parser("apply", help="Apply a Zen profile")
    apply_parser.add_argument("profile", nargs="?")
    apply_parser.add_argument("-a", "--all", action="store_true")
    apply_parser.add_argument("-p", "--zen-profile")
    apply_parser.add_argument("-m", "--map", action="append", default=[])

    apply_parser.add_argument(
        "-c", "--config", type=Path, default=Path("zen-config.toml")
    )

    apply_parser.add_argument(
        "-d", "--zen-directory", type=Path, default=default_zen_directory()
    )

    apply_parser.add_argument("-i", "--zen-installation", type=Path)
    apply_parser.add_argument("-u", "--owner")
    apply_parser.add_argument("-o", "--output", type=Path, default=Path("generated"))

    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()

    try:
        if arguments.command == "import":
            import_command(arguments.zen_directory, arguments.output)
        elif arguments.command == "render":
            if arguments.all == (arguments.profile is not None):
                parser.error("render requires exactly one of PROFILE or --all")

            render_command(
                arguments.config,
                arguments.profile,
                arguments.all,
                arguments.output,
            )
        elif arguments.command == "apply":
            if arguments.all == (arguments.profile is not None):
                parser.error("apply requires exactly one of PROFILE or --all")

            apply_command(
                arguments.config,
                arguments.profile,
                arguments.all,
                arguments.zen_profile,
                arguments.map,
                arguments.zen_directory,
                arguments.zen_installation,
                arguments.output,
                arguments.owner,
            )
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"error: {error}" + chr(10))


if __name__ == "__main__":
    main()
