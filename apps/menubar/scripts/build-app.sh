#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
package_dir="$(cd "$script_dir/.." && pwd -P)"
project_dir="$(cd "$script_dir/../../.." && pwd -P)"
requested_output="${CC_SWAPER_APP_OUTPUT:-$package_dir/dist/CcsMenuBar.app}"
if [[ "$requested_output" != /* || "$(basename "$requested_output")" != "CcsMenuBar.app" ]]; then
  printf 'CC_SWAPER_APP_OUTPUT must be an absolute path ending in CcsMenuBar.app.\n' >&2
  exit 1
fi
output_parent="$(dirname "$requested_output")"
if [[ -L "$output_parent" ]]; then
  printf 'Refusing to build through a symlinked output directory: %s\n' "$output_parent" >&2
  exit 1
fi
mkdir -p "$output_parent"
dist_dir="$(cd "$output_parent" && pwd -P)"
app_path="$dist_dir/CcsMenuBar.app"
build_path=""
staging=""
bundle_id="com.namsequence.ccswaper.menubar"
app_version="${CC_SWAPER_APP_VERSION:-$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$project_dir/pyproject.toml" | head -n 1)}"
if [[ ! "$app_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'CC_SWAPER_APP_VERSION must use MAJOR.MINOR.PATCH.\n' >&2
  exit 1
fi
build_path="$(mktemp -d "${TMPDIR:-/tmp}/ccs-menubar-build.XXXXXX")"

cleanup() {
  if [[ -n "$staging" && -d "$staging" ]]; then rm -rf "$staging"; fi
  if [[ -n "$build_path" && -d "$build_path" ]]; then rm -rf "$build_path"; fi
}
trap cleanup EXIT

if [[ -e "$app_path" ]]; then
  if [[ -L "$app_path" || ! -d "$app_path" ]]; then
    printf 'Refusing to replace an unsafe app path: %s\n' "$app_path" >&2
    exit 1
  fi
  existing_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app_path/Contents/Info.plist" 2>/dev/null || true)
  if [[ "$existing_id" != "$bundle_id" ]]; then
    printf 'Refusing to replace an app with an unexpected bundle identifier: %s\n' "$app_path" >&2
    exit 1
  fi
fi

trap cleanup EXIT
swift build --configuration release --product CcsMenuBar \
  --package-path "$package_dir" --build-path "$build_path"

staging=$(mktemp -d "$dist_dir/.CcsMenuBar.app.XXXXXX")
mkdir -p "$staging/Contents/MacOS" "$staging/Contents/Resources"
cp "$build_path/release/CcsMenuBar" "$staging/Contents/MacOS/CcsMenuBar"
chmod 755 "$staging/Contents/MacOS/CcsMenuBar"

cat > "$staging/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleExecutable</key><string>CcsMenuBar</string>
  <key>CFBundleIdentifier</key><string>com.namsequence.ccswaper.menubar</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>CC Swaper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>${app_version}</string>
  <key>CFBundleVersion</key><string>${app_version}</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

plutil -lint "$staging/Contents/Info.plist"
codesign --force --deep --sign - --timestamp=none "$staging"

if [[ -e "$app_path" ]]; then
  rm -rf "$app_path"
fi
mv "$staging" "$app_path"
staging=""
rm -rf "$build_path"
trap - EXIT
printf 'Built menu bar app: %s\n' "$app_path"
