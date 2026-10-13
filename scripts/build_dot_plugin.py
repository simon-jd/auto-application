#!/usr/bin/env python3
"""Build a portable Job Search plugin ZIP using only Python's standard library.

Before registration:
    python3 scripts/build_dot_plugin.py --skills-only --output /tmp/job-search-skills.zip
After registering the private MCP in ChatGPT, pass its actual asdk_app_... ID:
    python3 scripts/build_dot_plugin.py --app-id "$JOB_SEARCH_APP_ID" --output /tmp/job-search.zip

The app ID is syntax-checked, not verified against an account. A plugin URL's
plugin_asdk_app_... identifier is not the app ID: remove the plugin_ prefix.
The ZIP contains one job-search/ folder. Its files are explicitly allowlisted;
runtime data, credentials, MCP transport manifests and hooks are never bundled.
Building does not upload, install, connect, or publish the plugin. Use the
account's supported private plugin creation/import flow and test it in Dot.
Local marketplace installation alone does not establish cloud availability.

Official format: https://developers.openai.com/plugins/build/plugins
Existing app references: https://learn.chatgpt.com/docs/enterprise/plugin-management
Private creation: https://learn.chatgpt.com/docs/build-plugins
"""

import argparse
import json
from pathlib import Path
import re
import zipfile


def app_id(value: str) -> str:
    if not re.fullmatch(r"asdk_app_[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise argparse.ArgumentTypeError(
            "use the registered app ID beginning asdk_app_, not its plugin_ URL identifier"
        )
    return value


def build_package(output: Path, registered_app_id: str | None) -> None:
    source = Path(__file__).resolve().parents[1] / "plugins" / "job-search"
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    # Serialize fresh mappings instead of copying a possibly stale .app.json.
    extension = manifest.setdefault("extensions", {}).setdefault("com.openai", {})
    extension.pop("apps", None)
    files = {
        "skills/job-search/SKILL.md": (source / "skills" / "job-search" / "SKILL.md").read_bytes(),
    }
    if registered_app_id is not None:
        extension["apps"] = "./.app.json"
        files[".app.json"] = (json.dumps({
            "apps": {"job-search": {"id": registered_app_id, "required": True}},
        }, indent=2) + "\n").encode("utf-8")
    files["plugin.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as package:
        for name, content in sorted(files.items()):
            package.writestr("job-search/" + name, content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    connection = parser.add_mutually_exclusive_group(required=True)
    connection.add_argument("--app-id", type=app_id, help="Existing registered ChatGPT app ID (asdk_app_...).")
    connection.add_argument("--skills-only", action="store_true", help="Package instructions without tools or an app binding.")
    parser.add_argument("--output", type=Path, required=True, help="New ZIP path; existing files are not overwritten.")
    args = parser.parse_args()
    try:
        build_package(args.output, args.app_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Cannot build plugin: {exc}\n")
    if args.skills_only:
        print(f"Built {args.output}: skills only; job tools require a registered app connection.")
    else:
        print(f"Built {args.output}: bound to {args.app_id}; not installed or connection-tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
